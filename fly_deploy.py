"""One-shot Fly deployment: app, volume, secrets, deploy, smoke test.

    python fly_deploy.py

Run it AFTER `fly auth login`. Every step is idempotent, so re-running after a
failure picks up where it stopped rather than making a second app.

The order is not arbitrary. Secrets go in BEFORE the first deploy: a machine
that boots without COUNCIL_SECRET generates its own, and any key stored against
that throwaway value is unreadable the moment the machine restarts.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
TOML = ROOT / "fly.toml"
REGION = "fra"
VOLUME = "unstuck_data"


def run(args, **kw):
    print(f"\n$ {' '.join(args)}")
    return subprocess.run(args, text=True, capture_output=True, **kw)


def fly(*args, **kw):
    return run(["fly", *args], **kw)


def app_name() -> str:
    m = re.search(r'^app\s*=\s*"([^"]+)"', TOML.read_text(encoding="utf-8"), re.M)
    return m.group(1)


def set_app_name(name: str) -> None:
    text = TOML.read_text(encoding="utf-8")
    TOML.write_text(re.sub(r'^app\s*=\s*"[^"]+"', f'app = "{name}"', text, count=1,
                           flags=re.M), encoding="utf-8")
    print(f"  fly.toml now targets {name}")


def ensure_logged_in() -> bool:
    r = fly("auth", "whoami")
    if r.returncode != 0:
        print("  not logged in. Run `fly auth login` first.")
        return False
    print(f"  logged in as {r.stdout.strip()}")
    return True


def ensure_app() -> str | None:
    name = app_name()
    r = fly("status", "-a", name)
    if r.returncode == 0:
        print(f"  app {name} already exists")
        return name

    for candidate in (name, f"{name}-app", f"{name}-{abs(hash(name)) % 9973}"):
        r = fly("apps", "create", candidate, "--org", "personal")
        if r.returncode == 0:
            print(f"  created {candidate}")
            if candidate != name:
                set_app_name(candidate)
            return candidate
        if "taken" not in (r.stdout + r.stderr).lower():
            print((r.stdout + r.stderr).strip()[:400])
            return None
        print(f"  {candidate} is taken, trying another")
    return None


def ensure_volume(name: str) -> bool:
    r = fly("volumes", "list", "-a", name, "--json")
    if r.returncode == 0:
        try:
            if any(v.get("name") == VOLUME for v in json.loads(r.stdout or "[]")):
                print(f"  volume {VOLUME} already exists")
                return True
        except json.JSONDecodeError:
            pass
    # 1GB is the smallest billable unit and holds this database many times over.
    r = fly("volumes", "create", VOLUME, "-a", name, "--region", REGION,
            "--size", "1", "--yes")
    ok = r.returncode == 0
    print((r.stdout + r.stderr).strip()[:400])
    return ok


def push_secrets() -> bool:
    print("\n$ python fly_secrets.py --push")
    r = subprocess.run([sys.executable, "fly_secrets.py", "--push"],
                       text=True, capture_output=True, cwd=ROOT)
    # Values are masked by the script itself; this only echoes its report.
    print(r.stdout.strip()[-1200:] or r.stderr.strip()[-600:])
    return r.returncode == 0


def main() -> int:
    if not ensure_logged_in():
        return 1
    name = ensure_app()
    if not name:
        print("could not create the app")
        return 1
    if not ensure_volume(name):
        print("could not create the volume")
        return 1
    if not push_secrets():
        print("secrets failed -- refusing to deploy without COUNCIL_SECRET")
        return 1

    r = fly("deploy", "--remote-only", "-a", name)
    print((r.stdout + r.stderr).strip()[-2500:])
    if r.returncode != 0:
        return 1

    url = f"https://{name}.fly.dev"
    print(f"\ndeployed: {url}")
    check = run(["curl", "-s", "--max-time", "40", "-o", "/dev/null",
                 "-w", "%{http_code}", url + "/"])
    print(f"  GET / -> {check.stdout.strip()}")
    api = run(["curl", "-s", "--max-time", "40", url + "/api/auth-status"])
    print(f"  auth-status: {api.stdout.strip()[:220]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
