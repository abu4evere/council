"""Offline smoke test - exercises the whole engine with a fake model.

Spends no OpenRouter credit. Run it after changing the orchestrator:
    python smoke_test.py
"""
import asyncio
import os
import sys

# Fake keys for every provider, so all seats are "available" and the roster
# under test is the full one. No network call is made -- stream is faked.
for _v in ("GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
           "GITHUB_TOKEN", "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.setdefault(_v, "test-key-not-used")

from engine import config, orchestrator  # noqa: E402
from engine.llm import ModelError, NotConfigured  # noqa: E402

# Seats are failed by ROLE, not by model slug. Several seats deliberately share
# a model in config.py, so failing a slug would knock out two seats at once and
# the test would not be measuring what it claims to.
FAIL_ROLES: set[str] = set()
FAIL_LABELS: set[str] = set()


def role_of(system: str) -> str:
    for marker, name in [
        ("You are the Aggregator", "aggregator"),
        ("You are the Drafter", "drafter"),
        ("You are the Critic", "critic"),
        ("You are the Judge", "judge"),
        ("one of several independent advisors", "proposer"),
    ]:
        if marker in system:
            return name
    return "unknown"


async def fake_stream(client, model, system, user, max_tokens, temperature=0.7, **kw):
    """Stand-in for the real streaming call."""
    role = role_of(system)
    if role in FAIL_ROLES:
        raise ModelError(f"{model}: simulated {role} failure")
    # Proposers are told their own label, so we can fail exactly one of them.
    if role == "proposer" and any(f"You are {lbl}," in system for lbl in FAIL_LABELS):
        raise ModelError(f"{model}: simulated proposer failure")
    for word in (f"[{role}", f" via {model}]", " some", " generated", " text."):
        await asyncio.sleep(0)
        yield word


async def run_mode(mode: str, fail_roles=None, fail_labels=None):
    global FAIL_ROLES, FAIL_LABELS
    FAIL_ROLES = set(fail_roles or ())
    FAIL_LABELS = set(fail_labels or ())
    events = []

    async def emit(etype, agent, payload):
        events.append((etype, agent))

    orch = orchestrator.Orchestrator(emit, memory="")
    result = await orch.run(mode, "How should I structure a new project?")
    return result, events


async def main():
    orchestrator.stream_completion = fake_stream
    ok = True

    # 1. All three modes complete.
    for mode in ("moa", "debate", "full"):
        result, events = await run_mode(mode)
        starts = sum(1 for t, _ in events if t == "agent_start")
        errors = [a for t, a in events if t == "agent_error"]
        status = "PASS" if result and not errors else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"[{status}] mode={mode:<7} agents_run={starts:<3} events={len(events):<4} "
              f"result_len={len(result)}")

    # 2. One dead proposer must not kill a MoA run.
    result, events = await run_mode("moa", fail_labels={config.PROPOSERS[0].label})
    errs = [a for t, a in events if t == "agent_error"]
    if result and len(errs) == 1:
        print(f"[PASS] one proposer down -> run survived ({len(errs)} error, still got an answer)")
    else:
        ok = False
        print(f"[FAIL] one proposer down -> errors={errs} result={bool(result)}")

    # 3. A dead AGGREGATOR must not throw away the proposer answers we paid for.
    result, events = await run_mode("moa", fail_roles={"aggregator"})
    if result and "Synthesis failed" in result and "Advisor" in result:
        print("[PASS] aggregator down -> fell back to the raw proposer answers")
    else:
        ok = False
        print(f"[FAIL] aggregator down -> result={result[:120]!r}")

    # 4. Every proposer dead must raise, not hang or return junk.
    try:
        await run_mode("moa", fail_roles={"proposer"})
        ok = False
        print("[FAIL] all proposers down -> should have raised ModelError")
    except ModelError:
        print("[PASS] all proposers down -> raised ModelError as expected")

    # 5. A dead critic must still produce a verdict from the plan so far.
    result, events = await run_mode("debate", fail_roles={"critic"})
    has_judge = any(a == config.DEBATE_JUDGE.key for t, a in events if t == "agent_done")
    if result and has_judge:
        print("[PASS] critic down -> debate degraded gracefully, judge still ran")
    else:
        ok = False
        print(f"[FAIL] critic down -> judge_ran={has_judge} result={bool(result)}")

    # 6. A dead judge must still hand back the last revision of the plan.
    result, _ = await run_mode("debate", fail_roles={"judge"})
    if result and "Judge failed" in result and "[drafter" in result:
        print("[PASS] judge down -> returned the final plan instead of losing the debate")
    else:
        ok = False
        print(f"[FAIL] judge down -> result={result[:120]!r}")

    # 7. A dead drafter has nothing to salvage and must fail loudly, not hang.
    try:
        await run_mode("debate", fail_roles={"drafter"})
        ok = False
        print("[FAIL] drafter down -> should have raised ModelError")
    except ModelError:
        print("[PASS] drafter down -> raised ModelError as expected")

    # 8. THE FREE-TIER CASE: only one provider key present. Seats on the other
    #    providers must be SKIPPED (not failed), and the run must still finish.
    saved = {}
    for v in ("GEMINI_API_KEY", "CEREBRAS_API_KEY", "GITHUB_TOKEN", "MISTRAL_API_KEY",
              "OPENROUTER_API_KEY"):
        saved[v] = os.environ.pop(v, None)
    try:
        # Derive the expectation from the roster rather than hardcoding it --
        # seats move between providers whenever a model slug goes stale.
        expect_skipped = {a.key for a in config.PROPOSERS if not a.available()}
        expect_ran = {a.key for a in config.PROPOSERS if a.available()}

        result, events = await run_mode("moa")
        skipped = {a for t, a in events if t == "agent_skipped"}
        ran = {a for t, a in events if t == "agent_start"}
        errors = [a for t, a in events if t == "agent_error"]
        if (result and skipped == expect_skipped
                and expect_ran <= ran and not errors):
            print(f"[PASS] one key only -> {len(skipped)} seat(s) skipped cleanly, "
                  f"{len(expect_ran)} ran, still got an answer")
        else:
            ok = False
            print(f"[FAIL] one key only -> skipped={skipped} (want {expect_skipped}) "
                  f"ran={ran} errors={errors}")

        # The single-provider debate must borrow that provider for every seat.
        result, events = await run_mode("debate")
        if result:
            print("[PASS] one key only -> debate seats fell back to the available provider")
        else:
            ok = False
            print("[FAIL] one key only -> debate produced nothing")
    finally:
        for v, val in saved.items():
            if val is not None:
                os.environ[v] = val

    # 9. NO keys at all must raise a helpful message, not a crash.
    saved_all = {}
    for v in ("GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY", "GITHUB_TOKEN",
              "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
        saved_all[v] = os.environ.pop(v, None)
    try:
        await run_mode("moa")
        ok = False
        print("[FAIL] no keys -> should have raised NotConfigured")
    except NotConfigured as exc:
        if "free" in str(exc).lower() and "GROQ_API_KEY" in str(exc):
            print("[PASS] no keys -> raised NotConfigured naming the free options")
        else:
            ok = False
            print(f"[FAIL] no keys -> unhelpful message: {str(exc)[:120]!r}")
    finally:
        for v, val in saved_all.items():
            if val is not None:
                os.environ[v] = val

    # 10. The Judge's input must ALWAYS fit its budget. Regression test for a
    #    real failure: an unbounded debate transcript got the Judge call
    #    rejected with HTTP 413 on Groq's 8000 tokens/minute tier, which no
    #    retry can fix. Every one of these used to blow the limit.
    from engine.orchestrator import Orchestrator as _O
    B = config.MAX_JUDGE_INPUT_CHARS
    cases = [
        ("huge everything", "goal " * 50, "PLAN " * 3000, ["C" * 9000] * 3),
        ("huge plan, no critiques", "g" * 100, "P" * 40000, []),
        ("normal", "goal", "P" * 4000, ["C" * 1600] * 3),
        ("tiny", "g", "p", ["c"]),
    ]
    oversized = [(n, len(_O._judge_input(q, p, c)))
                 for n, q, p, c in cases if len(_O._judge_input(q, p, c)) > B]
    if not oversized:
        print(f"[PASS] judge input always fits its {B}-char budget ({len(cases)} cases)")
    else:
        ok = False
        print(f"[FAIL] judge input over budget: {oversized}")

    # 11. THE DIVERSITY RULE. Decision seats must not collapse onto one model
    #     when few providers are configured -- that turns the debate into a
    #     model reviewing itself, which is what the tool exists to avoid.
    saved_d = {}
    for v in ("GEMINI_API_KEY", "CEREBRAS_API_KEY", "GITHUB_TOKEN", "MISTRAL_API_KEY",
              "OPENROUTER_API_KEY"):
        saved_d[v] = os.environ.pop(v, None)
    try:
        pool = config.voice_pool()
        used, picked = set(), []
        for seat in (config.AGGREGATOR, config.DEBATE_ARCHITECT,
                     config.DEBATE_CRITIC, config.DEBATE_JUDGE):
            a = config.resolve_seat(seat, avoid=used)
            used.add(a.model)
            picked.append(a.model)
        # With N models reachable we expect min(N, 4) distinct decision seats.
        want = min(len(pool), 4)
        if len(set(picked)) == want:
            print(f"[PASS] one provider -> {want} distinct decision voices "
                  f"from a {len(pool)}-model pool")
        else:
            ok = False
            print(f"[FAIL] one provider -> got {len(set(picked))} distinct, want {want}: {picked}")

        # And specifically: the Critic must never be the Drafter's own model.
        if len(pool) > 1 and picked[1] != picked[2]:
            print("[PASS] Critic never shares the Drafter's model when an alternative exists")
        elif len(pool) > 1:
            ok = False
            print(f"[FAIL] Critic and Drafter both on {picked[1]}")
    finally:
        for v, val in saved_d.items():
            if val is not None:
                os.environ[v] = val

    # 12. Memory preamble must survive being passed through a real run.
    from engine import prompts as P
    pre = P.memory_preamble([{"user_prompt": "earlier q", "final_answer": "earlier a"}])
    if "earlier q" in pre and "earlier a" in pre and "Current question" in pre:
        print("[PASS] memory preamble builds correctly")
    else:
        ok = False
        print(f"[FAIL] memory preamble -> {pre[:120]!r}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
