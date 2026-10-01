"""The webhook that grants credit must refuse everything it cannot prove.

This endpoint adds money to accounts and is reachable by anyone, so the tests
that matter are the refusals: a forged signature, a replayed payment, a refund
dressed as a sale, an order that names no account.

    python test_payments.py
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SECRET = "test-webhook-secret-not-the-real-one"
os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = SECRET
os.environ["LEMONSQUEEZY_CHECKOUT_URL"] = "https://example.lemonsqueezy.com/buy/abc"
os.environ["CREDIT_CENTS_PER_DOLLAR"] = "100"
os.environ["COUNCIL_PASSWORD"] = ""
os.environ["GUEST_RUNS"] = "50"

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_payments.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

import httpx  # noqa: E402
import server  # noqa: E402
from engine import accounts, payments  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def body(event="order_created", status="paid", total=800, user_id="u1",
         order_id="ord_1", refunded=False):
    payload = {
        "meta": {"event_name": event, "custom_data": {"user_id": user_id}},
        "data": {"id": order_id,
                 "attributes": {"status": status, "total": total, "refunded": refunded}},
    }
    return json.dumps(payload).encode()


def sign(raw, secret=SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def post(client, raw, signature):
    return await client.post("/api/webhooks/lemonsqueezy", content=raw,
                             headers={"x-signature": signature or "",
                                      "content-type": "application/json"})


async def main():
    db.init()
    uid = db.create_verified_user("payer@example.com", "Payer", "",
                                  accounts.hash_password("longenough12"))
    tr = httpx.ASGITransport(app=server.app, client=("1.2.3.4", 1))
    async with httpx.AsyncClient(transport=tr, base_url="http://test") as c:

        def credit():
            return db.get_usage(uid, "2026-09-30").get("credit_cents", 0)

        # --- the refusals, which are the point -----------------------------
        raw = body(user_id=uid)
        r = await post(c, raw, "deadbeef" * 8)
        check("a forged signature grants nothing", credit() == 0, f"credit {credit()}")
        check("and still answers 200, so it is not retried forever",
              r.status_code == 200, str(r.status_code))

        r = await post(c, raw, None)
        check("a missing signature grants nothing", credit() == 0)

        r = await post(c, body(user_id=uid, total=800), sign(body(user_id=uid, total=500)))
        check("a signature for different content is refused", credit() == 0)

        raw = body(event="subscription_updated", user_id=uid)
        await post(c, raw, sign(raw))
        check("a non-order event grants nothing", credit() == 0)

        raw = body(user_id=uid, refunded=True, order_id="ord_refund")
        await post(c, raw, sign(raw))
        check("a refunded order grants nothing", credit() == 0)

        raw = body(user_id=uid, status="pending", order_id="ord_pending")
        await post(c, raw, sign(raw))
        check("an unpaid order grants nothing", credit() == 0)

        raw = body(user_id="", order_id="ord_nouser")
        await post(c, raw, sign(raw))
        check("an order naming no account grants nothing", credit() == 0)

        raw = body(user_id="nobody-at-all", order_id="ord_ghost")
        await post(c, raw, sign(raw))
        check("an order naming an unknown account grants nothing", credit() == 0)

        raw = body(user_id=uid, total=-500, order_id="ord_negative")
        await post(c, raw, sign(raw))
        check("a negative total grants nothing", credit() == 0)

        # --- the one that should work --------------------------------------
        raw = body(user_id=uid, total=800, order_id="ord_good")
        r = await post(c, raw, sign(raw))
        check("a properly signed paid order grants credit", credit() == 800,
              f"credit {credit()}")
        check("and says so", r.json().get("credited") is True, r.text)

        # --- the expensive bug ---------------------------------------------
        # Lemon Squeezy retries until it gets a 200, so this arrives again.
        r = await post(c, raw, sign(raw))
        check("a REPLAYED payment does not credit twice", credit() == 800,
              f"credit became {credit()}")
        check("and reports that it did not credit", r.json().get("credited") is False)

        # A different order for the same person does pay.
        raw2 = body(user_id=uid, total=500, order_id="ord_second")
        await post(c, raw2, sign(raw2))
        check("a second genuine order adds to the balance", credit() == 1300,
              f"credit {credit()}")

    # --- the markup dial ---------------------------------------------------
    os.environ["CREDIT_CENTS_PER_DOLLAR"] = "50"
    _, _, cents = payments.parse_order(body(user_id=uid, total=800, order_id="x"))
    check("a 2x markup halves the credit a dollar buys", cents == 400, str(cents))
    os.environ["CREDIT_CENTS_PER_DOLLAR"] = "100"

    # --- inert until configured --------------------------------------------
    os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = ""
    try:
        payments.verify(b"{}", "anything")
        check("unconfigured instances refuse everything", False, "it accepted")
    except payments.Rejected:
        check("unconfigured instances refuse everything", True)
    os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = SECRET

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
