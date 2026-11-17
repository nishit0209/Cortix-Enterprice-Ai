"""NEXORA base settings (Integrator-owned)."""

from pathlib import Path

import environ

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
REPO_DIR = BACKEND_DIR.parent

env = environ.Env()
for candidate in (REPO_DIR / ".env", BACKEND_DIR / ".env"):
    if candidate.exists():
        environ.Env.read_env(str(candidate), overwrite=False)
        break

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=["http://localhost:3000", "http://localhost:8080"])

# --- Apps: ALL planned NEXORA apps are pre-registered (shared file; agents never edit) -------------
NEXORA_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.orgs",
    "apps.governance",
    "apps.ai",
    "apps.connectors",
    "apps.datasets",
    "apps.warehouse",
    "apps.quality",
    "apps.lineage",
    "apps.pipelines",
    "apps.semantic",
    "apps.sqlguard",
    "apps.nl2sql",
    "apps.bi",
    "apps.documents",
    "apps.ml",
    "apps.customers",
    "apps.products",
    "apps.automation",
    "apps.platform_admin",
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    "django_otp",
    "django_otp.plugins.otp_totp",
    *NEXORA_APPS,
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.core.middleware.RequestIdMiddleware",
    "apps.core.tenancy.middleware.TenantContextClearMiddleware",
]

ROOT_URLCONF = "nexora.urls"
ASGI_APPLICATION = "nexora.asgi.application"
WSGI_APPLICATION = "nexora.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

# --- Databases ---------------------------------------------------------------------------------
# default: nexora_app role (RLS enforced). DJANGO_DB_ROLE=migrator switches default to the migrator
# role acting as nexora_owner (only for `manage.py migrate`). `ops`: BYPASSRLS, platform modules only.
_DB_ROLE = env("DJANGO_DB_ROLE", default="app")


def _db(url_var: str, default: str, *, owner: bool = False) -> dict:
    cfg = env.db_url(url_var, default=default)
    cfg["ENGINE"] = "django.db.backends.postgresql"
    cfg["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
    cfg["CONN_HEALTH_CHECKS"] = True
    options = {"prepare_threshold": None, "connect_timeout": env.int("DB_CONNECT_TIMEOUT", default=5)}
    if owner:
        options["options"] = "-c role=nexora_owner"
    cfg["OPTIONS"] = options
    return cfg


_APP_URL = "postgres://nexora_app:nexora_app_dev@localhost:5433/nexora_app"
_MIG_URL = "postgres://nexora_migrator:nexora_migrator_dev@localhost:5433/nexora_app"
_OPS_URL = "postgres://nexora_ops:nexora_ops_dev@localhost:5433/nexora_app"

DATABASES = {
    "default": (
        _db("DATABASE_URL_MIGRATOR", _MIG_URL, owner=True)
        if _DB_ROLE == "migrator"
        else _db("DATABASE_URL_APP", _APP_URL)
    ),
    "ops": _db("DATABASE_URL_OPS", _OPS_URL),
}
DATABASES["ops"]["TEST"] = {"MIRROR": "default"}
DATABASE_ROUTERS: list[str] = []
WH_DATABASE_URL_LOADER = env("WH_DATABASE_URL_LOADER", default="postgres://wh_loader:wh_loader_dev@localhost:5433/nexora_wh")
WH_DATABASE_URL_EXECUTOR = env(
    "WH_DATABASE_URL_EXECUTOR", default="postgres://wh_executor:wh_loader_dev@localhost:5433/nexora_wh"
)
# Test runs isolate warehouse objects per agent/test session with this schema prefix ("org_" in prod).
WH_SCHEMA_PREFIX = env("WH_SCHEMA_PREFIX", default="org_")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth / security ----------------------------------------------------------------------------
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # SPA reads it to send X-CSRFToken
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
API_KEY_PEPPER = env("API_KEY_PEPPER", default="dev-pepper-change-me")
NEXORA_KEK = env("NEXORA_KEK", default="ZGV2LW9ubHkta2V5LWNoYW5nZS1tZS0zMi1ieXRlcyE=")

# --- DRF -----------------------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.ApiKeyAuthentication",
        "apps.core.auth.CsrfEnforcedSessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultCursorPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.problem_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "600/min", "login": "10/min"},
}
SPECTACULAR_SETTINGS = {
    "TITLE": "NEXORA Enterprise AI API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# --- Redis / Celery -------------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6380/0")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 30
CELERY_TIMEZONE = "UTC"

# --- Object storage --------------------------------------------------------------------------------
S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default="http://localhost:9000")
S3_ACCESS_KEY = env("S3_ACCESS_KEY", default="nexora")
S3_SECRET_KEY = env("S3_SECRET_KEY", default="nexora_minio_dev")
S3_BUCKET = env("S3_BUCKET", default="nexora")
S3_REGION = env("S3_REGION", default="us-east-1")
MAX_UPLOAD_BYTES = env.int("MAX_UPLOAD_BYTES", default=200 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- Email -----------------------------------------------------------------------------------------
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Nexora <no-reply@nexora.local>")

# --- AI (Gemini ONLY) ----------------------------------------------------------------------------------
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_TIER = env("GEMINI_TIER", default="free")  # free | paid | vertex
GEMINI_MODEL_FAST = env("GEMINI_MODEL_FAST", default="gemini-3.5-flash-lite")
GEMINI_MODEL_REASONING = env("GEMINI_MODEL_REASONING", default="gemini-3.8-flash")
GEMINI_REQUEST_TIMEOUT_S = env.int("GEMINI_REQUEST_TIMEOUT_S", default=60)
AI_CONSENT_TEXT_VERSION = env("AI_CONSENT_TEXT_VERSION", default="2026-11-v1")
DEV_AGENT_RPD = env.int("DEV_AGENT_RPD", default=40)
EMBEDDING_PROVIDER = env("EMBEDDING_PROVIDER", default="local")
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="intfloat/multilingual-e5-base")
EMBEDDING_DIM = env.int("EMBEDDING_DIM", default=768)

# --- Connectors / SQL guard ---------------------------------------------------------------------------
ALLOW_PRIVATE_DB_HOSTS = env.bool("ALLOW_PRIVATE_DB_HOSTS", default=False)
NL2SQL_ROW_CAP = env.int("NL2SQL_ROW_CAP", default=1000)
NL2SQL_BYTE_CAP = env.int("NL2SQL_BYTE_CAP", default=5 * 1024 * 1024)
NL2SQL_TIMEOUT_S = env.int("NL2SQL_TIMEOUT_S", default=15)
NL2SQL_MAX_COST = env.float("NL2SQL_MAX_COST", default=5e6)

# --- I18N / static ------------------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BACKEND_DIR / "staticfiles"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
