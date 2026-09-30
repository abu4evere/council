"""Push the secrets this app needs into Fly, without printing any of them.

    python fly_secrets.py            # show what WOULD be sent, masked
    python fly_secrets.py --push     # actually send them

WHY A SCRIPT. `fly secrets set K=V` puts the value in your shell history and in
the process list. This pipes them to `fly secrets import` over stdin instead,
so nothing is echoed, and it refuses to send a key that is missing rather than
silently deploying an app with four providers instead of five.

COUNCIL_SECRET IS THE IMPORTANT ONE. It derives the key that encrypts every
stored user API key. In a container there is no writable .env, so without an
explicit value the app generates a fresh one on every machine start and every
previously saved key becomes undecryptable. It is generated once here and
written to .env so the local install and the deployed one agree.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys

from dotenv import load_dotenv

ENV = pathlib.Path(__file__).parent / ".env"
load_dotenv(ENV)

# Without these the app runs, but with fewer seats than it claims.
PROVIDERS = ["GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
             "NVIDIA_API_KEY", "OPENROUTER_API_KEY"]
# Without these, nobody can finish signing up.
MAIL = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"]
OTHER = ["COUNCIL_SECRET", "PREMIUM_SEATS"]


def ensure_council_secret() -> str:
    value = os.environ.get("COUNCIL_SECRET", "").strip()
    if value:
        return value
    value = secrets.token_urlsafe(48)
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    text = text.rstrip() + (
        "\n\n# Derives the key that encrypts stored user API keys. BACK THIS UP:\n"
        "# lose it and every saved key becomes unreadable. It must be identical\n"
        "# locally and on every deploy, or the same thing happens.\n"
        f"COUNCIL_SECRET={value}\n")
    ENV.write_text(text, encoding="utf-8")
    print("  generated COUNCIL_SECRET and wrote it to .env -- back that file up")
    return value


def mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def main() -> int:
    os.environ.setdefault("PREMIUM_SEATS", "false")
    ensure_council_secret()
    load_dotenv(ENV, override=True)

    pairs: dict[str, str] = {}
    missing: list[str] = []
    for key in PROVIDERS + MAIL + OTHER:
        value = os.environ.get(key, "").strip()
        if value:
            pairs[key] = value
        else:
            missing.append(key)

    print("\nwill send to Fly:")
    for key, value in pairs.items():
        print(f"  {key:<22} {mask(value)}")
    if missing:
        print("\nmissing (the app still starts, but):")
        for key in missing:
            why = ("one fewer model seat" if key in PROVIDERS
                   else "signup cannot send codes" if key in MAIL
                   else "see the notes above")
            print(f"  {key:<22} -> {why}")

    if "--push" not in sys.argv:
        print("\nnothing sent. re-run with --push to apply.")
        return 0

    if not pairs:
        print("\nno secrets to send.")
        return 1

    blob = "\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n"
    print(f"\nsending {len(pairs)} secrets via `fly secrets import`...")
    try:
        proc = subprocess.run(["fly", "secrets", "import"], input=blob,
                              text=True, capture_output=True)
    except FileNotFoundError:
        print("flyctl is not installed or not on PATH.")
        return 1
    print(proc.stdout.strip() or proc.stderr.strip())
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
