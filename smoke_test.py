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

    # 12. CIRCUIT BREAKER. Observed failure: when Gemini began returning 503,
    #     every Gemini seat independently retried and waited, turning a 40s run
    #     into three minutes. After N consecutive failures a provider must fail
    #     fast so the seat falls back immediately.
    from engine.breaker import CircuitBreaker
    b = CircuitBreaker(threshold=3, cooldown=60)
    opened_at_failure = None
    for i in range(1, 5):
        b.record_failure("gemini")
        if b.is_open("gemini") and opened_at_failure is None:
            opened_at_failure = i
    if opened_at_failure == 3 and not b.is_open("groq"):
        print("[PASS] breaker opens after 3 failures, and only for that provider")
    else:
        ok = False
        print(f"[FAIL] breaker opened at failure {opened_at_failure}, "
              f"groq_open={b.is_open('groq')}")

    b2 = CircuitBreaker(threshold=2, cooldown=0.01)
    for _ in range(2):
        b2.record_failure("x")
    was_open = b2.is_open("x")
    await asyncio.sleep(0.02)
    probe_allowed = not b2.is_open("x")
    b2.record_success("x")
    if was_open and probe_allowed and not b2.is_open("x"):
        print("[PASS] breaker half-opens after cooldown and closes on a good probe")
    else:
        ok = False
        print(f"[FAIL] breaker recovery: open={was_open} probe={probe_allowed}")

    # 13. A failing Critic must not collapse the debate. Observed live: the
    #     round-1 Critic errored, the loop broke, and rounds 2 and 3 never ran
    #     -- the Judge then ruled on a completely unchallenged draft.
    global FAIL_ROLES
    result, events = await run_mode("debate", fail_roles={"critic"})
    rounds_seen = {a for t, a in events if t == "agent_start"}
    judged = any(a == config.DEBATE_JUDGE.key for t, a in events if t == "agent_done")
    retried = any("retrying round" in str(p) for p, _ in [(e, None) for e in events])
    if result and judged:
        print("[PASS] critic failure -> debate degrades but still reaches a verdict")
    else:
        ok = False
        print(f"[FAIL] critic failure -> judged={judged} result={bool(result)}")

    # 14. Token budgets must be clamped per provider, never global. Sizing
    #     everything to Groq's 8000 tok/min starved the reasoning models on
    #     other providers, which then returned empty responses with no error.
    groq_cap = config.tokens_for("groq", config.MAX_TOKENS_PROPOSER)
    gem_cap = config.tokens_for("gemini", config.MAX_TOKENS_PROPOSER)
    if groq_cap < config.MAX_TOKENS_PROPOSER and gem_cap > groq_cap:
        print(f"[PASS] token budgets clamp per provider (groq {groq_cap} < gemini {gem_cap})")
    else:
        ok = False
        print(f"[FAIL] token caps not per-provider: groq={groq_cap} gemini={gem_cap}")

    # 15. Inline <thought> tags must never reach the answer. Some models
    #     (gemma-4-*, DeepSeek R1) put their thinking in the content stream
    #     rather than in separate reasoning deltas -- untreated, the raw
    #     thinking lands in the answer AND is fed to the next debate seat as
    #     though it were the argument.
    from engine.thinking import ThinkingStripper
    strip_cases = [
        (["<thought>hmm</thought>Answer"], "Answer", "hmm"),
        (["<thou", "ght>hmm", "</thou", "ght>Ans"], "Ans", "hmm"),
        (["Plain answer, no tags."], "Plain answer, no tags.", ""),
        (["Before <think>mid</think> after"], "Before  after", "mid"),
        (["<thought>never closed"], "", "never closed"),
    ]
    bad = []
    for chunks, want_answer, want_think in strip_cases:
        st = ThinkingStripper()
        a, t = "", ""
        for ch in chunks:
            x, y = st.feed(ch)
            a += x
            t += y
        x, y = st.flush()
        a += x
        t += y
        if a != want_answer or t != want_think:
            bad.append((chunks, a, t))
    if not bad:
        print(f"[PASS] inline <thought> tags stripped, including across chunk "
              f"boundaries ({len(strip_cases)} cases)")
    else:
        ok = False
        print(f"[FAIL] thinking stripper: {bad}")

    # 16. Budget escalation. Reasoning models spend a variable, prompt-dependent
    #     share of their budget thinking before writing anything -- gpt-oss,
    #     qwen, kimi and gemma each hit this at a different ceiling, and picking
    #     a number per model was a losing game. The client must notice the
    #     failure and retry with more room instead of giving up.
    import inspect
    import engine.llm as _llm
    src = inspect.getsource(_llm.stream_completion)
    has_escalation = "budget = min(budget * 2, cap)" in src
    resets_stripper = src.count("stripper = ThinkingStripper()") >= 2
    respects_cap = "cap = get_provider(provider).token_cap" in src
    if has_escalation and resets_stripper and respects_cap:
        print("[PASS] budget escalates on reasoning-only responses, capped per provider")
    else:
        ok = False
        print(f"[FAIL] escalation: doubles={has_escalation} "
              f"resets_stripper={resets_stripper} capped={respects_cap}")

    # 17. A seat must not be able to hold a run open indefinitely. Observed:
    #     one slow seat stalled a run for over five minutes (4 attempts x 75s
    #     plus backoff) while the interface showed a stage that never changed,
    #     which reads as broken rather than slow.
    import inspect
    import engine.llm as _l
    src2 = inspect.getsource(_l.stream_completion)
    has_deadline = "deadline" in src2 and "now >= deadline" in src2
    clamps_sleep = "min(wait, max(0.0, deadline - now))" in src2
    budget = config.SEAT_RETRY_BUDGET
    worst = budget + config.REQUEST_TIMEOUT
    if has_deadline and clamps_sleep and worst < 390:
        print(f"[PASS] one seat is capped at ~{int(worst)}s, was up to 390s")
    else:
        ok = False
        print(f"[FAIL] retry budget: deadline={has_deadline} "
              f"clamped={clamps_sleep} worst={worst}")

    # 18. LANGUAGE. The debate runs in English for consistency across four
    #     vendors, but the reply comes back in the language the question was
    #     asked in. Only the seats the user READS switch; the ones talking to
    #     each other stay in English, because a translation layer in the middle
    #     of a debate loses precision for no reader.
    from engine import language as _lang
    lang_cases = [
        ("English", "Should I build one big project or five small ones?"),
        ("Russian", "Стоит ли мне создавать проект"),
        ("Uzbek", "Men bitta katta loyiha qilishim kerakmi yoki boshqa"),
        ("English", "hi"),
        ("English", ""),
        ("English", "I use Docker and Kubernetes daily"),
    ]
    wrong = [(w, _lang.detect(t)) for w, t in lang_cases if _lang.detect(t) != w]
    if not wrong:
        print(f"[PASS] language detected correctly ({len(lang_cases)} cases, "
              f"including short and empty input)")
    else:
        ok = False
        print(f"[FAIL] language detection: {wrong}")

    english_silent = _lang.reply_instruction("English") == ""
    names_lang = "Uzbek" in _lang.reply_instruction("Uzbek")
    protects_code = "JSON block" in _lang.reply_instruction("Russian")
    if english_silent and names_lang and protects_code:
        print("[PASS] reply instruction: silent for English, names others, protects code")
    else:
        ok = False
        print(f"[FAIL] reply instruction: silent={english_silent} "
              f"names={names_lang} protects={protects_code}")

    # 20. PAID SEATS must be off unless explicitly enabled, and must never be
    #     enabled by accident. Switching them on with no credit on the account
    #     makes every Synthesis fail with a 402 -- worse than the free model it
    #     replaced -- so the default has to be the safe one.
    import importlib
    import engine.config as _cfg
    saved_premium = os.environ.pop("PREMIUM_SEATS", None)
    try:
        importlib.reload(_cfg)
        off = not _cfg.PREMIUM_SEATS
        syn_free = "free" in _cfg.resolve_seat(_cfg.AGGREGATOR).model or                    _cfg.resolve_seat(_cfg.AGGREGATOR).provider != "openrouter" or                    "claude" not in _cfg.resolve_seat(_cfg.AGGREGATOR).model
        check_off = off and syn_free

        os.environ["PREMIUM_SEATS"] = "true"
        importlib.reload(_cfg)
        on = _cfg.PREMIUM_SEATS
        syn = _cfg.resolve_seat(_cfg.AGGREGATOR)
        jud = _cfg.resolve_seat(_cfg.DEBATE_JUDGE, avoid={syn.model})
        upgraded = "claude" in syn.model and "claude" in jud.model
        # The five proposers must NOT be upgraded: paying for opinion
        # generation is the expensive way to buy the least.
        props_free = all("claude" not in a.model for a in _cfg.available_proposers())

        if check_off and on and upgraded and props_free:
            print("[PASS] paid seats off by default, upgrade only Synthesis and Judge")
        else:
            ok = False
            print(f"[FAIL] premium seats: default_off={check_off} on={on} "
                  f"upgraded={upgraded} proposers_stay_free={props_free}")
    finally:
        os.environ.pop("PREMIUM_SEATS", None)
        if saved_premium is not None:
            os.environ["PREMIUM_SEATS"] = saved_premium
        importlib.reload(_cfg)

    # 21. EVERY SEAT THAT WRITES A FINAL ANSWER must request the structured
    #     block, and no seat that does not must request it. This check exists
    #     because both halves were broken at once and nothing noticed: the
    #     Judge writes the answer in Debate and Full mode and was never given
    #     the instruction, so the panels silently never appeared in the two
    #     slowest modes; and the "decisions" half had been wired through the
    #     parser, the server and the UI while the prompt asking for it failed
    #     to apply, so every run returned zero decisions. Both were invisible
    #     because an absent panel looks identical to "they agreed".
    from engine import prompts as _p
    writes_answer = {"SYNTHESIS": _p.SYNTHESIS, "DEBATE_JUDGE": _p.DEBATE_JUDGE}
    internal = {"PROPOSER": _p.PROPOSER, "DEBATE_DRAFTER": _p.DEBATE_DRAFTER,
                "DEBATE_CRITIC": _p.DEBATE_CRITIC}

    missing_fence = [n for n, t in writes_answer.items() if "council-disagreements" not in t]
    missing_dec = [n for n, t in writes_answer.items() if '"decisions"' not in t]
    leaked = [n for n, t in internal.items() if "council-disagreements" in t]

    if not missing_fence and not missing_dec and not leaked:
        print("[PASS] both answer-writing seats request disagreements AND decisions; "
              "internal seats do not")
    else:
        ok = False
        print(f"[FAIL] block spec: missing_fence={missing_fence} "
              f"missing_decisions={missing_dec} leaked_to_internal={leaked}")

    # And the spec must be defined once, so the two cannot drift apart again.
    single = all(t.count("```json council-disagreements") == 1 for t in writes_answer.values())
    if single:
        print("[PASS] block spec defined once and shared, not duplicated per seat")
    else:
        ok = False
        print("[FAIL] block spec appears more than once in a prompt")

    # 22. Memory preamble must survive being passed through a real run.
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
