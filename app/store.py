"""In-memory state (cache, budgets, per-client buckets) behind small interfaces.

Everything here starts empty on process start; on Render's free tier that happens after every
sleep. A Redis-backed implementation can replace these classes without touching callers.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

Clock = Callable[[], float]


@dataclass
class CacheEntry:
    value: Any
    stored_at: float
    ttl: float
    size: int


class Cache(Protocol):
    def get(self, key: str) -> tuple[Any, float, bool] | None: ...
    def put(self, key: str, value: Any, ttl: float) -> None: ...
    def stats(self) -> dict[str, float]: ...


class LruCache:
    """LRU with max entries and max bytes. Entries live for stale_max so they can serve as fallback."""

    def __init__(self, max_entries: int, max_bytes: int, stale_max: float, clock: Clock = time.monotonic):
        self.max_entries, self.max_bytes, self.stale_max, self._clock = max_entries, max_bytes, stale_max, clock
        self._d: OrderedDict[str, CacheEntry] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[Any, float, bool] | None:
        """Return (value, age_seconds, fresh) or None if absent or beyond the stale window."""
        e = self._d.get(key)
        if e is None:
            return None
        age = self._clock() - e.stored_at
        if age > max(self.stale_max, e.ttl):
            self._drop(key)
            return None
        self._d.move_to_end(key)
        return e.value, age, age <= e.ttl

    def count(self, hit: bool) -> None:
        if hit:
            self.hits += 1
        else:
            self.misses += 1

    def put(self, key: str, value: Any, ttl: float) -> None:
        size = len(json.dumps(value, separators=(",", ":"), default=str))
        if size > self.max_bytes:
            return
        if key in self._d:
            self._drop(key)
        self._d[key] = CacheEntry(value, self._clock(), ttl, size)
        self._bytes += size
        while self._d and (len(self._d) > self.max_entries or self._bytes > self.max_bytes):
            self._drop(next(iter(self._d)))

    def _drop(self, key: str) -> None:
        e = self._d.pop(key, None)
        if e:
            self._bytes -= e.size

    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "entries": len(self._d),
            "bytes": self._bytes,
            "hit_rate": round(self.hits / total, 2) if total else 0.0,
        }


class BudgetExhausted(Exception):
    def __init__(self, scope: str, retry_after: int):
        super().__init__(scope)
        self.scope, self.retry_after = scope, max(1, int(retry_after))


class Budget(Protocol):
    def acquire(self, client: str, throttle: bool = False) -> None: ...
    def snapshot(self) -> dict[str, int]: ...


class MemoryBudget:
    """Sliding-window call counters (hour, day) plus a per-client token bucket (per minute).

    Unknown state means deny: any internal error in acquire() raises BudgetExhausted.
    The effective caps are scaled down by cold_factor until the process has been up for a full
    window, because usage from before the restart is unknown.
    """

    def __init__(
        self,
        per_hour: int,
        per_day: int,
        client_per_minute: int,
        cold_factor: float = 0.5,
        clock: Clock = time.monotonic,
        client_per_hour: int | None = None,
        client_per_day: int | None = None,
    ):
        self.per_hour, self.per_day, self.per_min = per_hour, per_day, client_per_minute
        self.client_per_hour, self.client_per_day = client_per_hour, client_per_day
        self._client_hist: dict[str, deque[float]] = {}  # per-client call times (last 24 h)
        self.cold_factor = cold_factor
        self._clock = clock
        self._started = clock()
        self._hour: deque[float] = deque()
        self._day: deque[float] = deque()
        self._buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, last_ts)

    def _prune(self, now: float) -> None:
        while self._hour and now - self._hour[0] >= 3600:
            self._hour.popleft()
        while self._day and now - self._day[0] >= 86400:
            self._day.popleft()

    def _factor(self, now: float, window: int) -> float:
        return self.cold_factor if now - self._started < window else 1.0

    def _caps(self, now: float, throttle: bool) -> tuple[int, int]:
        up = now - self._started
        hour = self.per_hour * (self.cold_factor if up < 3600 else 1)
        day = self.per_day * (self.cold_factor if up < 86400 else 1)
        if throttle:
            hour *= 0.5
        return max(1, int(hour)), max(1, int(day))

    def acquire(self, client: str, throttle: bool = False) -> None:
        try:
            now = self._clock()
            self._prune(now)
            hour_cap, day_cap = self._caps(now, throttle)
            if len(self._hour) >= hour_cap:
                raise BudgetExhausted("hourly", 3600 - (now - self._hour[0]))
            if len(self._day) >= day_cap:
                raise BudgetExhausted("daily", 86400 - (now - self._day[0]))
            hist = self._client_hist.setdefault(client, deque())
            while hist and now - hist[0] >= 86400:
                hist.popleft()
            day_cap = None
            if self.client_per_day is not None:
                day_cap = max(1, int(self.client_per_day * self._factor(now, 86400)))
            if day_cap is not None and len(hist) >= day_cap:
                raise BudgetExhausted("per-user-daily", 86400 - (now - hist[0]))
            if self.client_per_hour is not None:
                recent = [t for t in hist if now - t < 3600]
                if len(recent) >= max(1, int(self.client_per_hour * self._factor(now, 3600))):
                    raise BudgetExhausted("per-user-hourly", 3600 - (now - recent[0]))
            tokens, last = self._buckets.get(client, (float(self.per_min), now))
            rate = self.per_min / 60.0
            tokens = min(float(self.per_min), tokens + (now - last) * rate)
            if tokens < 1:
                raise BudgetExhausted("per-client", (1 - tokens) / rate)
            self._buckets[client] = (tokens - 1, now)
            hist.append(now)
            if len(self._buckets) > 1000:  # bound memory under many distinct IPs
                self._buckets = dict(list(self._buckets.items())[-500:])
                self._client_hist = {k: v for k, v in self._client_hist.items() if k in self._buckets}
            self._hour.append(now)
            self._day.append(now)
        except BudgetExhausted:
            raise
        except Exception:  # fail closed
            raise BudgetExhausted("unknown", 60) from None

    def snapshot(self) -> dict[str, int]:
        now = self._clock()
        self._prune(now)
        hour_cap, day_cap = self._caps(now, False)
        return {
            "hour_used": len(self._hour),
            "hour_left": max(0, hour_cap - len(self._hour)),
            "day_used": len(self._day),
            "day_left": max(0, day_cap - len(self._day)),
        }
