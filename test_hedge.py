"""Hedged seats: cut the straggler tail without dropping an answer.

The bug this guards against is not a crash, it is a slow run. Measured across
stored runs: four seats finished in 33s and a fifth sat silent for 153s inside
its retry budget, holding the whole run open. These tests pin the three things
that make hedging safe:

  * a SILENT seat gets raced, and the backup's answer reaches the panel
  * a STREAMING seat is never raced, however slow it is -- otherwise every
    slow-but-working model would burn a second provider's quota
  * nothing is ever dropped: if either side produces text, that text is the
    seat's answer

    python test_hedge.py
"""
import asyncio
import dataclasses
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_hedge.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

from engine import config, orchestrator  # noqa: E402
from engine.orchestrator import Orchestrator  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


PRIMARY = config.PROPOSERS[0]
BACKUP = dataclasses.replace(PRIMARY, model="backup-model", provider="cerebras")

# How each fake model behaves: (delay before first token, chunks, or an error).
BEHAVIOUR: dict = {}


def fake_stream(client, model, system, user, max_tokens, **kw):
    """Stand-in for stream_completion: an async generator per model.

    `think` emits reasoning deltas before any answer token, which is how a
    reasoning model behaves and is the case the second threshold exists for.
    """
    on_reasoning = kw.get("on_reasoning")

    async def gen():
        spec = BEHAVIOUR.get(model, {})
        if spec.get("error"):
            await asyncio.sleep(spec.get("delay", 0))
            raise orchestrator.ModelError(spec["error"])
        for _ in range(spec.get("think", 0)):
            await asyncio.sleep(spec.get("think_gap", 0.1))
            if on_reasoning:
                await on_reasoning("thinking...")
        await asyncio.sleep(spec.get("delay", 0))
        for chunk in spec.get("chunks", []):
            yield chunk
            await asyncio.sleep(spec.get("gap", 0))
    return gen()


class Bus:
    def __init__(self):
        self.events = []

    async def emit(self, type_, agent, payload):
        self.events.append((type_, agent, payload))

    def types(self):
        return [e[0] for e in self.events]

    def text_for(self, key):
        return "".join(p.get("text", "") for t, a, p in self.events
                       if t == "agent_chunk" and a == key)


def make_orch(bus):
    o = Orchestrator(bus.emit)
    o.question = "does the hedge fire?"
    return o


async def run_seat(bus, backup=BACKUP):
    o = make_orch(bus)
    o._backup_seat = lambda agent: backup
    return await o._run_proposer_hedged(None, PRIMARY)


