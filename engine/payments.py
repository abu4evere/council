"""Turn a completed Lemon Squeezy payment into account credit.

A webhook that adds credit is a money endpoint. Anyone on the internet can
POST to it, so everything here is about refusing to act on a request that has
not proved it came from Lemon Squeezy.

THREE THINGS THAT MUST ALL HOLD before a single cent is granted:

1. SIGNATURE. Lemon Squeezy signs the raw body with a secret only it and this
   server know. The check uses the exact bytes received -- re-serialising the
   JSON first would change the whitespace and break a signature that was
   actually valid, which is the classic way this gets written wrong.
2. EVENT. Only `order_created`. A refund or a subscription update must not
   quietly top someone up.
3. IDENTITY. The order has to say which account it belongs to, and the amount
   has to be a positive number of cents. Missing or malformed, no credit.

IDEMPOTENCE is handled by the caller, not here: Lemon Squeezy retries a
webhook until it gets a 200, so the same successful payment arrives more than
once as a matter of course. Crediting twice for one payment is the bug that
costs real money, so the order id is recorded and a repeat is a no-op.

UNCONFIGURED, this module is inert. Nothing is live until
LEMONSQUEEZY_WEBHOOK_SECRET is set, and the endpoint says so rather than
pretending to work.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os


class Rejected(Exception):
    """The request did not earn a credit. The message is for the log, not the payer."""


def configured() -> bool:
    return bool(os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "").strip())


def checkout_url() -> str | None:
    """Where to send someone who wants to top up, or None if not set up."""
    return os.environ.get("LEMONSQUEEZY_CHECKOUT_URL", "").strip() or None


# How much credit a dollar buys, in cents of model usage. 100 means a dollar
# buys a dollar of usage -- which after Lemon Squeezy's cut leaves the operator
# out of pocket, so this is a deliberate dial rather than a default to accept.
# Set it to 50 and a dollar buys fifty cents of usage, i.e. a 2x markup.
def credit_per_dollar() -> int:
    try:
        return max(1, int(os.environ.get("CREDIT_CENTS_PER_DOLLAR", "100")))
    except ValueError:
        return 100


def verify(raw_body: bytes, signature: str | None) -> None:
    """Raise Rejected unless this body really came from Lemon Squeezy."""
    secret = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise Rejected("payments are not configured on this instance")
    if not signature:
        raise Rejected("no signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # compare_digest, not ==, so a wrong signature cannot be discovered byte by
    # byte through timing.
    if not hmac.compare_digest(expected, signature.strip()):
        raise Rejected("signature does not match")


def parse_order(raw_body: bytes) -> tuple[str, str, int]:
    """Return (order_id, user_id, credit_cents) for a paid order.

    Raises Rejected for anything else, including events that look like money
    but are not -- a refund must never add credit.
    """
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Rejected(f"body is not JSON: {exc}") from exc

    meta = payload.get("meta") or {}
    event = meta.get("event_name")
    if event != "order_created":
        raise Rejected(f"ignoring event {event!r}")

    data = payload.get("data") or {}
    order_id = str(data.get("id") or "").strip()
    if not order_id:
        raise Rejected("order has no id, so a repeat could not be detected")

    attrs = data.get("attributes") or {}
    if attrs.get("status") not in ("paid", "complete", "completed"):
        raise Rejected(f"order status is {attrs.get('status')!r}, not paid")
    if attrs.get("refunded"):
        raise Rejected("order was refunded")

    # `total` is already in cents, and is what they actually paid after any
    # discount -- which is the number to honour, not the list price.
    try:
        paid_cents = int(attrs.get("total"))
    except (TypeError, ValueError):
        raise Rejected("order total is missing or not a number")
    if paid_cents <= 0:
        raise Rejected("order total is not positive")

    # Set as a custom field on the checkout. Matching on email instead would
    # credit the wrong account whenever someone pays from a different address
    # than they signed up with, which is common.
    custom = meta.get("custom_data") or {}
    user_id = str(custom.get("user_id") or "").strip()
    if not user_id:
        raise Rejected("order does not say which account it belongs to")

    credit = round(paid_cents * credit_per_dollar() / 100)
    if credit <= 0:
        raise Rejected("computed credit is not positive")
    return order_id, user_id, credit
