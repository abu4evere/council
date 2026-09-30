"""Per-user limits, so one visitor cannot spend everything.

THE PROBLEM THIS SOLVES FIRST. The moment a public URL exists, the operator's
keys are spendable by strangers. Free tiers cap out around eighty full runs a
day across ALL users, and paid seats cost real money per run. Without a limit,
one enthusiastic tester on their first afternoon empties both.

TWO TIERS, one mechanism.

  * FREE users get a daily allowance of runs on free models only. The allowance
    resets each day and nothing is ever charged.
  * PAID users hold a credit balance in cents. A run that uses paid models is
    refused unless the balance covers its WORST CASE, and the actual cost is
    deducted afterwards.

THE RULE THAT MATTERS: reserve before spending, not after. Checking the balance
after a run has already cost money is an accounting entry, not a limit. So a
run is refused up front unless the balance covers the most it could possibly
cost, and the difference is returned when it turns out cheaper.

Costs are recorded from real usage where the provider reports it, and from a
conservative estimate otherwise. An estimate that is too HIGH is safe -- it
refuses a run the user could have afforded. Too low is not: it spends money
that was never there.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

# What a free account gets per day. Deliberately small: the operator's free
# tiers are shared by everyone, so this is a slice of a fixed pot, not a
# per-user entitlement.
FREE_RUNS_PER_DAY = int(os.environ.get("FREE_RUNS_PER_DAY", "5"))

# The most a single run may cost, in cents. Used for the up-front reservation
# and as a hard stop: no run may exceed this however the seats are configured.
MAX_RUN_COST_CENTS = int(os.environ.get("MAX_RUN_COST_CENTS", "25"))

# The most any ONE account may ever spend of the operator's money, in cents.
# Lifetime, not daily: a daily cap still lets one person spend it again every
# morning, which is not a cap, it is a rate. spent_cents already accumulates
# and is never reset, so this needs no new state.
#
# It bounds PAID spend only. With PREMIUM_SEATS off every run goes to free
# providers and costs nothing real, so in that configuration this never
# triggers -- it is the guard for the day paid seats get switched on, and for
# anyone who tops up credit.
USER_SPEND_CAP_CENTS = int(os.environ.get("USER_SPEND_CAP_CENTS", "100"))

# Worst-case cost per mode, in cents, from measured runs on claude-sonnet-5:
# Quick ~$0.02, Debate ~$0.03, Full ~$0.06. Rounded up, because a reservation
# that is too small is the failure that costs money.
WORST_CASE_CENTS = {"moa": 5, "debate": 8, "full": 12}


def today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def reservation_for(mode: str) -> int:
    return min(WORST_CASE_CENTS.get(mode, 12), MAX_RUN_COST_CENTS)


class Denied(Exception):
    """A run must not start. The message is shown to the user verbatim."""


def check(usage: dict, mode: str, uses_paid: bool) -> int:
    """Raise Denied, or return the cents to reserve.

    `usage` is the caller's row: {"runs_today", "credit_cents", "reserved_cents"}.
    """
    if not uses_paid:
        used = usage.get("runs_today", 0)
        if used >= FREE_RUNS_PER_DAY:
            raise Denied(
                f"You have used your {FREE_RUNS_PER_DAY} free runs for today. "
                "Add your own API keys under 'API keys' to keep going without "
                "a limit, or come back tomorrow."
            )
        return 0

    spent = usage.get("spent_cents", 0)
    if USER_SPEND_CAP_CENTS and spent >= USER_SPEND_CAP_CENTS:
        raise Denied(
            f"This account has reached its ${USER_SPEND_CAP_CENTS / 100:.2f} "
            "lifetime spending limit. Add your own API keys under 'API keys' "
            "to keep going."
        )

    need = reservation_for(mode)
    # Two ceilings, and the run has to clear both: the credit actually held,
    # and what is left of this account's lifetime allowance. Checking only the
    # balance would let a topped-up account walk straight past the cap.
    headroom = (USER_SPEND_CAP_CENTS - spent) if USER_SPEND_CAP_CENTS else need
    available = min(usage.get("credit_cents", 0) - usage.get("reserved_cents", 0),
                    headroom)
    if available < need:
        if USER_SPEND_CAP_CENTS and headroom < need:
            raise Denied(
                f"This run needs about {need}c and only {max(headroom, 0)}c "
                f"remains of this account's ${USER_SPEND_CAP_CENTS / 100:.2f} "
                "lifetime limit. Add your own API keys to keep going."
            )
        raise Denied(
            f"This run needs about {need}c of credit and you have "
            f"{max(available, 0)}c left. Top up, switch to a free mode, or add "
            "your own API keys."
        )
    return need


def estimate_cents(mode: str, chars_out: int) -> int:
    """A conservative cost estimate when the provider reports no usage.

    Rounded UP. Charging slightly too much is recoverable; charging too little
    spends money that was never reserved.
    """
    tokens = max(chars_out, 0) / 4
    # Sonnet-class output at $10/M tokens, with input assumed comparable.
    cents = (tokens / 1_000_000) * 10 * 100 * 2
    return min(MAX_RUN_COST_CENTS, max(1, int(cents + 0.999)))
