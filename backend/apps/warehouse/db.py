"""Connections to the `nexora_wh` warehouse database (outside Django's ORM).

Two login roles are used:
  * wh_loader   (WH_DATABASE_URL_LOADER):   owns the per-org schemas, creates and loads dataset tables.
  * wh_executor (WH_DATABASE_URL_EXECUTOR): NOINHERIT, no privileges of its own; every query runs after
                                            `SET LOCAL ROLE <org reader role>` inside a READ ONLY transaction.
"""

from __future__ import annotations

import psycopg
from django.conf import settings

from .exceptions import WarehouseUnavailable

CONNECT_TIMEOUT_S = 5
APPLICATION_NAME = "nexora"


def _connect(url: str, *, autocommit: bool) -> psycopg.Connection:
    try:
        return psycopg.connect(
            url,
            autocommit=autocommit,
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name=APPLICATION_NAME,
            prepare_threshold=None,
        )
    except psycopg.OperationalError as exc:
        raise WarehouseUnavailable() from exc


def loader_connection(*, autocommit: bool = False) -> psycopg.Connection:
    return _connect(settings.WH_DATABASE_URL_LOADER, autocommit=autocommit)


def executor_connection() -> psycopg.Connection:
    return _connect(settings.WH_DATABASE_URL_EXECUTOR, autocommit=False)
