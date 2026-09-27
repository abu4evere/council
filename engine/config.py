"""Model roster and tunables -- FREE TIER setup.

Every seat below runs on a provider with a free tier that needs no payment card.
You do not need all of them: Council uses whatever keys are present in .env and
skips the rest. One key works. Three or four is much better, because the whole
value of the synthesis comes from the answers being genuinely different.

MODEL SLUGS GO STALE. Providers rename and retire models constantly. If a run
fails with "model slug probably wrong", run `python check_key.py` -- it checks
every seat against its provider's live catalogue and names the bad one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .providers import PROVIDERS, get as get_provider


@dataclass
class Agent:
    """One seat at the table."""
    key: str            # stable id used in the UI and the event stream
    label: str          # what the user sees
    provider: str       # key into providers.PROVIDERS
    model: str          # slug as that provider names it
    framing: str = ""   # proposer-only: forces a distinct angle of attack
    color: str = "#8b8b9e"

    def available(self) -> bool:
        return get_provider(self.provider).configured()


# --- Feature 1: the parallel proposers -------------------------------------
# Spread across FOUR providers on purpose. Different vendors means genuinely
# different training and different answers -- which is the entire point. Four
# seats on one provider would return four similar answers and the synthesis
# would be mush.
#
# The framings do the same job from the other direction: even identical models
# are pushed to attack the question from conflicting angles.

PROPOSERS: list[Agent] = [
    Agent(
        key="pragmatist", label="Pragmatist",
        provider="groq", model="openai/gpt-oss-120b",
        color="#c96442",
        framing=(
            "Optimise for shipping something that works this week. Prefer boring, "
            "proven tools. Explicitly name anything in the request that is scope "
            "creep and say what you would cut."
        ),
    ),
    Agent(
        key="architect", label="Systems Thinker",
        provider="gemini", model="gemini-3.8-flash",
        color="#5b8def",
        framing=(
            "Optimise for a structure that survives contact with change. Think about "
            "data flow, failure modes, and what becomes expensive to undo later. "
            "Say which decisions are one-way doors."
        ),
    ),
    Agent(
        key="contrarian", label="Contrarian",
        provider="cerebras", model="qwen-3.8-27b",
        color="#3fa87a",
        framing=(
            "Assume the obvious approach is wrong. Propose the option most people "
            "would skip, and argue for it honestly. If the request contains a hidden "
            "false assumption, attack that instead of answering the surface question."
        ),
    ),
    Agent(
        key="operator", label="Operator",
        provider="gemini", model="gemma-4-31b-it",
        color="#a855c7",
        framing=(
            "Focus on what happens after it is built: cost, latency, maintenance, "
            "debugging at 2am, and what the person will actually be annoyed by in a "
            "month. Put concrete numbers on things where you can."
        ),
    ),
    Agent(
        key="skeptic", label="Skeptic",
        provider="nvidia", model="moonshotai/kimi-k3",
        color="#d4a02c",
        framing=(
            "Look for what is missing rather than what is wrong. What has the "
            "request not mentioned that will decide whether this succeeds? Name "
            "the constraint, dependency or person nobody has accounted for, and "
            "say what breaks when it surfaces late."
        ),
    ),
]

# --- The synthesis and debate seats ----------------------------------------
# THE DIVERSITY RULE: no two of these four seats may run the same model.
#
# Drafter writes, Critic attacks, Judge rules, Synthesis merges. If any two of
# those are the same model, that step collapses into self-review -- a model
# shares the blind spots that produced the flaw, so it cannot see it, and a
# Judge scoring its own Critic's attack is not adjudicating anything.
#
# This costs some convenience (more keys to set up) and buys the only thing the
# whole tool is for. check_key.py reports the violation if you break it.
# These want the most capable model you have free access to, because they do the
# reasoning that actually needs capability. If the named provider has no key,
# resolve_seat() below falls back to whatever you do have.

# Synthesis reads every proposer answer at once, so it carries one of the two
# largest inputs in the system. It lives on Gemini for the headroom.
AGGREGATOR = Agent(key="synthesis", label="Synthesis",
                   provider="gemini", model="gemini-flash-latest", color="#5b8def")

DEBATE_ARCHITECT = Agent(key="drafter", label="Drafter",
                         provider="groq", model="openai/gpt-oss-120b", color="#c96442")

DEBATE_CRITIC = Agent(key="critic", label="Critic",
                      provider="nvidia", model="deepseek-ai/deepseek-v4.1-flash", color="#3fa87a")

# The Judge reads the final plan plus every critique -- the single largest input
# in the system, and the seat that failed with HTTP 413 on Groq's 8000 tok/min
# tier. Gemini's ceiling is what makes Debate mode reliable.
# The Judge reads the final plan plus every critique -- the largest input in
# the system. qwen sat here and failed repeatedly: it is the heaviest reasoner
# in the roster, and on the largest prompt it spent even an escalated budget
# thinking and never wrote a verdict. A seat's model has to suit the SHAPE of
# its job, not just be a distinct voice.
DEBATE_JUDGE = Agent(key="judge", label="Judge",
                     provider="nvidia", model="nvidia/nemotron-3-super-120b-a12b",
                     color="#d4a02c")


def resolve_seat(agent: Agent, avoid: set[str] | None = None,
                 avoid_providers: set[str] | None = None) -> Agent | None:
    """Return the seat, or an equivalent on a provider that IS configured.

    Two kinds of diversity, and they are not the same thing:

    * `avoid` (models) is about QUALITY. A model attacking or judging its own
      output shares the blind spots that produced the flaw.
    * `avoid_providers` is about RESILIENCE. Observed live: Google AI Studio
      started returning 503 and took out Synthesis, the Critic's replacement
      AND the Judge in one go, because all three sat on Gemini. Two different
      Gemini models are different voices but they fail together.

    Models are the stronger constraint, so a fresh model on a used provider
    beats a reused model on a fresh one. Provider spread is the tie-breaker.
    """
    avoid = avoid or set()
    avoid_providers = avoid_providers or set()

    if (agent.available() and agent.model not in avoid
            and agent.provider not in avoid_providers):
        return agent

    candidates = voice_pool()
    if not candidates:
        return None

    fresh = [c for c in candidates if c[1] not in avoid]
    if fresh:
        # Prefer one that is also on an unused provider.
        best = [c for c in fresh if c[0] not in avoid_providers]
        provider, model = (best or fresh)[0]
    elif agent.available() and agent.model not in avoid:
        return agent
    else:
        provider, model = candidates[0]

    return Agent(key=agent.key, label=agent.label, provider=provider,
                 model=model, framing=agent.framing, color=agent.color)


def voice_pool() -> list[tuple[str, str]]:
    """Every distinct (provider, model) actually reachable right now.

    Seats first, then each configured provider's other known chat models. With
    four keys the seats are already distinct and the extras go unused; with one
    key this is what stops the whole council collapsing onto a single model.
    """
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(provider: str, model: str) -> None:
        if model not in seen:
            seen.add(model)
            pool.append((provider, model))

    for a in PROPOSERS:
        if a.available():
            add(a.provider, a.model)
    for prov in PROVIDERS.values():
        if prov.env_var and prov.configured():
            for m in prov.alternates:
                add(prov.key, m)
    return pool


def roster_diversity() -> dict:
    """How many genuinely distinct voices the live roster actually has."""
    active = available_proposers()
    decision = []
    seen: set[str] = set()
    for seat in (AGGREGATOR, DEBATE_ARCHITECT, DEBATE_CRITIC, DEBATE_JUDGE):
        r = resolve_seat(seat, avoid=seen)
        if r:
            decision.append(r)
            seen.add(r.model)
    dup = len(decision) - len({d.model for d in decision})
    return {
        "proposers": [(a.label, a.model) for a in active],
        "decision": [(d.label, d.model) for d in decision],
        "distinct_models": len({a.model for a in active} | {d.model for d in decision}),
        "decision_duplicates": dup,
    }


def available_proposers() -> list[Agent]:
    """The proposers that can actually run, reassigned to reachable models.

    The FRAMINGS are what generate different perspectives -- the model behind a
    framing matters less than having the framing present at all. So a proposer
    whose provider has no key is not dropped; it is moved onto another distinct
    model that IS reachable, keeping its angle of attack.

    A seat is only dropped when the pool runs out of distinct models, because
    two seats on the same model with different framings still add something,
    but a third and fourth start returning the same answer twice.
    """
    pool = voice_pool()
    if not pool:
        return []

    out: list[Agent] = []
    used: set[str] = set()

    # Seats that can run as configured keep their model.
    for a in PROPOSERS:
        if a.available() and a.model not in used:
            out.append(a)
            used.add(a.model)

    # The rest borrow whatever distinct models are left.
    spare = [c for c in pool if c[1] not in used]
    for a in PROPOSERS:
        if any(o.key == a.key for o in out):
            continue
        if not spare:
            break
        provider, model = spare.pop(0)
        used.add(model)
        out.append(Agent(key=a.key, label=a.label, provider=provider, model=model,
                         framing=a.framing, color=a.color))

    # Preserve the declared order so the UI is stable between runs.
    order = {a.key: i for i, a in enumerate(PROPOSERS)}
    return sorted(out, key=lambda a: order[a.key])


def any_provider_configured() -> bool:
    return any(p.configured() for p in PROVIDERS.values() if p.env_var)


def missing_key_help() -> str:
    lines = ["No API keys found. Council needs at least one. All of these are free:"]
    for p in PROVIDERS.values():
        if p.env_var and p.key != "openrouter":
            lines.append(f"  {p.env_var:<20} {p.signup}")
    lines.append("Put one or more in .env, then restart the server.")
    return "\n".join(lines)


# --- Tunables ---------------------------------------------------------------
DEBATE_ROUNDS = 3          # critic->drafter cycles. 3 is the sweet spot; past 4
                           # the drafter starts agreeing with everything.
# Reasoning models (Groq's openai/gpt-oss-*) spend tokens THINKING before they
# write a word, and that thinking counts against these budgets. Sized for
# roughly 2-3x the visible answer so the reasoning never squeezes it out. If a
# seat errors with "spent its entire budget on reasoning", raise its number.
# Role budgets, BEFORE each provider's own cap is applied (see tokens_for()).
# Sized for reasoning models, which spend a large share of the budget thinking
# before producing a word -- too small and they return an empty response with
# no error at all.
MAX_TOKENS_PROPOSER = 5000
MAX_TOKENS_SYNTHESIS = 7000
MAX_TOKENS_CRITIC = 5000
MAX_TOKENS_JUDGE = 6000


def tokens_for(provider: str, role_budget: int) -> int:
    """Clamp a role's token budget to what this provider can actually take."""
    return min(role_budget, get_provider(provider).token_cap)


