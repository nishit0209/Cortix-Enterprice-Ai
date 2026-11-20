"""Database connection config from a few simple .env values (Integrator-owned).

The user sets only: POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD (superuser, used ONLY by
scripts/setup_local.py), APP_DB_NAME and WAREHOUSE_DB_NAME.

At runtime the app never connects as the superuser (a superuser bypasses Row-Level Security). It uses NEXORA's
internal least-privilege roles. Their passwords are DERIVED from SECRET_KEY, so they never have to be written
in .env. scripts/setup_local.py creates the roles with exactly these passwords. If SECRET_KEY changes, re-run
the setup (it resets the role passwords).

Explicit DATABASE_URL_* / WH_DATABASE_URL_* environment variables still override everything (Docker or production).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

# role -> database it connects to ("app" or "wh")
LOGIN_ROLES = {
    "nexora_migrator": "app",
    "nexora_app": "app",
    "nexora_ops": "app",
    "wh_loader": "wh",
    "wh_executor": "wh",
}


def role_password(role: str, secret_key: str) -> str:
    digest = hmac.new(secret_key.encode(), f"nexora-db-role:{role}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]  # URL-safe: [A-Za-z0-9_-]


def role_url(role: str, *, host: str, port: int | str, app_db: str, wh_db: str, secret_key: str) -> str:
    db = app_db if LOGIN_ROLES[role] == "app" else wh_db
    return f"postgres://{role}:{role_password(role, secret_key)}@{host}:{port}/{db}"
