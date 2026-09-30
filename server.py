"""Council - FastAPI backend.

Run:  python -m uvicorn server:app --host 0.0.0.0 --port 8000

--host 0.0.0.0 is what makes it reachable from your phone on the same Wi-Fi.
If you expose it beyond your LAN (a tunnel, a VPS), set COUNCIL_PASSWORD in .env
first -- otherwise anyone who finds the URL is spending your OpenRouter credit.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db  # noqa: E402
from engine import mailer, verification  # noqa: E402
from engine import (accounts, config, disagreement, keyring, memo, prompts,
                    providers, quota, vault)  # noqa: E402
from engine.llm import check_configured_models, list_models  # noqa: E402
from engine.orchestrator import Orchestrator  # noqa: E402

app = FastAPI(title="Council")
STATIC = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Live run plumbing
#
# Each running turn has a bus. The orchestrator writes every event to SQLite
# (durable, replayable) AND pushes it to any attached listeners (live). A client
# that reconnects asks for events after the last seq it saw, so dropping off
# Wi-Fi mid-debate costs you nothing.
# ---------------------------------------------------------------------------

class RunBus:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.seq = 0
        self.listeners: set[asyncio.Queue] = set()
        self.finished = False
        self.task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def emit(self, etype: str, agent: str | None, payload: dict) -> None:
        # The lock is load-bearing, not defensive. Four proposers stream at once,
        # and persisting hops to a worker thread -- without serialising, emit A
        # can take its seq, hand off to the thread, and have emit B overtake it
        # and reach the listener queues first. Subscribers dedupe with a
        # high-water mark (`seq <= last_seq`), so an out-of-order arrival is not
        # merely late, it is silently DISCARDED. That cost real chunks.
        #
        # Holding the lock across seq assignment, persistence and fan-out makes
        # seq order, database order and delivery order the same order.
        async with self._lock:
            self.seq += 1
            seq = self.seq
            event = {"seq": seq, "type": etype, "agent": agent, **payload}
            await asyncio.to_thread(db.append_event, self.turn_id, seq, etype, agent, payload)
            for q in list(self.listeners):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # A stalled subscriber must not block the run. It will
                    # recover the gap by reconnecting with ?after=<last seq>.
                    pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.discard(q)


BUSES: dict[str, RunBus] = {}


def run_is_on_our_money(user_keys: dict) -> bool:
    """True when this run spends the operator's keys rather than the user's.

    A visitor who supplied their own keys is not costing anything, so no quota
    applies to them. Everyone else is drawing on a shared pot.
    """
    needed = {a.provider for a in config.available_proposers()}
    needed |= {config.AGGREGATOR.provider, config.DEBATE_ARCHITECT.provider,
               config.DEBATE_CRITIC.provider, config.DEBATE_JUDGE.provider}
    for name in needed:
        prov = providers.PROVIDERS.get(name)
        if prov and prov.env_var and not user_keys.get(prov.env_var):
            return True
    return False


def load_user_keys(user_id: str | None) -> dict:
    """Decrypted {env_var: api_key} for this user. Empty for a local install."""
    if not user_id:
        return {}
    out = {}
    for env_var, token in db.get_user_keys(user_id).items():
        plain = keyring.decrypt_for(user_id, token)
        if plain:
            out[env_var] = plain
    return out


async def execute_turn(turn_id: str, conversation_id: str, mode: str, question: str,
                       user_keys: dict | None = None,
                       turn_owner_id: str | None = None,
                       challenge: str = "medium",
                       reserved_cents: int = 0) -> None:
    bus = BUSES[turn_id]
    try:
        prior = await asyncio.to_thread(
            db.prior_turns, conversation_id, turn_id, config.MEMORY_TURNS
        )
        memory = prompts.memory_preamble(prior)

        # Long-term memory: the vault. Retrieved notes are ANNOUNCED before the
        # run starts -- this text is about to be sent to four AI providers, and
        # the user should be able to see exactly what left their machine rather
        # than trusting that the retrieval was sensible.
        notes = await asyncio.to_thread(_vault_notes, question, turn_owner_id)
        if notes:
            await bus.emit("vault", None, {
                "notes": [{"path": n.path, "heading": n.heading,
                           "chars": len(n.body)} for n in notes],
            })
            memory = vault.as_context(notes) + "\n" + memory
        orch = Orchestrator(bus.emit, memory=memory, challenge=challenge)
        # Every provider call inside this block sees this user's keys.
        with keyring.use_keys(user_keys or {}):
            answer = await orch.run(mode, question)

        # Lift the structured disagreement out of the answer and announce it
        # separately. The place the models disagreed is the most valuable thing
        # a run produces, and until now it was buried in prose.
        answer, points, decisions = disagreement.extract(answer)
        if points:
            await bus.emit("disagreement", None,
                           {"points": [p.as_dict() for p in points]})
        if decisions:
            # Emitted separately from the answer because it is the payoff, not
            # a footnote: the reason this tool exists is that people do not
            # always know which question to ask.
            await bus.emit("decisions", None,
                           {"decisions": [d.as_dict() for d in decisions]})
        await asyncio.to_thread(db.finish_turn, turn_id, answer, None)
        await bus.emit("done", None, {"final_answer": answer})
    except asyncio.CancelledError:
        # The user stopped it. Not an error -- record it plainly and let the UI
        # show a stopped run rather than a failed one.
        await asyncio.to_thread(db.finish_turn, turn_id, None, "stopped by user")
        await bus.emit("failed", None, {"error": "stopped by user"})
        bus.finished = True
        BUSES.pop(turn_id, None)
        raise
    except Exception as exc:  # surface the failure to the UI rather than hanging
        msg = str(exc) or repr(exc)
        await asyncio.to_thread(db.finish_turn, turn_id, None, msg)
        await bus.emit("failed", None, {"error": msg})
    finally:
        # Settle in `finally` so a crashed or cancelled run still releases its
        # reservation. A held reservation that is never freed silently shrinks
        # the balance forever.
        if reserved_cents and turn_owner_id:
            produced = 0
            try:
                for ev in await asyncio.to_thread(db.events_since, turn_id, 0):
                    if ev.get("type") == "agent_done":
                        produced += len(ev.get("text") or "")
            except Exception:
                produced = 0
            actual = quota.estimate_cents(mode, produced) if produced else 0
            await asyncio.to_thread(db.settle, turn_owner_id, reserved_cents, actual)

        bus.finished = True
        # Give reconnecting clients a window to drain the tail of the stream.
        await asyncio.sleep(90)
        BUSES.pop(turn_id, None)


# ---------------------------------------------------------------------------
# Auth (optional, single shared password)
# ---------------------------------------------------------------------------

# Legacy shared-password sessions, kept only while no account exists so that
# upgrading does not lock a self-hoster out of their own instance.
SHARED_SESSIONS: set[str] = set()

# Guest runs, keyed by a cookie. A guest is not an account: no history, no
# vault, no keys -- just one run so the visitor can see what the thing does
# before being asked to commit to it.
GUEST_RUNS_ALLOWED = int(os.environ.get("GUEST_RUNS", "1"))
_GUEST_USED: dict[str, int] = {}


def guest_id(request: Request, response: Response | None = None) -> str:
    """A stable id for an unauthenticated visitor.

    Cookie-based, so it is trivially defeated by clearing cookies -- which is
    fine. This is a nudge toward signing up, not a security control, and the
    free tier it guards costs nothing. Treating it as a real limit would mean
    fingerprinting people, which is a worse trade than a few extra free runs.
    """
    gid = request.cookies.get("council_guest")
    if not gid:
        gid = secrets.token_urlsafe(16)
        if response is not None:
            response.set_cookie("council_guest", gid, httponly=True,
                                samesite="lax", max_age=60 * 60 * 24 * 30)
    return gid


def guest_runs_left(gid: str) -> int:
    return max(0, GUEST_RUNS_ALLOWED - _GUEST_USED.get(gid, 0))

# Failed login attempts per client address. Password strength is only half the
# story -- an unthrottled login form lets a script try millions of guesses.
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_WINDOW = 300.0
_LOGIN_MAX_TRIES = 8


def _login_blocked(addr: str) -> float:
    now = time.monotonic()
    tries = [t for t in _LOGIN_FAILURES.get(addr, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILURES[addr] = tries
    if len(tries) < _LOGIN_MAX_TRIES:
        return 0.0
    return _LOGIN_WINDOW - (now - tries[0])


def _record_login_failure(addr: str) -> None:
    _LOGIN_FAILURES.setdefault(addr, []).append(time.monotonic())


def _addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "council_session", token, httponly=True, samesite="lax",
        max_age=accounts.SESSION_DAYS * 86400,
    )


def current_user(token: str | None) -> dict | None:
    """The signed-in user, or None.

    Sessions live in SQLite rather than in memory. That is the entire fix for
    "I have to type the password every time" -- the old in-memory set was
    emptied by every restart, which logged every device out.
    """
    if not token:
        return None
    return db.session_user(token)


def optional_user(token: str | None) -> dict | None:
    """The signed-in user, or None -- without refusing the request."""
    return current_user(token)


# On a public deployment the open fallback below is a hole, not a convenience:
# a fresh instance has no accounts yet, so until the first person signs up the
# whole app is unauthenticated and anyone who finds the URL spends the
# operator's API keys without limit. Set this on anything reachable from the
# internet. Signup and login stay open -- they have to -- so the first account
# can still be created.
REQUIRE_ACCOUNTS = os.environ.get("REQUIRE_ACCOUNTS", "").strip().lower() in ("1", "true", "yes")


def require_user(token: str | None) -> dict | None:
    """Enforce access. Returns the user, or None in shared-password mode.

    Four states:
      * REQUIRE_ACCOUNTS set -> a valid session is always required
      * accounts exist  -> a valid session is required
      * no accounts, COUNCIL_PASSWORD set -> the old shared gate still applies
      * no accounts, no password -> open (a local single-user install)
    """
    user = current_user(token)
    if user:
        return user
    if REQUIRE_ACCOUNTS or db.user_count() > 0:
        raise HTTPException(status_code=401, detail="not authenticated")
    if accounts.single_user_mode():
        if not token or token not in SHARED_SESSIONS:
            raise HTTPException(status_code=401, detail="not authenticated")
    return None


def owner_id(user: dict | None) -> str | None:
    return user["id"] if user else None


def _assert_owns(row_owner: str | None, user: dict | None) -> None:
    """Reject access to another account's data.

    404 rather than 403 on purpose: telling a stranger that a conversation
    exists but is not theirs is itself a small leak.
    """
    if user is None:
        return  # shared-password or open mode: there is only one person
    if row_owner != user["id"]:
        raise HTTPException(404, "no such conversation")


# Username-and-password signup with nothing proving the address is real. Off by
# default now: leaving it reachable would make the verified route decorative,
# since anyone could skip it by posting to the old endpoint. Kept behind a flag
# because a self-hoster creating their own first account has nobody to verify
# to, and no mail server to do it with.
ALLOW_LEGACY_SIGNUP = os.environ.get("ALLOW_LEGACY_SIGNUP", "").strip().lower() in ("1", "true", "yes")


# --- what the chat is allowed to say about the models ----------------------

SHOW_MODEL_NAMES = os.environ.get("SHOW_MODEL_NAMES", "").strip().lower() in ("1", "true", "yes")

# Fields that name a vendor or a model. Stripped from every event on the way
# out; the stored copy keeps them.
_MODEL_FIELDS = ("model", "provider")


def public_event(ev: dict) -> dict:
    """An event with vendor and model names removed.

    Returns the event unchanged when SHOW_MODEL_NAMES is on, and copies rather
    than mutating either way -- these dicts come straight from the row cache
    and from the live bus, and quietly editing them would corrupt the replay
    for every other subscriber.
    """
    if SHOW_MODEL_NAMES:
        return ev
    out = dict(ev)
    payload = out.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    if isinstance(payload, dict):
        clean = {k: v for k, v in payload.items() if k not in _MODEL_FIELDS}
        # These read as "no key for gemini" otherwise, which names the vendor
        # in prose rather than in a field.
        if out.get("type") == "agent_skipped":
            clean["reason"] = "this seat is not configured"
        if out.get("type") == "agent_hedge":
            clean["reason"] = "took too long, a second model was asked in parallel"
        out["payload"] = clean
    out.pop("model", None)
    out.pop("provider", None)
    return out


@app.post("/api/signup")
async def signup(request: Request, response: Response):
    if not ALLOW_LEGACY_SIGNUP:
        raise HTTPException(410, "Sign up with your email address.")
    body = await request.json()
    username = accounts.normalise_username(str(body.get("username", "")))
    password = str(body.get("password", ""))

    problem = accounts.username_problem(username) or accounts.password_problem(password)
    if problem:
        raise HTTPException(400, problem)

    uid = await asyncio.to_thread(db.create_user, username,
                                  accounts.hash_password(password))
    if uid is None:
        raise HTTPException(409, "That username is taken.")

    token = accounts.new_session_token()
    await asyncio.to_thread(db.create_session, token, uid, accounts.session_expiry())
    _set_session_cookie(response, token)
    return {"ok": True, "username": username}


# --- email-verified signup -------------------------------------------------
#
# Step 1 sends a code. Step 2 checks it. Step 3 creates the account -- and
# checks the code AGAIN. The code stays the proof of ownership for the whole
# flow, so there is no half-verified state to store and nothing to expire
# separately. Without that re-check, step 3 would be an unauthenticated
# "create an account for any address you like" endpoint.


async def _require_valid_code(email: str, code: str) -> None:
    """Raise unless this code is the live one for this address."""
    row = await asyncio.to_thread(db.get_verification, email)
    if not row:
        raise HTTPException(400, "Ask for a new code.")
    if verification.expired(row["created_at"]):
        await asyncio.to_thread(db.clear_verification, email)
        raise HTTPException(400, "That code expired. Ask for a new one.")
    if row["attempts"] >= verification.MAX_ATTEMPTS:
        await asyncio.to_thread(db.clear_verification, email)
        raise HTTPException(429, "Too many wrong codes. Ask for a new one.")
    if not verification.code_matches(email, code, row["code_hash"]):
        used = await asyncio.to_thread(db.count_verification_attempt, email)
        left = verification.MAX_ATTEMPTS - used
        if left <= 0:
            await asyncio.to_thread(db.clear_verification, email)
            raise HTTPException(429, "Too many wrong codes. Ask for a new one.")
        raise HTTPException(401, f"That code is not right -- {left} tries left.")


@app.post("/api/signup/start")
async def signup_start(request: Request):
    body = await request.json()
    email = verification.normalise_email(str(body.get("email", "")))
    problem = verification.email_problem(email)
    if problem:
        raise HTTPException(400, problem)

    if await asyncio.to_thread(db.get_user_by_email, email):
        # Said plainly rather than hidden. This does tell a stranger whether an
        # address has an account here -- but the alternative is sending someone
        # who already has an account off to wait for a code that never arrives
        # with an explanation, and that costs more than the leak is worth.
        raise HTTPException(409, "That email already has an account. Log in instead.")

    now = time.time()
    row = await asyncio.to_thread(db.get_verification, email)
    sends, window_start = 1, now
    if row:
        if now - row["last_sent"] < verification.RESEND_COOLDOWN_SECONDS:
            wait = int(verification.RESEND_COOLDOWN_SECONDS - (now - row["last_sent"])) + 1
            raise HTTPException(429, f"A code was just sent. Wait {wait}s before asking again.")
        if now - row["window_start"] < 3600:
            window_start, sends = row["window_start"], row["sends"] + 1
            if sends > verification.MAX_SENDS_PER_HOUR:
                raise HTTPException(429, "Too many codes requested for that address. Try again later.")

    code = verification.new_code()
    await asyncio.to_thread(db.put_verification, email,
                            verification.hash_code(email, code), window_start, sends)
    sent, detail = await asyncio.to_thread(mailer.send_code, email, code)
    if not sent:
        # The row would otherwise strand them behind a cooldown for a code they
        # never received, so clear it and let them retry straight away.
        await asyncio.to_thread(db.clear_verification, email)
        raise HTTPException(502, f"Could not send the code ({detail}).")
    return {"ok": True, "delivery": detail,
            "cooldown": verification.RESEND_COOLDOWN_SECONDS,
            "expires_in": verification.CODE_TTL_SECONDS}


@app.post("/api/signup/verify")
async def signup_verify(request: Request):
    body = await request.json()
    email = verification.normalise_email(str(body.get("email", "")))
    code = str(body.get("code", "")).strip()
    await _require_valid_code(email, code)
    return {"ok": True, "email": email}


@app.post("/api/signup/complete")
async def signup_complete(request: Request, response: Response):
    body = await request.json()
    email = verification.normalise_email(str(body.get("email", "")))
    code = str(body.get("code", "")).strip()
    full_name = str(body.get("full_name", "")).strip()
    purpose = str(body.get("purpose", "")).strip()[:200]
    password = str(body.get("password", ""))

    await _require_valid_code(email, code)

    problem = (verification.full_name_problem(full_name)
               or accounts.password_problem(password))
    if problem:
        raise HTTPException(400, problem)

    uid = await asyncio.to_thread(db.create_verified_user, email, full_name,
                                  purpose, accounts.hash_password(password))
    if uid is None:
        raise HTTPException(409, "That email already has an account. Log in instead.")
    await asyncio.to_thread(db.clear_verification, email)

    token = accounts.new_session_token()
    await asyncio.to_thread(db.create_session, token, uid, accounts.session_expiry())
    _set_session_cookie(response, token)
    return {"ok": True, "username": email, "full_name": full_name}


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    addr = _addr(request)
    wait = _login_blocked(addr)
    if wait > 0:
        raise HTTPException(429, f"too many failed attempts - try again in {int(wait)}s")

    username = accounts.normalise_username(str(body.get("username", "")))
    password = str(body.get("password", ""))

    if username:
        user = await asyncio.to_thread(db.get_user_by_name, username)
        # Verify even when the user does not exist, against a throwaway hash, so
        # a missing account cannot be told from a wrong password by timing.
        stored = user["password_hash"] if user else accounts.hash_password("x" * 12)
        if not accounts.verify_password(password, stored) or not user:
            _record_login_failure(addr)
            raise HTTPException(401, "Wrong username or password.")
        token = accounts.new_session_token()
        await asyncio.to_thread(db.create_session, token, user["id"], accounts.session_expiry())
        _LOGIN_FAILURES.pop(addr, None)
        _set_session_cookie(response, token)
        return {"ok": True, "username": user["username"]}

    # Shared-password fallback, only while no account exists.
    expected = os.environ.get("COUNCIL_PASSWORD", "")
    if not expected:
        return {"ok": True, "required": False}
    if await asyncio.to_thread(db.user_count) > 0:
        raise HTTPException(400, "This instance uses accounts. Enter a username.")
    if not secrets.compare_digest(password, expected):
        _record_login_failure(addr)
        raise HTTPException(401, "wrong password")
    _LOGIN_FAILURES.pop(addr, None)
    token = secrets.token_urlsafe(32)
    SHARED_SESSIONS.add(token)
    _set_session_cookie(response, token)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response, council_session: str | None = Cookie(default=None)):
    if council_session:
        await asyncio.to_thread(db.delete_session, council_session)
        SHARED_SESSIONS.discard(council_session)
    response.delete_cookie("council_session")
    return {"ok": True}


@app.get("/api/auth-status")
async def auth_status(request: Request, response: Response,
                      council_session: str | None = Cookie(default=None)):
    user = current_user(council_session)
    has_accounts = await asyncio.to_thread(db.user_count) > 0
    return {
        "mode": ("accounts" if (has_accounts or REQUIRE_ACCOUNTS)
                 else ("password" if accounts.single_user_mode() else "open")),
        "required": has_accounts or REQUIRE_ACCOUNTS or accounts.single_user_mode(),
        "authenticated": bool(user) or (council_session in SHARED_SESSIONS),
        "username": user["username"] if user else None,
        "can_signup": True,
        "verified_signup": True,
        "mail_configured": mailer.configured(),
        "guest_runs_left": (0 if user else guest_runs_left(guest_id(request, response))),
    }


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
async def api_list_conversations(council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    return await asyncio.to_thread(db.list_conversations, owner_id(user))


@app.post("/api/conversations")
async def api_create_conversation(request: Request, response: Response,
                                  council_session: str | None = Cookie(default=None)):
    user = optional_user(council_session)
    if user is None and council_session not in SHARED_SESSIONS:
        # Guest: allowed a conversation only while they have a run left.
        gid = guest_id(request, response)
        if guest_runs_left(gid) <= 0:
            raise HTTPException(401, "not authenticated")
        cid = await asyncio.to_thread(db.create_conversation, "Trial", None)
        return {"id": cid, "guest": True}
    cid = await asyncio.to_thread(db.create_conversation, "New conversation", owner_id(user))
    return {"id": cid}


@app.get("/api/conversations/{cid}")
async def api_get_conversation(cid: str, council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    conv = await asyncio.to_thread(db.get_conversation, cid)
    if not conv:
        raise HTTPException(404, "no such conversation")
    _assert_owns(conv.get("user_id"), user)
    return conv


@app.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str, council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.conversation_owner, cid), user)
    await asyncio.to_thread(db.delete_conversation, cid)
    return {"ok": True}


@app.patch("/api/conversations/{cid}")
async def api_rename_conversation(
    cid: str, request: Request, council_session: str | None = Cookie(default=None)
):
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.conversation_owner, cid), user)
    body = await request.json()
    await asyncio.to_thread(db.rename_conversation, cid, str(body.get("title", "")).strip() or "Untitled")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

@app.post("/api/conversations/{cid}/turns")
async def api_create_turn(
    cid: str, request: Request, response: Response,
    council_session: str | None = Cookie(default=None)
):
    user = optional_user(council_session)
    is_guest = user is None and council_session not in SHARED_SESSIONS
    gid = ""
    if is_guest:
        gid = guest_id(request, response)
        if guest_runs_left(gid) <= 0:
            raise HTTPException(
                401,
                "That was your free run. Create an account to keep going -- it "
                "takes one line and your history is saved.")
        owner = await asyncio.to_thread(db.conversation_owner, cid)
        if owner is not None:
            raise HTTPException(404, "no such conversation")
    else:
        if db.user_count() > 0 and user is None and council_session not in SHARED_SESSIONS:
            raise HTTPException(401, "not authenticated")
        _assert_owns(await asyncio.to_thread(db.conversation_owner, cid), user)

    body = await request.json()
    question = str(body.get("prompt", "")).strip()
    mode = str(body.get("mode", "moa"))
    challenge = str(body.get("challenge", "medium")).lower()
    if challenge not in ("low", "medium", "high"):
        challenge = "medium"
    if not question:
        raise HTTPException(400, "prompt is empty")
    if mode not in ("moa", "debate", "full"):
        raise HTTPException(400, f"unknown mode {mode!r}")
    # Fail fast on the one error that is certain before we start: no keys at
    # all. Letting the run begin means a pile of identical auth failures and a
    # turn stored as an error, when the real problem is an empty .env.
    if not config.any_provider_configured():
        raise HTTPException(400, config.missing_key_help())

    conv = await asyncio.to_thread(db.get_conversation, cid)
    if not conv:
        raise HTTPException(404, "no such conversation")

    turn_id = await asyncio.to_thread(db.create_turn, cid, question, mode)

    # First prompt names the conversation, so the sidebar is navigable.
    if not conv["turns"]:
        title = question[:60] + ("..." if len(question) > 60 else "")
        await asyncio.to_thread(db.rename_conversation, cid, title)

    user_keys = await asyncio.to_thread(load_user_keys, owner_id(user))

    # Limits apply only when the run spends the operator's keys. Reserve BEFORE
    # the run, not after: checking a balance once the money is gone is an
    # accounting entry, not a limit.
    reserved = 0
    uid = owner_id(user)
    if uid and run_is_on_our_money(user_keys):
        day = quota.today()
        usage = await asyncio.to_thread(db.get_usage, uid, day)
        uses_paid = config.PREMIUM_SEATS
        try:
            need = quota.check(usage, mode, uses_paid)
        except quota.Denied as exc:
            raise HTTPException(429, str(exc))
        if need:
            if not await asyncio.to_thread(db.reserve, uid, day, need):
                raise HTTPException(429, "Your credit ran out while this run was starting.")
            reserved = need
        else:
            await asyncio.to_thread(db.count_free_run, uid, day)

    if is_guest and gid:
        _GUEST_USED[gid] = _GUEST_USED.get(gid, 0) + 1

    bus = RunBus(turn_id)
    BUSES[turn_id] = bus
    bus.task = asyncio.create_task(
        execute_turn(turn_id, cid, mode, question, user_keys, owner_id(user),
                     challenge, reserved))
    return {"turn_id": turn_id}


@app.post("/api/turns/{turn_id}/cancel")
async def api_cancel_turn(turn_id: str, council_session: str | None = Cookie(default=None)):
    """Stop a running turn.

    A Full run is 12+ calls and can take seven minutes. Without this the only
    way to abandon a run you regret is to close the tab and let it keep
    spending rate limit in the background.
    """
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.turn_owner, turn_id), user)
    bus = BUSES.get(turn_id)
    if bus is None or bus.task is None or bus.task.done():
        return {"ok": False, "reason": "not running"}
    bus.task.cancel()
    return {"ok": True}


@app.get("/api/turns/{turn_id}/stream")
async def api_stream(
    turn_id: str, after: int = 0, council_session: str | None = Cookie(default=None)
):
    user = optional_user(council_session)
    owner = await asyncio.to_thread(db.turn_owner, turn_id)
    if owner is not None or user is not None:
        user = require_user(council_session)
        _assert_owns(owner, user)
    turn = await asyncio.to_thread(db.get_turn, turn_id)
    if not turn:
        raise HTTPException(404, "no such turn")

    async def gen():
        # ORDER MATTERS. We subscribe to the live bus BEFORE reading the stored
        # backlog. Doing it the other way round opens a window between the read
        # and the subscribe in which emitted events go to neither -- they are
        # persisted but never reach this client. With four proposers streaming
        # concurrently that window is wide enough to swallow real output.
        #
        # Subscribing first means those events land in the queue instead, and
        # the `seq <= last_seq` check below drops the ones the backlog already
        # covered. Nothing lost, nothing duplicated.
        bus = BUSES.get(turn_id)
        q = bus.subscribe() if bus else None

        try:
            backlog = await asyncio.to_thread(db.events_since, turn_id, after)
            last_seq = after
            for ev in backlog:
                last_seq = ev["seq"]
                yield f"data: {json.dumps(public_event(ev))}\n\n"

            if q is None:
                # Run already finished and its bus was reaped; backlog was the
                # whole story.
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                return

            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Comment frame keeps proxies and phone radios from killing an
                    # idle connection during a long model call.
                    yield ": keepalive\n\n"
                    continue
                if ev["seq"] <= last_seq:
                    # Already delivered via the backlog replay above.
                    continue
                last_seq = ev["seq"]
                yield f"data: {json.dumps(public_event(ev))}\n\n"
                if ev["type"] in ("done", "failed"):
                    break
        finally:
            if q is not None and bus is not None:
                bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/turns/{turn_id}/events")
async def api_turn_events(
    turn_id: str, after: int = 0, council_session: str | None = Cookie(default=None)
):
    """Non-streaming fallback, and how the UI rebuilds a past turn's detail view."""
    user = optional_user(council_session)
    owner = await asyncio.to_thread(db.turn_owner, turn_id)
    if owner is not None or user is not None:
        user = require_user(council_session)
        _assert_owns(owner, user)
    events = await asyncio.to_thread(db.events_since, turn_id, after)
    return [public_event(e) for e in events]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def api_health(council_session: str | None = Cookie(default=None)):
    require_user(council_session)
    configured = providers.configured_providers()
    usable = config.available_proposers()
    return {
        "ok": bool(configured),
        "api_key_present": bool(configured),
        "providers_configured": [p.label for p in configured],
        "proposers_available": len(usable),
        "proposers_total": len(config.PROPOSERS),
        "help": "" if configured else config.missing_key_help(),
        "rounds": config.DEBATE_ROUNDS,
        "breaker": __import__("engine.llm", fromlist=["BREAKER"]).BREAKER.snapshot(),
        "seats": [
            {"key": a.key, "label": a.label, "model": a.model,
             "provider": a.provider, "color": a.color,
             "available": a.available()}
            for a in config.PROPOSERS
        ],
    }


