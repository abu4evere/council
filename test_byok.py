"""Bring-your-own-key: encryption, isolation, and whose quota gets spent.

The checks that matter: one user's key must never be readable by another, must
never be returned in full over the API, and must actually be the key used when
that user runs something. The last one is the whole point -- storing keys
correctly is worthless if the call still uses the operator's.

    python test_byok.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ["COUNCIL_PASSWORD"] = ""
os.environ["ALLOW_LEGACY_SIGNUP"] = "true"   # these suites predate email verification
os.environ["COUNCIL_SECRET"] = "test-secret-not-the-real-one"
os.environ["GROQ_API_KEY"] = "server-owned-key"

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_byok.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

import httpx  # noqa: E402
import server  # noqa: E402
from engine import keyring, providers  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def client(addr):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=(addr, 1)),
        base_url="http://test")


async def main():
    db.init()

    # --- encryption at rest ---
    ct = keyring.encrypt_for("user-a", "gsk_aaa_secret_value")
    check("ciphertext is not the plaintext", "gsk_aaa_secret_value" not in ct)
    check("owner can decrypt", keyring.decrypt_for("user-a", ct) == "gsk_aaa_secret_value")
    check("ANOTHER USER CANNOT DECRYPT", keyring.decrypt_for("user-b", ct) is None)
    check("same key encrypts differently each time",
          keyring.encrypt_for("user-a", "x" * 20) != keyring.encrypt_for("user-a", "x" * 20))
    check("corrupt token returns None, does not raise",
          keyring.decrypt_for("user-a", "garbage") is None)

    # --- which key actually gets used ---
    prov = providers.get("groq")
    check("falls back to the server key when the user has none",
          prov.api_key() == "server-owned-key")
    with keyring.use_keys({"GROQ_API_KEY": "user-owned-key"}):
        check("USER'S KEY IS THE ONE USED", prov.api_key() == "user-owned-key")
    check("context is restored afterwards", prov.api_key() == "server-owned-key")

    os.environ["BYOK_ONLY"] = "true"
    check("byok_only refuses to fall back to the host's key", prov.api_key() is None)
    with keyring.use_keys({"GROQ_API_KEY": "user-owned-key"}):
        check("byok_only still uses the user's own key", prov.api_key() == "user-owned-key")
    os.environ.pop("BYOK_ONLY")

    # --- keys survive into a run's background task ---
    async def pretend_run():
        return providers.get("groq").api_key()

    async def start():
        with keyring.use_keys({"GROQ_API_KEY": "run-scoped-key"}):
            return asyncio.create_task(pretend_run())

    check("keys reach the background task that does the run",
          await (await start()) == "run-scoped-key")

    # --- over the API ---
    async with client("10.1.0.1") as alice:
        await alice.post("/api/signup", json={"username": "alice", "password": "alicepassword"})
        r = await alice.get("/api/keys")
        body = r.json()
        check("key list returns providers", r.status_code == 200 and body["providers"])
        groq = next(p for p in body["providers"] if p["provider"] == "groq")
        check("no key set yet", groq["yours"] is None)
        check("reports that the host's key would be used", groq["server_fallback"] is True)

        r = await alice.put("/api/keys/groq", json={"key": "short"})
        check("obviously bogus key rejected without a network call", r.status_code == 400)

        # Store directly, bypassing the live probe (no network in tests).
        uid = db.get_user_by_name("alice")["id"]
        db.set_user_key(uid, "GROQ_API_KEY", keyring.encrypt_for(uid, "gsk_alice_real_key_1234"))

        r = await alice.get("/api/keys")
        groq = next(p for p in r.json()["providers"] if p["provider"] == "groq")
        check("shows that a key is saved", bool(groq["yours"]))
        check("KEY IS MASKED, NEVER RETURNED IN FULL",
              "gsk_alice_real_key_1234" not in r.text, "full key appeared in the response")

        loaded = server.load_user_keys(uid)
        check("server can load it back for a run",
              loaded.get("GROQ_API_KEY") == "gsk_alice_real_key_1234")

    # --- a second account cannot see or use it ---
    async with client("10.1.0.2") as bob:
        await bob.post("/api/signup", json={"username": "bob", "password": "bobpassword12"})
        r = await bob.get("/api/keys")
        groq = next(p for p in r.json()["providers"] if p["provider"] == "groq")
        check("bob sees no key of his own", groq["yours"] is None)
        check("ALICE'S KEY NEVER APPEARS FOR BOB", "gsk_alice" not in r.text)

        bob_id = db.get_user_by_name("bob")["id"]
        check("bob's run loads no key from alice", server.load_user_keys(bob_id) == {})

    # --- deletion ---
    async with client("10.1.0.3") as alice2:
        await alice2.post("/api/login", json={"username": "alice", "password": "alicepassword"})
        await alice2.delete("/api/keys/groq")
        uid = db.get_user_by_name("alice")["id"]
        check("deleted key is gone", server.load_user_keys(uid) == {})

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db.DB_PATH) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
    sys.exit(code)
