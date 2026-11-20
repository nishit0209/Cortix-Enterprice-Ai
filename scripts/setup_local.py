"""ONE-COMMAND local setup for NEXORA (PostgreSQL only, no Docker).

1. In the repo-root .env, fill:
       POSTGRES_HOST=localhost
       POSTGRES_PORT=5432
       POSTGRES_USER=postgres            (your PostgreSQL superuser)
       POSTGRES_PASSWORD=...             (its password)
       APP_DB_NAME=nexora_app            (database 1: users, orgs, settings, chats, dashboards, audit)
       WAREHOUSE_DB_NAME=nexora_wh       (database 2: the uploaded business data)
   You may create the two databases yourself first (for example in pgAdmin), or let this script create them.
2. Run from the repo root:
       .venv\\Scripts\\python.exe scripts\\setup_local.py

It does everything, and is safe to re-run (nothing is ever dropped):
- creates NEXORA's internal least-privilege users (the app never runs as the superuser, because a superuser bypasses
  Row-Level Security); their passwords are derived from SECRET_KEY, so they are not stored in .env;
- creates both databases if missing, and fixes owner and permissions if you created them yourself;
- creates the cache table and all tables (migrate);
- checks the warehouse;
- checks the Gemini key when one is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import psycopg

# Importing the settings loads .env into os.environ exactly as the app does, so the URLs and passwords match.
from nexora.dbconfig import role_password
from nexora.settings import base as settings
from psycopg import sql

# role -> attributes. nexora_owner owns the app tables (migrator acts as it); nexora_definer owns the
# SECURITY DEFINER lookup functions; wh_executor runs read-only queries through per-org reader roles.
ROLES = [
    ("nexora_owner", "NOLOGIN CREATEDB"),
    ("nexora_definer", "NOLOGIN BYPASSRLS"),
    ("nexora_migrator", "LOGIN CREATEDB"),
    ("nexora_app", "LOGIN NOBYPASSRLS"),
    ("nexora_ops", "LOGIN BYPASSRLS"),
    ("wh_loader", "LOGIN CREATEROLE"),
    ("wh_executor", "LOGIN NOINHERIT"),
]
MEMBERSHIPS = [("nexora_owner", "nexora_migrator"), ("nexora_definer", "nexora_migrator"),
               ("nexora_definer", "nexora_owner")]


def say(msg: str) -> None:
    print(msg, flush=True)


def connect(dbname: str, *, host, port, user, password):
    return psycopg.connect(host=host, port=port, user=user, password=password, dbname=dbname,
                           autocommit=True, connect_timeout=10)


def exists(cur, query: str, *params) -> bool:
    cur.execute(query, params)
    return cur.fetchone() is not None


def extension(cur, name: str) -> bool:
    if not exists(cur, "SELECT 1 FROM pg_available_extensions WHERE name = %s", name):
        return False
    cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(name)))
    return True


def setup_database(host, port, user, password, app_db, wh_db, secret_key) -> int:
    ident = sql.Identifier
    try:
        conn = connect("postgres", host=host, port=port, user=user, password=password)
    except psycopg.OperationalError as exc:
        say(f"ERROR: cannot connect to PostgreSQL at {host}:{port} as '{user}':\n  {exc}\n"
            "Check POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD in .env.")
        return 2
    with conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            say(f"ERROR: POSTGRES_USER '{user}' must be a superuser (for example 'postgres').")
            return 2
        cur.execute("SHOW server_version")
        say(f"Connected: PostgreSQL {cur.fetchone()[0]}")
        for role, attrs in ROLES:
            verb = "ALTER" if exists(cur, "SELECT 1 FROM pg_roles WHERE rolname = %s", role) else "CREATE"
            cur.execute(sql.SQL(f"{verb} ROLE {{}} {attrs}").format(ident(role)))
            if "NOLOGIN" not in attrs:
                cur.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    ident(role), sql.Literal(role_password(role, secret_key))))
        for group, member in MEMBERSHIPS:
            if not exists(cur, "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
                               "JOIN pg_roles u ON u.oid=m.member WHERE r.rolname=%s AND u.rolname=%s", group, member):
                cur.execute(sql.SQL("GRANT {} TO {}").format(ident(group), ident(member)))
        cur.execute("ALTER ROLE nexora_migrator SET statement_timeout = 0")
        say("Users: ok (nexora_owner, nexora_definer, nexora_migrator, nexora_app, nexora_ops, wh_loader, wh_executor)")

    # Extensions in template1, so new databases (including test databases) inherit them.
    with connect("template1", host=host, port=port, user=user, password=password) as conn, conn.cursor() as cur:
        has_vector = extension(cur, "vector")
        for ext in ("citext", "pgcrypto"):
            if not extension(cur, ext):
                say(f"ERROR: the PostgreSQL extension '{ext}' is not available.")
                return 2
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute("GRANT USAGE, CREATE ON SCHEMA public TO nexora_owner, nexora_definer")
        cur.execute("GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops")

    with connect("postgres", host=host, port=port, user=user, password=password) as conn, conn.cursor() as cur:
        for db, owner in ((app_db, "nexora_owner"), (wh_db, "wh_loader")):
            if exists(cur, "SELECT 1 FROM pg_database WHERE datname = %s", db):
                cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(ident(db), ident(owner)))
                say(f"Database {db}: already exists (owner set to {owner})")
            else:
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template1").format(ident(db), ident(owner)))
                say(f"Database {db}: created")

    # Apply the same settings directly, in case the databases were created by hand (not from template1).
    with connect(app_db, host=host, port=port, user=user, password=password) as conn, conn.cursor() as cur:
        for ext in (("vector",) if has_vector else ()) + ("citext", "pgcrypto"):
            extension(cur, ext)
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute("GRANT USAGE, CREATE ON SCHEMA public TO nexora_owner, nexora_definer")
        cur.execute("GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops")
    with connect(wh_db, host=host, port=port, user=user, password=password) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident(wh_db)))
        cur.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO wh_loader").format(ident(wh_db)))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO wh_executor").format(ident(wh_db)))
        cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    say(f"Vector search: {'pgvector' if has_vector else 'built-in fallback (pgvector not installed; fine)'}")
    return 0


def manage(*args: str, role: str | None = None) -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.setdefault("DJANGO_SETTINGS_MODULE", "nexora.settings.dev")
    env.pop("DJANGO_DB_ROLE", None)
    if role:
        env["DJANGO_DB_ROLE"] = role
    return subprocess.call([sys.executable, "manage.py", *args], cwd=str(BACKEND), env=env)


def valid_name(name: str) -> bool:
    return bool(name) and name.isascii() and name[0].isalpha() and name.replace("_", "").isalnum() and len(name) <= 50


def main() -> int:
    host, port = settings.POSTGRES_HOST, settings.POSTGRES_PORT
    user = os.environ.get("POSTGRES_USER", "postgres").strip() or "postgres"
    password = os.environ.get("POSTGRES_PASSWORD", "")
    app_db, wh_db = settings.APP_DB_NAME.strip().lower(), settings.WAREHOUSE_DB_NAME.strip().lower()
    if not password:
        say("POSTGRES_PASSWORD is empty in .env: write your PostgreSQL password there, save, and run this again.")
        return 2
    for label, name in (("APP_DB_NAME", app_db), ("WAREHOUSE_DB_NAME", wh_db)):
        if not valid_name(name):
            say(f"{label} '{name}' is not valid: use letters, digits and _ (start with a letter), e.g. nexora_app.")
            return 2
    if app_db != settings.APP_DB_NAME or wh_db != settings.WAREHOUSE_DB_NAME:
        say("Write APP_DB_NAME and WAREHOUSE_DB_NAME in lowercase in .env (e.g. nexora_app), then run this again.")
        return 2
    if app_db == wh_db:
        say("APP_DB_NAME and WAREHOUSE_DB_NAME must be two DIFFERENT databases.")
        return 2

    say("\n=== 1/4 Databases and users ===")
    rc = setup_database(host, port, user, password, app_db, wh_db, settings.SECRET_KEY)
    if rc:
        return rc
    say("\n=== 2/4 Tables ===")
    if manage("createcachetable", role="migrator") or manage("migrate", "--noinput", role="migrator"):
        say("\nCreating tables failed (see above). Send the error to Claude.")
        return 1
    say("\n=== 3/4 Warehouse check ===")
    if manage("check_warehouse", "--provision-test"):
        say("\nWarehouse check failed (see above). Send the error to Claude.")
        return 1
    say("\n=== 4/4 Gemini key ===")
    if os.environ.get("GEMINI_API_KEY", "").strip():
        if manage("verify_gemini"):
            say("The Gemini key check failed (see above). The app still works; AI answers need a valid key.")
    else:
        say("GEMINI_API_KEY is empty: skipped (add it to .env later).")
    say('\nDONE. The local database is ready. Tell Claude: "DB done".')
    return 0


if __name__ == "__main__":
    sys.exit(main())
