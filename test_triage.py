"""Greetings must not start a run; real questions must always start one.

The asymmetry here is the whole design. Letting a greeting through wastes a few
seconds of a shared quota. Catching a real question means the product refused
to do the one thing it exists for -- so every ambiguous case must fall on the
side of running.

    python test_triage.py
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_triage.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

from engine import triage  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


# --- must be answered instantly, with no models ---------------------------
GREETINGS = [
    "hi", "hey", "hello", "Hello!", "HI", "  hey there  ",
    "yo", "yoooo", "yooooooo", "Yo!!!",          # the one a real user sent
    "sup", "whats up", "whats up??", "how are you?", "hru",
    "good morning", "gm", "hiii",
    "test", "  TEST  ", "testing", "ping", "123", "aaa", "asdf",
    "thanks", "thank you", "ok", "okayyyy", "cool", "lol", "bye",
    "salom", "privet",                            # languages this instance sees
]

# --- must ALWAYS run, however short or casual -----------------------------
REAL = [
    "Should I quit?",
    "What should I build next?",
    "Should I charge for this?",
    "hiring a designer",
    "yo should I use postgres or sqlite?",        # greeting + real question
    "hey, should I rewrite this or patch it?",
    "test my login flow for bugs",                # starts with a trigger word
    "ok so my plan is to ship friday, realistic?",
    "cool idea or dumb idea: a CLI for notes?",
    "Thanks for nothing, now fix my deploy",
    "Is my architecture wrong? Argue with it.",
    "I have two weeks. One project or two?",
]


def main():
    for g in GREETINGS:
        check(f"greeting caught: {g!r}", triage.is_small_talk(g))

    for q in REAL:
        check(f"real question runs: {q!r}", not triage.is_small_talk(q),
              "this would have been refused")

    # --- edges ------------------------------------------------------------
    check("empty string is not small talk", not triage.is_small_talk(""))
    check("None is handled", not triage.is_small_talk(None))

    # A long message is never small talk, whatever it opens with. The guard
    # exists so that padding a greeting cannot turn into a refusal.
    long_one = "hey " * 20
    check("a long message always runs", not triage.is_small_talk(long_one))

    # "hi" must not match inside a word.
    for w in ("hiring", "history", "think", "oklahoma", "testing a payment flow"):
        check(f"no substring match in {w!r}", not triage.is_small_talk(w))

    # The reply has to say the thing that stops the user feeling cheated.
    check("reply says no run was spent", "did not count" in triage.REPLY)
    check("reply shows examples", triage.REPLY.count("*") >= 6)

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
