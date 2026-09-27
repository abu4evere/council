"""Preflight: which free providers are configured, and do their model slugs exist?

Never prints a key. Run this before starting the server, and any time a run
fails with an auth or "model not found" error.

    python check_key.py
"""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from engine import config, providers  # noqa: E402


def mask(k: str) -> str:
    return f"{k[:6]}...{k[-4:]} ({len(k)} chars)" if len(k) > 14 else "(too short?)"


def check_providers() -> bool:
    print("PROVIDERS")
    print("-" * 68)
    any_ok = False
    for p in providers.PROVIDERS.values():
        if p.env_var is None:
            continue  # local Ollama, checked only when actually used
        key = p.api_key()
        if key:
            any_ok = True
            print(f"  [ OK ] {p.label:<18} {mask(key)}")
        else:
            print(f"  [ -- ] {p.label:<18} not set  ->  {p.signup}")
    if not any_ok:
        print("\nNo keys at all. Council needs at least one.")
        print("Quickest: get a free Groq key at https://console.groq.com/keys")
        print("then put it in .env as  GROQ_API_KEY=...")
    return any_ok


async def check_models() -> bool:
    from engine.llm import check_configured_models

    print("\nSEATS (checking each model against its provider's live catalogue)")
    print("-" * 68)
    r = await check_configured_models()

    symbol = {"ok": "[ OK ]", "missing": "[FAIL]", "unconfigured": "[ -- ]",
              "unreachable": "[WARN]"}
    for s in r["seats"]:
        line = f"  {symbol[s['status']]} {s['seat']:<16} {s['provider']:<18} {s['model']}"
        if s["status"] == "missing":
            line += "   <- slug not found"
        print(line)

    print("-" * 68)
    usable = r["usable_count"]
    if usable == 0:
        print("No usable seats. Add a key, or fix the slugs in engine/config.py.")
        return False

    missing = [s for s in r["seats"] if s["status"] == "missing"]
    if missing:
        print(f"\n{len(missing)} model slug(s) are stale. Find the current name with:")
        for s in {m["provider"]: m for m in missing}.values():
            print(f"    python -c \"import asyncio,engine.llm as l;"
                  f"print([m['id'] for m in asyncio.run(l.list_models('?'))][:40])\"")
            break
        print("  (replace '?' with the provider key: groq, gemini, cerebras, github...)")
        print("Then edit engine/config.py.")

    # --- diversity report: the thing the whole tool depends on ---
    d = config.roster_diversity()
    print("\nVOICES (how many genuinely different models you actually have)")
    print("-" * 68)
    for label, model in d["proposers"]:
        print(f"  proposer   {label:<16} {model}")
    for label, model in d["decision"]:
        print(f"  decision   {label:<16} {model}")
    print("-" * 68)
    print(f"  {d['distinct_models']} distinct model(s) across the roster")
    if d["decision_duplicates"]:
        print(f"  WARNING: {d['decision_duplicates']} decision seat(s) reuse a model.")
        print("  Drafter/Critic/Judge sharing a model means the debate is partly")
        print("  self-review -- a model cannot see the blind spots that produced")
        print("  its own flaws. Add another free provider key to fix this:")
        for prov in providers.PROVIDERS.values():
            if prov.env_var and not prov.configured() and prov.key != "openrouter":
                print(f"    {prov.env_var:<20} {prov.signup}")

    n_prop = len(config.available_proposers())
    print(f"\n{usable} seat(s) usable, {n_prop} of {len(config.PROPOSERS)} proposers available.")
    if n_prop == 1:
        print("Only one proposer -- you will get a single answer with no synthesis.")
        print("Add a second provider key to make the council actually deliberate.")
    elif n_prop >= 2:
        print("Ready. Start the server:")
        print("    python -m uvicorn server:app --host 0.0.0.0 --port 8000")
    return not missing


async def discover(only: str | None = None) -> None:
    """List every chat model each configured provider actually offers.

    Use this instead of guessing slugs -- every name I have ever assumed from
    memory has been wrong. Paste what you want into engine/config.py.
    """
    from engine.llm import list_models

    # These are not chat models and only clutter the list.
    NOISE = ("whisper", "embed", "tts", "audio", "image", "rerank", "guard",
             "safeguard", "moderation", "vision-ocr", "lyria", "veo", "imagen",
             "nano-banana", "transcribe", "orpheus", "robotics")

    for prov in providers.PROVIDERS.values():
        if only and prov.key != only:
            continue
        if not prov.env_var or not prov.configured():
            continue
        try:
            models = await list_models(prov.key)
        except Exception as exc:
            print(f"\n{prov.label}: could not list -> {str(exc)[:110]}")
            continue
        ids = sorted({str(m.get("id", "")).split("/", 1)[-1] if prov.key == "gemini"
                      else str(m.get("id", "")) for m in models})
        chat = [i for i in ids if i and not any(n in i.lower() for n in NOISE)]
        print(f"\n{prov.label} -- {len(chat)} chat model(s) of {len(ids)} total")
        for i in chat[:40]:
            print("   ", i)
        if len(chat) > 40:
            print(f"    ... and {len(chat) - 40} more")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        only = sys.argv[2] if len(sys.argv) > 2 else None
        asyncio.run(discover(only))
        sys.exit(0)
    if not check_providers():
        sys.exit(1)
    sys.exit(0 if asyncio.run(check_models()) else 1)
