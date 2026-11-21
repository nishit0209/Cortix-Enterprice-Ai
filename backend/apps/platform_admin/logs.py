"""Recent application logs for the platform console (`GET /api/v1/platform/logs`).

`DatabaseLogHandler` (installed on the root logger by PlatformAdminConfig.ready()) captures WARNING+ records into a
bounded in-memory queue; ONE daemon writer thread drains it in batches into `platform_admin.AppLogRecord` on the
`ops` alias. Logging therefore never blocks a request, never runs DB code inside an event loop, and a failing DB
only drops log copies (the console handler still has them). The table is a ring: it is trimmed to
PLATFORM_LOG_MAX_ROWS by the writer and by the periodic job `platform.trim_logs`.

Settings (optional; requested from the Integrator): PLATFORM_LOG_TO_DB (default: on, except under the test settings
or the eager task runner), PLATFORM_LOG_MAX_ROWS (default 5000), PLATFORM_LOG_LEVEL (default WARNING).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime

from django.conf import settings

OPS_ALIAS = "ops"
DEFAULT_MAX_ROWS = 5000
QUEUE_SIZE = 2000
BATCH = 200
TRIM_EVERY = 500
BACKOFF_S = 30.0
MAX_MESSAGE = 4000
# Never capture the DB layer (a failing log write would log again) or this module itself.
SKIP_PREFIXES = ("django.db.backends", "apps.platform_admin.logs", "nexora.platform.logs")

log = logging.getLogger("nexora.platform.logs")


def max_rows() -> int:
    return max(100, int(getattr(settings, "PLATFORM_LOG_MAX_ROWS", DEFAULT_MAX_ROWS) or DEFAULT_MAX_ROWS))


def enabled() -> bool:
    flag = getattr(settings, "PLATFORM_LOG_TO_DB", None)
    if flag is not None:
        return bool(flag)
    module = str(getattr(settings, "SETTINGS_MODULE", "") or "")
    return not module.endswith(".test") and str(getattr(settings, "TASK_RUNNER", "")) != "eager"


def record_to_row(record: logging.LogRecord) -> dict:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format string must not lose the record
        message = str(record.msg)
    from apps.core.middleware import request_id_var

    exc_type = ""
    if record.exc_info and record.exc_info[0] is not None:
        exc_type = getattr(record.exc_info[0], "__name__", "")[:120]
    return {
        "created_at": datetime.fromtimestamp(record.created, tz=UTC),
        "level": int(record.levelno),
        "level_name": record.levelname[:10],
        "logger": record.name[:120],
        "message": message[:MAX_MESSAGE],
        "where": f"{record.module}:{record.funcName}:{record.lineno}"[:200],
        "exc_type": exc_type,
        "request_id": (request_id_var.get() or "")[:64],
        "process": record.process,
    }


def write_rows(rows: list[dict], *, alias: str = OPS_ALIAS) -> int:
    from .models import AppLogRecord

    AppLogRecord.objects.using(alias).bulk_create([AppLogRecord(**r) for r in rows])
    return len(rows)


def trim(keep: int | None = None, *, alias: str = OPS_ALIAS) -> int:
    """Delete everything but the newest `keep` rows. Returns the number deleted."""
    from django.db import connections

    keep = max_rows() if keep is None else int(keep)
    with connections[alias].cursor() as cur:
        cur.execute("SELECT id FROM platform_admin_applogrecord ORDER BY id DESC OFFSET %s LIMIT 1", [keep])
        row = cur.fetchone()
        if row is None:
            return 0
        cur.execute("DELETE FROM platform_admin_applogrecord WHERE id <= %s", [row[0]])
        return cur.rowcount or 0


class DatabaseLogHandler(logging.Handler):
    def __init__(self, level: int = logging.WARNING, *, start_thread: bool = True):
        super().__init__(level)
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0
        self.written = 0
        self._start_thread = start_thread
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(SKIP_PREFIXES):
            return
        try:
            self.queue.put_nowait(record_to_row(record))
        except queue.Full:
            self.dropped += 1
            return
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
            return
        if self._start_thread:
            self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="nexora-log-writer", daemon=True)
                self._thread.start()

    def drain(self, limit: int = BATCH) -> list[dict]:
        rows: list[dict] = []
        while len(rows) < limit:
            try:
                rows.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def flush_now(self, *, alias: str = OPS_ALIAS) -> int:
        """Write everything queued (synchronously, in the caller's thread). Used by the writer and by tests."""
        total = 0
        while True:
            rows = self.drain()
            if not rows:
                return total
            total += self._write(rows, alias=alias)

    def _write(self, rows: list[dict], *, alias: str = OPS_ALIAS) -> int:
        n = write_rows(rows, alias=alias)
        before = self.written
        self.written += n
        if self.written // TRIM_EVERY != before // TRIM_EVERY:
            trim(alias=alias)
        return n

    def _run(self) -> None:
        from django.db import close_old_connections

        while not self._stop.is_set():
            try:
                first = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            rows = [first, *self.drain(BATCH - 1)]
            try:
                close_old_connections()
                self._write(rows)
                self.flush_now()
            except Exception:  # noqa: BLE001 - the DB is unavailable: drop this batch and back off
                self.dropped += len(rows) + len(self.drain(QUEUE_SIZE))
                self._stop.wait(BACKOFF_S)
            finally:
                close_old_connections()

    def close(self) -> None:
        self._stop.set()
        super().close()


_installed: DatabaseLogHandler | None = None


def install() -> DatabaseLogHandler | None:
    """Attach the handler to the root logger once per process (no-op when disabled)."""
    global _installed
    if _installed is not None or not enabled():
        return _installed
    level = logging.getLevelName(str(getattr(settings, "PLATFORM_LOG_LEVEL", "WARNING")).upper())
    handler = DatabaseLogHandler(level if isinstance(level, int) else logging.WARNING)
    logging.getLogger().addHandler(handler)
    _installed = handler
    log.debug("platform log capture installed (pid %s)", os.getpid())
    return handler


def installed() -> DatabaseLogHandler | None:
    return _installed


def trim_job() -> int:
    """The periodic job `platform.trim_logs`."""
    started = time.monotonic()
    deleted = trim()
    if deleted:
        log.info("trimmed %s log records in %.2fs", deleted, time.monotonic() - started)
    return deleted
