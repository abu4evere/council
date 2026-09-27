"""Per-user API keys: encryption at rest, and getting them to the call site.

BRING YOUR OWN KEY. A hosted Council cannot run on the operator's free tiers --
one Groq account allows about eighty full runs a day across every user, and
reselling free-tier capacity is the kind of thing that gets accounts closed. So
each person supplies their own keys and spends their own quota.

TWO PROBLEMS, SOLVED SEPARATELY.

1. Storing keys. They are encrypted with Fernet (AES-128-CBC + HMAC), under a
   key derived per user with HKDF from one server secret. Per-user derivation
   means one user's ciphertext cannot be decrypted with another's derived key,
   and the user id is bound into the derivation, so ciphertext cannot be moved
   between accounts.

   The server secret is PERSISTED, not regenerated at boot. A design where the
   master key lives only in memory sounds safer until you notice it forces
   every user to retype four API keys after every restart -- which is exactly
   the annoyance that made this project add durable sessions in the first
   place. Losing the secret makes stored keys unrecoverable; that is the
   correct trade, and it is documented rather than hidden.

2. Getting them to the HTTP call. The provider layer used to read os.environ,
   which is process-global and therefore wrong the moment two users run at
   once. A ContextVar carries the active user's keys instead: asyncio.create_task
   copies the current context, so a run started inside a request keeps that
   user's keys for its whole life, with no argument threaded through every
   function between here and the socket.
"""
from __future__ import annotations

import base64
import contextvars
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Keys for the user whose run is currently executing: {provider_key: api_key}.
# Empty means "fall back to the server's own environment", which is what a
# single-user local install does.
CURRENT_KEYS: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "council_user_keys", default={}
)


@contextmanager
def use_keys(keys: dict):
    token = CURRENT_KEYS.set(keys or {})
    try:
        yield
    finally:
        CURRENT_KEYS.reset(token)


def current_key(env_var: str) -> str | None:
    """The active user's key for this provider, if they supplied one."""
    return (CURRENT_KEYS.get() or {}).get(env_var) or None


# --- the server secret ------------------------------------------------------

def _server_secret() -> bytes:
    """Load COUNCIL_SECRET, creating and persisting one on first run.

    Written to .env rather than held in memory, so a restart does not orphan
    every stored key. If the file is lost, stored keys are gone -- which is the
    point of encrypting them.
    """
    raw = os.environ.get("COUNCIL_SECRET", "").strip()
    if raw:
        return raw.encode("utf-8")

    generated = secrets.token_urlsafe(48)
    os.environ["COUNCIL_SECRET"] = generated

    env = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        if "COUNCIL_SECRET=" not in text:
            text += (
                "\n# Encrypts stored API keys. Generated automatically.\n"
                "# Back this up: losing it makes every saved key unrecoverable.\n"
                f"COUNCIL_SECRET={generated}\n"
            )
            env.write_text(text, encoding="utf-8")
    except OSError:
        # Read-only deployment: the process keeps working for this run, but
        # keys saved now will not decrypt after a restart. Better to say so.
        print("[keyring] WARNING: could not persist COUNCIL_SECRET to .env -- "
              "set it in the environment or stored keys will be lost on restart.")
    return generated.encode("utf-8")


def _fernet_for(user_id: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"council-user-api-keys-v1",
        info=user_id.encode("utf-8"),
    ).derive(_server_secret())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_for(user_id: str, plaintext: str) -> str:
    return _fernet_for(user_id).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_for(user_id: str, token: str) -> str | None:
    """Returns None rather than raising when a token cannot be read.

    A rotated secret, a corrupted row or a token from another account should
    degrade to "this user has no key for that provider", not crash their run.
    """
    try:
        return _fernet_for(user_id).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def mask(key: str) -> str:
    """A key fragment safe to show back to its owner."""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:2] + "..."
    return f"{key[:6]}...{key[-4:]}"
