"""Bootstrap a LOCAL PostgreSQL (no Docker) for NEXORA.

Creates, idempotently:
- the roles: nexora_owner, nexora_migrator, nexora_app, nexora_ops, nexora_definer, wh_loader and wh_executor;
- the databases nexora_app and nexora_wh;
- the extensions (citext, pgcrypto, and vector when pgvector is installed) and the privileges.

It then writes the matching connection URLs into the repo-root .env.

Usage (PowerShell, from the repo root):
    $env:PGPASSWORD = "<your postgres superuser password>"
    .venv\\Scripts\\python.exe scripts\\bootstrap_local_db.py --host localhost --port 5432 --superuser postgres

Passwords for the NEXORA roles come from .env (NEXORA_APP_PASSWORD etc.) when present. Otherwise strong random
ones are generated and saved to .env. Re-running the script is safe: existing roles get their password
re-set, and existing databases are kept as they are (nothing is dropped).

pgvector: if the `vector` extension is not installed in this PostgreSQL, the script sets VECTOR_BACKEND=array.
Embeddings are then stored as float arrays and ranked in Python, so the app still works fully.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

ROLE_PASSWORD_KEYS = {
    "nexora_migrator": "NEXORA_MIGRATOR_PASSWORD",
    "nexora_app": "NEXORA_APP_PASSWORD",
    "nexora_ops": "NEXORA_OPS_PASSWORD",
    "wh_loader": "WH_LOADER_PASSWORD",
    "wh_executor": "WH_EXECUTOR_PASSWORD",
}
WEAK_DEFAULTS = {"nexora_app_dev", "nexora_migrator_dev", "nexora_ops_dev", "wh_loader_dev", ""}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in updates if k not in seen]
    if missing:
        out.append("")
        out.append("# --- written by scripts/bootstrap_local_db.py ---")
        out.extend(f"{k}={updates[k]}" for k in missing)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def connect(args, dbname: str) -> psycopg.Connection:
    return psycopg.connect(
        host=args.host, port=args.port, user=args.superuser, password=args.password, dbname=dbname,
        autocommit=True, connect_timeout=10,
    )


def role_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def ensure_role(cur, name: str, attrs: str, password: str | None) -> None:
    ident = sql.Identifier(name)
    if role_exists(cur, name):
        cur.execute(sql.SQL("ALTER ROLE {} " + attrs).format(ident))
        print(f"  role {name}: exists (attributes refreshed)")
    else:
        cur.execute(sql.SQL("CREATE ROLE {} " + attrs).format(ident))
        print(f"  role {name}: created")
    if password is not None:
        cur.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(password)))


def grant_membership(cur, role: str, member: str) -> None:
    cur.execute(
        "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
        "WHERE r.rolname=%s AND u.rolname=%s", (role, member),
    )
    if cur.fetchone() is None:
        cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(member)))


def try_extension(cur, ext: str) -> bool:
    cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", (ext,))
    if cur.fetchone() is None:
        return False
    cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--superuser", default="postgres")
    p.add_argument("--password", default=os.environ.get("PGPASSWORD"), help="superuser password (or set PGPASSWORD)")
    p.add_argument("--app-db", default="nexora_app")
    p.add_argument("--wh-db", default="nexora_wh")
    p.add_argument("--no-env", action="store_true", help="do not modify .env (print the lines instead)")
    args = p.parse_args()
    if not args.password:
        args.password = getpass.getpass(f"Password for PostgreSQL superuser '{args.superuser}': ")

    env = read_env(ENV_PATH)
    passwords: dict[str, str] = {}
    for role, key in ROLE_PASSWORD_KEYS.items():
        current = env.get(key, "")
        if role == "wh_executor" and not current:
            current = env.get("WH_LOADER_PASSWORD", "")  # legacy: executor shared the loader password
        passwords[role] = current if current not in WEAK_DEFAULTS else secrets.token_urlsafe(24)
    if args.no_env and any(env.get(key, "") != passwords[role] for role, key in ROLE_PASSWORD_KEYS.items()):
        # Fail before touching the server: a role whose new password was never saved would be locked out.
        print("--no-env needs every role password already set in .env (NEXORA_APP_PASSWORD, NEXORA_MIGRATOR_PASSWORD, "
              "NEXORA_OPS_PASSWORD, WH_LOADER_PASSWORD, WH_EXECUTOR_PASSWORD, none of them a dev default). "
              "Set them there or run without --no-env so the script can save generated ones.", file=sys.stderr)
        return 2

    print(f"Connecting to {args.host}:{args.port} as {args.superuser} ...")
    try:
        conn = connect(args, "postgres")
    except psycopg.OperationalError as exc:
        print(f"ERROR: cannot connect: {exc}", file=sys.stderr)
        return 2

    with conn, conn.cursor() as cur:
        cur.execute("SHOW server_version")
        print(f"PostgreSQL {cur.fetchone()[0]}")
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            print("ERROR: --superuser must be a PostgreSQL superuser (needed to create BYPASSRLS roles).", file=sys.stderr)
            return 2
        print("Roles:")
        ensure_role(cur, "nexora_owner", "NOLOGIN CREATEDB", None)
        ensure_role(cur, "nexora_migrator", "LOGIN CREATEDB", passwords["nexora_migrator"])
        grant_membership(cur, "nexora_owner", "nexora_migrator")
        ensure_role(cur, "nexora_app", "LOGIN NOBYPASSRLS", passwords["nexora_app"])
        ensure_role(cur, "nexora_ops", "LOGIN BYPASSRLS", passwords["nexora_ops"])
        ensure_role(cur, "nexora_definer", "NOLOGIN BYPASSRLS", None)
        grant_membership(cur, "nexora_definer", "nexora_migrator")
        grant_membership(cur, "nexora_definer", "nexora_owner")
        ensure_role(cur, "wh_loader", "LOGIN CREATEROLE", passwords["wh_loader"])
        ensure_role(cur, "wh_executor", "LOGIN NOINHERIT", passwords["wh_executor"])
        cur.execute("ALTER ROLE nexora_migrator SET statement_timeout = 0")

    print("Extensions in template1 (inherited by new DBs, incl. test DBs):")
    with connect(args, "template1") as conn, conn.cursor() as cur:
        has_vector = try_extension(cur, "vector")
        for ext in ("citext", "pgcrypto"):
            if not try_extension(cur, ext):
                print(f"ERROR: required extension {ext} is not available in this PostgreSQL.", file=sys.stderr)
                return 2
        print(f"  citext, pgcrypto: ok; vector (pgvector): {'ok' if has_vector else 'NOT INSTALLED -> VECTOR_BACKEND=array'}")
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute("GRANT USAGE, CREATE ON SCHEMA public TO nexora_owner, nexora_definer")
        cur.execute("GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops")

    print("Databases:")
    with connect(args, "postgres") as conn, conn.cursor() as cur:
        for db, owner in ((args.app_db, "nexora_owner"), (args.wh_db, "wh_loader")):
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if cur.fetchone():
                print(f"  {db}: exists (kept)")
            else:
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template1").format(
                    sql.Identifier(db), sql.Identifier(owner)))
                print(f"  {db}: created (owner {owner})")

    # Databases that already existed did not inherit template1's settings: apply them directly.
    with connect(args, args.app_db) as conn, conn.cursor() as cur:
        if has_vector:
            try_extension(cur, "vector")
        try_extension(cur, "citext")
        try_extension(cur, "pgcrypto")
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute("GRANT USAGE, CREATE ON SCHEMA public TO nexora_owner, nexora_definer")
        cur.execute("GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops")
    with connect(args, args.wh_db) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(args.wh_db)))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO wh_loader, wh_executor").format(sql.Identifier(args.wh_db)))
        cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")

    hp = f"{args.host}:{args.port}"
    updates = {
        "NEXORA_APP_PASSWORD": passwords["nexora_app"],
        "NEXORA_MIGRATOR_PASSWORD": passwords["nexora_migrator"],
        "NEXORA_OPS_PASSWORD": passwords["nexora_ops"],
        "WH_LOADER_PASSWORD": passwords["wh_loader"],
        "WH_EXECUTOR_PASSWORD": passwords["wh_executor"],
        "DATABASE_URL_APP": f"postgres://nexora_app:{passwords['nexora_app']}@{hp}/{args.app_db}",
        "DATABASE_URL_MIGRATOR": f"postgres://nexora_migrator:{passwords['nexora_migrator']}@{hp}/{args.app_db}",
        "DATABASE_URL_OPS": f"postgres://nexora_ops:{passwords['nexora_ops']}@{hp}/{args.app_db}",
        "WH_DATABASE_URL_LOADER": f"postgres://wh_loader:{passwords['wh_loader']}@{hp}/{args.wh_db}",
        "WH_DATABASE_URL_EXECUTOR": f"postgres://wh_executor:{passwords['wh_executor']}@{hp}/{args.wh_db}",
        "VECTOR_BACKEND": "pgvector" if has_vector else "array",
    }
    if args.no_env:
        print("\nAdd these lines to .env:")
        for k, v in updates.items():
            print(f"{k}={v}")
    else:
        write_env(ENV_PATH, updates)
        print(f"\nUpdated {ENV_PATH} (DB URLs, role passwords, VECTOR_BACKEND).")
    print("\nNext:  cd backend;  $env:DJANGO_DB_ROLE='migrator';  ..\\.venv\\Scripts\\python.exe manage.py migrate;"
          "  Remove-Item Env:DJANGO_DB_ROLE;  ..\\.venv\\Scripts\\python.exe manage.py createcachetable;"
          "  ..\\.venv\\Scripts\\python.exe manage.py seed_demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
