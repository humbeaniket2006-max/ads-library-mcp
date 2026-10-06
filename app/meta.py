"""Meta Graph API client: cache, single-flight, budgets, backoff, circuit breaker, token health."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .fixtures import mock_ads_archive
from .format import strip_token
from .redact import log_event, redact
from .store import Budget, BudgetExhausted, LruCache

GRAPH_HOST = "https://graph.facebook.com"  # hard-coded; never built from user input
TOKEN_BLOCK_SECONDS = 600
RATE_BLOCK_SECONDS = 300
USAGE_FRESH_SECONDS = 600
RETRIES = 2
MAX_RETRY_AFTER = 20
RATE_CODES = {4, 17, 32, 613} | set(range(80000, 80015))


class MetaError(Exception):
    """Carries a one-sentence, secret-free message for the model."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.message, self.retry_after = message, retry_after


def stop_msg(what: str, retry_after: int) -> str:
    return f"{what}; retry_after_seconds={int(retry_after)}. Do not retry this call."


@dataclass
class MetaResult:
    data: list[dict]
    after: str | None
    cached: bool = False
    stale: bool = False
    calls: int = 0
    warn: str | None = None


def to_query(params: dict[str, Any]) -> dict[str, str]:
    """Encode params for Graph API: arrays as JSON, fields comma-joined."""
    out: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        if k == "fields":
            out[k] = ",".join(v)
        elif isinstance(v, list):
            out[k] = json.dumps(v, separators=(",", ":"))
        else:
            out[k] = str(v)
    return out


