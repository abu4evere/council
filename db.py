"""SQLite persistence.

Why SQLite and not Postgres: this is one user with one app. SQLite is a single
file, needs no server, survives restarts, and backs up by copying one file.

Why events are persisted rather than held in memory: you want to use this from
your phone. Phones change networks, sleep, and drop connections constantly. Every
event is stored with a monotonic `seq`, so a reconnecting client sends the last
seq it saw and we replay from there instead of losing the run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

# Next to the code for a local install; on a mounted volume in a container.
# The app directory is part of the image and is REPLACED on every deploy, so
# a database living there would take every account with it each time.
DB_PATH = Path(os.environ.get("COUNCIL_DB")
                or Path(__file__).parent / "council.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS visitor_usage (
    fingerprint TEXT NOT NULL,
    day         TEXT NOT NULL,
    runs        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fingerprint, day)
);

CREATE TABLE IF NOT EXISTS instance_usage (
    day  TEXT PRIMARY KEY,
    runs INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS verifications (
    email       TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    sends       INTEGER NOT NULL DEFAULT 1,
    last_sent   REAL NOT NULL,
    window_start REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_usage (
    user_id        TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    day            TEXT NOT NULL,
    runs_today     INTEGER NOT NULL DEFAULT 0,
    credit_cents   INTEGER NOT NULL DEFAULT 0,
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    spent_cents    INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_keys (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    env_var    TEXT NOT NULL,
    secret     TEXT NOT NULL,   -- Fernet token, never plaintext
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, env_var)
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_prompt     TEXT NOT NULL,
    mode            TEXT NOT NULL,
    final_answer    TEXT,
    status          TEXT NOT NULL,   -- running | done | error
    error           TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id  TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    type     TEXT NOT NULL,
    agent    TEXT,
    payload  TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id, seq);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the writer (a running debate) and readers (the UI polling history)
    # work at the same time without blocking each other.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Migration: conversations predate accounts and have no owner. Add the
        # column if it is missing rather than recreating the table, so existing
        # history survives the upgrade. NULL means "created before accounts
        # existed" and is claimed by the first account made.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id)")
        # Each account points at its own notes folder. Before this, one global
        # VAULT_PATH meant a second user's questions would retrieve the FIRST
        # user's private notes and send them to four AI providers.
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "vault_path" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN vault_path TEXT")
        # Profile fields, added without rebuilding the table so existing
        # accounts survive. Nullable: the one account that predates email
        # verification has neither, and locking its owner out of their own
        # instance would be an absurd way to enforce a signup rule.
        for col in ("email", "full_name", "purpose"):
            if col not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                     "ON users(email) WHERE email IS NOT NULL")
        conn.commit()
    finally:
        conn.close()


# --- users -----------------------------------------------------------------

def create_user(username: str, password_hash: str) -> str | None:
    """Returns the new user id, or None when the username is taken."""
    uid = _id()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (uid, username, password_hash, _now()),
        )
        # The first account adopts any history created before accounts existed,
        # so the operator does not lose their own conversations on upgrade.
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1:
            conn.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (uid,))
        conn.commit()
        return uid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_name(username: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_user_vault(user_id: str, path: str | None) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE users SET vault_path = ? WHERE id = ?", (path or None, user_id))
        conn.commit()
    finally:
        conn.close()


def get_user_vault(user_id: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT vault_path FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["vault_path"] if row else None
    finally:
        conn.close()


def all_users() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT id, username, vault_path FROM users")]
    finally:
        conn.close()


def user_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    finally:
        conn.close()




# --- email verification ----------------------------------------------------

def get_verification(email: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM verifications WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def put_verification(email: str, code_hash: str, window_start: float, sends: int) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO verifications (email, code_hash, created_at, attempts,
                                          sends, last_sent, window_start)
               VALUES (?,?,?,0,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                   code_hash = excluded.code_hash,
                   created_at = excluded.created_at,
                   attempts = 0,
                   sends = excluded.sends,
                   last_sent = excluded.last_sent,
                   window_start = excluded.window_start""",
            (email, code_hash, _now(), sends, _now(), window_start))
        conn.commit()
    finally:
        conn.close()


def count_verification_attempt(email: str) -> int:
    """Increment and return the attempt count for this code."""
    conn = connect()
    try:
        conn.execute("UPDATE verifications SET attempts = attempts + 1 WHERE email = ?", (email,))
        conn.commit()
        row = conn.execute("SELECT attempts FROM verifications WHERE email = ?", (email,)).fetchone()
        return row["attempts"] if row else 0
    finally:
        conn.close()


def clear_verification(email: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM verifications WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_verified_user(email: str, full_name: str, purpose: str,
                         password_hash: str) -> str | None:
    """Create an account whose email has already been proven. None if taken."""
    uid = _id()
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO users (id, username, password_hash, created_at,
                                  email, full_name, purpose)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, email, password_hash, _now(), email, full_name, purpose or None))
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1:
            conn.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (uid,))
        conn.commit()
        return uid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


