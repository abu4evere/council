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
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "council.db"

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


def user_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
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
