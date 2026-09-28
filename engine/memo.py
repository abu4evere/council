"""Render a finished run as a decision memo.

WHY. A chat log is not a record. Three weeks after deciding something you want
to know what you decided, what the alternatives were, who argued against it,
and what you knew at the time -- and scrolling a transcript gives you none of
that quickly.

A memo is a file: diffable, greppable, readable without running anything, and
attachable to an email. It turns "I asked some models" into "here is the
decision and the reasoning behind it".

THE LOOP THIS CLOSES. Written into the user's vault, a memo becomes searchable
memory for later runs. Decide something in March, ask a related question in
June, and the March reasoning is retrieved automatically. The tool stops being
a question-answerer and starts being a decision journal that remembers.

Everything here is assembled from what a run already produced. Nothing calls a
model, so a memo costs no tokens and cannot fail on a rate limit.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:limit].rstrip("-")) or "decision"


def filename_for(turn: dict) -> str:
    when = datetime.fromtimestamp(turn.get("created_at") or 0, tz=timezone.utc)
    return f"{when:%Y-%m-%d}-{_slug(turn.get('user_prompt', ''))}.md"


def _headline(answer: str) -> str:
    """The first real sentence of the answer, for the summary line.

    Markdown headings, block quotes and list markers are skipped: the opening
    of a synthesis is often "## The recommendation", which says nothing.
    """
    for raw in (answer or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "|", "`", "-", "*")):
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        return line[:300]
    return ""


def build(turn: dict, events: list[dict]) -> str:
    """Assemble the memo. `events` is the stored event log for this turn."""
    seats, order, failures, skipped = {}, [], [], []
    points, decisions, notes = [], [], []

    for e in events:
        etype, agent = e.get("type"), e.get("agent")
        if etype == "agent_start":
            if agent not in seats:
                order.append(agent)
            seats[agent] = {"label": e.get("label", agent), "model": e.get("model", ""),
                            "provider": e.get("provider", ""), "role": e.get("role", ""),
                            "chars": 0, "ok": False}
        elif etype == "agent_done" and agent in seats:
            seats[agent]["ok"] = True
            seats[agent]["chars"] = len(e.get("text") or "")
        elif etype == "agent_error" and agent in seats:
            failures.append((seats[agent]["label"], str(e.get("error", ""))[:160]))
        elif etype == "agent_skipped":
            skipped.append(e.get("label", agent))
        elif etype == "disagreement":
            points = e.get("points") or []
        elif etype == "decisions":
            decisions = e.get("decisions") or []
        elif etype == "vault":
            notes = e.get("notes") or []

    created = datetime.fromtimestamp(turn.get("created_at") or 0, tz=timezone.utc)
    answer = (turn.get("final_answer") or "").strip()
    mode = {"moa": "Quick", "debate": "Debate", "full": "Full"}.get(turn.get("mode"), turn.get("mode"))

    out = [
        f"# {(turn.get('user_prompt') or 'Decision').strip().splitlines()[0][:110]}",
        "",
        f"*{created:%d %B %Y}* · {mode} · "
        f"{sum(1 for s in seats.values() if s['ok'])} of {len(seats)} seats answered",
        "",
    ]

    headline = _headline(answer)
    if headline:
        out += ["> **Decision.** " + headline, ""]

    out += ["---", "", "## The question", "",
            "> " + (turn.get("user_prompt") or "").strip().replace("\n", "\n> "), ""]

    if decisions:
        # First, not last. A memo read in three weeks is read for what is still
        # undecided, not for the prose.
        out += ["## Still to decide", ""]
        for i, d in enumerate(decisions, 1):
            line = f"{i}. **{d.get('question', '').strip()}**"
            if d.get("why"):
                line += f"  \n   {d['why'].strip()}"
            out.append(line)
        out.append("")

    if points:
        out += ["## Where the models disagreed", ""]
        for p in points:
            out.append(f"**{p.get('point', '').strip()}**")
            out.append("")
            for side in p.get("sides", []):
                seat = side.get("seat") or "One model"
                out.append(f"- *{seat}*: {side.get('says', '').strip()}")
            if p.get("resolution"):
                out += ["", f"→ {p['resolution'].strip()}"]
            out.append("")

    if answer:
        out += ["## The answer", "", answer, ""]

    out += ["---", "", "## How this was produced", ""]
    for key in order:
        s = seats[key]
        status = "ok" if s["ok"] else "failed"
        size = f", {s['chars']:,} chars" if s["chars"] else ""
        out.append(f"- **{s['label']}** — `{s['provider']}/{s['model']}` ({status}{size})")
    if skipped:
        out.append(f"- Skipped, no key configured: {', '.join(skipped)}")
    out.append("")

    if failures:
        # Kept deliberately. A memo that hides which models failed overstates
        # how much agreement is behind its conclusion.
        out += ["**Failures during this run**", ""]
        for label, err in failures:
            out.append(f"- {label}: `{err}`")
        out.append("")

    if notes:
        out += ["**Notes recalled from the vault**", ""]
        for n in notes:
            head = f" → {n['heading']}" if n.get("heading") else ""
            out.append(f"- `{n.get('path', '')}`{head}")
        out.append("")

    out += ["*Generated by [Council AI](https://github.com/abu4evere/council). "
            "No model was called to write this memo.*"]
    return "\n".join(out)
