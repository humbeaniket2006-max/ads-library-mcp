import asyncio
import json

import httpx
import pytest
import respx

from app.meta import MetaClient, MetaError
from app.store import LruCache, MemoryBudget

from .conftest import TOKEN, make_settings

URL = "https://graph.facebook.com/v26.0/ads_archive"
PARAMS = {"ad_reached_countries": ["DE"], "search_terms": "x", "limit": 10, "fields": ["id"]}
OK = {"data": [{"id": "1", "page_id": "9", "page_name": "P"}],
      "paging": {"cursors": {"after": "AFTER"}, "next": "https://graph.facebook.com/next?access_token=EAAsecret1234"}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def build(clock=None, **env):
    clock = clock or Clock()
    s = make_settings(META_MOCK="false", **env)
    cache = LruCache(s.cache_max_entries, s.cache_max_bytes, s.cache_stale_max, clock)
    budget = MemoryBudget(s.max_calls_per_hour, s.max_calls_per_day, s.client_max_calls_per_minute, 1.0, clock)
    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)

    return MetaClient(s, cache, budget, sleep=sleep, clock=clock), clock, sleeps


def ok_route(mock, **kw):
    return mock.get(URL).respond(200, json=OK, **kw)


@respx.mock
async def test_token_sent_but_never_in_result_or_cache_key():
    meta, _, _ = build()
    route = ok_route(respx)
    res = await meta.fetch(PARAMS, 900, "c")
    sent = route.calls[0].request.url
    assert sent.params["access_token"] == TOKEN
    assert sent.params["ad_reached_countries"] == '["DE"]'
    assert res.after == "AFTER" and "access_token" not in json.dumps(res.data)
    assert TOKEN not in meta.cache_key(PARAMS)


@respx.mock
async def test_cache_hit_does_not_spend_budget():
    meta, _, _ = build()
    route = ok_route(respx)
    a = await meta.fetch(PARAMS, 900, "c")
    b = await meta.fetch(PARAMS, 900, "c")
    assert (a.cached, a.calls, b.cached, b.calls) == (False, 1, True, 0)
    assert route.call_count == 1 and meta.budget.snapshot()["hour_used"] == 1
    assert meta.cache.stats()["hit_rate"] == 0.5


@respx.mock
async def test_ttl_expiry_refetches():
    meta, clock, _ = build()
    route = ok_route(respx)
    await meta.fetch(PARAMS, 900, "c")
    clock.t += 901
    res = await meta.fetch(PARAMS, 900, "c")
    assert route.call_count == 2 and not res.cached


@respx.mock
async def test_key_ignores_param_order_and_differs_by_params():
    meta, _, _ = build()
    assert meta.cache_key({"a": 1, "b": 2}) == meta.cache_key({"b": 2, "a": 1})
    assert meta.cache_key({"a": 1}) != meta.cache_key({"a": 2})


@respx.mock
async def test_stale_fallback_when_budget_exhausted():
    meta, clock, _ = build(META_MAX_CALLS_PER_HOUR="1", COLD_START_BUDGET_FACTOR="1")
    ok_route(respx)
    await meta.fetch(PARAMS, 900, "c")
    clock.t += 1000  # fresh window gone, still within one hour -> budget exhausted
    res = await meta.fetch(PARAMS, 900, "c")
    assert res.stale and res.cached and res.data[0]["id"] == "1"


@respx.mock
async def test_stale_fallback_on_meta_error_and_expiry_after_24h():
    meta, clock, _ = build()
    respx.get(URL).mock(side_effect=[httpx.Response(200, json=OK)] + [httpx.Response(500)] * 3 + [httpx.Response(500)] * 3)
    await meta.fetch(PARAMS, 900, "c")
    clock.t += 2000
    assert (await meta.fetch(PARAMS, 900, "c")).stale
    clock.t += 86400  # older than 24h: no stale data allowed any more
    with pytest.raises(MetaError):
        await meta.fetch(PARAMS, 900, "c")


