"""End-to-end test of the HTTP layer: conversations, SSE streaming, persistence,
reconnect-replay and memory. Uses a fake model, so it costs nothing.

    python test_server.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

for _v in ("GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
           "GITHUB_TOKEN", "OPENROUTER_API_KEY"):
    os.environ[_v] = "test-key-not-used"
os.environ["COUNCIL_PASSWORD"] = ""  # auth off for this test
# This file exercises the run pipeline, not the guest gate -- the one-run trial
# limit is covered by test_accounts.py. Without this it would stop after the
# first run, which is the limit behaving correctly rather than a pipeline bug.
os.environ["GUEST_RUNS"] = "50"

# Use a throwaway database so the test never touches real history.
import db  # noqa: E402
db.DB_PATH = Path(__file__).parent / "test_council.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(db.DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.llm import ModelError  # noqa: E402

FAIL_ROLES: set[str] = set()


async def fake_stream(client, model, system, user, max_tokens, temperature=0.7, **kw):
    role = "proposer"
    for marker, name in [
        ("You are the Aggregator", "aggregator"),
        ("You are the Drafter", "drafter"),
        ("You are the Critic", "critic"),
        ("You are the Judge", "judge"),
    ]:
        if marker in system:
            role = name
            break
    if role in FAIL_ROLES:
        raise ModelError(f"{model}: simulated {role} failure")
    # Echo back whether memory reached the model, so we can assert on it.
    saw_memory = "MEMORY_MARKER_XYZ" in user
    for word in [f"[{role}]", " answer", (" WITH_MEMORY" if saw_memory else " NO_MEMORY")]:
        await asyncio.sleep(0)
        yield word


orchestrator.stream_completion = fake_stream

import server  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


async def drain_stream(client, turn_id, after=0, stop_after=None):
    """Read the SSE stream, returning parsed events."""
    events = []
    async with client.stream("GET", f"/api/turns/{turn_id}/stream?after={after}") as resp:
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            events.append(ev)
            if ev.get("type") in ("done", "failed", "stream_end"):
                break
            if stop_after and len(events) >= stop_after:
                break
    return events


async def main():
    db.init()
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # --- health ---
        r = await client.get("/api/health")
        check("GET /api/health", r.status_code == 200 and r.json()["api_key_present"])

        # --- create conversation ---
        r = await client.post("/api/conversations")
        cid = r.json()["id"]
        check("POST /api/conversations", r.status_code == 200 and bool(cid))

        # --- run a turn ---
        r = await client.post(
            f"/api/conversations/{cid}/turns",
            json={"prompt": "MEMORY_MARKER_XYZ first question", "mode": "moa"},
        )
        turn_id = r.json()["turn_id"]
        check("POST turn accepted", r.status_code == 200 and bool(turn_id))

        events = await drain_stream(client, turn_id)
        types = [e.get("type") for e in events]
        check("SSE streamed to completion", "done" in types, f"types={types[:8]}")
        check("SSE carried agent chunks", types.count("agent_chunk") > 0)
        check("SSE carried agent_done", types.count("agent_done") >= 4,
              f"got {types.count('agent_done')}")

        final = next((e for e in events if e.get("type") == "done"), None)
        check("final answer present", bool(final and final.get("final_answer")))

        # --- events are monotonic, no gaps ---
        seqs = [e["seq"] for e in events if "seq" in e]
        check("event seq strictly increasing", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))

        # --- reconnect replay: asking from seq N returns only the tail ---
        mid = seqs[len(seqs) // 2]
        replay = await client.get(f"/api/turns/{turn_id}/events?after={mid}")
        tail = replay.json()
        check("replay after=N returns only later events",
              all(e["seq"] > mid for e in tail) and len(tail) > 0,
              f"n={len(tail)}")

        # --- full replay equals what we streamed ---
        full = (await client.get(f"/api/turns/{turn_id}/events?after=0")).json()
        check("persisted events match streamed count",
              len(full) == len([e for e in events if "seq" in e]),
              f"persisted={len(full)} streamed={len([e for e in events if 'seq' in e])}")

        # --- persistence: conversation now has the turn, with its answer ---
        conv = (await client.get(f"/api/conversations/{cid}")).json()
        check("turn persisted as done",
              len(conv["turns"]) == 1 and conv["turns"][0]["status"] == "done")
        check("conversation auto-titled from first prompt",
              conv["title"].startswith("MEMORY_MARKER_XYZ"), conv["title"])

        # --- MEMORY: a second turn must receive the first turn's conclusion ---
        r = await client.post(
            f"/api/conversations/{cid}/turns",
            json={"prompt": "second question", "mode": "moa"},
        )
        turn2 = r.json()["turn_id"]
        events2 = await drain_stream(client, turn2)
        final2 = next((e for e in events2 if e.get("type") == "done"), None)
        # The fake echoes WITH_MEMORY when the marker from turn 1 reached it.
        agent_texts = " ".join(
            e.get("text", "") for e in events2 if e.get("type") == "agent_done"
        )
        check("second turn received conversation memory",
              "WITH_MEMORY" in agent_texts,
              "memory preamble did not reach the model")
        check("second turn completed", bool(final2 and final2.get("final_answer")))

        # --- failure path: every proposer down -> turn marked error, UI told ---
        global FAIL_ROLES
        FAIL_ROLES = {"proposer"}
        r = await client.post(
            f"/api/conversations/{cid}/turns", json={"prompt": "doomed", "mode": "moa"}
        )
        turn3 = r.json()["turn_id"]
        events3 = await drain_stream(client, turn3)
        check("failed run emits 'failed' event",
              any(e.get("type") == "failed" for e in events3))
        t3 = (await client.get(f"/api/conversations/{cid}")).json()["turns"][-1]
        check("failed run persisted as error", t3["status"] == "error", t3["status"])
        FAIL_ROLES = set()

        # --- validation ---
        r = await client.post(f"/api/conversations/{cid}/turns", json={"prompt": "", "mode": "moa"})
        check("empty prompt rejected", r.status_code == 400)
        r = await client.post(f"/api/conversations/{cid}/turns", json={"prompt": "x", "mode": "bogus"})
        check("unknown mode rejected", r.status_code == 400)
        r = await client.get("/api/conversations/does-not-exist")
        check("missing conversation 404s", r.status_code == 404)

        # --- delete cascades ---
        await client.delete(f"/api/conversations/{cid}")
        convs = (await client.get("/api/conversations")).json()
        check("conversation deleted", not any(c["id"] == cid for c in convs))
        leftover = (await client.get(f"/api/turns/{turn_id}/events?after=0")).json()
        check("events cascade-deleted with conversation", leftover == [], f"n={len(leftover)}")

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db.DB_PATH) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    sys.exit(code)
