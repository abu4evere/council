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
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db  # noqa: E402
from engine import config, prompts, providers  # noqa: E402
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
        orch = Orchestrator(bus.emit, memory=memory)
        answer = await orch.run(mode, question)
        await asyncio.to_thread(db.finish_turn, turn_id, answer, None)
        await bus.emit("done", None, {"final_answer": answer})
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

SESSIONS: set[str] = set()


def password_required() -> bool:
    return bool(os.environ.get("COUNCIL_PASSWORD", "").strip())


def check_auth(session: str | None) -> None:
    if not password_required():
        return
    if not session or session not in SESSIONS:
        raise HTTPException(status_code=401, detail="not authenticated")


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    expected = os.environ.get("COUNCIL_PASSWORD", "")
    if not expected:
        return {"ok": True, "required": False}
    if not secrets.compare_digest(str(body.get("password", "")), expected):
        raise HTTPException(status_code=401, detail="wrong password")
    token = secrets.token_urlsafe(32)
    SESSIONS.add(token)
    response.set_cookie(
        "council_session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )
    return {"ok": True}


@app.get("/api/auth-status")
async def auth_status(council_session: str | None = Cookie(default=None)):
    return {
        "required": password_required(),
        "authenticated": (not password_required()) or council_session in SESSIONS,
    }


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
async def api_list_conversations(council_session: str | None = Cookie(default=None)):
    check_auth(council_session)
    return await asyncio.to_thread(db.list_conversations)


@app.post("/api/conversations")
async def api_create_conversation(council_session: str | None = Cookie(default=None)):
    check_auth(council_session)
    cid = await asyncio.to_thread(db.create_conversation)
    return {"id": cid}


@app.get("/api/conversations/{cid}")
async def api_get_conversation(cid: str, council_session: str | None = Cookie(default=None)):
    check_auth(council_session)
    conv = await asyncio.to_thread(db.get_conversation, cid)
    if not conv:
        raise HTTPException(404, "no such conversation")
    return conv


@app.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str, council_session: str | None = Cookie(default=None)):
    check_auth(council_session)
    await asyncio.to_thread(db.delete_conversation, cid)
    return {"ok": True}


@app.patch("/api/conversations/{cid}")
async def api_rename_conversation(
    cid: str, request: Request, council_session: str | None = Cookie(default=None)
):
    check_auth(council_session)
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
    check_auth(council_session)
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

    BUSES[turn_id] = RunBus(turn_id)
    asyncio.create_task(execute_turn(turn_id, cid, mode, question))
    return {"turn_id": turn_id}


@app.get("/api/turns/{turn_id}/stream")
async def api_stream(
    turn_id: str, after: int = 0, council_session: str | None = Cookie(default=None)
):
    check_auth(council_session)
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
    check_auth(council_session)
    return await asyncio.to_thread(db.events_since, turn_id, after)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def api_health(council_session: str | None = Cookie(default=None)):
    check_auth(council_session)
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
    check_auth(council_session)
    return [
        {"key": p.key, "label": p.label, "env_var": p.env_var,
         "signup": p.signup, "notes": p.notes, "configured": p.configured()}
        for p in providers.PROVIDERS.values()
    ]


@app.get("/api/models/check")
async def api_models_check(council_session: str | None = Cookie(default=None)):
    """Validate every slug in config.py against OpenRouter's live catalogue.
    Hit this first if a run dies with 'model not found'."""
    check_auth(council_session)
    return await check_configured_models()


@app.get("/api/models")
async def api_models(provider: str = "groq", q: str = "",
                     council_session: str | None = Cookie(default=None)):
    """List one provider's live catalogue -- use this to fix a stale slug."""
    check_auth(council_session)
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


@app.on_event("startup")
async def startup():
    db.init()


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
