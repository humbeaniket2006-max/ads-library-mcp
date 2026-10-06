import json
import logging

import httpx
import respx

from app.redact import RedactingFilter, Redactor, log_event, redact, setup_logging

from .conftest import KEY_A, SECRET, TOKEN, Rpc, make_settings, run_lifespan


def test_redactor_masks_tokens_urls_and_secrets():
    r = Redactor([SECRET, KEY_A])
    text = (f"GET https://graph.facebook.com/v26.0/ads_archive?access_token={TOKEN}&limit=1 "
            f'{{"access_token": "{TOKEN}"}} EAAxxxxxxxxxxxxxxxxxx Authorization: Bearer {KEY_A} path /mcp/{SECRET}')
    out = r(text)
    for leaked in (TOKEN, SECRET, KEY_A, "EAAxxxxxxxx"):
        assert leaked not in out
    assert "limit=1" in out


def test_logs_are_redacted_including_exceptions(capsys):
    redact.set_secrets([SECRET])
    setup_logging("INFO")
    log = logging.getLogger("t")
    try:
        raise httpx.ConnectError(f"https://graph.facebook.com/x?access_token={TOKEN} secret={SECRET}")
    except httpx.ConnectError:
        log.exception("failed with %s", TOKEN)
    log_event("event", token=TOKEN, note=f"x {SECRET}")
    out = capsys.readouterr().out
    assert out and TOKEN not in out and SECRET not in out


def test_filter_redacts_message_args():
    rec = logging.LogRecord("n", logging.INFO, "f", 1, "tok %s", (TOKEN,), None)
    RedactingFilter().filter(rec)
    assert TOKEN not in rec.msg


@respx.mock
async def test_tool_error_responses_never_contain_token(capsys):
    from app.main import create_app

    app = create_app(make_settings(META_MOCK="false"))
    task, done = await run_lifespan(app)
    respx.get("https://graph.facebook.com/v26.0/ads_archive").respond(
        400, json={"error": {"code": 190, "message": f"Invalid token {TOKEN}"}})
    r = Rpc(app)
    err, text = await r.call("search_ads", countries=["DE"], query="x")
    st = await r.json_call("get_status")
    await r.http.aclose()
    done.set()
    await task
    assert err and TOKEN not in text and "Do not retry" in text and "\n" not in text and "Traceback" not in text
    assert st["token"] == "unauthorised" and st["mock"] is False
    assert TOKEN not in capsys.readouterr().out


async def test_get_status_shape_and_no_meta_call(rpc):
    with respx.mock(assert_all_called=False) as m:
        route = m.get("https://graph.facebook.com/v26.0/ads_archive")
        st = await rpc.json_call("get_status")
        assert route.call_count == 0
    assert st["cold_start"] is True and st["mock"] is True
    assert set(st) >= {"filters", "budget", "cache_hit_rate", "token", "mock", "cold_start", "meta"}
    assert set(st["budget"]) == {"hour", "day"} and set(st["budget"]["hour"]) == {"used", "left"}
    assert "active" in st["filters"]["status"]
    await rpc.json_call("search_ads", countries=["DE"], query="Nordlicht")
    assert (await rpc.json_call("get_status"))["cold_start"] is False


async def test_response_meta_object_shape(rpc):
    out = await rpc.json_call("search_ads", countries=["DE"], query="Nordlicht")
    assert set(out["meta"]) >= {"n", "cached", "stale", "cc", "mock"}
    again = await rpc.json_call("search_ads", countries=["DE"], query="Nordlicht")
    assert again["meta"]["cached"] is True
    assert json.dumps(out, separators=(",", ":")) == json.dumps(out, separators=(",", ":"))
