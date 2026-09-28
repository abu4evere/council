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
]}
```

That is all.'''


def main() -> int:
    # --- the happy path ---
    answer, points = extract(GOOD)
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
        ans, pts = extract(text)
        ok = len(pts) == want and "```json" not in ans
        check(f"handles: {name}", ok, f"got {len(pts)} points, want {want}")

    # --- the rule that matters most ---
    broken = '```json council-disagreements\n{{{ totally broken\n```\nThe real answer text.'
    ans, pts = extract(broken)
    check("A BROKEN BLOCK NEVER COSTS THE ANSWER", "The real answer text." in ans, ans[:80])
    check("broken fence is still stripped from view", "```json" not in ans)

    check("empty input is safe", extract("") == ("", []))
    check("None-ish input is safe", extract(None)[1] == [])

    # --- bounds ---
    many = '{"disagreements":[' + ",".join(
        f'{{"point":"P{i}"}}' for i in range(20)) + ']}'
    _, pts = extract(f"```json council-disagreements\n{many}\n```")
    check("caps runaway lists", len(pts) <= 6, f"{len(pts)} points")

    long_point = '{"disagreements":[{"point":"' + "x" * 2000 + '"}]}'
    _, pts = extract(f"```json council-disagreements\n{long_point}\n```")
    check("truncates absurdly long text", len(pts[0].point) <= 400, str(len(pts[0].point)))

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
