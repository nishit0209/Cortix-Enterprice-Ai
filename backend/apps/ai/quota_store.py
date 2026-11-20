"""PostgreSQL-backed stand-in for the few Redis commands the quota limiter uses (no-Redis local runtime).

`QuotaLimiter` was written against redis-py (`transaction` with WATCH/MULTI, `mget`, `incrby`, `expire`, `get`,
`set`, `delete`). `DbRedis` implements exactly that subset with the same semantics (bytes values, TTLs) on top of
a pluggable row store:

* `OrmCounterBackend` - the `ai.AIQuotaCounter` table. Every operation runs in ONE transaction on the durable
  alias that locks the touched keys with SELECT ... FOR UPDATE (placeholder rows are inserted first with
  ON CONFLICT DO NOTHING), so a reservation's read-decide-increment is atomic across processes, like the Redis
  optimistic transaction it replaces.
* `MemoryCounterBackend` - a process-local dict (unit tests).

Selected automatically by `engine._default_limiter()` when `REDIS_URL` is empty.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Entry:
    value: bytes | None
    expire_at: float | None  # epoch seconds

    def live(self, now: float) -> bool:
        return self.value is not None and (self.expire_at is None or self.expire_at > now)


Snapshot = dict[str, Entry]


class MemoryCounterBackend:
    def __init__(self):
        self._data: dict[str, Entry] = {}
        self._lock = threading.RLock()

    def read(self, keys: list[str]) -> Snapshot:
        with self._lock:
            return {k: Entry(self._data[k].value, self._data[k].expire_at) if k in self._data else Entry(None, None)
                    for k in keys}

    @contextmanager
    def locked(self, keys: list[str]) -> Iterator[Snapshot]:
        with self._lock:
            snap = {k: Entry(self._data[k].value, self._data[k].expire_at) if k in self._data else Entry(None, None)
                    for k in keys}
            yield snap  # an exception inside the block discards every change
            for k, e in snap.items():
                if e.value is None:
                    self._data.pop(k, None)
                else:
                    self._data[k] = Entry(e.value, e.expire_at)

    def purge(self, now: float) -> int:
        with self._lock:
            dead = [k for k, e in self._data.items() if not e.live(now)]
            for k in dead:
                del self._data[k]
            return len(dead)


class OrmCounterBackend:
    def __init__(self, alias: str | None = None):
        self._alias = alias

    def alias(self) -> str:
        if self._alias:
            return self._alias
        from .recorder import log_alias

        return log_alias()

    @staticmethod
    def _entry(row) -> Entry:
        return Entry(row.value.encode() if row.value is not None else None,
                     row.expires_at.timestamp() if row.expires_at else None)

    def read(self, keys: list[str]) -> Snapshot:
        """Plain read (no lock, no placeholder rows): reporting (`usage`, `breaker`) only."""
        from .models import AIQuotaCounter

        rows = {r.key: r for r in AIQuotaCounter.objects.using(self.alias()).filter(key__in=list(set(keys)))}
        return {k: self._entry(rows[k]) if k in rows else Entry(None, None) for k in keys}

    @contextmanager
    def locked(self, keys: list[str]) -> Iterator[Snapshot]:
        from django.db import DatabaseError, transaction

        from .models import AIQuotaCounter

        alias = self.alias()
        keys = sorted(set(keys))
        with transaction.atomic(using=alias):
            mgr = AIQuotaCounter.objects.using(alias)
            rows: dict[str, AIQuotaCounter] = {}
            # A placeholder row committed earlier can be purged between our INSERT ... ON CONFLICT DO NOTHING and
            # the SELECT ... FOR UPDATE; insert + lock again so every key is locked before deciding.
            for _attempt in range(3):
                missing = [k for k in keys if k not in rows]
                if not missing:
                    break
                mgr.bulk_create([AIQuotaCounter(key=k) for k in missing], ignore_conflicts=True)
                rows.update({r.key: r for r in mgr.select_for_update().filter(key__in=missing).order_by("key")})
            if len(rows) != len(keys):
                raise DatabaseError("quota counter rows could not be locked")
            snap = {k: self._entry(rows[k]) for k in keys}
            before = {k: (e.value, e.expire_at) for k, e in snap.items()}
            yield snap
            for k, e in snap.items():
                if (e.value, e.expire_at) == before[k]:
                    continue
                mgr.filter(key=k).update(
                    value=e.value.decode() if e.value is not None else None,
                    expires_at=datetime.fromtimestamp(e.expire_at, tz=UTC) if e.expire_at is not None else None)

    def purge(self, now: float) -> int:
        from django.db.models import Q

        from .models import AIQuotaCounter

        cutoff = datetime.fromtimestamp(now, tz=UTC)
        deleted, _ = AIQuotaCounter.objects.using(self.alias()).filter(
            Q(expires_at__lt=cutoff) | Q(value__isnull=True)).delete()
        return deleted


def _b(value) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode()


class DbRedis:
    """The redis-py subset used by QuotaLimiter, over a counter backend."""

    def __init__(self, backend=None, *, clock: Callable[[], float] = time.time):
        self.backend = backend if backend is not None else OrmCounterBackend()
        self._clock = clock

    def _norm(self, snap: Snapshot, now: float) -> Snapshot:
        for e in snap.values():
            if e.value is not None and not e.live(now):
                e.value, e.expire_at = None, None
        return snap

    # --- single commands -----------------------------------------------------------------------------
    def get(self, key):
        return self.mget([key])[0]

    def mget(self, keys, *args):
        """Read-only (like Redis MGET outside a transaction): no locks, no placeholder rows."""
        keys = [keys, *args] if isinstance(keys, str) else list(keys) + list(args)
        snap = self._norm(self.backend.read(keys), self._clock())
        return [snap[k].value for k in keys]

    def set(self, key, value, ex=None, px=None, exat=None, nx=False):
        now = self._clock()
        with self.backend.locked([key]) as snap:
            self._norm(snap, now)
            if nx and snap[key].value is not None:
                return None
            expire_at = None
            if ex is not None:
                expire_at = now + float(ex)
            elif px is not None:
                expire_at = now + float(px) / 1000
            elif exat is not None:
                expire_at = float(exat)
            snap[key].value, snap[key].expire_at = _b(value), expire_at
            return True

    def incrby(self, key, amount=1):
        now = self._clock()
        with self.backend.locked([key]) as snap:
            self._norm(snap, now)
            return _incr(snap[key], amount)

    def incr(self, key, amount=1):
        return self.incrby(key, amount)

    def expire(self, key, seconds):
        now = self._clock()
        with self.backend.locked([key]) as snap:
            self._norm(snap, now)
            if snap[key].value is None:
                return False
            snap[key].expire_at = now + float(seconds)
            return True

    def delete(self, *keys):
        now = self._clock()
        with self.backend.locked(list(keys)) as snap:
            self._norm(snap, now)
            n = 0
            for k in keys:
                if snap[k].value is not None:
                    snap[k].value, snap[k].expire_at = None, None
                    n += 1
            return n

    # --- WATCH/MULTI transaction ---------------------------------------------------------------------
    def transaction(self, func, *watches, value_from_callable: bool = False, **_kw):
        """Run `func(pipe)` with the watched keys locked; queued commands apply atomically when it returns.
        Commands may only touch watched keys (QuotaLimiter's usage)."""
        now = self._clock()
        with self.backend.locked(list(watches)) as snap:
            self._norm(snap, now)
            pipe = _Pipe(snap, now)
            result = func(pipe)
            out = pipe.execute()
        return result if value_from_callable else out

    def purge_expired(self) -> int:
        return self.backend.purge(self._clock())


def _incr(entry: Entry, amount) -> int:
    current = int(entry.value) if entry.value is not None else 0
    new = current + int(amount)
    entry.value = _b(new)
    return new


class _Pipe:
    def __init__(self, snap: Snapshot, now: float):
        self._snap = snap
        self._now = now
        self._multi = False
        self._queued: list[tuple[str, tuple]] = []

    def _entry(self, key: str) -> Entry:
        if key not in self._snap:
            raise KeyError(f"DbRedis transaction touched an unwatched key: {key}")
        return self._snap[key]

    def watch(self, *keys):
        return True

    def multi(self):
        self._multi = True

    def mget(self, keys, *args):
        keys = [keys, *args] if isinstance(keys, str) else list(keys) + list(args)
        return [self._entry(k).value for k in keys]

    def get(self, key):
        return self._entry(key).value

    def incrby(self, key, amount=1):
        self._entry(key)
        self._queued.append(("incrby", (key, amount)))
        return self

    def expire(self, key, seconds):
        self._entry(key)
        self._queued.append(("expire", (key, seconds)))
        return self

    def execute(self) -> list:
        out = []
        for name, args in self._queued:
            if name == "incrby":
                out.append(_incr(self._entry(args[0]), args[1]))
            else:
                e = self._entry(args[0])
                if e.value is None:
                    out.append(False)
                else:
                    e.expire_at = self._now + float(args[1])
                    out.append(True)
        self._queued.clear()
        return out


_default: DbRedis | None = None
_default_lock = threading.Lock()


def default_db_redis() -> DbRedis:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = DbRedis(OrmCounterBackend())
    return _default


PURGE_JOB_NAME = "ai.purge_quota_counters"
PURGE_EVERY_S = 3600


def purge_expired_counters(*_args, **_kwargs) -> int:
    """Periodic job "ai.purge_quota_counters" (only meaningful without Redis)."""
    return default_db_redis().purge_expired()


def register_periodic_job() -> bool:
    """Register the hourly purge with apps.core.scheduler when the PostgreSQL counter store is in use (no
    REDIS_URL). Skipped when the scheduler module does not exist yet (Backend, W3)."""
    from django.conf import settings

    if str(getattr(settings, "REDIS_URL", "") or "").strip():
        return False
    try:
        from apps.core.scheduler import register_periodic
    except ImportError:
        return False
    register_periodic(PURGE_JOB_NAME, PURGE_EVERY_S, purge_expired_counters)
    return True
