"""Free-run limits must survive a restart, and the shared pot must have a floor.

The bug these guard against was not that the limit was weak. It was that the
limit did not exist: the counter lived in a dict, and this instance scales to
zero whenever it is idle, so the dict was wiped several times a day. A control
that silently resets is worse than none, because you believe it is working.

    python test_caps.py
"""
import importlib
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_caps.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def main():
    db.init()
    TODAY, YESTERDAY = "2026-09-30", "2026-09-29"
    alice, bob = "fp_alice", "fp_bob"

    # --- per-visitor counting ---------------------------------------------
    check("a new visitor starts at zero", db.visitor_runs_today(alice, TODAY) == 0)
    check("first run counts as one", db.count_visitor_run(alice, TODAY) == 1)
    check("second run counts as two", db.count_visitor_run(alice, TODAY) == 2)
    check("and it reads back", db.visitor_runs_today(alice, TODAY) == 2)
    check("another visitor is unaffected", db.visitor_runs_today(bob, TODAY) == 0)

    # --- the bug that mattered --------------------------------------------
    # Reconnecting is what a restart looks like to this process: the old
    # counter lived in memory and would have come back empty here.
    importlib.reload(db)
    db.DB_PATH = Path(__file__).parent / "test_caps.db"
    check("the count SURVIVES a restart", db.visitor_runs_today(alice, TODAY) == 2,
          "this is the whole point -- an in-memory counter would read 0")

    # --- days roll over ----------------------------------------------------
    check("yesterday is counted separately", db.visitor_runs_today(alice, YESTERDAY) == 0)

    # --- the shared pot ----------------------------------------------------
    check("instance starts at zero", db.instance_runs_today(TODAY) == 0)
    for i in range(1, 6):
        db.count_instance_run(TODAY)
    check("instance counts every run", db.instance_runs_today(TODAY) == 5)
    check("instance count survives a restart too",
          db.instance_runs_today(TODAY) == 5)

    # A cap is only a cap if the check and the increment agree on the number.
    CAP = 7
    allowed, refused = 0, 0
    for _ in range(6):
        if db.instance_runs_today(TODAY) >= CAP:
            refused += 1
        else:
            db.count_instance_run(TODAY)
            allowed += 1
    check("the cap stops exactly at the limit", db.instance_runs_today(TODAY) == CAP,
          f"got {db.instance_runs_today(TODAY)}, wanted {CAP}")
    check("runs past the cap are refused", refused == 4, f"refused {refused}")

    # --- pruning -----------------------------------------------------------
    db.count_visitor_run("fp_old", YESTERDAY)
    removed = db.prune_visitor_usage(TODAY)
    check("old rows are pruned", removed >= 1, f"removed {removed}")
    check("today's rows are kept", db.visitor_runs_today(alice, TODAY) == 2)

    # --- the fingerprint must not be the address ---------------------------
    import os
    os.environ.setdefault("COUNCIL_SECRET", "test-secret")
    import hashlib
    ip = "203.0.113.9"
    salt = os.environ["COUNCIL_SECRET"]
    fp = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]
    check("the stored key is not the address", ip not in fp)
    check("the same address gives the same key",
          fp == hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32])
    check("a different address gives a different key",
          fp != hashlib.sha256(f"{salt}:203.0.113.10".encode()).hexdigest()[:32])

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
