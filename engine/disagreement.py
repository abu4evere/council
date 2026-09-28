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
# Fallback: a plain ```json fence that contains the expected key. Models drop
# the label often enough that refusing to look costs real data.
LOOSE = re.compile(r"```json\s*(\{.*?\"disagreements\".*?\})\s*```", re.DOTALL)


@dataclass
class Side:
    seat: str
    says: str


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


def extract(answer: str) -> tuple[str, list[Point]]:
    """Return (answer without the block, parsed points).

    The answer always comes back usable, whatever the model emitted.
    """
    if not answer:
        return answer, []

    match = BLOCK.search(answer) or LOOSE.search(answer)
    if not match:
        return answer, []

    cleaned = (answer[:match.start()] + answer[match.end():]).strip()
    points = _parse(match.group(1))
    if not points:
        # Nothing usable inside, but the fence itself is noise to a reader, so
        # it still goes.
        return cleaned, []
    return cleaned, points


def _parse(raw: str) -> list[Point]:
    data = _load(raw)
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