@respx.mock
async def test_single_flight_shares_one_call():
    meta, _, _ = build()
    gate = asyncio.Event()

    async def handler(request):
        await gate.wait()
        return httpx.Response(200, json=OK)

    route = respx.get(URL).mock(side_effect=handler)
    tasks = [asyncio.create_task(meta.fetch(PARAMS, 900, f"c{i}")) for i in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert route.call_count == 1
    assert sorted(r.calls for r in results) == [0, 0, 0, 0, 1]


@respx.mock
async def test_budget_exhaustion_message_and_no_call():
    meta, _, _ = build(META_MAX_CALLS_PER_HOUR="2", COLD_START_BUDGET_FACTOR="1")
    route = ok_route(respx)
    for i in range(2):
        await meta.fetch({**PARAMS, "search_terms": str(i)}, 900, "c")
    with pytest.raises(MetaError) as e:
        await meta.fetch({**PARAMS, "search_terms": "z"}, 900, "c")
    assert "retry_after_seconds=" in e.value.message and e.value.message.endswith("Do not retry this call.")
    assert route.call_count == 2


@respx.mock
async def test_per_client_bucket_isolated_per_client():
    meta, _, _ = build(CLIENT_MAX_CALLS_PER_MINUTE="2", META_MAX_CALLS_PER_HOUR="50", COLD_START_BUDGET_FACTOR="1")
    ok_route(respx)
    for i in range(2):
        await meta.fetch({**PARAMS, "search_terms": str(i)}, 900, "alice")
    with pytest.raises(MetaError, match="per-minute"):
        await meta.fetch({**PARAMS, "search_terms": "n"}, 900, "alice")
    await meta.fetch({**PARAMS, "search_terms": "n"}, 900, "bob")


def test_cold_start_halves_budget_until_window_passes():
    clock = Clock()
    b = MemoryBudget(10, 100, 100, 0.5, clock)
    assert b.snapshot()["hour_left"] == 5
    clock.t += 3601
    assert b.snapshot()["hour_left"] == 10


def test_budget_unknown_state_denies():
    from app.store import BudgetExhausted

    b = MemoryBudget(10, 100, 100, 1.0)
    b._hour = None  # corrupt state
    with pytest.raises(BudgetExhausted):
        b.acquire("c")


@respx.mock
async def test_backoff_retries_twice_honours_retry_after_and_counts_budget():
    meta, _, sleeps = build()
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(503, headers={"retry-after": "3"}), httpx.Response(500), httpx.Response(200, json=OK)])
    res = await meta.fetch(PARAMS, 900, "c")
    assert route.call_count == 3 and res.calls == 3
    assert 3 <= sleeps[0] < 3.6 and len(sleeps) == 2
    assert meta.budget.snapshot()["hour_used"] == 3


@respx.mock
async def test_gives_up_after_two_retries():
    meta, _, _ = build()
    route = respx.get(URL).respond(502)
    with pytest.raises(MetaError, match="Do not retry"):
        await meta.fetch(PARAMS, 900, "c")
    assert route.call_count == 3


@respx.mock
async def test_huge_retry_after_is_not_waited_on():
    meta, _, sleeps = build()
    route = respx.get(URL).respond(429, headers={"retry-after": "3600"})
    with pytest.raises(MetaError) as e:
        await meta.fetch(PARAMS, 900, "c")
    assert route.call_count == 1 and not sleeps and e.value.retry_after == 3600


@respx.mock
async def test_circuit_breaker_opens_then_recovers():
    meta, clock, _ = build(BREAKER_FAILURES="3", BREAKER_COOLDOWN="60", META_MAX_CALLS_PER_HOUR="100",
                           COLD_START_BUDGET_FACTOR="1")
    route = respx.get(URL).respond(500)
    with pytest.raises(MetaError):
        await meta.fetch(PARAMS, 900, "c")  # 3 attempts -> breaker opens
    n = route.call_count
    with pytest.raises(MetaError, match="paused"):
        await meta.fetch({**PARAMS, "search_terms": "other"}, 900, "c")
    assert route.call_count == n  # no call while open
    clock.t += 61
    respx.get(URL).respond(200, json=OK)
    assert (await meta.fetch({**PARAMS, "search_terms": "other"}, 900, "c")).data


