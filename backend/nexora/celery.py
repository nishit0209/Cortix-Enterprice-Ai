"""Celery application. Queues are pre-declared so agents never edit this file mid-wave."""

import os

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nexora.settings.dev")

app = Celery("nexora")
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.task_queues = [
    Queue("default"),
    Queue("ingest"),     # file parsing, profiling, pipelines, warehouse loads
    Queue("query_wh"),   # guarded SQL on nexora_wh
    Queue("query_ext"),  # guarded SQL on customer external DBs (egress-isolated in prod)
    Queue("llm"),        # every Gemini call (global limiter)
    Queue("ml"),         # embeddings, OCR, training, batch prediction
]
app.conf.task_default_queue = "default"
app.conf.task_routes = {
    "apps.datasets.*": {"queue": "ingest"},
    "apps.pipelines.*": {"queue": "ingest"},
    "apps.quality.*": {"queue": "ingest"},
    "apps.warehouse.*": {"queue": "ingest"},
    "apps.connectors.*": {"queue": "ingest"},
    "apps.customers.*": {"queue": "ingest"},
    "apps.products.*": {"queue": "ingest"},
    "apps.sqlguard.tasks.run_query_wh": {"queue": "query_wh"},
    "apps.sqlguard.tasks.run_query_ext": {"queue": "query_ext"},
    "apps.ai.*": {"queue": "llm"},
    "apps.nl2sql.*": {"queue": "llm"},
    "apps.bi.tasks.*": {"queue": "llm"},
    "apps.documents.*": {"queue": "ml"},
    "apps.ml.*": {"queue": "ml"},
}
app.autodiscover_tasks()