# Hard ceiling on the Judge's INPUT. The debate transcript grows every round; on
# an 8000 tokens/minute tier an unbounded transcript gets the Judge call
# rejected with HTTP 413, which retrying can never fix. ~4 chars per token, and
# the reply budget has to fit in the same window.
MAX_JUDGE_INPUT_CHARS = 12000
# Per-ATTEMPT timeout, and attempts are retried up to 4 times. Keep this
# modest: 180s x 4 attempts is a twelve-minute worst case for one dead seat,
# which is far worse than giving up on it and letting the others answer.
REQUEST_TIMEOUT = 75.0

# Parallelism is now a property of each provider (see providers.py), because
# their limits differ by an order of magnitude. Groq allows 1 concurrent seat;
# Gemini allows 3. Override any provider here, e.g. MAX_PARALLEL=groq:2,gemini:4
_OVERRIDES = {}
for _pair in os.environ.get("MAX_PARALLEL", "").split(","):
    if ":" in _pair:
        _k, _, _v = _pair.partition(":")
        try:
            _OVERRIDES[_k.strip()] = int(_v)
        except ValueError:
            pass


def parallel_for(provider: str) -> int:
    if provider in _OVERRIDES:
        return max(1, _OVERRIDES[provider])
    return max(1, get_provider(provider).max_parallel)


# How many previous turns to replay as context. We send the FINAL answer of each
# prior turn, never the full debate log -- otherwise token use grows
# quadratically and the model drowns in its own old arguments.
MEMORY_TURNS = 6


def all_agents() -> list[Agent]:
    return [*PROPOSERS, AGGREGATOR, DEBATE_ARCHITECT, DEBATE_CRITIC, DEBATE_JUDGE]