@respx.mock
@pytest.mark.parametrize(("err", "health"), [
    ({"code": 190, "error_subcode": 463, "message": "Error validating access token: Session has expired"}, "expired"),
    ({"code": 190, "message": "Invalid OAuth access token."}, "unauthorised"),
    ({"code": 10, "error_subcode": 2332002, "message": "Application does not have permission"}, "unauthorised"),
])
async def test_token_errors_never_retry_and_block_ten_minutes(err, health):
    meta, clock, _ = build()
    route = respx.get(URL).respond(400, json={"error": err})
    with pytest.raises(MetaError, match="Do not retry"):
        await meta.fetch(PARAMS, 900, "c")
    assert route.call_count == 1 and meta.token_health == health
    with pytest.raises(MetaError):
        await meta.fetch({**PARAMS, "search_terms": "again"}, 900, "c")
    assert route.call_count == 1  # blocked, no Meta call
    clock.t += 601
    respx.get(URL).respond(200, json=OK)
    await meta.fetch({**PARAMS, "search_terms": "again"}, 900, "c")
    assert meta.token_health == "ok"


@respx.mock
async def test_rate_limit_error_code_stops_calls():
    meta, _, _ = build()
    route = respx.get(URL).respond(400, json={"error": {"code": 4, "message": "limit"}})
    with pytest.raises(MetaError, match="rate limit"):
        await meta.fetch(PARAMS, 900, "c")
    with pytest.raises(MetaError):
        await meta.fetch({**PARAMS, "search_terms": "b"}, 900, "c")
    assert route.call_count == 1


@respx.mock
async def test_app_usage_warn_throttle_stop():
    meta, clock, _ = build(META_MAX_CALLS_PER_HOUR="10", COLD_START_BUDGET_FACTOR="1")
    respx.get(URL).respond(200, json=OK, headers={"x-app-usage": json.dumps({"call_count": 72, "total_time": 10})})
    res = await meta.fetch(PARAMS, 900, "c")
    assert res.warn == "Meta app usage at 72%."

    respx.get(URL).respond(200, json=OK, headers={"x-app-usage": json.dumps({"call_count": 86})})
    await meta.fetch({**PARAMS, "search_terms": "b"}, 900, "c")
    # throttled: hourly cap is halved (10 -> 5); 2 used so far, so 3 more are allowed
    for i in range(3):
        await meta.fetch({**PARAMS, "search_terms": f"t{i}"}, 900, "c")
    with pytest.raises(MetaError, match="hourly"):
        await meta.fetch({**PARAMS, "search_terms": "t9"}, 900, "c")

    meta2, _, _ = build()
    route = respx.get(URL).respond(200, json=OK, headers={"x-app-usage": json.dumps({"total_cputime": 96})})
    before = route.call_count
    await meta2.fetch(PARAMS, 900, "c")
    with pytest.raises(MetaError, match="stopped"):
        await meta2.fetch({**PARAMS, "search_terms": "b"}, 900, "c")
    assert route.call_count == before + 1


@respx.mock
async def test_oversized_response_rejected():
    meta, _, _ = build()
    meta.s = make_settings(META_MOCK="false")
    object.__setattr__(meta.s, "max_meta_response_bytes", 100)
    respx.get(URL).respond(200, content=b"x" * 5000)
    with pytest.raises(MetaError, match="large"):
        await meta.fetch(PARAMS, 900, "c")


@respx.mock
async def test_network_error_message_has_no_url_or_token(caplog):
    meta, _, _ = build()
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"boom {URL}?access_token={TOKEN}"))
    with pytest.raises(MetaError) as e:
        await meta.fetch(PARAMS, 900, "c")
    assert TOKEN not in e.value.message and "access_token" not in e.value.message


@respx.mock
async def test_snapshot_url_token_is_stripped():
    meta, _, _ = build()
    body = {"data": [{"id": "1", "ad_snapshot_url": "https://www.facebook.com/ads/archive/render_ad/?id=1&access_token=EAAleak1234567"}]}
    respx.get(URL).respond(200, json=body)
    res = await meta.fetch(PARAMS, 900, "c")
    assert "access_token" not in res.data[0]["ad_snapshot_url"] and "EAAleak" not in json.dumps(res.data)


@respx.mock
async def test_only_graph_host_is_contacted():
    meta, _, _ = build()
    ok_route(respx)
    await meta.fetch(PARAMS, 900, "c")
    assert {c.request.url.host for c in respx.calls} == {"graph.facebook.com"}
