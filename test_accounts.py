"""Accounts, durable sessions, and — most importantly — isolation between users.

The interesting tests here are the ones that try to BREAK isolation: one
account reaching another's conversations, turns, events and cancel endpoint.
A leak there is worse than any bug in the debate engine.

    python test_accounts.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ["COUNCIL_PASSWORD"] = ""          # accounts mode, not shared password
os.environ["ALLOW_LEGACY_SIGNUP"] = "true"   # these suites predate email verification
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_accounts.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

import httpx  # noqa: E402
import server  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


def client(addr="10.0.0.9"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=(addr, 1)),
        base_url="http://test",
    )


async def main():
    db.init()

    # --- signup ---
    async with client("10.0.0.1") as alice:
        r = await alice.post("/api/signup", json={"username": "Alice", "password": "alicepassword"})
        check("signup succeeds", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        check("username normalised to lowercase", r.json().get("username") == "alice")

        r2 = await alice.get("/api/auth-status")
        check("signed in straight after signup", r2.json()["authenticated"] is True)
        check("mode switches to accounts", r2.json()["mode"] == "accounts")

        # weak input rejected
        async with client("10.0.0.7") as tmp:
            r3 = await tmp.post("/api/signup", json={"username": "bo", "password": "longenough1"})
            check("short username rejected", r3.status_code == 400)
            r4 = await tmp.post("/api/signup", json={"username": "bobby", "password": "short"})
            check("short password rejected", r4.status_code == 400)
            r5 = await tmp.post("/api/signup", json={"username": "alice", "password": "anotherpass1"})
            check("duplicate username rejected", r5.status_code == 409, f"got {r5.status_code}")

        # Alice makes a conversation and a turn
        cid_a = (await alice.post("/api/conversations")).json()["id"]
        await alice.patch(f"/api/conversations/{cid_a}", json={"title": "alice private notes"})
        check("alice sees her own conversation",
              any(c["id"] == cid_a for c in (await alice.get("/api/conversations")).json()))

    # --- second account ---
    async with client("10.0.0.2") as bob:
        r = await bob.post("/api/signup", json={"username": "bob", "password": "bobpassword1"})
        check("second signup succeeds", r.status_code == 200)

        convs = (await bob.get("/api/conversations")).json()
        check("bob's list does NOT contain alice's conversation",
              all(c["id"] != cid_a for c in convs), f"bob saw {len(convs)} conversations")

        # direct access attempts
        r = await bob.get(f"/api/conversations/{cid_a}")
        check("bob cannot open alice's conversation by id", r.status_code == 404, f"got {r.status_code}")

        r = await bob.delete(f"/api/conversations/{cid_a}")
        check("bob cannot delete alice's conversation", r.status_code == 404, f"got {r.status_code}")

        r = await bob.patch(f"/api/conversations/{cid_a}", json={"title": "hacked"})
        check("bob cannot rename alice's conversation", r.status_code == 404, f"got {r.status_code}")

        r = await bob.post(f"/api/conversations/{cid_a}/turns",
                           json={"prompt": "hi", "mode": "moa"})
        check("bob cannot start a turn in alice's conversation", r.status_code == 404,
              f"got {r.status_code}")

    # alice's title survived every attempt
    conv = db.get_conversation(cid_a)
    check("alice's conversation is unchanged", conv["title"] == "alice private notes",
          conv["title"])

    # --- sessions are durable across a "restart" ---
    async with client("10.0.0.3") as c1:
        await c1.post("/api/login", json={"username": "alice", "password": "alicepassword"})
        cookie = c1.cookies.get("council_session")
        check("login returns a session cookie", bool(cookie))

    # A fresh client using only the stored cookie -- equivalent to the server
    # having restarted, since nothing is held in memory.
    async with client("10.0.0.4") as c2:
        c2.cookies.set("council_session", cookie)
        r = await c2.get("/api/auth-status")
        check("session survives with no in-memory state (restart-proof)",
              r.json()["authenticated"] is True and r.json()["username"] == "alice")

    # --- wrong password, and logout ---
    async with client("10.0.0.5") as c3:
        r = await c3.post("/api/login", json={"username": "alice", "password": "wrongpassword"})
        check("wrong password rejected", r.status_code == 401)
        r = await c3.post("/api/login", json={"username": "ghost", "password": "whatever12345"})
        check("unknown user rejected the same way", r.status_code == 401)

    async with client("10.0.0.6") as c4:
        await c4.post("/api/login", json={"username": "bob", "password": "bobpassword1"})
        check("bob logged in", (await c4.get("/api/auth-status")).json()["authenticated"])
        await c4.post("/api/logout")
        check("logout ends the session",
              (await c4.get("/api/auth-status")).json()["authenticated"] is False)

    # --- anonymous access is refused entirely ---
    async with client("10.0.0.8") as anon:
        for path in ("/api/conversations", "/api/health", f"/api/conversations/{cid_a}"):
            r = await anon.get(path)
            check(f"anonymous refused {path}", r.status_code == 401, f"got {r.status_code}")

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
