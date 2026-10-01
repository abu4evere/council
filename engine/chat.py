"""A fast, friendly reply for the things that are not decisions.

WHY THIS EXISTS. People keep coming back to assistants that feel like someone
is there, not just to tools that are useful. Being answered by a canned block
of text when you say hello is the opposite of that -- it tells you the thing
is a form, not a companion.

WHY IT IS NOT THE COUNCIL. Five models arguing for forty seconds is the right
answer to "should I rewrite this" and an absurd one to "hey". Measured, a
single small model replies in 0.2-0.6 seconds: one call instead of nine, which
is roughly a tenth of the cost and fast enough to feel like conversation.

So the product runs at two speeds. Casual message: one model, instantly, warm.
Real question: the whole council. The user never picks -- the message decides.

WHAT THIS IS NOT ALLOWED TO BECOME. A general chatbot. There is no competing
with the big assistants on breadth, and trying would spend a shared free tier
on trivia. This layer is deliberately short-winded and steers back towards the
thing the product is actually good at, without being pushy about it.
"""
from __future__ import annotations

from . import config
from .llm import stream_completion

# Fastest first. Measured on this instance: cerebras 0.2s, groq 0.6s.
_PREFERRED = (
    ("cerebras", "qwen-3.8-27b"),
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.8-flash"),
)

MAX_TOKENS = 220
TIMEOUT = 12.0


def _pick_model():
    """The fastest configured provider, or None if nothing is set up."""
    for provider, model in _PREFERRED:
        try:
            if config.get_provider(provider).configured():
                return provider, model
        except Exception:
            continue
    return None


def _system(name: str | None) -> str:
    who = f"The person you are talking to is called {name}. Use their name " \
          f"naturally, not in every sentence." if name else ""
    return f"""You are Unstuck. You are warm, brief and genuinely friendly.

{who}

Right now the person has said something casual -- a greeting, a thank you, or
small talk. Reply the way a friendly person would: one or two short sentences.
Match their energy. If they said "yo", you can say "yo" back.

You are not a general assistant and you should not pretend to be. What you are
good at is helping someone think through a decision they are stuck on: several
AI models answer separately, argue with each other, and finish by naming the
choices the person still has to make.

Mention that only if it fits naturally. Do not pitch. Do not list features. Do
not use bullet points. If they just said thanks, just say you are welcome.

Never invent facts about the person or claim to remember things you were not
told. Keep it under 40 words."""


async def reply(client, text: str, name: str | None = None,
                history: list | None = None) -> str | None:
    """A short conversational reply, or None if no model is reachable.

    Returning None rather than raising: the caller has a canned fallback, and
    a greeting is never worth failing a request over.
    """
    picked = _pick_model()
    if picked is None:
        return None
    provider, model = picked

    user = text
    if history:
        # Enough to feel continuous, not enough to cost anything. Only what
        # the person said -- their own words are what makes a callback feel
        # like memory rather than surveillance.
        recent = " | ".join(h[:60] for h in history[-3:] if h)
        if recent:
            user = f"[earlier in this conversation they asked: {recent}]\n\n{text}"

    try:
        chunks = []
        async for chunk in stream_completion(
            client, model, _system(name), user,
            config.tokens_for(provider, MAX_TOKENS),
            temperature=0.85, provider=provider, timeout=TIMEOUT,
        ):
            chunks.append(chunk)
        out = "".join(chunks).strip()
        return out or None
    except Exception:
        # Any failure at all falls back to the canned text. This is a hello,
        # not a run worth retrying.
        return None
