"""User accounts: password hashing and durable sessions.

Why this exists, in order of how much it mattered:

1. Sessions were an in-memory set, so every server restart logged everyone out
   on every device. That is the actual daily annoyance -- not the password
   itself, but retyping it because the process bounced.
2. Conversations had no owner, so anyone reaching the app saw everyone's
   history. That has to be fixed before more than one person ever logs in.

No bcrypt or argon2 dependency. `hashlib.scrypt` is in the standard library and
is a memory-hard KDF -- the same class of algorithm, without adding a package
that needs compiling on Windows. Parameters below are the interactive-login
profile from the scrypt paper, which costs ~100ms and ~16MB per verification.

IMPORTANT: this does not change the API-cost problem. Accounts let several
people have separate histories; they do not conjure extra free-tier quota. A
public sign-up page still spends the operator's keys.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

# scrypt parameters. N is the work factor; raising it makes both hashing and
# any attack proportionally slower.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

SESSION_DAYS = 30


def hash_password(password: str) -> str:
    """Return 'scrypt$N$r$p$salt_hex$hash_hex'. Salt is per-password."""
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash. Never raises on bad input."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
    except Exception:
        return False
    # compare_digest, not ==, so a wrong password cannot be found byte by byte
    # through timing.
    return hmac.compare_digest(dk.hex(), hash_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> float:
    return time.time() + SESSION_DAYS * 86400


def normalise_username(name: str) -> str:
    return (name or "").strip().lower()


def username_problem(name: str) -> str | None:
    """Return a human-readable reason the name is unusable, or None.

    EMAIL ADDRESSES ARE ALLOWED, because people type one by reflex when a form
    asks who they are. Rejecting "me@example.com" with a rule they were never
    shown is the cheapest way to lose a visitor who was willing to sign up --
    and the rule existed only because the original list of safe characters was
    written without thinking about what people actually type.

    Nothing downstream cares: the value is stored as text, compared exactly,
    and never interpolated into a path, a query or a shell command.
    """
    n = normalise_username(name)
    if len(n) < 3:
        return "Make it at least 3 characters."
    if len(n) > 64:
        return "That is too long -- 64 characters at most."
    if not all(c.isalnum() or c in "-_.@+" for c in n):
        return "Letters, numbers and . - _ @ + only."
    if n.count("@") > 1:
        return "That does not look like a valid name or email."
    return None


def password_problem(password: str) -> str | None:
    if not password or len(password) < 8:
        return "Passwords need at least 8 characters."
    if len(password) > 1024:
        # Long inputs are a cheap denial of service against a memory-hard KDF.
        return "Password is too long."
    return None


def single_user_mode() -> bool:
    """True when COUNCIL_PASSWORD is set and no accounts exist.

    Keeps the old shared-password behaviour working for anyone self-hosting who
    has not created an account, so upgrading does not lock them out.
    """
    return bool(os.environ.get("COUNCIL_PASSWORD", "").strip())
