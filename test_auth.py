"""Verify the password gate end to end. Never prints the password.

    python test_auth.py
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_auth.db"
for suffix in ("", "-wal", "-shm"):
    f = Path(str(db.DB_PATH) + suffix)
    if f.exists():
        f.unlink()

import httpx  # noqa: E402
import server  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if not cond and detail else ""))


async def main():
    db.init()
    real = os.environ.get("COUNCIL_PASSWORD", "")
    if not real:
        print("COUNCIL_PASSWORD is not set - nothing to test")
        return 1

    # Each client gets its own address so throttling in one test does not
    # bleed into the next.
    def client(addr="1.2.3.4"):
        tr = httpx.ASGITransport(app=server.app, client=(addr, 123))
        return httpx.AsyncClient(transport=tr, base_url="http://test")

    async with client("10.0.0.1") as c:
        r = await c.get("/api/auth-status")
        check("auth is required", r.json()["required"] is True)
        check("starts unauthenticated", r.json()["authenticated"] is False)

        r = await c.get("/api/conversations")
        check("API refuses without login", r.status_code == 401, f"got {r.status_code}")

        r = await c.post("/api/login", json={"password": "definitely-wrong"})
        check("wrong password rejected", r.status_code == 401, f"got {r.status_code}")

        r = await c.post("/api/login", json={"password": real})
        check("correct password accepted", r.status_code == 200, f"got {r.status_code}")
        check("session cookie set", "council_session" in r.cookies or
              any("council_session" in v for v in r.headers.get_list("set-cookie")))

        r = await c.get("/api/conversations")
        check("API works once logged in", r.status_code == 200, f"got {r.status_code}")

        r = await c.get("/api/auth-status")
        check("auth-status reflects login", r.json()["authenticated"] is True)

    # Throttling: a fresh address, hammered with wrong guesses.
    async with client("10.0.0.2") as c:
        codes = []
        for _ in range(12):
            r = await c.post("/api/login", json={"password": "guess"})
            codes.append(r.status_code)
        check("brute force gets throttled", 429 in codes,
              f"codes seen: {sorted(set(codes))}")
        first_429 = codes.index(429) if 429 in codes else -1
        check("throttle kicks in within 10 tries", 0 < first_429 <= 10,
              f"first 429 at attempt {first_429 + 1}")

        # Even the right password is refused while throttled -- that is the point.
        r = await c.post("/api/login", json={"password": real})
        check("throttle applies to correct password too", r.status_code == 429,
              f"got {r.status_code}")

    # A different address is unaffected by someone else's throttling.
    async with client("10.0.0.3") as c:
        r = await c.post("/api/login", json={"password": real})
        check("throttling is per-address, not global", r.status_code == 200,
              f"got {r.status_code}")

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
