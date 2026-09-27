"""The engine: Mixture-of-Agents synthesis, and the debate loop.

Three modes:
  moa     - fan out to the proposers in parallel, then synthesise. Fast (~20s).
  debate  - Drafter vs Critic for N rounds, then the Judge. Thorough (~90s).
  full    - moa, then debate over the synthesis. Slowest, best for "plan my project".

This is deliberately plain asyncio rather than a graph framework. Feature 1 is
asyncio.gather over N calls; feature 2 is a for loop over a growing transcript.
A framework would add an abstraction to learn and a layer to debug through,
and buy nothing at this size.

Free-tier realities this has to survive: a seat whose provider has no key at all
(skip it), and per-minute rate limits (space out seats sharing a provider).
"""
from __future__ import annotations

import asyncio
import collections
from typing import Awaitable, Callable

import httpx

from . import config, prompts
from .llm import ModelError, NotConfigured, stream_completion

Emit = Callable[[str, "str | None", dict], Awaitable[None]]


class Orchestrator:
    def __init__(self, emit: Emit, memory: str = ""):
        self.emit = emit
        self.memory = memory
        # One semaphore per provider. Four seats on four different providers run
        # fully parallel; several seats on ONE provider are throttled, because
        # free tiers rate-limit per minute and a 429 wastes the whole seat.
        self._gates: dict[str, asyncio.Semaphore] = {}
        # Models already given a decision seat in THIS run. Full mode chains
        # synthesis -> debate, so without this the Drafter can end up revising
        # a plan its own model just wrote.
        self._used_models: set[str] = set()
        # Providers already used for a decision seat. Model diversity protects
        # answer quality; provider diversity protects against one vendor's
        # outage taking out the whole decision layer at once.
        self._used_providers: set[str] = set()

    def _gate(self, provider: str) -> asyncio.Semaphore:
        """One semaphore per provider, sized by that provider's own limit."""
        if provider not in self._gates:
            self._gates[provider] = asyncio.Semaphore(config.parallel_for(provider))
        return self._gates[provider]

    def _with_memory(self, prompt: str) -> str:
        return f"{self.memory}{prompt}" if self.memory else prompt

    async def _stream(self, client, agent, system, user, max_tokens, temperature):
        """Stream one seat's output, throttled per provider, emitting as it goes."""
        buf: list[str] = []

        async def on_reasoning(text: str) -> None:
            # Kept separate from the answer. Useful to watch, never part of the
            # text that gets fed to the next seat in the debate.
            await self.emit("agent_reasoning", agent.key, {"text": text})

        capped = config.tokens_for(agent.provider, max_tokens)
        async with self._gate(agent.provider):
            async for chunk in stream_completion(
                client, agent.model, system, user, capped,
                temperature=temperature, provider=agent.provider,
                timeout=config.REQUEST_TIMEOUT, on_reasoning=on_reasoning,
            ):
                buf.append(chunk)
                await self.emit("agent_chunk", agent.key, {"text": chunk})
        return "".join(buf).strip()

    async def _announce(self, agent, role, round_=None):
        payload = {"label": agent.label, "model": agent.model,
                   "provider": agent.provider, "color": agent.color, "role": role}
        if round_ is not None:
            payload["round"] = round_
        await self.emit("agent_start", agent.key, payload)

    # -- Feature 1 ----------------------------------------------------------

    async def _run_proposer(self, client, agent):
        """Never raises -- one dead seat must not take down the other three."""
        await self._announce(agent, "proposer")
        try:
            system = prompts.proposer_system(agent.label, agent.framing)
            text = await self._stream(
                client, agent, system, self._with_memory(self.question),
                config.MAX_TOKENS_PROPOSER, 0.85,
            )
            if not text:
                raise ModelError(f"{agent.model}: empty response")
            await self.emit("agent_done", agent.key, {"text": text})
            return agent, text, None
        except ModelError as exc:
            await self.emit("agent_error", agent.key, {"error": str(exc)})
            return agent, None, str(exc)
        except Exception as exc:
            await self.emit("agent_error", agent.key, {"error": repr(exc)})
            return agent, None, repr(exc)

    async def run_moa(self, client, question: str) -> str:
        seats = config.available_proposers()
        if not seats:
            raise NotConfigured(config.missing_key_help())

        skipped = [a for a in config.PROPOSERS if a not in seats]
        for a in skipped:
            await self.emit("agent_skipped", a.key,
                            {"label": a.label, "provider": a.provider,
                             "reason": f"no key for {a.provider}"})

        await self.emit("stage", None,
                        {"stage": "proposing",
                         "label": f"{len(seats)} model{'s' if len(seats) > 1 else ''} thinking in parallel"})

        results = await asyncio.gather(*(self._run_proposer(client, a) for a in seats))
        good = [(a, t) for a, t, e in results if t]

        if not good:
            seen: list[str] = []
            for _, _, e in results:
                if e and e not in seen:
                    seen.append(e)
            raise ModelError("every proposer failed -- " + " | ".join(seen))

        if len(good) == 1:
            await self.emit("stage", None,
                            {"stage": "synthesis",
                             "label": "only one model answered - using it directly"})
            return good[0][1]

        agg = config.resolve_seat(config.AGGREGATOR, avoid=self._used_models,
                                  avoid_providers=self._used_providers)
        if agg:
            self._used_models.add(agg.model)
            self._used_providers.add(agg.provider)
        blocks = "\n\n".join(
            f"### Advisor {i}: {a.label}\n{t}" for i, (a, t) in enumerate(good, 1)
        )
        if agg is None:
            return blocks

        await self.emit("stage", None,
                        {"stage": "synthesis", "label": f"merging {len(good)} answers"})
        await self._announce(agg, "aggregator")

        user = f"ORIGINAL QUESTION:\n{question}\n\n---\n\nADVISOR ANSWERS:\n\n{blocks}"
        try:
            text = await self._stream(client, agg, prompts.SYNTHESIS,
                                      self._with_memory(user),
                                      config.MAX_TOKENS_SYNTHESIS, 0.4)
            if not text:
                raise ModelError(f"{agg.model}: empty synthesis")
        except ModelError as exc:
            # We already paid for N good answers -- in rate-limit terms as well
            # as money. Losing them because the merge failed is the worst
            # outcome; hand back the raw proposals instead.
            await self.emit("agent_error", agg.key, {"error": str(exc)})
            await self.emit("stage", None,
                            {"stage": "synthesis",
                             "label": "synthesis failed - returning the raw answers"})
            return ("> **Synthesis failed** (`" + str(exc) + "`).\n"
                    "> The individual answers are preserved below, unmerged.\n\n" + blocks)

        await self.emit("agent_done", agg.key, {"text": text})
        return text

    # -- Feature 2 ----------------------------------------------------------

    async def run_debate(self, client, question: str, seed_plan: str | None = None) -> str:
        # Resolved in order, each avoiding the models already taken, so a
        # single-provider fallback still gives three different voices where it
        # can rather than one model debating itself.
        drafter = config.resolve_seat(config.DEBATE_ARCHITECT, avoid=self._used_models)
        taken = set(self._used_models)
        provs = {drafter.provider} if drafter else set()
        if drafter:
            taken.add(drafter.model)
        critic = config.resolve_seat(config.DEBATE_CRITIC, avoid=taken,
                                     avoid_providers=provs)
        if critic:
            taken.add(critic.model)
            provs.add(critic.provider)
        judge = config.resolve_seat(config.DEBATE_JUDGE, avoid=taken,
                                    avoid_providers=provs)
        self._used_models = taken | ({judge.model} if judge else set())
        if drafter is None:
            raise NotConfigured(config.missing_key_help())

        transcript: list[str] = []
        critiques: list[str] = []
        plan = seed_plan

        if plan is None:
            await self.emit("stage", None,
                            {"stage": "drafting", "label": "drafting the initial plan"})
            await self._announce(drafter, "drafter", 0)
            plan = await self._stream(client, drafter, prompts.DEBATE_DRAFTER,
                                      self._with_memory(question),
                                      config.MAX_TOKENS_PROPOSER, 0.7)
            await self.emit("agent_done", drafter.key, {"text": plan})
        else:
            await self.emit("stage", None,
                            {"stage": "drafting",
                             "label": "starting debate from the synthesised plan"})

        transcript.append(f"DRAFTER (initial plan):\n{plan}")

        if critic is not None:
            for rnd in range(1, config.DEBATE_ROUNDS + 1):
                await self.emit("stage", None,
                                {"stage": "critique",
                                 "label": f"round {rnd} of {config.DEBATE_ROUNDS} - critique"})
                await self._announce(critic, "critic", rnd)
                crit_user = (f"ORIGINAL GOAL:\n{question}\n\n"
                             f"PLAN TO ATTACK (round {rnd}):\n{plan}")
                try:
                    critique = await self._stream(client, critic, prompts.DEBATE_CRITIC,
                                                  crit_user, config.MAX_TOKENS_CRITIC, 0.8)
                except ModelError as exc:
                    await self.emit("agent_error", critic.key, {"error": str(exc)})
                    # One bad Critic used to end the whole debate: a round-1
                    # failure skipped rounds 2 and 3 entirely and the Judge got
                    # an unchallenged draft. Try a different model once before
                    # giving up -- the usual cause is this model, not the plan.
                    replacement = config.resolve_seat(
                        config.DEBATE_CRITIC,
                        avoid={critic.model, drafter.model},
                        avoid_providers={critic.provider},
                    )
                    if replacement is None or replacement.model == critic.model:
                        break
                    await self.emit("stage", None,
                                    {"stage": "critique",
                                     "label": f"critic failed - retrying round {rnd} "
                                              f"with {replacement.model}"})
                    critic = replacement
                    await self._announce(critic, "critic", rnd)
                    try:
                        critique = await self._stream(
                            client, critic, prompts.DEBATE_CRITIC, crit_user,
                            config.MAX_TOKENS_CRITIC, 0.8)
                    except ModelError as exc2:
                        await self.emit("agent_error", critic.key, {"error": str(exc2)})
                        break
                await self.emit("agent_done", critic.key, {"text": critique})
                transcript.append(f"CRITIC (round {rnd}):\n{critique}")

                await self.emit("stage", None,
                                {"stage": "revising",
                                 "label": f"round {rnd} of {config.DEBATE_ROUNDS} - revision"})
                await self._announce(drafter, "drafter", rnd)
                rev_user = (f"ORIGINAL GOAL:\n{question}\n\n"
                            f"YOUR CURRENT PLAN:\n{plan}\n\n"
                            f"THE CRITIC ATTACKED IT (round {rnd}):\n{critique}\n\n"
                            "Revise where the Critic is right. Push back where they are wrong.")
                try:
                    plan = await self._stream(client, drafter, prompts.DEBATE_DRAFTER,
                                              rev_user, config.MAX_TOKENS_PROPOSER, 0.7)
                except ModelError as exc:
                    await self.emit("agent_error", drafter.key, {"error": str(exc)})
                    break
                await self.emit("agent_done", drafter.key, {"text": plan})
                transcript.append(f"DRAFTER (revision {rnd}):\n{plan}")

        if judge is None:
            return plan

        await self.emit("stage", None, {"stage": "judging", "label": "extracting the verdict"})
        await self._announce(judge, "judge")
        judge_user = self._judge_input(question, plan, critiques)
        try:
            verdict = await self._stream(client, judge, prompts.DEBATE_JUDGE,
                                         self._with_memory(judge_user),
                                         config.MAX_TOKENS_JUDGE, 0.4)
            if not verdict:
                raise ModelError(f"{judge.model}: empty verdict")
        except ModelError as exc:
            await self.emit("agent_error", judge.key, {"error": str(exc)})
            return ("> **The Judge failed** (`" + str(exc) + "`).\n"
                    "> Below is the plan as it stood after the final revision.\n\n" + plan)

        await self.emit("agent_done", judge.key, {"text": verdict})
        return verdict


    @staticmethod
    def _judge_input(question: str, plan: str, critiques: list[str]) -> str:
        """Build the Judge's prompt within a hard size budget.

        Sending the whole debate transcript does not work on a free tier. It
        grows every round, and on Groq's 8000 tokens/minute limit the Judge call
        is rejected outright with a 413 -- which no amount of retrying fixes.

        It is also unnecessary. Each revision already absorbs the previous
        critique, so the intermediate drafts are largely redundant with the
        final plan. What the Judge actually needs is the FINAL plan in full plus
        what was argued against it. So: full plan, critiques trimmed to fit, and
        the oldest critique trimmed hardest -- later rounds attack the version
        closest to what shipped.
        """
        total_budget = config.MAX_JUDGE_INPUT_CHARS
        q = question if len(question) <= 2000 else question[:2000] + "\n[...trimmed]"
        overhead = len(q) + 200

        # The plan is the most important input, but it is not allowed to eat the
        # whole budget on its own -- a long plan would put us straight back at a
        # 413. Cap it at 70% and trim the MIDDLE, since a plan's opening
        # (recommendation) and end (assumptions) carry the most signal.
        plan_allow = max(1000, int((total_budget - overhead) * 0.70))
        if len(plan) > plan_allow:
            keep = plan_allow // 2
            plan = plan[:keep] + "\n\n[...middle trimmed...]\n\n" + plan[-keep:]

        head = (f"ORIGINAL GOAL:\n{q}\n\n---\n\n"
                f"FINAL PLAN:\n{plan}\n\n---\n\n")
        budget = total_budget - len(head) - 200
        if budget < 500 or not critiques:
            return head + "(critiques omitted - the plan alone filled the budget)"

        # Newest critique gets the largest share.
        weights = [i + 1 for i in range(len(critiques))]
        total = sum(weights)
        parts = []
        for i, (c, w) in enumerate(zip(critiques, weights), 1):
            allow = max(400, int(budget * w / total))
            text = c if len(c) <= allow else c[:allow] + "\n[...trimmed]"
            parts.append(f"CRITICISM RAISED (round {i}):\n{text}")
        return head + "WHAT THE CRITIC ARGUED:\n\n" + "\n\n---\n\n".join(parts)

    # -- entry point --------------------------------------------------------

    async def run(self, mode: str, question: str) -> str:
        self.question = question
        limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
        async with httpx.AsyncClient(limits=limits) as client:
            if mode == "moa":
                return await self.run_moa(client, question)
            if mode == "debate":
                return await self.run_debate(client, question)
            if mode == "full":
                synthesis = await self.run_moa(client, question)
                return await self.run_debate(client, question, seed_plan=synthesis)
            raise ValueError(f"unknown mode: {mode!r}")
