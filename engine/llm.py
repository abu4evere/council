"""Provider-agnostic streaming client.

Every provider in providers.py speaks OpenAI-compatible chat completions, so
this one module talks to all of them. An Agent carries its provider; the only
per-provider differences are the base URL and the auth header.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import AsyncIterator

import httpx

from .breaker import CircuitBreaker
from .thinking import ThinkingStripper
from .providers import Provider, get as get_provider

# One breaker for the process. Shared across runs on purpose: if Gemini is
# overloaded, the next run should already know that.
BREAKER = CircuitBreaker()


class ModelError(Exception):
    """A single model failed. Callers decide whether that kills the run."""


class NotConfigured(ModelError):
    """This seat's provider has no API key set. Skip the seat, don't fail the run."""


def _headers(provider: Provider) -> dict:
    h = {"Content-Type": "application/json"}
    key = provider.api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    if provider.key == "openrouter":
        # OpenRouter uses these for its dashboard. Harmless, optional.
        h["HTTP-Referer"] = "http://localhost:8000"
        h["X-Title"] = os.environ.get("APP_NAME", "council")
    return h


async def stream_completion(
    client: httpx.AsyncClient,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float = 0.7,
    provider: str = "openrouter",
    timeout: float = 180.0,
    on_reasoning=None,
) -> AsyncIterator[str]:
    """Yield answer text as the model produces it.

    REASONING MODELS. Some models (Groq's openai/gpt-oss-*, and others) stream
    their private chain of thought as `reasoning` deltas BEFORE any `content`.
    Two consequences, both of which bit us:

      1. Those tokens count against max_tokens. Too small a budget is spent
         entirely on thinking and the answer comes back EMPTY -- with a 200 and
         no error. If that happens we raise, rather than return "".
      2. The reasoning is worth surfacing. Pass `on_reasoning` (an async
         callable taking a str) to receive it; it is never mixed into the
         yielded answer text.

    Raises NotConfigured when the provider has no key (caller should skip the
    seat), or ModelError for anything else that means no usable text.
    """
    prov = get_provider(provider)
    if prov.env_var and not prov.api_key():
        raise NotConfigured(
            f"{prov.label} has no API key -- set {prov.env_var} in .env "
            f"(free key: {prov.signup})"
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    if BREAKER.is_open(prov.key):
        # ModelError, not a distinct type: every fallback path in the
        # orchestrator already handles a failed seat correctly, and a tripped
        # breaker IS a failed seat -- just one we predicted instead of paying
        # a timeout to discover.
        raise ModelError(
            f"{prov.label}: skipped, {BREAKER.seconds_remaining(prov.key):.0f}s "
            "left in cooldown after repeated failures"
        )

    saw_content = False
    saw_reasoning = False
    finish = None

    # Free tiers rate-limit hard (Groq: 8k tokens/minute). A 429 is normal
    # traffic control, not a failure -- waiting and retrying almost always
    # works. We only retry while nothing has been yielded yet; once the caller
    # has seen output we cannot rewind the stream, so we surface the error.
    # Some models put their thinking inline in the content, wrapped in
    # <thought> tags, rather than in separate reasoning deltas. Without this
    # the raw thinking lands in the answer AND gets passed to the next debate
    # seat as though it were the argument.
    stripper = ThinkingStripper()

    # Budget escalation. Reasoning models spend a wildly variable share of the
    # budget thinking before they write anything, and the amount depends on the
    # prompt, not just the model -- gpt-oss, qwen, kimi and gemma have each hit
    # this at a different ceiling. Guessing a number per model was a losing
    # game, so instead: notice the failure and retry with more room, up to what
    # the provider allows. Self-correcting, and costs nothing on models that
    # answer normally.
    budget = max_tokens
    cap = get_provider(provider).token_cap

    # A WALL-CLOCK CEILING, not just an attempt count. Four attempts at a 75s
    # timeout plus backoff can hold one seat for over five minutes, during
    # which the interface shows a stage that never changes and the run looks
    # frozen. A stranger closes the tab and concludes it is broken. Better to
    # give up on one seat and let the others answer.
    started = asyncio.get_event_loop().time()
    deadline = started + config_retry_budget()

    attempt = 0
    while True:
        payload["max_tokens"] = budget
        try:
            async for piece in _attempt(client, prov, model, payload, timeout, on_reasoning):
                kind, value = piece
                if kind == "content":
                    answer_part, thought_part = stripper.feed(value)
                    if thought_part:
                        saw_reasoning = True
                        if on_reasoning is not None:
                            await on_reasoning(thought_part)
                    if answer_part:
                        saw_content = True
                        yield answer_part
                elif kind == "reasoning":
                    saw_reasoning = True
                elif kind == "finish":
                    finish = value
            # Release anything the stripper was holding back at the boundary.
            tail_answer, tail_thought = stripper.flush()
            if tail_thought and on_reasoning is not None:
                saw_reasoning = True
                await on_reasoning(tail_thought)
            if tail_answer:
                saw_content = True
                yield tail_answer

            if not saw_content and saw_reasoning and budget < cap:
                # Thought the whole budget away. Give it more room and retry.
                budget = min(budget * 2, cap)
                saw_reasoning = False
                stripper = ThinkingStripper()
                continue

            BREAKER.record_success(prov.key)
            break
        except _RateLimited as rl:
            BREAKER.record_failure(prov.key)
            now = asyncio.get_event_loop().time()
            if saw_content or attempt >= 4 or now >= deadline:
                spent = int(now - started)
                suffix = f" after {spent}s" if now >= deadline else ""
                raise ModelError(f"{prov.label}/{model}: {rl.detail}{suffix}") from None
            if BREAKER.is_open(prov.key):
                raise ModelError(
                    f"{prov.label}/{model}: {rl.detail} - provider now in cooldown"
                ) from None
            # FULL JITTER. A deterministic backoff makes several seats that hit
            # the same limit together retry together and collide again. Spreading
            # them uniformly across the window is strictly better under
            # contention, at no cost when there is none.
            if rl.retry_after:
                wait = rl.retry_after
            else:
                wait = random.uniform(0, min(2 ** attempt * 3, 30))
            # Never sleep past the deadline -- waiting 30s to then give up is
            # the worst of both outcomes.
            wait = min(wait, max(0.0, deadline - now))
            if wait <= 0:
                raise ModelError(
                    f"{prov.label}/{model}: {rl.detail} after "
                    f"{int(now - started)}s") from None
            attempt += 1
            await asyncio.sleep(wait)

    # NEVER return empty silently. A caller that receives "" has no way to tell
    # a broken provider from a legitimately empty answer, and an empty plan
    # propagates into the next debate round as though it were real. Observed:
    # a GitHub token missing the Models permission returns a plain-text
    # "200 OK" with no SSE frames at all -- no content, no reasoning, no
    # finish_reason -- which sailed straight through the old checks.
    if not saw_content:
        if saw_reasoning:
            raise ModelError(
                f"{prov.label}/{model}: spent its entire {max_tokens}-token budget "
                "on reasoning and never produced an answer, even after escalation."
            )
        if finish == "length":
            raise ModelError(
                f"{prov.label}/{model}: hit the {max_tokens}-token limit "
                "before producing any answer."
            )
        raise ModelError(
            f"{prov.label}/{model}: returned no content and gave no reason. "
            "The endpoint answered but sent nothing usable -- usually a "
            "credential that authenticates but lacks permission for this API."
        )


def config_retry_budget() -> float:
    """Total seconds a single seat may spend across all its attempts."""
    from . import config
    return config.SEAT_RETRY_BUDGET


class _NeedsMoreTokens(Exception):
    """The model spent its whole budget thinking and never wrote an answer."""


class _RateLimited(Exception):
    def __init__(self, detail: str, retry_after: float | None):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


async def _attempt(client, prov, model, payload, timeout, on_reasoning):
    """One HTTP attempt. Yields ('content'|'reasoning'|'finish', value)."""
    try:
        async with client.stream(
            "POST",
            f"{prov.url()}/chat/completions",
            headers=_headers(prov),
            json=payload,
            timeout=timeout,
        ) as resp:
            # Transient and worth retrying: 429 is rate limiting, 5xx means the
            # provider is overloaded. Gemini's free tier returns 503 "model is
            # currently overloaded" often enough that not retrying it loses
            # seats on a regular basis -- treat both the same way.
            if resp.status_code in (429, 500, 502, 503, 504):
                body = (await resp.aread()).decode("utf-8", "replace")[:200]
                ra = resp.headers.get("retry-after")
                try:
                    retry_after = float(ra) if ra else None
                except ValueError:
                    retry_after = None
                label = "rate limited" if resp.status_code == 429 else "provider overloaded"
                raise _RateLimited(f"{label} ({resp.status_code}) {body.strip()}", retry_after)
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:400]
                hint = ""
                if resp.status_code in (401, 403):
                    hint = f" (check {prov.env_var} in .env)"
                elif resp.status_code == 404:
                    hint = " (model slug probably wrong -- run: python check_key.py)"
                raise ModelError(f"{prov.label}/{model}: HTTP {resp.status_code}{hint} {body}")

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    msg = chunk["error"]
                    if isinstance(msg, dict):
                        msg = msg.get("message", str(msg))
                    raise ModelError(f"{prov.label}/{model}: {msg}")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if choices[0].get("finish_reason"):
                    yield ("finish", choices[0]["finish_reason"])
                delta = choices[0].get("delta") or {}

                # Reasoning models emit this instead of content while thinking.
                think = delta.get("reasoning") or delta.get("reasoning_content")
                if think:
                    yield ("reasoning", think)
                    if on_reasoning is not None:
                        await on_reasoning(think)

                text = delta.get("content")
                if text:
                    yield ("content", text)


    except httpx.TimeoutException as exc:
        # Retryable: a provider that is slow now is often fine seconds later,
        # and the caller only surfaces this once attempts are exhausted.
        raise _RateLimited(f"timed out after {timeout}s", None) from exc
    except httpx.HTTPError as exc:
        # Includes DNS failures (getaddrinfo) and dropped connections, which on
        # a home network are usually a blip rather than a real outage.
        raise _RateLimited(f"connection failed ({exc})", None) from exc


async def list_models(provider: str) -> list[dict]:
    """Live model list for one provider."""
    prov = get_provider(provider)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{prov.url()}/models", headers=_headers(prov), timeout=30.0
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", body if isinstance(body, list) else [])


async def check_configured_models() -> dict:
    """Validate every seat in config.py against its provider's live catalogue."""
    from . import config

    seats = config.all_agents()
    wanted = {a.provider for a in seats}

    catalogues: dict[str, set[str] | None] = {}
    errors: dict[str, str] = {}
    for name in wanted:
        prov = get_provider(name)
        if prov.env_var and not prov.api_key():
            catalogues[name] = None
            errors[name] = f"no key ({prov.env_var} not set)"
            continue
        try:
            ids = set()
            for m in await list_models(name):
                mid = str(m.get("id", ""))
                ids.add(mid)
                # Google lists models as "models/gemini-3.8-flash" but accepts
                # the bare name in requests. Comparing raw strings reported
                # working models as missing, which is worse than not checking.
                if "/" in mid:
                    ids.add(mid.split("/", 1)[1])
            catalogues[name] = ids
        except Exception as exc:
            catalogues[name] = None
            errors[name] = f"could not reach: {exc}"

    results = []
    for a in seats:
        cat = catalogues.get(a.provider)
        if cat is None:
            status = "unconfigured" if "no key" in errors.get(a.provider, "") else "unreachable"
        else:
            status = "ok" if a.model in cat else "missing"
        results.append({
            "seat": a.label,
            "provider": get_provider(a.provider).label,
            "model": a.model,
            "status": status,
            "detail": errors.get(a.provider, ""),
        })

    usable = [r for r in results if r["status"] == "ok"]
    return {
        "ok": bool(usable) and not any(r["status"] == "missing" for r in results),
        "seats": results,
        "usable_count": len(usable),
        "provider_errors": errors,
    }