async def main():
    db.init()
    orchestrator.stream_completion = fake_stream
    config.HEDGE_AFTER = 0.3          # keep the suite fast
    config.HEDGE_CONTENT_AFTER = 0.9
    config.REQUEST_TIMEOUT = 5

    # --- 1. a silent primary gets raced, and the backup's answer is used ----
    BEHAVIOUR.clear()
    BEHAVIOUR["backup-model"] = {"delay": 0.05, "chunks": ["backup ", "answer"]}
    BEHAVIOUR[PRIMARY.model] = {"delay": 30, "chunks": ["too late"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=5)
    check("silent seat is hedged", "agent_hedge" in bus.types(), str(bus.types()))
    check("the backup's answer is returned", text == "backup answer", repr(text))
    check("no error reported", err is None, str(err))
    check("the panel is filled", bus.text_for(PRIMARY.key) == "backup answer",
          repr(bus.text_for(PRIMARY.key)))
    check("the seat keeps its identity", agent.key == PRIMARY.key)
    check("the panel says it was hedged",
          any(t == "agent_start" and p.get("hedged") for t, a, p in bus.events))
    check("the run does not report a failure",
          "agent_error" not in bus.types(), str(bus.types()))

    # --- 2. a streaming seat is NEVER hedged, however slow ------------------
    BEHAVIOUR.clear()
    # First token quickly, then a long slow tail: alive, so hands off.
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.05, "chunks": ["slow ", "but ", "working"], "gap": 0.2}
    BEHAVIOUR["backup-model"] = {"delay": 0, "chunks": ["should not be used"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=5)
    check("a streaming seat is not hedged", "agent_hedge" not in bus.types(), str(bus.types()))
    check("its own answer is kept", text == "slow but working", repr(text))
    check("the backup never reaches the panel",
          "should not be used" not in bus.text_for(PRIMARY.key))

    # --- 3. primary fails outright, backup carries the seat -----------------
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.5, "error": "429 rate limited"}
    BEHAVIOUR["backup-model"] = {"delay": 0.05, "chunks": ["rescued"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=5)
    check("a failing primary is rescued by the hedge", text == "rescued", repr(text))
    check("the rescue is not reported as an error", err is None, str(err))

    # --- 4. both sides fail: an error, not a hang and not a crash -----------
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.4, "error": "primary down"}
    BEHAVIOUR["backup-model"] = {"delay": 0.4, "error": "backup down"}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=5)
    check("both failing yields no text", text is None, repr(text))
    check("an error is reported", bool(err), str(err))
    check("the panel shows the failure", "agent_error" in bus.types(), str(bus.types()))

    # --- 5. nowhere to hedge to: behave exactly as before -------------------
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.5, "chunks": ["eventually"]}
    bus = Bus()
    o = make_orch(bus)
    o._backup_seat = lambda agent: None
    agent, text, err = await asyncio.wait_for(
        o._run_proposer_hedged(None, PRIMARY), timeout=5)
    check("with no backup available the seat still answers", text == "eventually", repr(text))
    check("and no hedge is announced", "agent_hedge" not in bus.types())

    # --- 6. the loser is cancelled, not left running ------------------------
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 30, "chunks": ["never"]}
    BEHAVIOUR["backup-model"] = {"delay": 0.05, "chunks": ["fast"]}
    before = len(asyncio.all_tasks())
    bus = Bus()
    await asyncio.wait_for(run_seat(bus), timeout=5)
    await asyncio.sleep(0.15)
    leaked = len(asyncio.all_tasks()) - before
    check("the losing attempt is cancelled, not leaked", leaked <= 0, f"{leaked} extra tasks")

    # --- 7. the hedge is a different provider, never the same one -----------
    o = make_orch(Bus())
    same = o._backup_seat(PRIMARY)
    check("a backup on the same provider is refused",
          same is None or same.provider != PRIMARY.provider,
          f"{same.provider if same else None} vs {PRIMARY.provider}")

    # --- 8. reasoning without an answer is hedged on the SECOND threshold ---
    # Measured in production: synthesis emitted reasoning at 24s and its first
    # answer token at 107.8s, costing 243s overall. It was never silent, so the
    # first threshold could never have caught it.
    config.HEDGE_CONTENT_AFTER = 0.9
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"think": 40, "think_gap": 0.1, "chunks": ["far too late"]}
    BEHAVIOUR["backup-model"] = {"delay": 0.05, "chunks": ["quick ", "answer"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=6)
    check("a seat that only reasons is hedged", "agent_hedge" in bus.types(), str(bus.types()))
    reason = next((p.get("reason") for t, a, p in bus.events if t == "agent_hedge"), "")
    check("and it is hedged for the RIGHT reason", "reasoning" in reason, reason)
    check("the backup's answer is used", text == "quick answer", repr(text))

    # --- 9. reasoning THEN answering in time is left alone ------------------
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"think": 3, "think_gap": 0.1, "chunks": ["worth ", "the wait"]}
    BEHAVIOUR["backup-model"] = {"delay": 0, "chunks": ["should not be used"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=6)
    check("a model that thinks then answers in time is not hedged",
          "agent_hedge" not in bus.types(), str(bus.types()))
    check("its own answer is kept", text == "worth the wait", repr(text))

    # --- 10. reasoning alone does not satisfy the FIRST threshold ----------
    # Reasoning proves the seat is alive, so it must not be hedged at 0.3s for
    # "no output at all" -- only later, for "no answer".
    config.HEDGE_CONTENT_AFTER = 5
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"think": 6, "think_gap": 0.1, "chunks": ["made it"]}
    BEHAVIOUR["backup-model"] = {"delay": 0, "chunks": ["not this"]}
    bus = Bus()
    agent, text, err = await asyncio.wait_for(run_seat(bus), timeout=6)
    check("reasoning counts as alive for the first threshold",
          "agent_hedge" not in bus.types(), str(bus.types()))
    check("so the seat keeps its own answer", text == "made it", repr(text))
    config.HEDGE_CONTENT_AFTER = 0.9

    # --- 11. decision seats: the _stream contract is preserved -------------
    # synthesis and the critic wrap their call in `except ModelError` and fall
    # back. If the hedge swallowed failures instead of raising, those
    # fallbacks would silently stop working.
    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.4, "error": "primary down"}
    BEHAVIOUR["backup-model"] = {"delay": 0.4, "error": "backup down"}
    bus = Bus()
    o = make_orch(bus)
    o._backup_seat = lambda agent: BACKUP
    raised = None
    try:
        await asyncio.wait_for(
            o._hedged_stream(None, PRIMARY, "sys", "usr", 100, 0.5), timeout=6)
    except Exception as exc:
        raised = exc
    check("_hedged_stream raises when both sides fail", raised is not None)
    check("and it raises ModelError, so the fallbacks still catch it",
          isinstance(raised, orchestrator.ModelError), type(raised).__name__)

    BEHAVIOUR.clear()
    BEHAVIOUR[PRIMARY.model] = {"delay": 0.05, "chunks": ["merged ", "answer"]}
    bus = Bus()
    o = make_orch(bus)
    o._backup_seat = lambda agent: BACKUP
    out = await asyncio.wait_for(
        o._hedged_stream(None, PRIMARY, "sys", "usr", 100, 0.5), timeout=6)
    check("_hedged_stream returns text like _stream does", out == "merged answer", repr(out))
    check("and streams it live when nothing stalls",
          bus.text_for(PRIMARY.key) == "merged answer", repr(bus.text_for(PRIMARY.key)))

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
