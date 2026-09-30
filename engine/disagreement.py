"""Pull the structured disagreement out of a synthesis answer.

WHY. When five models answer the same question, the place they DISAGREE is the
most valuable thing the run produces -- it is where the easy consensus answer
would have been wrong. Until now that lived as prose inside the merged markdown
and the interface did nothing with it, so the product's best output was also
its least visible.

HOW, AND WHY NOT THE OBVIOUS WAY. The obvious approach is a second call asking
a model to extract the disagreements. That costs another request on a tier that
allows 8000 tokens a minute, and the synthesis already knows what it resolved.
So the synthesis emits a small JSON block alongside its prose, and this module
lifts it out and removes it from the text the reader sees.

THE RULE THIS MODULE OBEYS: a parsing failure must never damage the answer.
Models emit malformed JSON, wrap it in stray prose, use single quotes, or skip
it entirely. Every one of those degrades to "no panel, full answer intact" --
never to a crash or a truncated answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# The synthesis is asked to emit exactly this fence.
BLOCK = re.compile(
    r"```json\s+council-disagreements\s*(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Fallback: a plain ```json fence that contains either expected key. Models
# drop the label often enough that refusing to look costs real data, and a
# block holding only "decisions" is exactly the one that used to be lost.
LOOSE = re.compile(
    r"```json\s*(\{.*?\"(?:disagreements|decisions)\".*?\})\s*```", re.DOTALL)

# A section heading with nothing underneath it. The model puts the content in
# the data block and leaves the heading bare, so the reader gets the word
# "DISAGREEMENTS" followed by white space -- which reads as a broken feature
# rather than as "they agreed".
_EMPTY_SECTION = re.compile(
    r"(?im)^[ \t]*(?:\#{1,6}[ \t]*|\*\*[ \t]*)"
    r"(?:DISAGREEMENTS|OPEN QUESTIONS|DECISIONS|UNKNOWNS)"
    r"[ \t]*\*{0,2}[ \t]*:?[ \t]*$"
    r"[\s\-*_]*?"
    r"(?=^[ \t]*\#{1,6}[ \t]|^[ \t]*\*\*\S|\Z)")


def _drop_empty_sections(text: str) -> str:
    """Remove headings left with no body.

    Only the four headings this tool asks for by name, so a heading the model
    invented and genuinely left empty is not silently eaten.
    """
    cleaned = _EMPTY_SECTION.sub("", text)
    # Collapse the run of blank lines and rules the removal can leave behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]*-{3,}[ \t]*\n(?=\s*(?:-{3,}|\Z))", "", cleaned)
    return cleaned.strip()


def _all_blocks(answer: str) -> list:
    """Every data block in the answer, in order, labelled or not.

    Reading only the first match was the whole bug: models routinely split the
    material across two fences, and the decisions were always in the second
    one. That block was neither parsed nor stripped, so its JSON leaked into
    the prose while the panel it was meant to fill stayed empty.
    """
    found = list(BLOCK.finditer(answer))
    spans = [(m.start(), m.end()) for m in found]
    for m in LOOSE.finditer(answer):
        if not any(start <= m.start() < end for start, end in spans):
            found.append(m)
    return sorted(found, key=lambda m: m.start())


@dataclass
class Side:
    seat: str
    says: str


@dataclass
class Decision:
    """A question the user still has to answer, and what hangs on it."""
    question: str
    why: str = ""

    def as_dict(self) -> dict:
        return {"question": self.question, "why": self.why}


@dataclass
class Point:
    point: str
    sides: list[Side] = field(default_factory=list)
    resolution: str = ""

    def as_dict(self) -> dict:
        return {
            "point": self.point,
            "sides": [{"seat": s.seat, "says": s.says} for s in self.sides],
            "resolution": self.resolution,
        }


def extract(answer: str) -> tuple[str, list[Point], list[Decision]]:
    """Return (answer without the block, disagreements, decisions).

    The answer always comes back usable, whatever the model emitted.
    """
    if not answer:
        return answer, [], []

    matches = _all_blocks(answer)
    if not matches:
        return _drop_empty_sections(answer), [], []

    # Strip from the end backwards so the earlier spans stay valid. The fence
    # is noise to a reader whether or not its contents parse, so it goes
    # either way.
    cleaned = answer
    for m in reversed(matches):
        cleaned = cleaned[:m.start()] + cleaned[m.end():]

    points: list[Point] = []
    decisions: list[Decision] = []
    for m in matches:
        data = _load(m.group(1))
        points.extend(_parse_points(data))
        decisions.extend(_parse_decisions(data))

    return _drop_empty_sections(cleaned), points[:6], decisions[:6]


def _parse_decisions(data) -> list[Decision]:
    """The questions the user still has to answer.

    Deliberately separate from the disagreements: a disagreement is about what
    the models thought, a decision is about what the READER must now do. Those
    are different kinds of thing and conflating them buries the second.
    """
    if not isinstance(data, dict):
        return []
    items = data.get("decisions") or data.get("open_questions") or []
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:6]:
        if isinstance(item, str) and item.strip():
            out.append(Decision(question=item.strip()[:300]))
        elif isinstance(item, dict):
            q = _text(item, "question", "decision", "q", "ask")
            if q:
                out.append(Decision(
                    question=q,
                    why=_text(item, "why", "depends", "impact", "because", "matters"),
                ))
    return out


def _parse_points(data) -> list[Point]:
    if data is None:
        return []

    if isinstance(data, dict):
        items = data.get("disagreements") or data.get("points") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []

    out: list[Point] = []
    for item in items[:6]:          # a run that "disagrees" six ways is noise
        if not isinstance(item, dict):
            continue
        text = _text(item, "point", "claim", "issue", "topic")
        if not text:
            continue
        sides = []
        raw_sides = item.get("sides") or item.get("positions") or []
        if isinstance(raw_sides, list):
            for s in raw_sides[:5]:
                if isinstance(s, dict):
                    seat = _text(s, "seat", "model", "advisor", "name")
                    says = _text(s, "says", "position", "stance", "argument", "view")
                    if seat or says:
                        sides.append(Side(seat=seat or "One model", says=says))
                elif isinstance(s, str) and s.strip():
                    sides.append(Side(seat="", says=s.strip()))
        out.append(Point(
            point=text,
            sides=sides,
            resolution=_text(item, "resolution", "resolved", "verdict", "chose"),
        ))
    return out


def _load(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Trailing commas and single quotes are the two things models get wrong
    # most often. Both are cheap to forgive.
    repaired = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # Last resort: the outermost object or array in the blob.
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = repaired.find(opener), repaired.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(repaired[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def _text(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:400]
    return ""