# --- usage and credit ------------------------------------------------------


# --- free-run limits, counted where they survive a restart -----------------

def visitor_runs_today(fingerprint: str, day: str) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT runs FROM visitor_usage WHERE fingerprint = ? AND day = ?",
            (fingerprint, day)).fetchone()
        return row["runs"] if row else 0
    finally:
        conn.close()


def count_visitor_run(fingerprint: str, day: str) -> int:
    """Record one run for this visitor and return the new total.

    Insert and increment in ONE statement: two requests arriving together must
    not both read zero and both decide they are allowed.
    """
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO visitor_usage (fingerprint, day, runs) VALUES (?,?,1)
               ON CONFLICT(fingerprint, day) DO UPDATE SET runs = runs + 1""",
            (fingerprint, day))
        conn.commit()
        row = conn.execute(
            "SELECT runs FROM visitor_usage WHERE fingerprint = ? AND day = ?",
            (fingerprint, day)).fetchone()
        return row["runs"] if row else 1
    finally:
        conn.close()


def instance_runs_today(day: str) -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT runs FROM instance_usage WHERE day = ?", (day,)).fetchone()
        return row["runs"] if row else 0
    finally:
        conn.close()


def count_instance_run(day: str) -> int:
    """One more run against the shared free tiers. Returns the new total."""
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO instance_usage (day, runs) VALUES (?,1)
               ON CONFLICT(day) DO UPDATE SET runs = runs + 1""", (day,))
        conn.commit()
        row = conn.execute("SELECT runs FROM instance_usage WHERE day = ?", (day,)).fetchone()
        return row["runs"] if row else 1
    finally:
        conn.close()


def prune_visitor_usage(before_day: str) -> int:
    """Drop rows older than a day. Nothing here is worth keeping as history."""
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM visitor_usage WHERE day < ?", (before_day,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_usage(user_id: str, day: str) -> dict:
    """This user's counters, rolling the daily count over when the day changes."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM user_usage WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO user_usage (user_id, day, runs_today, credit_cents,
                                           reserved_cents, spent_cents, updated_at)
                   VALUES (?,?,0,0,0,0,?)""", (user_id, day, _now()))
            conn.commit()
            return {"user_id": user_id, "day": day, "runs_today": 0,
                    "credit_cents": 0, "reserved_cents": 0, "spent_cents": 0}
        d = dict(row)
        if d["day"] != day:
            # New day: the free allowance resets. Credit does NOT -- it was
            # paid for and does not expire.
            conn.execute(
                "UPDATE user_usage SET day = ?, runs_today = 0, updated_at = ? WHERE user_id = ?",
                (day, _now(), user_id))
            conn.commit()
            d["day"], d["runs_today"] = day, 0
        return d
    finally:
        conn.close()


def reserve(user_id: str, day: str, cents: int) -> bool:
    """Hold credit before a run. False when it no longer covers the amount.

    The check and the write happen in ONE statement, so two runs started at the
    same moment cannot both pass a balance check that only one of them can
    afford.
    """
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE user_usage
               SET reserved_cents = reserved_cents + ?, runs_today = runs_today + 1,
                   updated_at = ?
               WHERE user_id = ? AND (credit_cents - reserved_cents) >= ?""",
            (cents, _now(), user_id, cents))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def count_free_run(user_id: str, day: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE user_usage SET runs_today = runs_today + 1, updated_at = ? WHERE user_id = ?",
            (_now(), user_id))
        conn.commit()
    finally:
        conn.close()


def settle(user_id: str, reserved_cents: int, actual_cents: int) -> None:
    """Release the reservation and charge what the run really cost."""
    conn = connect()
    try:
        conn.execute(
            """UPDATE user_usage
               SET reserved_cents = MAX(0, reserved_cents - ?),
                   credit_cents   = MAX(0, credit_cents - ?),
                   spent_cents    = spent_cents + ?,
                   updated_at     = ?
               WHERE user_id = ?""",
            (reserved_cents, actual_cents, actual_cents, _now(), user_id))
        conn.commit()
    finally:
        conn.close()


def add_credit(user_id: str, cents: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE user_usage SET credit_cents = credit_cents + ?, updated_at = ? WHERE user_id = ?",
            (cents, _now(), user_id))
        conn.commit()
    finally:
        conn.close()


# --- per-user API keys ------------------------------------------------------

def set_user_key(user_id: str, env_var: str, secret: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO user_keys (user_id, env_var, secret, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id, env_var) DO UPDATE SET secret = excluded.secret,
                                                           created_at = excluded.created_at""",
            (user_id, env_var, secret, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_keys(user_id: str) -> dict:
    """{env_var: ciphertext}. Decryption happens in engine.keyring."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT env_var, secret FROM user_keys WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {r["env_var"]: r["secret"] for r in rows}
    finally:
        conn.close()


def delete_user_key(user_id: str, env_var: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM user_keys WHERE user_id = ? AND env_var = ?",
                     (user_id, env_var))
        conn.commit()
    finally:
        conn.close()


# --- sessions (durable, so a restart does not log everyone out) -------------

def create_session(token: str, user_id: str, expires_at: float) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, _now(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def session_user(token: str) -> dict | None:
    """The user behind a session token, or None if unknown or expired."""
    if not token:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, _now()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_sessions() -> int:
    conn = connect()
    try:
        n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _now() -> float:
    return time.time()


def _id() -> str:
    return uuid.uuid4().hex[:16]


# --- conversations ----------------------------------------------------------

def create_conversation(title: str = "New conversation", user_id: str | None = None) -> str:
    cid = _id()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, user_id) VALUES (?,?,?,?,?)",
            (cid, title, _now(), _now(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def list_conversations(user_id: str | None = None) -> list[dict]:
    """Conversations belonging to this user.

    Scoping is applied in SQL, not filtered afterwards in Python -- a filter
    that is forgotten at one call site leaks someone else's history, while a
    WHERE clause simply returns nothing.
    """
    conn = connect()
    try:
        sql = """SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turn_count
                 FROM conversations c"""
        if user_id is None:
            rows = conn.execute(sql + " ORDER BY c.updated_at DESC").fetchall()
        else:
            rows = conn.execute(sql + " WHERE c.user_id = ? ORDER BY c.updated_at DESC",
                                (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def conversation_owner(cid: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT user_id FROM conversations WHERE id = ?", (cid,)).fetchone()
        return row["user_id"] if row else None
    finally:
        conn.close()


def turn_owner(turn_id: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT c.user_id FROM turns t JOIN conversations c
               ON c.id = t.conversation_id WHERE t.id = ?""", (turn_id,)).fetchone()
        return row["user_id"] if row else None
    finally:
        conn.close()


