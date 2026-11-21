"""Production settings. Use with DJANGO_SETTINGS_MODULE=nexora.settings.prod.

Fails fast on insecure configuration. Expects TLS to be terminated by a reverse proxy (Caddy/nginx) that sets
X-Forwarded-Proto, and the frontend and API to be served from the same origin (see docs/DEPLOYMENT_ARCHITECTURE.md).
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False

# --- Refuse to start with development secrets -------------------------------------------------------
_DEV_DEFAULTS = {
    "SECRET_KEY": ("dev-insecure-change-me", "dev-insecure-change-me-0123456789abcdef"),
    "API_KEY_PEPPER": ("dev-pepper-change-me",),
    "NEXORA_KEK": ("ZGV2LW9ubHkta2V5LWNoYW5nZS1tZS0zMi1ieXRlcyE=",),
}
for _name, _bad in _DEV_DEFAULTS.items():
    _value = globals()[_name]
    if not _value or _value in _bad or (_name == "SECRET_KEY" and len(_value) < 50):
        raise ImproperlyConfigured(
            f"{_name} is missing or a development default. Set a strong random value in the environment "
            "(see docs/RUN_GUIDE.md, 'Production checklist').")
if not env.list("ALLOWED_HOSTS", default=[]):
    raise ImproperlyConfigured("ALLOWED_HOSTS must list the production host name(s).")
if not env.list("CSRF_TRUSTED_ORIGINS", default=[]):
    raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must list the production origin(s), e.g. https://app.example.com.")

# --- HTTPS / cookies / headers ----------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_NAME = "__Host-nexora_session"
CSRF_COOKIE_NAME = "__Host-nexora_csrf"
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# --- API: JSON only (no browsable API in production) ---------------------------------------------------
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]}  # noqa: F405

# --- Database connections: persistent with health checks ---------------------------------------------------
for _alias in DATABASES:  # noqa: F405
    DATABASES[_alias]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=300)  # noqa: F405

# --- Structured JSON logs (one line per record; ship them to any log collector) -----------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "nexora.logjson.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {"django.security": {"level": "WARNING", "propagate": True}},
}
