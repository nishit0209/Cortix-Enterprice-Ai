"""Global pytest setup (Integrator-owned).

1. The test DB is created + migrated by the migrator role (acting as nexora_owner) — see settings.test.
2. Then the `default` connection is switched to the nexora_app role so ALL tests run under real
   Row-Level Security, exactly like production. Teardown switches back to drop the DB.
"""

from __future__ import annotations

import os

import pytest
from django.conf import settings

pytest_plugins = ["testing.fixtures"]


def _app_credentials() -> tuple[str, str]:
    import environ

    url = os.environ.get("DATABASE_URL_APP", "postgres://nexora_app:nexora_app_dev@localhost:5433/nexora_app")
    cfg = environ.Env.db_url_config(url)
    return cfg["USER"], cfg["PASSWORD"]


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):  # noqa: F811 - wraps pytest-django's fixture
    from django.db import connections

    conn = connections["default"]
    owner_user = conn.settings_dict["USER"]
    owner_pw = conn.settings_dict["PASSWORD"]
    owner_opts = dict(conn.settings_dict.get("OPTIONS", {}))
    with django_db_blocker.unblock():
        conn.close()
        user, pw = _app_credentials()
        conn.settings_dict["USER"] = user
        conn.settings_dict["PASSWORD"] = pw
        conn.settings_dict["OPTIONS"] = {k: v for k, v in owner_opts.items() if k != "options"}
    yield
    with django_db_blocker.unblock():
        conn.close()
        conn.settings_dict["USER"] = owner_user
        conn.settings_dict["PASSWORD"] = owner_pw
        conn.settings_dict["OPTIONS"] = owner_opts


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


def pytest_report_header(config):
    return f"nexora: test DB suffix={os.environ.get('PYTEST_DB_SUFFIX', 'main')} settings={settings.SETTINGS_MODULE}"
