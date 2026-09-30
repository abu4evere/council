"""Parsing the disagreement block out of a synthesis answer.

Every case here is a way a model has actually mangled structured output:
missing labels, trailing commas, single quotes, prose wrapped around the fence,
the block absent entirely. The rule under test is that ALL of them leave the
answer readable -- a parse failure must cost the panel, never the answer.

    python test_disagreement.py
"""
import sys

from engine.disagreement import extract

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


GOOD = '''Here is the merged answer.

## The recommendation
Ship the small thing first.

```json council-disagreements
{"disagreements": [
  {"point": "Whether to use a backend at all",
   "sides": [{"seat": "Pragmatist", "says": "localStorage is enough for v1"},
             {"seat": "Systems Thinker", "says": "you will need sync within a month"}],
   "resolution": "Start without one; the migration is cheap if it stays a thin layer."}
],
 "decisions": [
  {"question": "Do you need this to work offline?",
   "why": "it decides whether a backend is required at all"}
]}
```

That is all.'''


def main() -> int:
    # --- the happy path ---
    answer, points, _ = extract(GOOD)
    check("parses a well-formed block", len(points) == 1, str(points))
    check("block is removed from the answer", "council-disagreements" not in answer)
    check("prose before AND after the block survives",
          "Ship the small thing first." in answer and "That is all." in answer)
    p = points[0]
    check("point text captured", p.point.startswith("Whether to use a backend"))
    check("both sides captured", len(p.sides) == 2, str(p.sides))
    check("seat names captured",
          {s.seat for s in p.sides} == {"Pragmatist", "Systems Thinker"})
    check("resolution captured", "migration is cheap" in p.resolution)

    # --- the ways models mangle it ---
    mangled = [
        ("no block at all", "Just a normal answer with no JSON anywhere.", 0),
        ("unlabelled json fence",
         'Text\n```json\n{"disagreements":[{"point":"A","sides":[]}]}\n```\nmore', 1),
        ("trailing comma",
         '```json council-disagreements\n{"disagreements":[{"point":"A",}],}\n```', 1),
        ("bare array instead of an object",
         '```json council-disagreements\n[{"point":"A","sides":[]}]\n```', 1),
        ("prose wrapped inside the fence",
         '```json council-disagreements\nHere you go:\n{"disagreements":[{"point":"A"}]}\n```', 1),
        ("alternative key names",
         '```json council-disagreements\n{"points":[{"claim":"A","positions":[{"model":"X","stance":"Y"}]}]}\n```', 1),
        ("sides given as plain strings",
         '```json council-disagreements\n{"disagreements":[{"point":"A","sides":["X thinks one thing"]}]}\n```', 1),
        ("total garbage inside the fence",
         '```json council-disagreements\nnot json at all {{{ \n```', 0),
        ("empty list", '```json council-disagreements\n{"disagreements":[]}\n```', 0),
        ("entries missing the point text",
         '```json council-disagreements\n{"disagreements":[{"sides":[]}]}\n```', 0),
    ]
    for name, text, want in mangled:
        ans, pts, _ = extract(text)
        ok = len(pts) == want and "```json" not in ans
        check(f"handles: {name}", ok, f"got {len(pts)} points, want {want}")

    # --- the rule that matters most ---
    broken = '```json council-disagreements\n{{{ totally broken\n```\nThe real answer text.'
    ans, pts, _ = extract(broken)
    check("A BROKEN BLOCK NEVER COSTS THE ANSWER", "The real answer text." in ans, ans[:80])
    check("broken fence is still stripped from view", "```json" not in ans)

    check("empty input is safe", extract("") == ("", [], []))
    check("None-ish input is safe", extract(None)[1] == [])

    # --- bounds ---
    many = '{"disagreements":[' + ",".join(
        f'{{"point":"P{i}"}}' for i in range(20)) + ']}'
    _, pts, _ = extract(f"```json council-disagreements\n{many}\n```")
    check("caps runaway lists", len(pts) <= 6, f"{len(pts)} points")

    long_point = '{"disagreements":[{"point":"' + "x" * 2000 + '"}]}'
    _, pts, _ = extract(f"```json council-disagreements\n{long_point}\n```")
    check("truncates absurdly long text", len(pts[0].point) <= 400, str(len(pts[0].point)))

    # --- decisions: the payoff section ---
    both = ('```json council-disagreements\n'
            '{"disagreements":[{"point":"A","sides":[]}],'
            ' "decisions":[{"question":"Do you need offline support?",'
            '"why":"it decides whether you need a backend"},'
            '{"question":"Who is this for?"}]}\n```')
    ans, pts, decs = extract(both)
    check("parses decisions alongside disagreements", len(decs) == 2, str(decs))
    check("decision question captured", decs[0].question.startswith("Do you need offline"))
    check("decision rationale captured", "backend" in decs[0].why)
    check("a decision without a rationale still parses", decs[1].question == "Who is this for?")

    _, _, plain = extract('```json council-disagreements\n{"decisions":["Just a string question"]}\n```')
    check("decisions given as plain strings", len(plain) == 1 and plain[0].question.startswith("Just a"))

    _, _, alt = extract('```json council-disagreements\n{"open_questions":[{"question":"X"}]}\n```')
    check("alternative key name for decisions", len(alt) == 1)

    _, _, none = extract('```json council-disagreements\n{"disagreements":[]}\n```')
    check("no decisions key is safe", none == [])

    # --- two blocks in one answer -----------------------------------------
    # This is what actually happened in production: the model split the
    # material across two fences. extract() read only the first, so the
    # decisions were never parsed AND their JSON was left sitting in the
    # prose. The decisions panel fired zero times in twenty runs because of it.
    two = (
        "Here is the answer.\n\n"
        "### DISAGREEMENTS\n\n"
        '```json council-disagreements\n'
        '{"disagreements":[{"point":"Ship now or wait",'
        '"sides":[{"seat":"Pragmatist","says":"Ship"},{"seat":"Skeptic","says":"Wait"}],'
        '"resolution":"Ship; waiting costs more"}]}\n'
        "```\n\n"
        "### OPEN QUESTIONS\n\n"
        '```json council-disagreements\n'
        '{"decisions":[{"question":"Do you need offline?","why":"Decides the backend"},'
        '{"question":"Who pays?","why":"Decides pricing"}]}\n'
        "```\n")
    cleaned, pts, decs = extract(two)
    check("two blocks: disagreement from the first is kept", len(pts) == 1)
    check("two blocks: decisions from the SECOND are parsed", len(decs) == 2,
          f"got {len(decs)}")
    check("two blocks: no JSON left in the prose", "```json" not in cleaned)
    check("two blocks: prose survives", "Here is the answer." in cleaned)

    # An unlabelled block holding only decisions. The old fallback pattern
    # required the word "disagreements", so this was silently dropped.
    unlabelled = ('Answer text.\n\n```json\n'
                  '{"decisions":[{"question":"Which host?","why":"Cost"}]}\n```')
    cleaned, _, decs = extract(unlabelled)
    check("unlabelled decisions-only block is recovered", len(decs) == 1,
          f"got {len(decs)}")
    check("unlabelled block is stripped", "```json" not in cleaned)

    # --- empty prose headings ----------------------------------------------
    # The model moves the content into the data block and leaves the heading
    # bare. A lone word "DISAGREEMENTS" sitting over white space reads as a
    # broken feature rather than as "they agreed", so it comes out.
    bare = ("Recommendation here.\n\n"
            "### DISAGREEMENTS\n\n---\n\n"
            "### OPEN QUESTIONS\n\n"
            "- Something real\n")
    cleaned, _, _ = extract(bare)
    check("an empty DISAGREEMENTS heading is removed",
          "DISAGREEMENTS" not in cleaned, repr(cleaned))
    check("a heading WITH content is kept",
          "OPEN QUESTIONS" in cleaned and "Something real" in cleaned, repr(cleaned))

    bold = "Answer.\n\n**DISAGREEMENTS**\n\n**OPEN QUESTIONS**\n\n- Real item\n"
    cleaned, _, _ = extract(bold)
    check("bold-style empty heading is removed too",
          "DISAGREEMENTS" not in cleaned and "Real item" in cleaned, repr(cleaned))

    kept = "Answer.\n\n### DISAGREEMENTS\n\nThey split on pricing.\n"
    cleaned, _, _ = extract(kept)
    check("a DISAGREEMENTS section with prose is left alone",
          "They split on pricing." in cleaned and "DISAGREEMENTS" in cleaned)

    # A heading the model invented must not be eaten by the same rule.
    invented = "Answer.\n\n### RANDOM SECTION\n\n### OPEN QUESTIONS\n\n- x\n"
    cleaned, _, _ = extract(invented)
    check("an unrelated empty heading is left alone", "RANDOM SECTION" in cleaned)

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
