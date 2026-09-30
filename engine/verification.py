"""Email verification codes.

THE THREAT, and why each defence exists. A six-digit code is one in a million,
which sounds safe and is not: unthrottled, a script tries a million guesses in
minutes. So a code is useless without the limits around it.

  * Codes EXPIRE (15 minutes). An old code left in an inbox is a live key
    otherwise.
  * Attempts are COUNTED (5). After that the code dies and a new one must be
    requested, which turns a million guesses into five.
  * Requests are THROTTLED per address. Without it the endpoint is a way to
    send someone else a hundred emails, and the operator's mail account is the
    thing that gets suspended.
  * Codes are stored HASHED. A database read should not hand over live codes.
  * Verification is compared in CONSTANT TIME, so a wrong code cannot be found
    digit by digit through timing.

A code is deliberately six digits rather than a long random string: it has to
be read from a phone and typed into a laptop, and a code people will not type
is a signup that does not happen.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

CODE_TTL_SECONDS = 15 * 60
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_SENDS_PER_HOUR = 5

# Deliberately permissive. Strict email regexes reject valid addresses, and the
# real proof that an address works is that a code sent to it comes back.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def email_problem(email: str) -> str | None:
    e = normalise_email(email)
    if not e:
        return "Enter your email address."
    if len(e) > 254:
        return "That address is too long."
    if not EMAIL_RE.match(e):
        return "That does not look like an email address."
    return None


def new_code() -> str:
    """Six digits, uniformly distributed, from a cryptographic source."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(email: str, code: str) -> str:
    """Bound to the address, so a code cannot be replayed against another."""
    return hashlib.sha256(f"{normalise_email(email)}:{code}".encode()).hexdigest()


def code_matches(email: str, code: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_code(email, code), stored_hash or "")


def expired(created_at: float) -> bool:
    return (time.time() - created_at) > CODE_TTL_SECONDS


def full_name_problem(name: str) -> str | None:
    n = (name or "").strip()
    if len(n) < 2:
        return "Enter your name."
    if len(n) > 80:
        return "That name is too long."
    return None