@app.get("/api/providers")
async def api_providers(council_session: str | None = Cookie(default=None)):
    """Which free providers exist, and which you have keys for."""
    require_user(council_session)
    return [
        {"key": p.key, "label": p.label, "env_var": p.env_var,
         "signup": p.signup, "notes": p.notes, "configured": p.configured()}
        for p in providers.PROVIDERS.values()
    ]


@app.get("/api/models/check")
async def api_models_check(council_session: str | None = Cookie(default=None)):
    """Validate every slug in config.py against OpenRouter's live catalogue.
    Hit this first if a run dies with 'model not found'."""
    require_user(council_session)
    return await check_configured_models()


@app.get("/api/models")
async def api_models(provider: str = "groq", q: str = "",
                     council_session: str | None = Cookie(default=None)):
    """List one provider's live catalogue -- use this to fix a stale slug."""
    require_user(council_session)
    try:
        models = await list_models(provider)
    except Exception as exc:
        raise HTTPException(400, f"could not list {provider} models: {exc}")
    if q:
        needle = q.lower()
        models = [m for m in models if needle in str(m.get("id", "")).lower()]
    return [
        {"id": m.get("id"), "name": m.get("name"), "context": m.get("context_length")}
        for m in models[:200]
    ]


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/")
async def landing(request: Request,
                  council_session: str | None = Cookie(default=None)):
    """Marketing page for visitors; straight to the app if already signed in.

    Someone who is already logged in does not need to be sold the product they
    are using, so they skip it -- unless they asked for this page on purpose.
    ?home=1 is what the mark in the sidebar links to, and without it that link
    would bounce straight back to the app and look broken.
    """
    asked_for_home = request.query_params.get("home") is not None
    if not asked_for_home and (current_user(council_session)
                               or council_session in SHARED_SESSIONS):
        return RedirectResponse("/app", status_code=302)
    return FileResponse(STATIC / "landing.html", headers={"Cache-Control": "no-store"})


