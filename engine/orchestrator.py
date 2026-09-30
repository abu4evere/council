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

from . import config, language, prompts
from .llm import ModelError, NotConfigured, stream_completion

Emit = Callable[[str, "str | None", dict], Awaitable[None]]


class Mute:
    """A silence switch a stream can read while it is already running.

    A plain bool is captured at call time, and a hedge has to be able to mute
    a stream that started twenty seconds earlier.
    """
    __slots__ = ("on",)

    def __init__(self, on: bool = False) -> None:
        self.on = on


class Orchestrator:
    def __init__(self, emit: Emit, memory: str = "", challenge: str = "medium"):
        self.emit = emit
        self.memory = memory
        self.challenge = challenge
        # Set once per run from the question. Internal seats always work in
        # English; only the seats the user reads switch.
        self.language = "English"
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

    def _internal_note(self) -> str:
        """Told only to seats that talk to other seats, never to the reader.

        BEST EFFORT, and knowingly so. Models mirror the language of the
        question hard, and this instruction is sometimes ignored outright --
        observed with an Uzbek question, where a proposer answered in Uzbek
        despite being told twice not to. Nothing here breaks when that happens:
        the seat still reasons well, the user-facing seats still reply in the
        user's language, and the only loss is cross-vendor consistency in the
        middle of the pipeline. Enforcing it would cost a translation pass per
        seat, which is worse than the problem.
        """
        return language.INTERNAL_NOTE if self.language != "English" else ""

    def _reply_note(self) -> str:
        """Told only to the seats whose output the user actually reads."""
        return language.reply_instruction(self.language)

    def _with_memory(self, prompt: str) -> str:
        return f"{self.memory}{prompt}" if self.memory else prompt

    async def _stream(self, client, agent, system, user, max_tokens, temperature,
                      *, key=None, silent=False, on_first_token=None,
                      on_any_output=None):
        """Stream one seat's output, throttled per provider, emitting as it goes.

        `silent` buffers without emitting, for a hedged attempt that has not won
        yet -- two attempts streaming into one panel would interleave two
        different answers. It may be a plain bool, or a Mute object, which lets
        a stream ALREADY IN FLIGHT be silenced when a hedge starts.

        Two liveness callbacks, because they mean different things:
        `on_first_token` fires on the first answer token, `on_any_output` fires
        on the first output of any kind including reasoning. A model emitting
        reasoning is alive but has not answered, and those need telling apart.
        """
        buf: list[str] = []
        panel = key or agent.key
        mute = silent if hasattr(silent, "on") else Mute(bool(silent))

        async def on_reasoning(text: str) -> None:
            # Kept separate from the answer. Useful to watch, never part of the
            # text that gets fed to the next seat in the debate.
            if on_any_output is not None:
                on_any_output()
            if not mute.on:
                await self.emit("agent_reasoning", panel, {"text": text})

        capped = config.tokens_for(agent.provider, max_tokens)
        async with self._gate(agent.provider):
            async for chunk in stream_completion(
                client, agent.model, system, user, capped,
                temperature=temperature, provider=agent.provider,
                timeout=config.REQUEST_TIMEOUT, on_reasoning=on_reasoning,
            ):
                if not buf and chunk:
                    if on_first_token is not None:
                        on_first_token()
                    if on_any_output is not None:
                        on_any_output()
                buf.append(chunk)
                if not mute.on:
                    await self.emit("agent_chunk", panel, {"text": chunk})
        return "".join(buf).strip()

    async def _stall_reason(self, primary, got_any, got_content):
        """Why this seat should be hedged, or None to leave it alone.

        Returns as soon as the answer starts, so a seat that is writing is
        never raced -- spending a second provider's quota to overtake a model
        that is already delivering is pure waste.
        """
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        any_w = asyncio.create_task(got_any.wait())
        content_w = asyncio.create_task(got_content.wait())
        try:
            await asyncio.wait({primary, any_w, content_w},
                               timeout=config.HEDGE_AFTER,
                               return_when=asyncio.FIRST_COMPLETED)
            if primary.done() or got_content.is_set():
                return None
            if not got_any.is_set():
                return f"no output at all in {config.HEDGE_AFTER:.0f}s"

            # Alive, but still thinking. Give it the longer budget.
            remaining = config.HEDGE_CONTENT_AFTER - (loop.time() - t0)
            if remaining > 0:
                # Only the CONTENT waiter here. any_w has already finished --
                # that is how we got to this branch -- and FIRST_COMPLETED on a
                # set holding a finished task returns instantly, which would
                # skip the wait entirely and hedge every reasoning model.
                await asyncio.wait({primary, content_w}, timeout=remaining,
                                   return_when=asyncio.FIRST_COMPLETED)
            if primary.done() or got_content.is_set():
                return None
            return (f"reasoning but no answer after "
                    f"{config.HEDGE_CONTENT_AFTER:.0f}s")
        finally:
            for t in (any_w, content_w):
                if not t.done():
                    t.cancel()

    async def _hedged_stream(self, client, agent, system, user, max_tokens,
                             temperature, *, panel=None):
        """_stream, with a second provider raced in when the seat stalls.

        Same contract as _stream -- returns text, raises on failure -- so every
        fallback already wrapped around these call sites (synthesis dropping
        back to the raw proposals, the critic's failover chain) keeps working
        untouched.
        """
        key = panel or agent.key
        mute = Mute(False)
        got_any = asyncio.Event()
        got_content = asyncio.Event()

        async def attempt(seat, seat_mute):
            try:
                # _stream, NOT _hedged_stream: this is the attempt itself, and
                # calling back into the hedge here would recurse forever.
                text = await self._stream(
                    client, seat, system, user, max_tokens, temperature,
                    key=key, silent=seat_mute,
                    on_first_token=got_content.set if seat is agent else None,
                    on_any_output=got_any.set if seat is agent else None)
                return seat, text, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return seat, None, exc

        primary = asyncio.create_task(attempt(agent, mute))

        try:
            reason = await self._stall_reason(primary, got_any, got_content)
        except asyncio.CancelledError:
            primary.cancel()
            raise

        backup_seat = None if reason is None else self._backup_seat(agent)
        if backup_seat is None:
            seat, text, exc = await primary
            if exc is not None:
                raise exc
            return text

        # The panel holds no answer text yet -- that is what triggered the
        # hedge -- so muting the primary now keeps it clean for the winner.
        mute.on = True
        await self.emit("agent_hedge", key,
                        {"label": agent.label, "provider": backup_seat.provider,
                         "model": backup_seat.model, "reason": reason})
        backup = asyncio.create_task(attempt(backup_seat, Mute(True)))

        pending = {primary, backup}
        winner = None
        fallback = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    seat, text, exc = task.result()
                    if text:
                        winner = (seat, text, exc)
                        break
                    if fallback is None:
                        fallback = (seat, text, exc)
                if winner:
                    break
        finally:
            for t in (primary, backup):
                if not t.done():
                    t.cancel()

        if winner is None:
            seat, text, exc = fallback or (agent, None, ModelError("no answer"))
            raise exc if exc is not None else ModelError(f"{agent.model}: empty response")

        seat, text, _ = winner
        # Both sides were muted from the hedge onward, so the winner's answer
        # has never reached the panel. Put it there in one piece, and say which
        # model actually produced it.
        await self.emit("agent_start", key,
                        {"label": agent.label, "model": seat.model,
                         "provider": seat.provider, "color": agent.color,
                         "role": "proposer", "hedged": seat is not agent})
        await self.emit("agent_chunk", key, {"text": text})
        return text

    async def _announce(self, agent, role, round_=None):
        payload = {"label": agent.label, "model": agent.model,
                   "provider": agent.provider, "color": agent.color, "role": role}
        if round_ is not None:
            payload["round"] = round_
        await self.emit("agent_start", agent.key, payload)

    # -- Feature 1 ----------------------------------------------------------

    async def _run_proposer(self, client, agent, *, panel=None, silent=False,
                            on_first_token=None, announce=True):
        """Never raises -- one dead seat must not take down the other three."""
        key = panel or agent.key
        if announce:
            await self._announce(agent, "proposer")
        try:
            # The language note goes FIRST: the same text at the end was
            # observed being ignored entirely.
            system = (self._internal_note()
                      + prompts.proposer_system(agent.label, agent.framing, self.challenge))
            text = await self._stream(
                client, agent, system, self._with_memory(self.question),
                config.MAX_TOKENS_PROPOSER, 0.85,
                key=key, silent=silent, on_first_token=on_first_token,
            )
            if not text:
                raise ModelError(f"{agent.model}: empty response")
            if not silent:
                await self.emit("agent_done", key, {"text": text})
            return agent, text, None
        except asyncio.CancelledError:
            raise
        except ModelError as exc:
            if not silent:
                await self.emit("agent_error", key, {"error": str(exc)})
            return agent, None, str(exc)
        except Exception as exc:
            if not silent:
                await self.emit("agent_error", key, {"error": repr(exc)})
            return agent, None, repr(exc)

    def _backup_seat(self, agent):
        """An equivalent seat for the same role on a DIFFERENT provider.

        The straggler is nearly always stuck behind one provider's rate limit,
        so a backup on the same provider would queue behind the same wall.
        """
        # Deliberately laxer than the primary resolution: it avoids only THIS
        # seat's provider, not every provider used so far. By the time the
        # synthesis seat stalls, all five proposer providers are already spent,
        # and a strict rule would leave nowhere to hedge to. A slightly less
        # diverse answer beats a seat that stalls for four minutes.
        alt = config.resolve_seat(
            agent,
            avoid={agent.model},
            avoid_providers={agent.provider},
        )
        if alt is None or alt.provider == agent.provider:
            return None
        return alt

    async def _run_proposer_hedged(self, client, agent):
        """A proposer seat, hedged. Returns (agent, text, error); never raises."""
        await self._announce(agent, "proposer")
        try:
            # The language note goes FIRST: the same text at the end was
            # observed being ignored entirely.
            system = (self._internal_note()
                      + prompts.proposer_system(agent.label, agent.framing, self.challenge))
            text = await self._hedged_stream(
                client, agent, system, self._with_memory(self.question),
                config.MAX_TOKENS_PROPOSER, 0.85)
            if not text:
                raise ModelError(f"{agent.model}: empty response")
            await self.emit("agent_done", agent.key, {"text": text})
            return agent, text, None
        except asyncio.CancelledError:
            raise
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

        results = await asyncio.gather(*(self._run_proposer_hedged(client, a) for a in seats))
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
            text = await self._hedged_stream(client, agg,
                                      prompts.with_rules(prompts.SYNTHESIS, self.challenge)
                                      + self._reply_note(),
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


    async def _critique(self, client, crit_user: str, rnd: int):
        """One round of criticism, trying each available critic in turn.

        The old code tried exactly one replacement and then broke out of the
        whole loop. Measured latency explains why that mattered: the configured
        critic took 216 SECONDS to fail while three alternatives answered in
        under two. A single retry on a bad day is not enough, and giving up
        afterwards throws away rounds that would have worked.

        Returns the critique, or None when every candidate failed.
        """
        tried: set[str] = set()
        seat = self.critic
        for attempt in range(3):
            if seat is None or seat.model in tried:
                break
            tried.add(seat.model)
            if attempt:
                await self.emit("stage", None,
                                {"stage": "critique",
                                 "label": f"critic unavailable - round {rnd} "
                                          f"retrying with {seat.model}"})
            await self._announce(seat, "critic", rnd)
            try:
                text = await self._hedged_stream(
                    client, seat,
                    self._internal_note()
                    + prompts.with_rules(prompts.DEBATE_CRITIC, self.challenge),
                    crit_user, config.MAX_TOKENS_CRITIC, 0.8)
                # A working critic becomes the default for later rounds, so one
                # slow model is paid for once rather than every round.
                self.critic = seat
                return text
            except ModelError as exc:
                await self.emit("agent_error", seat.key, {"error": str(exc)})
                seat = config.resolve_seat(
                    config.DEBATE_CRITIC,
                    avoid=tried | {self.drafter.model} if self.drafter else tried,
                    avoid_providers={seat.provider},
                )
        return None

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
        failed_rounds = 0
        self.critic = critic
        self.drafter = drafter
        plan = seed_plan

        if plan is None:
            await self.emit("stage", None,
                            {"stage": "drafting", "label": "drafting the initial plan"})
            await self._announce(drafter, "drafter", 0)
            plan = await self._hedged_stream(client, drafter, self._internal_note() + prompts.with_rules(prompts.DEBATE_DRAFTER, self.challenge),
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
                crit_user = (f"ORIGINAL GOAL:\n{question}\n\n"
                             f"PLAN TO ATTACK (round {rnd}):\n{plan}")
                critique = await self._critique(client, crit_user, rnd)
                if critique is None:
                    # This round produced nothing. That is a reason to skip the
                    # round, NOT to abandon the debate: a transient failure in
                    # round 1 used to cost rounds 2 and 3 as well, and the Judge
                    # then ruled on a completely unchallenged draft. The plan
                    # still exists, so try the next round against it.
                    failed_rounds += 1
                    if failed_rounds >= 2:
                        await self.emit("stage", None,
                                        {"stage": "critique",
                                         "label": "critics unavailable - going to the verdict"})
                        break
                    continue
                await self.emit("agent_done", self.critic.key, {"text": critique})
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
                    plan = await self._hedged_stream(client, drafter, self._internal_note() + prompts.with_rules(prompts.DEBATE_DRAFTER, self.challenge),
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
            verdict = await self._hedged_stream(client, judge, prompts.with_rules(prompts.DEBATE_JUDGE, self.challenge) + self._reply_note(),
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
        self.language = language.detect(question)
        if self.language != "English":
            await self.emit("language", None, {"language": self.language})
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
