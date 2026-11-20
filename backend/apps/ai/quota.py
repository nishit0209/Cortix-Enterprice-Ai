"""Redis quota limiter + circuit breaker (AI_ARCHITECTURE.md §3.4).

Atomicity: every reservation runs as an optimistic Redis transaction (WATCH all counters -> read ->
decide -> MULTI/INCRBY/EXPIRE -> EXEC, retried on WatchError), so concurrent workers can never jointly
overshoot a limit.

Checked, in order:
  1. project RPM / TPM / RPD for the model (from AIModelQuota, 10% safety margin), with the
     priority-class share applied to the RPD (interactive 100%, assistive 60%, background 30%);
  2. the org daily request + token budget (AIOrgConfig), priority share applied;
  3. the per-user daily request cap.

Circuit breaker per model: `open_minute` (429 per-minute, reopens after retryDelay) and `open_day`
(429 per-day or project RPD reached, reopens at the next midnight America/Los_Angeles).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from .constants import (
    CODE_LIMITER_UNAVAILABLE,
    CODE_QUOTA_EXHAUSTED,
    CODE_RATE_LIMITED,
    PRIORITY_SHARE,
    BreakerState,
)
from .timeutil import next_pacific_midnight, pacific_day, seconds_until_pacific_midnight

SAFETY_MARGIN = 0.9
KEY_PREFIX = "ai:q"


@dataclass(frozen=True)
class ModelLimits:
    rpm: int
    tpm: int
    rpd: int

    def effective(self) -> ModelLimits:
        return ModelLimits(
            rpm=max(1, math.floor(self.rpm * SAFETY_MARGIN)),
            tpm=max(1, math.floor(self.tpm * SAFETY_MARGIN)),
            rpd=max(1, math.floor(self.rpd * SAFETY_MARGIN)),
        )


# Conservative defaults when no AIModelQuota row exists (free-tier community numbers, §2.3).
DEFAULT_LITE_LIMITS = ModelLimits(rpm=15, tpm=250_000, rpd=500)
DEFAULT_FLASH_LIMITS = ModelLimits(rpm=5, tpm=250_000, rpd=20)


def default_model_limits(model: str) -> ModelLimits:
    return DEFAULT_LITE_LIMITS if "lite" in (model or "").lower() else DEFAULT_FLASH_LIMITS


@dataclass(frozen=True)
class OrgLimits:
    daily_requests: int = 50
    daily_tokens: int = 250_000
    per_user_daily_requests: int = 20


@dataclass(frozen=True)
class Denial:
    scope: str             # project_minute | project_day | project_day_priority | org_day | org_tokens | user_day | breaker
    code: str              # ai_rate_limited | ai_quota_exhausted
    resets_at: datetime
    retry_after_s: int
    open_breaker_day: bool = False

    @property
    def status(self) -> str:
        return "rate_limited" if self.code == CODE_RATE_LIMITED else "quota_exhausted"


class QuotaDenied(Exception):
    def __init__(self, denial: Denial):
        super().__init__(f"{denial.scope}: {denial.code}")
        self.denial = denial


class LimiterUnavailable(Exception):
    code = CODE_LIMITER_UNAVAILABLE


@dataclass
class Reservation:
    model: str
    est_tokens: int
    token_keys: tuple[str, ...]
    ttl_by_key: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BreakerStatus:
    state: str
    resets_at: datetime | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    org_requests: int
    org_tokens: int
    user_requests: int | None
    day: str


def _int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, bytes):
        v = v.decode()
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class QuotaLimiter:
    def __init__(self, redis_factory: Callable[[], object], *, clock: Callable[[], float] = time.time):
        self._redis_factory = redis_factory
        self._clock = clock

    # --- keys ----------------------------------------------------------------------------------
    def _keys(self, now: float, model: str, org_id: UUID | str, user_id: UUID | str | None) -> dict[str, str]:
        minute = int(now // 60)
        day = pacific_day(now).isoformat()
        keys = {
            "rpm": f"{KEY_PREFIX}:model:{model}:m:{minute}:req",
            "tpm": f"{KEY_PREFIX}:model:{model}:m:{minute}:tok",
            "rpd": f"{KEY_PREFIX}:model:{model}:d:{day}:req",
            "org_req": f"{KEY_PREFIX}:org:{org_id}:d:{day}:req",
            "org_tok": f"{KEY_PREFIX}:org:{org_id}:d:{day}:tok",
        }
        if user_id:
            keys["user"] = f"{KEY_PREFIX}:org:{org_id}:u:{user_id}:d:{day}:req"
        return keys

    @staticmethod
    def breaker_key(model: str) -> str:
        return f"ai:breaker:{model}"

    def _redis(self):
        try:
            return self._redis_factory()
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc

    # --- decision (pure) ---------------------------------------------------------------------------
    def evaluate(self, *, now: float, used: dict[str, int], est_tokens: int, priority: str,
                 model_limits: ModelLimits, org_limits: OrgLimits | None, check_user: bool) -> Denial | None:
        share = PRIORITY_SHARE.get(priority, PRIORITY_SHARE["background"])
        eff = model_limits.effective()
        minute_left = max(1, 60 - int(now % 60))
        minute_reset = datetime.fromtimestamp((int(now // 60) + 1) * 60, tz=UTC)
        day_reset = next_pacific_midnight(now)
        day_left = seconds_until_pacific_midnight(now)

        if used["rpm"] + 1 > eff.rpm:
            return Denial("project_minute", CODE_RATE_LIMITED, minute_reset, minute_left)
        if used["tpm"] > 0 and used["tpm"] + est_tokens > eff.tpm:
            return Denial("project_minute", CODE_RATE_LIMITED, minute_reset, minute_left)
        if used["rpd"] + 1 > eff.rpd:
            return Denial("project_day", CODE_QUOTA_EXHAUSTED, day_reset, day_left, open_breaker_day=True)
        if used["rpd"] + 1 > max(1, math.floor(eff.rpd * share)):
            return Denial("project_day_priority", CODE_QUOTA_EXHAUSTED, day_reset, day_left)
        if org_limits is not None:
            if used["org_req"] + 1 > math.floor(org_limits.daily_requests * share):
                return Denial("org_day", CODE_QUOTA_EXHAUSTED, day_reset, day_left)
            tok_cap = math.floor(org_limits.daily_tokens * share)
            if used["org_tok"] >= tok_cap or (used["org_tok"] > 0 and used["org_tok"] + est_tokens > tok_cap):
                return Denial("org_tokens", CODE_QUOTA_EXHAUSTED, day_reset, day_left)
            if check_user and used.get("user", 0) + 1 > org_limits.per_user_daily_requests:
                return Denial("user_day", CODE_QUOTA_EXHAUSTED, day_reset, day_left)
        return None

    # --- reservation ---------------------------------------------------------------------------------
    def reserve(self, *, org_id: UUID | str, user_id: UUID | str | None, model: str, priority: str,
                est_tokens: int, model_limits: ModelLimits, org_limits: OrgLimits | None) -> Reservation:
        """Atomically check every limit and count one request + `est_tokens`.

        `org_limits=None` counts only the PROJECT counters (used for retry attempts of an already
        budgeted request). Raises QuotaDenied or LimiterUnavailable.
        """
        now = self._clock()
        keys = self._keys(now, model, org_id, user_id if org_limits is not None else None)
        if org_limits is None:
            keys = {k: v for k, v in keys.items() if k in ("rpm", "tpm", "rpd")}
        names = list(keys)
        minute_ttl = 120
        day_ttl = seconds_until_pacific_midnight(now) + 3600
        ttl = {k: (minute_ttl if k in ("rpm", "tpm") else day_ttl) for k in names}
        incr = {k: (est_tokens if k in ("tpm", "org_tok") else 1) for k in names}
        r = self._redis()

        def txn(pipe):
            values = pipe.mget([keys[n] for n in names])
            used = {n: _int(v) for n, v in zip(names, values, strict=True)}
            for n in ("rpm", "tpm", "rpd", "org_req", "org_tok"):
                used.setdefault(n, 0)
            denial = self.evaluate(now=now, used=used, est_tokens=est_tokens, priority=priority,
                                   model_limits=model_limits, org_limits=org_limits, check_user="user" in keys)
            if denial is not None:
                raise QuotaDenied(denial)
            pipe.multi()
            for n in names:
                pipe.incrby(keys[n], incr[n])
                pipe.expire(keys[n], ttl[n])

        try:
            r.transaction(txn, *[keys[n] for n in names])
        except QuotaDenied as denied:
            if denied.denial.open_breaker_day:
                self.open_day(model)
            raise
        except LimiterUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - redis errors -> fail closed
            raise LimiterUnavailable(str(exc)) from exc
        token_keys = tuple(keys[n] for n in ("tpm", "org_tok") if n in keys)
        return Reservation(model=model, est_tokens=est_tokens, token_keys=token_keys,
                           ttl_by_key={keys[n]: ttl[n] for n in names})

    def reconcile(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace the estimated token count with the actual one (best effort)."""
        delta = int(actual_tokens) - int(reservation.est_tokens)
        if delta == 0 or not reservation.token_keys:
            return
        try:
            r = self._redis()
            for key in reservation.token_keys:
                r.incrby(key, delta)
                r.expire(key, reservation.ttl_by_key.get(key, 120))
        except Exception:  # noqa: BLE001 - accounting drift is preferable to failing a finished call
            return

    # --- breaker -------------------------------------------------------------------------------------
    def breaker(self, model: str) -> BreakerStatus:
        try:
            raw = self._redis().get(self.breaker_key(model))
        except LimiterUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc
        if not raw:
            return BreakerStatus(BreakerState.CLOSED.value)
        try:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            until = float(data["until"])
        except (ValueError, KeyError, TypeError):
            return BreakerStatus(BreakerState.CLOSED.value)
        if until <= self._clock():
            return BreakerStatus(BreakerState.CLOSED.value)
        return BreakerStatus(str(data.get("state", BreakerState.OPEN_MINUTE.value)),
                             datetime.fromtimestamp(until, tz=UTC))

    def _open(self, model: str, state: str, until: float) -> BreakerStatus:
        current = self.breaker(model)
        if current.state == BreakerState.OPEN_DAY.value and current.resets_at and current.resets_at.timestamp() >= until:
            return current  # never shorten a day-open breaker
        payload = json.dumps({"state": state, "until": until})
        try:
            self._redis().set(self.breaker_key(model), payload, exat=int(math.ceil(until)))
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc
        return BreakerStatus(state, datetime.fromtimestamp(until, tz=UTC))

    def open_day(self, model: str) -> BreakerStatus:
        until = next_pacific_midnight(self._clock()).timestamp()
        return self._open(model, BreakerState.OPEN_DAY.value, until)

    def open_minute(self, model: str, seconds: float) -> BreakerStatus:
        until = self._clock() + max(1.0, float(seconds))
        return self._open(model, BreakerState.OPEN_MINUTE.value, until)

    def close(self, model: str) -> None:
        try:
            self._redis().delete(self.breaker_key(model))
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc

    # --- reporting -----------------------------------------------------------------------------------
    def project_day_requests(self, model: str) -> int:
        """Requests counted today against the project RPD of `model`."""
        key = f"{KEY_PREFIX}:model:{model}:d:{pacific_day(self._clock()).isoformat()}:req"
        try:
            return _int(self._redis().get(key))
        except LimiterUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc

    def mark_once(self, name: str, *, org_id: UUID | str) -> bool:
        """True exactly once per org, `name` and Pacific quota day (SET NX with a TTL until the day ends + 1 h)."""
        now = self._clock()
        key = f"{KEY_PREFIX}:org:{org_id}:d:{pacific_day(now).isoformat()}:once:{name}"
        try:
            return bool(self._redis().set(key, "1", nx=True, ex=seconds_until_pacific_midnight(now) + 3600))
        except LimiterUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc

    def usage(self, org_id: UUID | str, user_id: UUID | str | None = None) -> UsageSnapshot:
        now = self._clock()
        day = pacific_day(now).isoformat()
        keys = [f"{KEY_PREFIX}:org:{org_id}:d:{day}:req", f"{KEY_PREFIX}:org:{org_id}:d:{day}:tok"]
        if user_id:
            keys.append(f"{KEY_PREFIX}:org:{org_id}:u:{user_id}:d:{day}:req")
        try:
            values = self._redis().mget(keys)
        except Exception as exc:  # noqa: BLE001
            raise LimiterUnavailable(str(exc)) from exc
        return UsageSnapshot(
            org_requests=_int(values[0]),
            org_tokens=_int(values[1]),
            user_requests=_int(values[2]) if user_id else None,
            day=day,
        )
