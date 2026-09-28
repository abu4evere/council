"""The decision memo: does it record what actually happened?

The failure that matters is a memo that reads better than the run deserved --
one that hides which models failed, or omits the open decisions, and so
overstates how much agreement sits behind its conclusion. Several checks here
exist only to catch that.

    python test_memo.py
"""
import sys
import time

from engine import memo

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


TURN = {
    "id": "abc123",
    "user_prompt": "Should I build one large project or five small ones?",
    "mode": "full",
    "status": "done",
    "created_at": time.mktime((2026, 9, 27, 14, 30, 0, 0, 0, -1)),
    "final_answer": (
        "## The recommendation\n\n"
        "Build one project that real people use, not five that nobody does.\n\n"
        "### Why\nDepth is legible to a reviewer; breadth is not.\n"
    ),
}

EVENTS = [
    {"type": "vault", "notes": [{"path": "projects/council.md", "heading": "Constraints"}]},
    {"type": "agent_start", "agent": "prag", "label": "Pragmatist",
     "model": "gpt-oss-120b", "provider": "groq", "role": "proposer"},
    {"type": "agent_done", "agent": "prag", "text": "x" * 2100},
    {"type": "agent_start", "agent": "con", "label": "Contrarian",
     "model": "qwen-3.8-27b", "provider": "cerebras", "role": "proposer"},
    {"type": "agent_done", "agent": "con", "text": "y" * 1800},
    {"type": "agent_start", "agent": "skep", "label": "Skeptic",
     "model": "kimi-k3", "provider": "nvidia", "role": "proposer"},
    {"type": "agent_error", "agent": "skep", "error": "rate limited (429) quota exceeded"},
    {"type": "agent_skipped", "agent": "op", "label": "Operator"},
    {"type": "disagreement", "points": [{
        "point": "Depth versus breadth",
        "sides": [{"seat": "Pragmatist", "says": "one project, finished"},
                  {"seat": "Contrarian", "says": "five proves range"}],
        "resolution": "One, because an unfinished project proves nothing."}]},
    {"type": "decisions", "decisions": [
        {"question": "Who is the audience for this portfolio?",
         "why": "it decides whether depth or range matters more"},
        {"question": "When do you start applying?"}]},
]


def main() -> int:
    text = memo.build(TURN, EVENTS)

    check("has a title from the question", text.startswith("# Should I build one large"))
    check("dates the decision", "27 September 2026" in text, text[:200])
    check("records the mode", "Full" in text.split("\n")[2])
    check("counts seats that answered", "2 of 3 seats answered" in text, text.split("\n")[2])

    # The summary line must skip markdown chrome, not quote a heading.
    check("summary line skips the heading",
          "**Decision.** Build one project that real people use" in text,
          [l for l in text.splitlines() if "Decision." in l][:1])

    check("includes the question", "Should I build one large project" in text)
    check("includes the full answer", "Depth is legible to a reviewer" in text)

    # The parts a memo exists for.
    check("open decisions appear", "Who is the audience" in text)
    check("decisions come BEFORE the prose answer",
          text.index("Still to decide") < text.index("## The answer"))
    check("decision rationale kept", "depth or range matters more" in text)
    check("a decision without a rationale still renders", "When do you start applying?" in text)

    check("disagreement recorded", "Depth versus breadth" in text)
    check("both sides named", "Pragmatist" in text and "five proves range" in text)
    check("resolution recorded", "an unfinished project proves nothing" in text)

    # Honesty about the run.
    check("NAMES THE MODEL THAT FAILED", "Skeptic" in text and "rate limited" in text,
          "a memo hiding failures overstates its own confidence")
    check("records skipped seats", "Operator" in text)
    check("names providers and models", "groq/gpt-oss-120b" in text)
    check("records which notes were recalled", "projects/council.md" in text)
    check("states that no model wrote the memo", "No model was called" in text)

    # Filename: sortable, safe, descriptive.
    name = memo.filename_for(TURN)
    check("filename is date-prefixed", name.startswith("2026-09-27-"), name)
    check("filename is slugged", name.endswith(".md") and " " not in name, name)
    check("filename has no unsafe characters",
          all(c.isalnum() or c in "-._" for c in name), name)

    # Degenerate inputs must not raise.
    empty = memo.build({"user_prompt": "", "mode": "moa", "created_at": 0,
                        "final_answer": ""}, [])
    check("empty run still produces a memo", isinstance(empty, str) and len(empty) > 40)
    check("empty run claims no seats answered", "0 of 0 seats answered" in empty, empty[:160])
    check("filename falls back when the question is empty",
          memo.filename_for({"user_prompt": "", "created_at": 0}).endswith("decision.md"))

    weird = memo.build({"user_prompt": "What about C++/C#?  ", "mode": "moa",
                        "created_at": 0, "final_answer": "Use Rust."}, [])
    check("punctuation in the question is handled", "C++/C#" in weird)
    check("filename strips punctuation",
          "+" not in memo.filename_for({"user_prompt": "What about C++/C#?", "created_at": 0}))

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
