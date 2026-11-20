"""Background re-embedding of rows stored while the local embedding model was unavailable.

Apps that store embeddings (documents chunks, the nl2sql semantic cache) register a `Reembedder` from their
AppConfig.ready() (apps.ai is a lower layer and never imports them). `enqueue_reembed_pending()` is registered as
the periodic job "ai.reembed_pending" (every 600 s) with `apps.core.scheduler` when that module exists. It only
hands the run to the task runner (`apps.ai.tasks.reembed_pending`, an ops task; a daemon thread when that base is
missing), so the download and the CPU encoding never stall the scheduler tick that dispatches every other job.

One run (`run_reembed_pending()`):
  1. each reembedder lists the orgs that have pending rows (ids only, cross-org scan on the `ops` alias);
  2. nothing pending -> return immediately (no model load);
  3. the model must be available; if it is not cached yet and EMBEDDING_AUTO_DOWNLOAD is true, it is downloaded
     here (background only - requests never download, and never wait for it);
  4. per org, each reembedder embeds up to `batch` rows in slices of COMMIT_EVERY, each slice in its own
     `tenant_context(org)` transaction (committed as it goes; a failure loses one slice, not the org's run).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from django.db import DatabaseError, connections

from .embeddings import EmbeddingUnavailable, LocalEmbedder, auto_download_allowed, get_embedder

log = logging.getLogger("nexora.ai")

PERIODIC_NAME = "ai.reembed_pending"
PERIODIC_EVERY_S = 600
DEFAULT_BATCH = 500
COMMIT_EVERY = 64
SCAN_LIMIT = 200


@dataclass(frozen=True)
class Reembedder:
    name: str
    # SQL returning DISTINCT org_id values with pending rows (run on the ops alias, ids only).
    scan_sql: str
    # run(embedder, limit) -> number of rows embedded; called inside tenant_context(org).
    run: Callable[[object, int], int]


_registry: dict[str, Reembedder] = {}
_run_lock = threading.Lock()


def register_reembedder(reembedder: Reembedder) -> None:
    _registry[reembedder.name] = reembedder


def registered() -> list[Reembedder]:
    return list(_registry.values())


def scan_orgs(reembedder: Reembedder, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[UUID]:
    try:
        with connections[using].cursor() as cur:
            cur.execute(f"{reembedder.scan_sql} LIMIT %s", [int(limit)])
            return [UUID(str(r[0])) for r in cur.fetchall()]
    except DatabaseError:
        log.warning("reembed: pending scan for %s failed", reembedder.name, exc_info=True)
        return []


def _ready_embedder(*, allow_download: bool):
    """The embedder when it can embed now; downloads the local model first when allowed. None otherwise."""
    embedder = get_embedder()
    if getattr(embedder, "available", True):
        return embedder
    if allow_download and isinstance(embedder, LocalEmbedder):
        try:
            log.info("reembed: downloading embedding model %s", embedder.model_name)
            embedder.download()
            return embedder
        except EmbeddingUnavailable as exc:
            log.warning("reembed: embedding model download failed (%s)", exc.reason)
    return None


def run_reembed_pending(*_args, using: str = "ops", batch: int = DEFAULT_BATCH,
                        allow_download: bool | None = None, **_kwargs) -> dict:
    """Periodic job body. Never raises (one broken org or app must not stop the others)."""
    from apps.core.tenancy import tenant_context

    if not _run_lock.acquire(blocking=False):
        return {"skipped": "already_running"}
    try:
        work = [(r, scan_orgs(r, using=using)) for r in registered()]
        work = [(r, orgs) for r, orgs in work if orgs]
        if not work:
            return {"pending": 0}
        allow = auto_download_allowed() if allow_download is None else allow_download
        embedder = _ready_embedder(allow_download=allow)
        if embedder is None:
            return {"skipped": "model_unavailable", "pending_orgs": sum(len(o) for _, o in work)}
        out: dict[str, int] = {}
        for reembedder, orgs in work:
            done = 0
            for org_id in orgs:
                try:
                    done += _run_org(reembedder, embedder, org_id, batch, tenant_context)
                except EmbeddingUnavailable as exc:
                    log.warning("reembed: model became unavailable (%s)", exc.reason)
                    out[reembedder.name] = done
                    return {**out, "stopped": "model_unavailable"}
                except Exception:  # noqa: BLE001
                    log.exception("reembed: %s failed for org %s", reembedder.name, org_id)
            out[reembedder.name] = done
        return out
    finally:
        _run_lock.release()


def _run_org(reembedder: Reembedder, embedder, org_id: UUID, batch: int, tenant_context) -> int:
    """Up to `batch` rows of one org, COMMIT_EVERY rows per transaction. Stops when a slice comes back short."""
    done = 0
    while done < batch:
        want = min(COMMIT_EVERY, batch - done)
        with tenant_context(org_id):
            got = int(reembedder.run(embedder, want) or 0)
        done += got
        if got < want:
            break
    return done


def _reembed_task():
    """apps.ai.tasks.reembed_pending (NexoraTask), or None when that base class does not exist yet."""
    try:
        from .tasks import reembed_pending
    except ImportError:  # celery / apps.core.tenancy.tasks unavailable
        return None
    return reembed_pending


def _run_detached() -> None:
    from django.db import close_old_connections

    close_old_connections()
    try:
        run_reembed_pending()
    except Exception:  # noqa: BLE001 - run_reembed_pending never raises; belt and braces for a daemon thread
        log.exception("reembed: background run failed")
    finally:
        connections.close_all()


def enqueue_reembed_pending(*_args, **_kwargs) -> dict:
    """Periodic job fn: hand the run to the task runner and return at once (never downloads or encodes here)."""
    if _run_lock.locked():
        return {"skipped": "already_running"}
    task = _reembed_task()
    if task is not None:
        task.delay()
        return {"enqueued": "task"}
    threading.Thread(target=_run_detached, name="nexora-reembed", daemon=True).start()
    return {"enqueued": "thread"}


def register_periodic_job() -> bool:
    """Register with apps.core.scheduler (Backend, W3). Skipped when the scheduler module does not exist."""
    try:
        from apps.core.scheduler import register_periodic
    except ImportError:
        return False
    register_periodic(PERIODIC_NAME, PERIODIC_EVERY_S, enqueue_reembed_pending)
    return True
