"""Answer greetings without waking five models.

A real user typed "yoooo" and got a full deliberation: five proposers, a
synthesis, and a debate. That is the most expensive possible response to the
cheapest possible input, and it is a bad answer too -- nobody wants four
paragraphs of structured reasoning about "yo".

The cost is the sharper problem. Free tiers are shared by everyone on the
instance and allow roughly eighty full runs a day in total. A handful of people
saying hello eats a real slice of that, and the person who actually had a
question is the one told to come back tomorrow.

THE RULE THIS MODULE OBEYS: be far more willing to let a greeting through than
to catch a real question. Running the council on "hey" wastes a few seconds of
quota. Refusing a genuine question for being short is the product failing at
its only job. So this fires only on an exact match against a known list after
normalising -- never on heuristics like "it is short" or "it has no question
mark", both of which would catch "should I quit?".

It costs nothing to run: no model call, no network, just string comparison.
"""
from __future__ import annotations

import re
import unicodedata

# Things people open a chat window with. Matched as the WHOLE message, never as
# a substring -- "hi" must not catch "hiring a designer".
_GREETINGS = {
    # English
    "hi", "hii", "hey", "hey there", "hello", "helo", "yo", "sup", "wassup",
    "whats up", "what s up", "whatsup", "how are you", "how r u", "hru",
    "good morning", "good afternoon", "good evening", "morning", "gm",
    "howdy", "hiya", "greetings", "anyone there", "you there", "are you there",
    # Testing the thing rather than using it
    "test", "testing", "ping", "hello world", "does this work",
    "are you working", "123", "abc", "asdf", "a", "aa", "aaa",
    # Politeness that needs no deliberation
    "thanks", "thank you", "thx", "ty", "ok", "okay", "cool", "nice", "lol",
    "bye", "goodbye", "see you", "good night",
    # The two other languages this instance actually sees
    "salom", "assalomu alaykum", "qalaysan", "yaxshimisiz",
    "privet", "privyet", "zdravstvuyte", "kak dela",
}

_RUN = re.compile(r"(.)\1{2,}")      # three or more in a row
_RUN_ALL = re.compile(r"(.)\1+")     # any repeat, down to one
_STRIP = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, drop punctuation and emoji, collapse whitespace."""
    t = unicodedata.normalize("NFKD", text or "").casefold()
    t = _STRIP.sub(" ", t)
    return _SPACE.sub(" ", t).strip()


def _forms(text: str) -> set:
    """Every spelling of a message worth testing against the list.

    All three derive from the SAME cleaned string. Deriving one from another is
    a trap worth naming: squeezing "yoooo" to "yoo" and then squeezing THAT
    does nothing, because two letters is not a run, so "yo" is never reached
    and the greeting slips through. That was the first version's bug.
    """
    t = normalise(text)
    return {
        t,                            # "aaa"
        _RUN.sub(r"\1\1", t),         # "yoooo" -> "yoo"
        _RUN_ALL.sub(r"\1", t),       # "yoooo" -> "yo"
    }


def is_small_talk(text: str) -> bool:
    """True only when the message is ENTIRELY a greeting or a test.

    A greeting followed by a real question -- "hey, should I use Postgres?" --
    is a real question and runs normally.
    """
    if not text:
        return False
    if len(text) > 40:
        # Long enough to contain something worth answering, whatever it opens
        # with. The cap is generous on purpose.
        return False
    return bool(_forms(text) & _GREETINGS)


REPLY = (
    "Hello. I am built for one thing: questions you are stuck on.\n\n"
    "Ask me something you have to decide, and give me the context you would "
    "give a friend. For example:\n\n"
    "- *Should I rewrite this project, or keep patching it?*\n"
    "- *I have two weeks and one free evening a day. What should I build?*\n"
    "- *Here is my plan for X. Argue with it.*\n\n"
    "Several models will answer separately, argue about it, and finish by "
    "naming the decisions you still have to make.\n\n"
    "*No models were used for this reply, so it did not count against your "
    "runs for today.*"
)
