"""Test settings.

The test database is CREATED and MIGRATED by the migrator role (acting as nexora_owner), then
backend/conftest.py switches the `default` connection to the nexora_app role so every test runs
under real Row-Level Security. Parallel agents isolate themselves with PYTEST_DB_SUFFIX.
"""

from .base import *  # noqa: F403
from .base import _MIG_URL, _db, env

_suffix = env("PYTEST_DB_SUFFIX", default="main")

DATABASES = {
    "default": _db("DATABASE_URL_MIGRATOR", _MIG_URL, owner=True),
    "ops": _db("DATABASE_URL_OPS", "postgres://nexora_ops:nexora_ops_dev@localhost:5433/nexora_app"),
}
DATABASES["default"]["CONN_MAX_AGE"] = 0
DATABASES["default"]["TEST"] = {"NAME": f"test_nexora_{_suffix}"}
DATABASES["ops"]["CONN_MAX_AGE"] = 0
DATABASES["ops"]["TEST"] = {"MIRROR": "default"}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
GEMINI_API_KEY = ""  # tests never call Gemini live; use apps.ai fakes / cassettes
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}  # noqa: F405
ALLOW_PRIVATE_DB_HOSTS = True
# nexora_wh is a real (non-test) database: isolate test schemas per agent/session
WH_SCHEMA_PREFIX = f"t_{_suffix}_"
