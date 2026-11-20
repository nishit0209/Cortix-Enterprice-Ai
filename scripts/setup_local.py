"""ONE-COMMAND local setup for NEXORA (PostgreSQL only, no Docker).

1. Fill these lines at the top of the repo-root .env:
       POSTGRES_HOST=localhost
       POSTGRES_PORT=5432
       POSTGRES_USER=postgres          (your PostgreSQL superuser)
       POSTGRES_PASSWORD=...           (its password)
       NEXORA_DB_NAME=nexora           (database name; the warehouse DB becomes <name>_wh)
       GEMINI_API_KEY=...              (optional now; needed for live AI answers)
2. Run from the repo root:
       .venv\\Scripts\\python.exe scripts\\setup_local.py

It does everything, and is safe to re-run:
- creates the app + warehouse databases and NEXORA's internal least-privilege users (the app never runs as the superuser, because a superuser
  bypasses Row-Level Security);
- writes their connection URLs into .env;
- creates the cache table and all tables (migrate);
- verifies the warehouse;
- checks the Gemini key when one is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap_local_db as boot


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def manage(*args: str, role: str | None = None) -> int:
    env = {k: v for k, v in os.environ.items() if k not in {"DJANGO_DB_ROLE"}}
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("DJANGO_SETTINGS_MODULE", "nexora.settings.dev")
    if role:
        env["DJANGO_DB_ROLE"] = role
    return subprocess.call([sys.executable, "manage.py", *args], cwd=str(BACKEND), env=env)


def main() -> int:
    env = boot.read_env(boot.ENV_PATH)
    host = env.get("POSTGRES_HOST") or "localhost"
    port = env.get("POSTGRES_PORT") or "5432"
    user = env.get("POSTGRES_USER") or "postgres"
    password = env.get("POSTGRES_PASSWORD", "").strip()
    if len(password) >= 2 and password[0] == password[-1] and password[0] in "'\"":
        password = password[1:-1]  # allow POSTGRES_PASSWORD="..." or '...'
    name = (env.get("NEXORA_DB_NAME") or "nexora").strip().lower()

    if not password:
        print("POSTGRES_PASSWORD is empty in .env.\n"
              "Open .env, fill POSTGRES_PASSWORD with your PostgreSQL 'postgres' user's password, save, and run this again.")
        return 2
    if not name.replace("_", "").isalnum() or not name[0].isalpha() or len(name) > 50:
        print(f"NEXORA_DB_NAME '{name}' is not valid: use letters, digits and _ only, starting with a letter (e.g. nexora).")
        return 2
    app_db, wh_db = name, f"{name}_wh"

    step(f"1/4 Databases '{app_db}' + '{wh_db}' and NEXORA users")
    rc = boot.main(["--host", host, "--port", str(port), "--superuser", user, "--password", password,
                    "--app-db", app_db, "--wh-db", wh_db, "--no-next"])
    if rc != 0:
        print("\nDatabase setup failed (see the message above). Check POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_PORT in .env.")
        return rc

    step("2/4 Tables (cache table + migrate)")
    if manage("createcachetable", role="migrator") != 0 or manage("migrate", "--noinput", role="migrator") != 0:
        print("\nCreating tables failed (see above). Send the error to Claude.")
        return 1

    step("3/4 Warehouse check")
    if manage("check_warehouse", "--provision-test") != 0:
        print("\nWarehouse check failed (see above). Send the error to Claude.")
        return 1

    step("4/4 Gemini key")
    if boot.read_env(boot.ENV_PATH).get("GEMINI_API_KEY", "").strip():
        if manage("verify_gemini") != 0:
            print("\nThe Gemini key check failed (see above). The app still works; AI answers need a valid key.")
    else:
        print("GEMINI_API_KEY is empty: skipped. Add it to .env later and run: cd backend; ..\\.venv\\Scripts\\python.exe manage.py verify_gemini")

    print("\nDONE. Local database is ready. Tell Claude: \"DB done\".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