def rename_conversation(cid: str, title: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title[:120], _now(), cid),
        )
        conn.commit()
    finally:
        conn.close()


def delete_conversation(cid: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM events WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id = ?)", (cid,))
        conn.execute("DELETE FROM turns WHERE conversation_id = ?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()


def get_conversation(cid: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        if not row:
            return None
        turns = conn.execute(
            "SELECT * FROM turns WHERE conversation_id = ? ORDER BY created_at", (cid,)
        ).fetchall()
        return {**dict(row), "turns": [dict(t) for t in turns]}
    finally:
        conn.close()


# --- turns ------------------------------------------------------------------

def create_turn(cid: str, prompt: str, mode: str) -> str:
    tid = _id()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO turns (id, conversation_id, user_prompt, mode, status, created_at) VALUES (?,?,?,?,?,?)",
            (tid, cid, prompt, mode, "running", _now()),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), cid))
        conn.commit()
    finally:
        conn.close()
    return tid


def finish_turn(tid: str, final_answer: str | None, error: str | None = None) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE turns SET final_answer = ?, status = ?, error = ? WHERE id = ?",
            (final_answer, "error" if error else "done", error, tid),
        )
        conn.commit()
    finally:
        conn.close()


def fail_orphaned_runs() -> int:
    """Mark every still-'running' turn as failed. Returns how many.

    A run lives in an asyncio task, not in the database, so a process that
    stops mid-run -- a deploy, a crash, a host that suspends the machine --
    leaves its turn marked `running` forever. The interface believes it and
    shows a spinner that will never resolve.

    This is the first thing the server does on boot, because by definition
    nothing can legitimately be running yet: any task that owned those rows
    died with the previous process.
    """
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE turns SET status = 'error', error = ? WHERE status = 'running'",
            ("The server restarted while this run was in progress, so it was "
             "interrupted. Nothing was charged -- ask again.",))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_turn(tid: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM turns WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def prior_turns(cid: str, before_turn_id: str, limit: int) -> list[dict]:
    """Completed turns before this one, oldest first -- the conversation memory."""
    conn = connect()
    try:
        target = conn.execute("SELECT created_at FROM turns WHERE id = ?", (before_turn_id,)).fetchone()
        if not target:
            return []
        rows = conn.execute(
            """SELECT user_prompt, final_answer FROM turns
               WHERE conversation_id = ? AND created_at < ? AND status = 'done'
               ORDER BY created_at DESC LIMIT ?""",
            (cid, target["created_at"], limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


# --- events -----------------------------------------------------------------

def append_event(tid: str, seq: int, etype: str, agent: str | None, payload: dict) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO events (turn_id, seq, type, agent, payload, at) VALUES (?,?,?,?,?,?)",
            (tid, seq, etype, agent, json.dumps(payload), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def events_since(tid: str, after_seq: int) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT seq, type, agent, payload FROM events WHERE turn_id = ? AND seq > ? ORDER BY seq",
            (tid, after_seq),
        ).fetchall()
        return [
            {"seq": r["seq"], "type": r["type"], "agent": r["agent"], **json.loads(r["payload"])}
            for r in rows
        ]
    finally:
        conn.close()
