"""Tool logic. Pure Python; no MCP types, so it is easy to test."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from datetime import date
from typing import Any

from pydantic import ValidationError

from . import context, models
from .config import Settings
from .format import (
    NOTE,
    PLATFORM_SHORT,
    ad_row,
    dedupe,
    dumps,
    hoist_page,
    query_hash,
    render,
    sanitize_text,
    sign_cursor,
    verify_cursor,
)
from .meta import MetaClient, MetaError, MetaResult
from .redact import log_event
from .store import LruCache, MemoryBudget

SEARCH_FIELDS = [
    "id", "page_id", "page_name", "ad_creative_bodies", "ad_delivery_start_time",
    "ad_delivery_stop_time", "publisher_platforms",
]
FULL_FIELDS = ["spend", "currency", "impressions", "eu_total_reach", "demographic_distribution"]
SUMMARY_FIELDS = [*SEARCH_FIELDS, "languages"]
ADVERTISER_FIELDS = ["id", "page_id", "page_name"]


class ToolFailure(Exception):
    """One-sentence error for the model."""


class Service:
    def __init__(self, settings: Settings, meta: MetaClient | None = None, cache: LruCache | None = None,
                 budget: MemoryBudget | None = None):
        self.s = settings
        self.cache = cache or LruCache(settings.cache_max_entries, settings.cache_max_bytes, settings.cache_stale_max)
        self.budget = budget or MemoryBudget(
            settings.max_calls_per_hour, settings.max_calls_per_day,
            settings.client_max_calls_per_minute, settings.cold_start_budget_factor,
            client_per_hour=settings.client_max_calls_per_hour, client_per_day=settings.client_max_calls_per_day,
        )
        self.meta = meta or MetaClient(settings, self.cache, self.budget)
        self.started = time.monotonic()

    # -- shared -------------------------------------------------------------------------------
    def _meta_obj(self, n: int, results: list[MetaResult], nxt: str | None = None) -> dict[str, Any]:
        m: dict[str, Any] = {
            "n": n,
            "cached": bool(results) and all(r.cached for r in results),
            "stale": any(r.stale for r in results),
            "cc": sum(r.calls for r in results),
            "mock": self.s.meta_mock,
        }
        if nxt:
            m["next"] = nxt
        warn = next((r.warn for r in results if r.warn), None)
        if warn:
            m["warn"] = warn
        return m

    def _log(self, tool: str, status: str, t0: float, qtext: str | None, results: list[MetaResult]) -> None:
        log_event(
            "tool call", logging.INFO if status == "ok" else logging.WARNING,
            request_id=context.request_id.get(), client=context.client_label.get(), tool=tool, status=status,
            latency_ms=int((time.monotonic() - t0) * 1000),
            cache=("hit" if results and all(r.cached for r in results) else "miss"),
            meta_calls=sum(r.calls for r in results),
            query_sha=hashlib.sha256(qtext.encode()).hexdigest()[:12] if qtext else None,
        )

    def _validate(self, model, args: dict):
        try:
            return model.model_validate({k: v for k, v in args.items() if v is not None})
        except ValidationError as exc:
            raise ToolFailure(models.first_error(exc)) from None

    # -- search_ads ---------------------------------------------------------------------------
    def _search_params(self, a: models.SearchAds) -> dict[str, Any]:
        p: dict[str, Any] = {
            "ad_reached_countries": sorted(a.countries),
            "ad_active_status": a.status.upper(),
            "ad_type": "POLITICAL_AND_ISSUE_ADS" if a.ad_type == "political" else "ALL",
            "limit": min(a.limit, self.s.max_limit),
            "fields": sorted({*SEARCH_FIELDS, *(FULL_FIELDS if a.detail == "full" else []),
                              *(["ad_snapshot_url"] if a.include_links else [])}),
        }
        if a.query:
            p["search_terms"] = a.query
        if a.page_ids:
            p["search_page_ids"] = sorted(int(x) for x in a.page_ids)
        if a.date_from:
            p["ad_delivery_date_min"] = a.date_from
        if a.date_to:
            p["ad_delivery_date_max"] = a.date_to
        if a.platforms:
            p["publisher_platforms"] = sorted(x.upper() for x in a.platforms)
        if a.languages:
            p["languages"] = sorted(a.languages)
        if a.media and a.media != "all":
            p["media_type"] = a.media.upper()
        return p

    async def search_ads(self, args: dict[str, Any]) -> str:
        t0, results = time.monotonic(), []
        qtext = str(args.get("query") or "")
        try:
            a = self._validate(models.SearchAds, args)
            try:
                a.check()
            except ValueError as exc:
                raise ToolFailure(f"Invalid input: {exc}.") from None
            params = self._search_params(a)
            qh = query_hash(params)
            after = None
            if a.cursor:
                after = verify_cursor(a.cursor, qh, self.s.cursor_signing_key)
                if after is None:
                    raise ToolFailure("Invalid cursor for this query; start again without a cursor.")
            ttl = self.s.cache_ttl_active if a.status == "active" and not (a.date_from or a.date_to) \
                else self.s.cache_ttl_historical
            ads: list[dict] = []
            for _ in range(self.s.max_pages_per_call):
                res = await self.meta.fetch({**params, **({"after": after} if after else {})}, ttl,
                                            context.client_key.get())
                results.append(res)
                ads += res.data
                after = res.after
                if not after:
                    break
            rows = dedupe([ad_row(x, detail=a.detail, include_links=a.include_links,
                                  text_max=self.s.ad_text_max_chars) for x in ads])
            top = hoist_page(rows)
            nxt = sign_cursor(after, qh, self.s.cursor_signing_key) if after else None
            out = render(rows, self._meta_obj(len(rows), results, nxt), self.s.max_response_chars, top)
            self._log("search_ads", "ok", t0, qtext, results)
            return out
        except MetaError as exc:
            self._log("search_ads", "error", t0, qtext, results)
            raise ToolFailure(exc.message) from None
        except ToolFailure:
            self._log("search_ads", "invalid", t0, qtext, results)
            raise

    # -- find_advertiser ----------------------------------------------------------------------
    async def find_advertiser(self, args: dict[str, Any]) -> str:
        t0, results = time.monotonic(), []
        try:
            a = self._validate(models.FindAdvertiser, args)
            params = {
                "ad_reached_countries": sorted(a.countries), "search_terms": a.name,
                "ad_active_status": "ALL", "limit": self.s.max_limit, "fields": ADVERTISER_FIELDS,
            }
            res = await self.meta.fetch(params, self.s.cache_ttl_advertiser, context.client_key.get())
            results.append(res)
            seen: dict[str, dict[str, Any]] = {}
            for ad in res.data:
                pid = str(ad.get("page_id", ""))
                if not pid:
                    continue
                name = sanitize_text(ad.get("page_name"), 60)
                e = seen.setdefault(pid, {"page_id": pid, "page": name, "ads_seen": 0})
                e["ads_seen"] += 1
            top = sorted(seen.values(), key=lambda e: -e["ads_seen"])[:10]
            out = dumps({"advertisers": top, "meta": self._meta_obj(len(top), results), "note": NOTE})
            self._log("find_advertiser", "ok", t0, a.name, results)
            return out
        except MetaError as exc:
            self._log("find_advertiser", "error", t0, args.get("name"), results)
            raise ToolFailure(exc.message) from None
        except ToolFailure:
            self._log("find_advertiser", "invalid", t0, None, results)
            raise

    # -- summarize_advertiser -----------------------------------------------------------------
    async def summarize_advertiser(self, args: dict[str, Any], today: date | None = None) -> str:
        t0, results = time.monotonic(), []
        today = today or date.today()
        try:
            a = self._validate(models.SummarizeAdvertiser, args)
            params: dict[str, Any] = {
                "ad_reached_countries": sorted(a.countries), "search_page_ids": [int(a.page_id)],
                "ad_active_status": a.status.upper(), "limit": self.s.max_limit, "fields": SUMMARY_FIELDS,
            }
            ttl = self.s.cache_ttl_historical if a.status == "inactive" else self.s.cache_ttl_active
            ads: list[dict] = []
            after = None
            for _ in range(self.s.max_summary_pages):
                res = await self.meta.fetch({**params, **({"after": after} if after else {})}, ttl,
                                            context.client_key.get())
                results.append(res)
                ads += res.data
                after = res.after
                if not after:
                    break
            sampled = bool(after)
            out = self._summary(a.page_id, ads, sampled, today, results)
            self._log("summarize_advertiser", "ok", t0, None, results)
            return out
        except MetaError as exc:
            self._log("summarize_advertiser", "error", t0, None, results)
            raise ToolFailure(exc.message) from None
        except ToolFailure:
            self._log("summarize_advertiser", "invalid", t0, None, results)
            raise

    def _summary(self, page_id: str, ads: list[dict], sampled: bool, today: date, results: list[MetaResult]) -> str:
        from .format import day

        status: Counter[str] = Counter()
        plats: Counter[str] = Counter()
        langs: Counter[str] = Counter()
        starts: list[str] = []
        spans: list[tuple[int, dict]] = []
        for ad in ads:
            start, stop = day(ad.get("ad_delivery_start_time")), day(ad.get("ad_delivery_stop_time"))
            running = stop is None or stop >= today.isoformat()
            status["active" if running else "inactive"] += 1
            plats.update(PLATFORM_SHORT.get(p, str(p).lower()[:12]) for p in (ad.get("publisher_platforms") or []))
            langs.update(str(x).lower()[:3] for x in (ad.get("languages") or []))
            if start:
                starts.append(start)
                end = date.fromisoformat(stop) if stop else today
                spans.append(((end - date.fromisoformat(start)).days, ad))
        spans.sort(key=lambda t: -t[0])
        longest = []
        for days, ad in spans[:5]:
            row = {"id": str(ad.get("id", ""))[:24], "days": days}
            text = sanitize_text((ad.get("ad_creative_bodies") or [""])[0], 80)
            if text:
                row["text"] = text
            longest.append(row)
        body: dict[str, Any] = {"page_id": page_id}
        name = next((sanitize_text(a.get("page_name"), 60) for a in ads if a.get("page_name")), "")
        if name:
            body["page"] = name
        body.update(ads=len(ads), status=dict(status), platforms=dict(plats), langs=dict(langs))
        if starts:
            body["start"] = [min(starts), max(starts)]
        body.update(longest=longest, sampled=sampled, meta=self._meta_obj(len(ads), results), note=NOTE)
        return dumps(body)

    # -- get_status ---------------------------------------------------------------------------
    def get_status(self) -> str:
        b = self.budget.snapshot()
        st = self.meta.status()
        cache = self.cache.stats()
        body = {
            "filters": {
                "status": list(models.STATUSES), "ad_type": list(models.AD_TYPES),
                "detail": list(models.DETAILS), "media": list(models.MEDIA),
                "platforms": dict(PLATFORM_SHORT), "date": "YYYY-MM-DD",
                "max_countries": 5, "max_limit": self.s.max_limit,
            },
            "budget": {
                "hour": {"used": b["hour_used"], "left": b["hour_left"]},
                "day": {"used": b["day_used"], "left": b["day_left"]},
            },
            "cache_hit_rate": cache["hit_rate"],
            "token": st["token"],
            "app_usage_pct": st["app_usage_pct"],
            "graph": self.s.graph_api_version,
            "mock": self.s.meta_mock,
            "cold_start": cache["entries"] == 0 and st["calls_made"] == 0,
            "uptime_s": int(time.monotonic() - self.started),
            "meta": {"n": 1, "cached": False, "stale": False, "cc": 0, "mock": self.s.meta_mock},
        }
        return json.dumps({k: v for k, v in body.items() if v is not None}, separators=(",", ":"))