@app.get("/app")
async def app_page():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Never let the browser cache the UI.

    This is a tool you edit. Without this, a changed app.js keeps serving from
    cache and you debug code that is not running -- which costs more time than
    the caching ever saves on a localhost app.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path in ("/", "/app"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _user_vault_root(user_id: str | None) -> "Path | None":
    """This user's own notes folder.

    Falls back to the server-wide VAULT_PATH only when there are no accounts --
    a single-user local install. Once accounts exist, one person's notes must
    never be reachable from another person's question.
    """
    from pathlib import Path
    if user_id:
        raw = (db.get_user_vault(user_id) or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_dir() else None
        return None
    return vault.vault_path()


def _vault_notes(question: str, user_id: str | None):
    """Relevant sections from THIS user's vault, or []."""
    owner = user_id or "__local__"
    if _user_vault_root(user_id) is None:
        return []
    conn = db.connect()
    try:
        return vault.search(conn, question, owner,
                            budget_chars=config.VAULT_BUDGET_CHARS,
                            max_notes=config.VAULT_MAX_NOTES)
    except Exception:
        return []
    finally:
        conn.close()


def _reindex_vault(user_id: str | None) -> dict:
    owner = user_id or "__local__"
    root = _user_vault_root(user_id)
    if root is None:
        return {"configured": False, "sections": 0,
                "help": "Point Council at a folder of markdown notes to give it memory."}
    conn = db.connect()
    try:
        n = vault.build_index(conn, root, owner)
        return {"configured": True, "path": str(root), "sections": n}
    finally:
        conn.close()


@app.get("/api/usage")
async def api_usage(council_session: str | None = Cookie(default=None)):
    """What this account has left today."""
    user = require_user(council_session)
    if not user:
        return {"tracked": False}
    u = await asyncio.to_thread(db.get_usage, user["id"], quota.today())
    own_keys = bool(await asyncio.to_thread(db.get_user_keys, user["id"]))
    return {
        "tracked": True,
        "own_keys": own_keys,
        "free_runs_left": max(0, quota.FREE_RUNS_PER_DAY - u["runs_today"]),
        "free_runs_per_day": quota.FREE_RUNS_PER_DAY,
        "credit_cents": u["credit_cents"] - u["reserved_cents"],
        "spent_cents": u["spent_cents"],
        "paid_seats_on": config.PREMIUM_SEATS,
    }


@app.get("/api/keys")
async def api_list_keys(council_session: str | None = Cookie(default=None)):
    """Which providers this user has a key for. Never returns a key."""
    user = require_user(council_session)
    saved = {}
    if user:
        for env_var, token in (await asyncio.to_thread(db.get_user_keys, user["id"])).items():
            plain = keyring.decrypt_for(user["id"], token)
            saved[env_var] = keyring.mask(plain) if plain else "unreadable"

    byok_only = os.environ.get("BYOK_ONLY", "").strip().lower() in ("1", "true", "yes")
    out = []
    for prov in providers.PROVIDERS.values():
        if not prov.env_var:
            continue
        out.append({
            "provider": prov.key,
            "label": prov.label,
            "env_var": prov.env_var,
            "signup": prov.signup,
            "notes": prov.notes,
            "yours": saved.get(prov.env_var),
            # True when the operator's own key would be used instead.
            "server_fallback": bool(os.environ.get(prov.env_var, "").strip()) and not byok_only,
        })
    return {"providers": out, "byok_only": byok_only}


@app.put("/api/keys/{provider}")
async def api_set_key(provider: str, request: Request,
                      council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    if not user:
        raise HTTPException(400, "Create an account before saving keys.")
    try:
        prov = providers.get(provider)
    except KeyError:
        raise HTTPException(404, "unknown provider")
    if not prov.env_var:
        raise HTTPException(400, "that provider takes no key")

    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key or len(key) < 8:
        raise HTTPException(400, "That does not look like an API key.")
    if len(key) > 500:
        raise HTTPException(400, "That is too long to be an API key.")

    # Verify before saving. A key that authenticates but lacks permission is
    # the failure mode that cost this project an afternoon -- a GitHub token
    # without the Models scope returned a plain-text 200 that parsed as an
    # empty answer, looking exactly like success.
    ok, detail = await _probe_key(prov.key, key)
    if not ok:
        raise HTTPException(400, f"That key did not work: {detail}")

    await asyncio.to_thread(db.set_user_key, user["id"], prov.env_var,
                            keyring.encrypt_for(user["id"], key))
    return {"ok": True, "masked": keyring.mask(key), "detail": detail}


@app.delete("/api/keys/{provider}")
async def api_delete_key(provider: str, council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    if not user:
        raise HTTPException(400, "no account")
    try:
        prov = providers.get(provider)
    except KeyError:
        raise HTTPException(404, "unknown provider")
    await asyncio.to_thread(db.delete_user_key, user["id"], prov.env_var)
    return {"ok": True}


async def _probe_key(provider: str, key: str) -> tuple[bool, str]:
    """Does this key actually work? Runs a real request, not a format check."""
    from engine.llm import list_models
    prov = providers.get(provider)
    with keyring.use_keys({prov.env_var: key}):
        try:
            models = await list_models(provider)
            return True, f"{len(models)} models available"
        except Exception as exc:
            return False, str(exc)[:140]


@app.get("/api/turns/{turn_id}/memo")
async def api_turn_memo(turn_id: str, save: int = 0,
                        council_session: str | None = Cookie(default=None)):
    """A finished run as a markdown decision memo.

    With ?save=1 it is also written into the user's vault, which makes it
    searchable memory for later runs -- decide something in March, ask a
    related question in June, and the March reasoning comes back on its own.
    """
    user = optional_user(council_session)
    owner = await asyncio.to_thread(db.turn_owner, turn_id)
    if owner is not None or user is not None:
        user = require_user(council_session)
        _assert_owns(owner, user)
    turn = await asyncio.to_thread(db.get_turn, turn_id)
    if not turn:
        raise HTTPException(404, "no such turn")
    if turn["status"] != "done":
        raise HTTPException(400, "that run did not finish, so there is nothing to record")

    events = await asyncio.to_thread(db.events_since, turn_id, 0)
    text = memo.build(turn, [public_event(e) for e in events])
    name = memo.filename_for(turn)

    saved_to = None
    if save:
        root = _user_vault_root(owner_id(user))
        if root is None:
            raise HTTPException(400, "Set a memory folder first, under API keys.")
        folder = root / "Decisions"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            await asyncio.to_thread(path.write_text, text, "utf-8")
            saved_to = str(path)
            # Re-index so the memo is retrievable by the next question.
            await asyncio.to_thread(_reindex_vault, owner_id(user))
        except OSError as exc:
            raise HTTPException(400, f"could not write the memo: {exc}")

    return {"filename": name, "markdown": text, "saved_to": saved_to}


@app.get("/api/vault")
async def api_vault_status(council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    uid = owner_id(user)
    owner = uid or "__local__"
    root = _user_vault_root(uid)
    conn = db.connect()
    try:
        vault.ensure_index(conn)
        n = conn.execute("SELECT COUNT(*) c FROM vault_fts WHERE owner = ?",
                         (owner,)).fetchone()["c"]
    except Exception:
        n = 0
    finally:
        conn.close()
    return {
        "configured": root is not None,
        "path": str(root) if root else (db.get_user_vault(uid) if uid else ""),
        "sections": n,
        "excluded": sorted(vault.ALWAYS_EXCLUDE),
        "help": "A folder of markdown notes. Yours alone -- other accounts never see it.",
    }


@app.put("/api/vault")
async def api_vault_set(request: Request, council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    if not user:
        raise HTTPException(400, "Create an account to use memory.")
    body = await request.json()
    raw = str(body.get("path", "")).strip()

    if not raw:
        await asyncio.to_thread(db.set_user_vault, user["id"], None)
        conn = db.connect()
        try:
            vault.ensure_index(conn)
            conn.execute("DELETE FROM vault_fts WHERE owner = ?", (user["id"],))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "configured": False}

    from pathlib import Path
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"No folder at {p}")
    await asyncio.to_thread(db.set_user_vault, user["id"], str(p))
    result = await asyncio.to_thread(_reindex_vault, user["id"])
    return {"ok": True, **result}


@app.post("/api/vault/reindex")
async def api_vault_reindex(council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    return await asyncio.to_thread(_reindex_vault, owner_id(user))


@app.on_event("startup")
async def startup():
    db.init()
    # Runs live in asyncio tasks, not in the database. Anything still marked
    # running belonged to a process that is already gone, so it can never
    # finish -- and a user watching it sees a spinner with no end. The first
    # real user hit exactly this: a Full run interrupted by the host stopping
    # the machine sat at "running" for 91 minutes.
    orphaned = db.fail_orphaned_runs()
    if orphaned:
        print(f"[startup] marked {orphaned} interrupted run(s) as failed", flush=True)
    # Index once at boot so the first question already has memory. Cheap for a
    # personal vault; re-run from the UI after editing notes.
    try:
        users = await asyncio.to_thread(db.all_users)
        if users:
            for u in users:
                if u.get("vault_path"):
                    r = await asyncio.to_thread(_reindex_vault, u["id"])
                    print(f"[vault] {u['username']}: {r.get('sections', 0)} sections")
        elif vault.vault_path() is not None:
            r = await asyncio.to_thread(_reindex_vault, None)
            print(f"[vault] local: {r.get('sections', 0)} sections from {r.get('path')}")
    except Exception as exc:
        print(f"[vault] index failed: {exc}")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
