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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db  # noqa: E402
from engine import accounts, config, prompts, providers, vault  # noqa: E402
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


async def execute_turn(turn_id: str, conversation_id: str, mode: str, question: str) -> None:
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
        notes = await asyncio.to_thread(_vault_notes, question)
        if notes:
            await bus.emit("vault", None, {
                "notes": [{"path": n.path, "heading": n.heading,
                           "chars": len(n.body)} for n in notes],
            })
            memory = vault.as_context(notes) + "\n" + memory
        orch = Orchestrator(bus.emit, memory=memory)
        answer = await orch.run(mode, question)
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


def require_user(token: str | None) -> dict | None:
    """Enforce access. Returns the user, or None in shared-password mode.

    Three states:
      * accounts exist  -> a valid session is required
      * no accounts, COUNCIL_PASSWORD set -> the old shared gate still applies
      * no accounts, no password -> open (a local single-user install)
    """
    user = current_user(token)
    if user:
        return user
    if db.user_count() > 0:
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


@app.post("/api/signup")
async def signup(request: Request, response: Response):
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
async def auth_status(council_session: str | None = Cookie(default=None)):
    user = current_user(council_session)
    has_accounts = await asyncio.to_thread(db.user_count) > 0
    return {
        "mode": "accounts" if has_accounts else ("password" if accounts.single_user_mode() else "open"),
        "required": has_accounts or accounts.single_user_mode(),
        "authenticated": bool(user) or (council_session in SHARED_SESSIONS),
        "username": user["username"] if user else None,
        "can_signup": True,
    }


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
async def api_list_conversations(council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
    return await asyncio.to_thread(db.list_conversations, owner_id(user))


@app.post("/api/conversations")
async def api_create_conversation(council_session: str | None = Cookie(default=None)):
    user = require_user(council_session)
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
    cid: str, request: Request, council_session: str | None = Cookie(default=None)
):
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.conversation_owner, cid), user)
    body = await request.json()
    question = str(body.get("prompt", "")).strip()
    mode = str(body.get("mode", "moa"))
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

    bus = RunBus(turn_id)
    BUSES[turn_id] = bus
    bus.task = asyncio.create_task(execute_turn(turn_id, cid, mode, question))
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
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.turn_owner, turn_id), user)
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
                yield f"data: {json.dumps(ev)}\n\n"

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
                yield f"data: {json.dumps(ev)}\n\n"
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
    user = require_user(council_session)
    _assert_owns(await asyncio.to_thread(db.turn_owner, turn_id), user)
    return await asyncio.to_thread(db.events_since, turn_id, after)


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
async def index():
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
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _vault_notes(question: str):
    """Relevant vault sections, or [] when no vault is configured."""
    root = vault.vault_path()
    if root is None:
        return []
    conn = db.connect()
    try:
        return vault.search(conn, question,
                            budget_chars=config.VAULT_BUDGET_CHARS,
                            max_notes=config.VAULT_MAX_NOTES)
    except Exception:
        return []
    finally:
        conn.close()


def _reindex_vault() -> dict:
    root = vault.vault_path()
    if root is None:
        return {"configured": False, "sections": 0,
                "help": "Set VAULT_PATH in .env to the folder holding your notes."}
    conn = db.connect()
    try:
        n = vault.build_index(conn, root)
        return {"configured": True, "path": str(root), "sections": n}
    finally:
        conn.close()


@app.get("/api/vault")
async def api_vault_status(council_session: str | None = Cookie(default=None)):
    require_user(council_session)
    root = vault.vault_path()
    if root is None:
        return {"configured": False,
                "help": "Set VAULT_PATH in .env to a folder of markdown notes."}
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM vault_fts").fetchone()["c"]
    except Exception:
        n = 0
    finally:
        conn.close()
    return {"configured": True, "path": str(root), "sections": n,
            "excluded": sorted(vault.ALWAYS_EXCLUDE)}


@app.post("/api/vault/reindex")
async def api_vault_reindex(council_session: str | None = Cookie(default=None)):
    require_user(council_session)
    return await asyncio.to_thread(_reindex_vault)


@app.on_event("startup")
async def startup():
    db.init()
    # Index once at boot so the first question already has memory. Cheap for a
    # personal vault; re-run from the UI after editing notes.
    if vault.vault_path() is not None:
        try:
            r = await asyncio.to_thread(_reindex_vault)
            print(f"[vault] indexed {r.get('sections', 0)} sections from {r.get('path')}")
        except Exception as exc:
            print(f"[vault] index failed: {exc}")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
