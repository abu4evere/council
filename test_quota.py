"""Spend limits: can one visitor drain the operator's account?

Every check here is an attempt to overspend. The failure that matters is not a
wrong number in a dashboard -- it is a stranger emptying a balance on their
first afternoon, which is the risk created the moment a public URL exists.

    python test_quota.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ["COUNCIL_PASSWORD"] = ""
os.environ["COUNCIL_SECRET"] = "test-secret"
os.environ["FREE_RUNS_PER_DAY"] = "3"
os.environ["GROQ_API_KEY"] = "server-key"

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_quota.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

import importlib  # noqa: E402

from engine import accounts, quota  # noqa: E402

importlib.reload(quota)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def main() -> int:
    db.init()
    day = quota.today()
    uid = db.create_user("visitor", accounts.hash_password("visitorpass1"))
    other = db.create_user("other", accounts.hash_password("otherpass123"))

    # --- the free tier is a hard stop ---
    for i in range(quota.FREE_RUNS_PER_DAY):
        quota.check(db.get_usage(uid, day), "moa", uses_paid=False)
        db.count_free_run(uid, day)
    try:
        quota.check(db.get_usage(uid, day), "moa", uses_paid=False)
        check("free allowance is a hard stop", False, "a fourth run was allowed")
    except quota.Denied as exc:
        check("free allowance is a hard stop", True)
        check("the refusal tells the user what to do",
              "API keys" in str(exc) or "tomorrow" in str(exc), str(exc))

    # --- one user's usage never affects another ---
    try:
        quota.check(db.get_usage(other, day), "moa", uses_paid=False)
        check("limits are per account", True)
    except quota.Denied:
        check("limits are per account", False, "the second user inherited the first's usage")

    # --- paid runs need credit up front ---
    try:
        quota.check(db.get_usage(uid, day), "full", uses_paid=True)
        check("PAID RUN REFUSED WITH NO CREDIT", False, "it was allowed")
    except quota.Denied:
        check("PAID RUN REFUSED WITH NO CREDIT", True)

    # --- reservation arithmetic ---
    db.add_credit(uid, 20)
    need = quota.check(db.get_usage(uid, day), "full", uses_paid=True)
    check("a full run reserves more than a quick one",
          need > quota.reservation_for("moa"), f"full={need}")
    check("reserve succeeds when covered", db.reserve(uid, day, need))
    u = db.get_usage(uid, day)
    check("credit is held, not yet spent",
          u["credit_cents"] == 20 and u["reserved_cents"] == need, str(u))

    # --- THE OVERSPEND TEST: concurrent runs must not both pass ---
    db.settle(uid, need, 0)                      # release the first
    db.add_credit(other, 12)
    first = db.reserve(other, day, 12)
    second = db.reserve(other, day, 12)          # nothing left to cover this
    check("A SECOND RUN CANNOT RESERVE CREDIT THAT IS ALREADY HELD",
          first and not second, f"first={first} second={second}")

    # --- settling charges the real cost and frees the hold ---
    db.settle(other, 12, 4)
    u = db.get_usage(other, day)
    check("settle charges actual, not the reservation", u["credit_cents"] == 8, str(u))
    check("settle releases the hold", u["reserved_cents"] == 0, str(u))
    check("spend is recorded", u["spent_cents"] == 4, str(u))

    # --- a crashed run must not strand its reservation ---
    db.add_credit(uid, 50)
    r = quota.reservation_for("full")
    db.reserve(uid, day, r)
    db.settle(uid, r, 0)                          # the `finally` path
    check("a failed run releases its reservation",
          db.get_usage(uid, day)["reserved_cents"] == 0)

    # --- balance can never go negative ---
    db.add_credit(other, 0)
    db.settle(other, 0, 999)
    check("balance floors at zero rather than going negative",
          db.get_usage(other, day)["credit_cents"] == 0)

    # --- estimates round up, and are capped ---
    check("estimate rounds up rather than down", quota.estimate_cents("full", 400) >= 1)
    check("estimate is capped so one run cannot cost everything",
          quota.estimate_cents("full", 50_000_000) <= quota.MAX_RUN_COST_CENTS)
    check("a bigger answer estimates higher",
          quota.estimate_cents("full", 40000) > quota.estimate_cents("full", 4000))

    # --- the daily allowance resets, credit does not ---
    db.get_usage(uid, "1999-01-01")               # force a day rollover
    rolled = db.get_usage(uid, day)
    check("a new day resets the free allowance", rolled["runs_today"] == 0)
    check("a new day does NOT reset paid credit", rolled["credit_cents"] > 0, str(rolled))

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    code = main()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db.DB_PATH) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
    sys.exit(code)
