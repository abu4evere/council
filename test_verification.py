"""Email-verified signup, end to end.

The code itself is only as good as the limits around it, so most of what is
tested here is the refusals: expiry, attempt caps, resend throttling, codes
replayed against a different address, and the old unverified signup route
still being reachable after the new one exists.

    python test_verification.py
"""
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_verification.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

os.environ["GUEST_RUNS"] = "50"

import httpx  # noqa: E402
import server  # noqa: E402
from engine import verification as v  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


# Intercept delivery instead of sending mail. This is also how a developer with
# no SMTP account exercises the flow, so the test runs the same path they do.
SENT: list[tuple[str, str]] = []


def fake_send(to_address, code):
    SENT.append((to_address, code))
    return True, "test"


server.mailer.send_code = fake_send


def client(addr="1.2.3.4"):
    tr = httpx.ASGITransport(app=server.app, client=(addr, 123))
    return httpx.AsyncClient(transport=tr, base_url="http://test")


async def start(c, email):
    return await c.post("/api/signup/start", json={"email": email})


async def main():
    db.init()

    # --- the happy path ----------------------------------------------------
    async with client() as c:
        r = await start(c, "  Alice@Example.COM ")
        check("start accepts a valid address", r.status_code == 200, r.text)
        check("address is normalised before sending",
              SENT and SENT[-1][0] == "alice@example.com", str(SENT[-1:]))
        code = SENT[-1][1]
        check("code is six digits", len(code) == 6 and code.isdigit(), code)

        r = await c.post("/api/signup/verify",
                         json={"email": "alice@example.com", "code": code})
        check("correct code verifies", r.status_code == 200, r.text)

        r = await c.post("/api/signup/complete", json={
            "email": "alice@example.com", "code": code,
            "full_name": "Alice Nasrullayeva", "purpose": "thesis decisions",
            "password": "correcthorse1"})
        check("account is created", r.status_code == 200, r.text)
        check("signed in straight after signup",
              "council_session" in r.cookies or c.cookies.get("council_session"))

        row = db.get_user_by_email("alice@example.com")
        check("profile is stored", row and row["full_name"] == "Alice Nasrullayeva")
        check("purpose is stored", row and row["purpose"] == "thesis decisions")
        check("password is not stored in the clear",
              "correcthorse1" not in str(dict(row)))
        check("code is cleared once used",
              db.get_verification("alice@example.com") is None)

    # --- that account can now log in, and only with its own password -------
    async with client("1.2.3.5") as c:
        r = await c.post("/api/login", json={"username": "alice@example.com",
                                             "password": "correcthorse1"})
        check("verified account can log in", r.status_code == 200, r.text)
        r = await c.post("/api/login", json={"username": "alice@example.com",
                                             "password": "wrongpassword"})
        check("wrong password is refused", r.status_code == 401)

    async with client("1.2.3.6") as c:
        r = await c.post("/api/login", json={"username": "stranger@example.com",
                                             "password": "correcthorse1"})
        check("an address that never signed up cannot log in", r.status_code == 401)

    # --- the same address cannot be taken twice ----------------------------
    async with client("1.2.3.7") as c:
        r = await start(c, "alice@example.com")
        check("signup refuses an address that already has an account",
              r.status_code == 409, r.text)

    # --- rejections at the door --------------------------------------------
    async with client("1.2.3.8") as c:
        for bad in ("", "notanemail", "no@domain", "a b@c.com", "x@y.z"):
            r = await start(c, bad)
            check(f"refuses {bad!r}", r.status_code == 400, r.text[:80])
        r = await start(c, "a" * 250 + "@example.com")
        check("refuses an over-long address", r.status_code == 400)

    # --- wrong codes are counted, then the code dies -----------------------
    async with client("1.2.4.1") as c:
        r = await start(c, "bob@example.com")
        real_code = SENT[-1][1]
        wrong = "000000" if real_code != "000000" else "111111"
        for i in range(v.MAX_ATTEMPTS):
            r = await c.post("/api/signup/verify",
                             json={"email": "bob@example.com", "code": wrong})
            check(f"wrong code {i + 1} refused", r.status_code in (401, 429), r.text[:80])
        r = await c.post("/api/signup/verify",
                         json={"email": "bob@example.com", "code": real_code})
        check("the real code is dead after too many wrong ones",
              r.status_code in (400, 429), r.text[:80])
        check("the burnt code is gone from storage",
              db.get_verification("bob@example.com") is None)

    # --- an expired code is refused ----------------------------------------
    async with client("1.2.4.2") as c:
        await start(c, "carol@example.com")
        code = SENT[-1][1]
        row = db.get_verification("carol@example.com")
        db.put_verification("carol@example.com", row["code_hash"],
                            row["window_start"], row["sends"])
        # Age the code past its TTL by rewriting created_at directly.
        conn = db.connect()
        conn.execute("UPDATE verifications SET created_at = ? WHERE email = ?",
                     (time.time() - v.CODE_TTL_SECONDS - 5, "carol@example.com"))
        conn.commit()
        conn.close()
        r = await c.post("/api/signup/verify",
                         json={"email": "carol@example.com", "code": code})
        check("an expired code is refused", r.status_code == 400, r.text[:80])

    # --- resend throttling --------------------------------------------------
    async with client("1.2.4.3") as c:
        r = await start(c, "dave@example.com")
        check("first send is allowed", r.status_code == 200, r.text)
        r = await start(c, "dave@example.com")
        check("an immediate resend is throttled", r.status_code == 429, r.text[:80])

        # Walk past the cooldown each time to reach the hourly cap.
        blocked = False
        for _ in range(v.MAX_SENDS_PER_HOUR + 2):
            row = db.get_verification("dave@example.com")
            if row is None:
                break
            conn = db.connect()
            conn.execute("UPDATE verifications SET last_sent = ? WHERE email = ?",
                         (time.time() - v.RESEND_COOLDOWN_SECONDS - 1, "dave@example.com"))
            conn.commit()
            conn.close()
            r = await start(c, "dave@example.com")
            if r.status_code == 429:
                blocked = True
                break
        check("the hourly send cap eventually blocks", blocked)

    # --- a code cannot be replayed against another address ------------------
    async with client("1.2.4.4") as c:
        await start(c, "erin@example.com")
        erin_code = SENT[-1][1]
        await start(c, "frank@example.com")
        r = await c.post("/api/signup/verify",
                         json={"email": "frank@example.com", "code": erin_code})
        check("erin's code does not work for frank", r.status_code in (401, 429), r.text[:80])

    # --- step 3 cannot be called without a valid code -----------------------
    async with client("1.2.4.5") as c:
        r = await c.post("/api/signup/complete", json={
            "email": "mallory@example.com", "code": "123456",
            "full_name": "Mallory", "purpose": "", "password": "longenough12"})
        check("complete refuses an address with no live code",
              r.status_code in (400, 401), r.text[:80])
        check("no account was created for it",
              db.get_user_by_email("mallory@example.com") is None)

    # --- profile validation happens after the code is proven ----------------
    async with client("1.2.4.6") as c:
        await start(c, "grace@example.com")
        code = SENT[-1][1]
        r = await c.post("/api/signup/complete", json={
            "email": "grace@example.com", "code": code,
            "full_name": "G", "purpose": "", "password": "longenough12"})
        check("a one-letter name is refused", r.status_code == 400, r.text[:80])
        r = await c.post("/api/signup/complete", json={
            "email": "grace@example.com", "code": code,
            "full_name": "Grace Hopper", "purpose": "", "password": "short"})
        check("a short password is refused", r.status_code == 400, r.text[:80])
        r = await c.post("/api/signup/complete", json={
            "email": "grace@example.com", "code": code,
            "full_name": "Grace Hopper", "purpose": "", "password": "longenough12"})
        check("the code survives a rejected profile", r.status_code == 200, r.text[:80])

    # --- the unverified route is closed -------------------------------------
    async with client("1.2.4.7") as c:
        r = await c.post("/api/signup", json={"username": "sneaky",
                                              "password": "longenough12"})
        check("the old unverified signup route is closed",
              r.status_code == 410, f"{r.status_code} {r.text[:80]}")
        check("and it created nothing", db.get_user_by_name("sneaky") is None)

    # --- codes are never stored in a readable form --------------------------
    async with client("1.2.4.8") as c:
        await start(c, "heidi@example.com")
        code = SENT[-1][1]
        row = db.get_verification("heidi@example.com")
        check("the stored row does not contain the code",
              code not in str(dict(row)), "code is recoverable from the database")

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