class MetaClient:
    def __init__(
        self,
        settings: Settings,
        cache: LruCache,
        budget: Budget,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.s, self.cache, self.budget, self._sleep, self._clock = settings, cache, budget, sleep, clock
        self._http = http
        self._inflight: dict[str, asyncio.Task[MetaResult]] = {}
        self.token_health = "unknown"
        self._token_blocked_until = 0.0
        self._failures = 0
        self._breaker_until = 0.0
        self.usage_pct = 0
        self._usage_at = -1e9
        self.calls_made = 0
        self.started = clock()

    # -- helpers ------------------------------------------------------------------------------
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:  # created lazily: no network or heavy work at import/startup
            self._http = httpx.AsyncClient(
                base_url=GRAPH_HOST,
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
                follow_redirects=False,
                verify=True,
                headers={"Accept": "application/json", "User-Agent": "meta-ads-library-mcp/0.1"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def cache_key(self, params: dict[str, Any]) -> str:
        canon = json.dumps(
            {"v": self.s.graph_api_version, "mock": self.s.meta_mock, "p": params},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(canon.encode()).hexdigest()

    def _usage_fresh(self) -> bool:
        return self._clock() - self._usage_at < USAGE_FRESH_SECONDS

    def status(self) -> dict[str, Any]:
        return {
            "token": "unknown" if self.s.meta_mock else self.token_health,
            "calls_made": self.calls_made,
            "app_usage_pct": self.usage_pct if self._usage_fresh() else None,
        }

    # -- public -------------------------------------------------------------------------------
    async def fetch(self, params: dict[str, Any], ttl: int, client: str) -> MetaResult:
        key = self.cache_key(params)
        hit = self.cache.get(key)
        if hit and hit[2]:
            self.cache.count(True)
            return MetaResult(hit[0]["data"], hit[0]["after"], cached=True)
        self.cache.count(False)
        task = self._inflight.get(key)
        leader = task is None
        if task is None:
            task = asyncio.create_task(self._load(key, params, ttl, client))
            self._inflight[key] = task
            task.add_done_callback(lambda t: (self._inflight.pop(key, None), t.cancelled() or t.exception()))
        res = await asyncio.shield(task)
        if not leader:  # followers shared the call; they did not spend any
            return MetaResult(res.data, res.after, res.cached, res.stale, 0, res.warn)
        return res

    # -- internals ----------------------------------------------------------------------------
    async def _load(self, key: str, params: dict[str, Any], ttl: int, client: str) -> MetaResult:
        try:
            if self.s.meta_mock:
                body = mock_ads_archive(params)
                calls, warn = 0, None
            else:
                body, calls, warn = await self._call(params, client)
        except MetaError as exc:
            hit = self.cache.get(key)
            if hit is not None:  # stale fallback (entries expire from cache after CACHE_STALE_MAX)
                log_event("serving stale cache", logging.WARNING, reason=exc.message[:80])
                return MetaResult(hit[0]["data"], hit[0]["after"], cached=True, stale=True)
            raise
        data = body.get("data") if isinstance(body.get("data"), list) else []
        data = [self._scrub(ad) for ad in data if isinstance(ad, dict)]
        cursors = (body.get("paging") or {}).get("cursors") or {}
        after = cursors.get("after") if (body.get("paging") or {}).get("next") else None
        value = {"data": data, "after": after if isinstance(after, str) else None}
        self.cache.put(key, value, ttl)
        return MetaResult(value["data"], value["after"], calls=calls, warn=warn)

    @staticmethod
    def _scrub(ad: dict) -> dict:
        url = ad.get("ad_snapshot_url")
        if isinstance(url, str):
            ad = {**ad, "ad_snapshot_url": strip_token(url)}
        return ad

    def _preflight(self) -> bool:
        """Raise if calls are blocked; return True when usage-based throttling applies."""
        now = self._clock()
        if now < self._token_blocked_until:
            raise MetaError(self._token_message(), int(self._token_blocked_until - now))
        if now < self._breaker_until:
            raise MetaError(stop_msg("Meta calls are paused after repeated failures",
                                     self._breaker_until - now), int(self._breaker_until - now))
        if self._usage_fresh():
            if self.usage_pct >= self.s.usage_stop_pct:
                wait = USAGE_FRESH_SECONDS - (now - self._usage_at)
                raise MetaError(stop_msg(f"Meta app usage is at {self.usage_pct}%, calls are stopped", wait), int(wait))
            return self.usage_pct >= self.s.usage_throttle_pct
        return False

    def _token_message(self) -> str:
        if self.token_health == "expired":
            return ("The Meta access token has expired; an admin must renew META_ACCESS_TOKEN. "
                    "Do not retry this call.")
        return ("The Meta token was rejected (unauthorised or identity/permissions pending); "
                "an admin must check it. Do not retry this call.")

    def _acquire(self, client: str, throttle: bool) -> None:
        try:
            self.budget.acquire(client, throttle=throttle)
        except BudgetExhausted as exc:
            what = {
                "hourly": "The hourly Meta call budget is used up",
                "daily": "The daily Meta call budget is used up",
                "per-client": "Your per-minute call limit is reached",
            }.get(exc.scope, "Meta call budget state is unavailable")
            raise MetaError(stop_msg(what, exc.retry_after), exc.retry_after) from None

    def _fail(self) -> None:
        self._failures += 1
        if self._failures >= self.s.breaker_failures:
            self._breaker_until = self._clock() + self.s.breaker_cooldown
            self._failures = 0
            log_event("circuit breaker open", logging.WARNING, seconds=self.s.breaker_cooldown)

    def _read_usage(self, headers: httpx.Headers) -> str | None:
        raw = headers.get("x-app-usage")
        if not raw:
            return None
        try:
            vals = json.loads(raw)
            pct = max(int(v) for v in vals.values() if isinstance(v, int | float))
        except (ValueError, TypeError, AttributeError):
            return None
        self.usage_pct, self._usage_at = pct, self._clock()
        if pct >= self.s.usage_warn_pct:
            log_event("meta app usage high", logging.WARNING, pct=pct)
            return f"Meta app usage at {pct}%."
        return None

    async def _call(self, params: dict[str, Any], client: str) -> tuple[dict, int, str | None]:
        calls = 0
        warn: str | None = None
        for attempt in range(RETRIES + 1):
            throttle = self._preflight()
            self._acquire(client, throttle)  # retries count against the budget too
            calls += 1
            self.calls_made += 1
            retry_after: float | None = None
            try:
                status, headers, payload = await self._request(params)
            except httpx.TimeoutException:
                status, headers, payload = 599, httpx.Headers(), {}
            except (httpx.HTTPError, _TooBig) as exc:
                if isinstance(exc, _TooBig):
                    raise MetaError("Meta returned an unexpectedly large response.") from None
                status, headers, payload = 598, httpx.Headers(), {}
            warn = self._read_usage(headers) or warn
            if status == 200 and isinstance(payload, dict):
                self._failures = 0
                self.token_health = "ok"
                return payload, calls, warn
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                self._classify(err)  # raises for token / rate-limit / other client errors
            if status in (429, 500, 502, 503, 504, 598, 599):
                self._fail()
                try:
                    retry_after = float(headers.get("retry-after", ""))
                except ValueError:
                    retry_after = None
                if attempt < RETRIES and (retry_after is None or retry_after <= MAX_RETRY_AFTER):
                    delay = (retry_after if retry_after is not None else 2**attempt) + random.uniform(0, 0.5)
                    await self._sleep(delay)
                    continue
                raise MetaError(stop_msg("Meta is temporarily unavailable", retry_after or 60),
                                int(retry_after or 60))
            raise MetaError(f"Meta rejected the request (HTTP {status}).")
        raise MetaError("Meta is temporarily unavailable.")  # pragma: no cover

    def _classify(self, err: dict) -> None:
        code, sub = err.get("code"), err.get("error_subcode")
        if code == 190 or (code == 10 and sub == 2332002):
            expired = code == 190 and (sub in (463, 467) or "expired" in str(err.get("message", "")).lower())
            self.token_health = "expired" if expired else "unauthorised"
            self._token_blocked_until = self._clock() + TOKEN_BLOCK_SECONDS
            log_event("meta token rejected", logging.ERROR, code=code, subcode=sub, health=self.token_health)
            raise MetaError(self._token_message(), TOKEN_BLOCK_SECONDS)
        if code in RATE_CODES:
            self._usage_at, self.usage_pct = self._clock() - USAGE_FRESH_SECONDS + RATE_BLOCK_SECONDS, 100
            raise MetaError(stop_msg("Meta rate limit reached", RATE_BLOCK_SECONDS), RATE_BLOCK_SECONDS)
        if code in (1, 2):  # transient Graph API errors
            self._fail()
            raise MetaError("Meta had a temporary error. Do not retry this call.", 60)
        raise MetaError(f"Meta rejected the request (code {code}).")

    async def _request(self, params: dict[str, Any]) -> tuple[int, httpx.Headers, Any]:
        query = to_query(params)
        query["access_token"] = self.s.meta_access_token
        path = f"/{self.s.graph_api_version}/ads_archive"
        try:
            async with self.http.stream("GET", path, params=query) as resp:
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.s.max_meta_response_bytes:
                        raise _TooBig
                    chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks))
                except ValueError:
                    payload = {}
                return resp.status_code, resp.headers, payload
        except httpx.HTTPError as exc:
            log_event("meta transport error", logging.WARNING, error=redact(type(exc).__name__))
            raise


class _TooBig(Exception):
    pass
