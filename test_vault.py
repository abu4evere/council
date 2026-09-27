"""Vault memory: indexing, exclusions, retrieval and the token budget.

The checks that matter most are the negative ones -- private folders staying
out of the index, and the budget holding -- because a leak or a blown budget is
silent. Retrieval being slightly loose is recoverable; sending a diary entry to
four AI providers is not.

    python test_vault.py
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from engine import vault

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def build_fixture(root: Path) -> None:
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / ".private").mkdir(exist_ok=True)
    (root / ".obsidian").mkdir(exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)

    (root / "projects" / "council.md").write_text(
        "# Council\n\n## Hard constraints\n"
        "The laptop has 7.6GB of RAM and integrated graphics, so local models "
        "through Ollama are impossible. Groq allows 8000 tokens per minute.\n\n"
        "## Stack\nFastAPI, SQLite and vanilla JavaScript with no build step.\n",
        encoding="utf-8")
    (root / "projects" / "habits.md").write_text(
        "# Habits\n\n## The plan\n"
        "A habit tracker using localStorage and vanilla JavaScript, deployed to "
        "Netlify with no backend.\n", encoding="utf-8")
    (root / ".private" / "diary.md").write_text(
        "# Diary\n\n## Secret\nThis must never be indexed or sent anywhere. "
        "It mentions Ollama and localStorage to bait keyword retrieval.\n",
        encoding="utf-8")
    (root / ".obsidian" / "workspace.md").write_text(
        "# Config\n\n## Settings\nInternal Obsidian settings about Ollama.\n",
        encoding="utf-8")
    (root / "templates" / "note.md").write_text(
        "# Template\n\n## Body\nBoilerplate about localStorage and Groq.\n",
        encoding="utf-8")
    (root / "tiny.md").write_text("# Tiny\n\n## Stub\nshort\n", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="council-vault-"))
    try:
        build_fixture(tmp)
        notes = vault.scan(tmp)
        paths = {n.path for n in notes}

        check("indexes ordinary notes",
              "projects/council.md" in paths and "projects/habits.md" in paths, str(paths))
        check("EXCLUDES .private", not any(".private" in p for p in paths), str(paths))
        check("EXCLUDES .obsidian", not any(".obsidian" in p for p in paths), str(paths))
        check("EXCLUDES templates", not any("templates" in p for p in paths), str(paths))
        check("skips sections below the minimum length",
              not any(n.heading == "Stub" for n in notes))

        headings = {n.heading for n in notes}
        check("splits notes at headings",
              {"Hard constraints", "Stack", "The plan"} <= headings, str(headings))

        conn = sqlite3.connect(":memory:")
        n = vault.build_index(conn, tmp)
        check("index built", n == len(notes), f"{n} vs {len(notes)}")

        hits = vault.search(conn, "can I run models locally on this laptop?")
        check("retrieves the relevant section",
              any(h.heading == "Hard constraints" for h in hits),
              str([(h.path, h.heading) for h in hits]))
        check("private content never retrieved even on a keyword match",
              all(".private" not in h.path for h in hits))

        hits = vault.search(conn, "habit tracker storage choice")
        check("retrieves a different note for a different question",
              any(h.path == "projects/habits.md" for h in hits),
              str([(h.path, h.heading) for h in hits]))

        check("unrelated question retrieves nothing",
              vault.search(conn, "zzzz qqqq xxxx") == [])

        # Budget: a vault full of long matching notes must still fit.
        big = tmp / "big"
        big.mkdir()
        for i in range(30):
            (big / f"n{i}.md").write_text(
                f"# Note {i}\n\n## Groq limits\n" + ("groq tokens minute limit " * 200),
                encoding="utf-8")
        conn2 = sqlite3.connect(":memory:")
        vault.build_index(conn2, tmp)
        hits = vault.search(conn2, "groq tokens per minute limit", budget_chars=3000)
        total = sum(len(h.body) for h in hits)
        check("retrieval respects the character budget", total <= 3000, f"{total} chars")
        check("budget still returns something useful", len(hits) > 0)

        ctx = vault.as_context(hits)
        check("context labels notes as background, not instructions",
              "not instructions" in ctx)
        check("context names its sources", "---" in ctx and hits[0].path in ctx)

        passed, total_n = sum(results), len(results)
        print(f"\n{passed}/{total_n} checks passed")
        return 0 if passed == total_n else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
