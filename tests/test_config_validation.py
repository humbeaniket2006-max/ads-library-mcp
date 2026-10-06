import pytest
from pydantic import ValidationError

from app.config import ConfigError, Settings, parse_api_keys
from app.models import FindAdvertiser, SearchAds, SummarizeAdvertiser, first_error

from .conftest import KEY_A, make_env, make_settings


@pytest.mark.parametrize("over", [
    {"MCP_PATH_SECRET": "short"},
    {"MCP_PATH_SECRET": ""},
    {"MCP_PATH_SECRET": "a/" * 20},
    {"CURSOR_SIGNING_KEY": "short"},
    {"MCP_AUTH_MODE": "oauth"},
    {"MCP_AUTH_MODE": "bearer", "MCP_API_KEYS": ""},
    {"META_MOCK": "false", "META_ACCESS_TOKEN": ""},
    {"GRAPH_API_VERSION": "latest"},
    {"META_MAX_CALLS_PER_HOUR": "lots"},
    {"USAGE_WARN_PCT": "90", "USAGE_STOP_PCT": "80"},
    {"ALLOWED_HOSTS": "*"},
    {"MCP_API_KEYS": "label-only"},
    {"MCP_API_KEYS": "x:short"},
])
def test_invalid_config_fails_closed(over):
    with pytest.raises(ConfigError):
        Settings.load(make_env(**over))


def test_defaults_match_spec():
    s = make_settings()
    assert (s.max_calls_per_hour, s.max_calls_per_day, s.client_max_calls_per_minute) == (60, 500, 10)
    assert (s.cache_ttl_active, s.cache_ttl_historical, s.cache_ttl_advertiser) == (900, 21600, 86400)
    assert (s.default_limit, s.max_limit, s.max_pages_per_call, s.max_summary_pages) == (10, 50, 1, 2)
    assert (s.ad_text_max_chars, s.max_response_chars, s.graph_api_version) == (300, 12000, "v26.0")
    assert (s.usage_warn_pct, s.usage_throttle_pct, s.usage_stop_pct) == (70, 85, 95)
    assert Settings.load({**make_env(), "META_MOCK": ""}).meta_mock is False  # code default is false


def test_render_hostname_is_allowed_automatically():
    s = Settings.load(make_env(ALLOWED_HOSTS="", RENDER_EXTERNAL_HOSTNAME="svc.onrender.com"))
    assert s.allowed_hosts == ("svc.onrender.com",)


def test_secrets_not_in_repr():
    assert "EAA" not in repr(make_settings()) and "ssss" not in repr(make_settings())


def test_api_keys_parse_labelled():
    keys = parse_api_keys(f"alice:{KEY_A},bob:{'b' * 40}")
    assert list(keys) == ["alice", "bob"]
    with pytest.raises(ConfigError):
        parse_api_keys(f"a:{KEY_A},a:{KEY_A}")


def test_search_requires_query_or_page_ids():
    with pytest.raises(ValueError, match="query or page_ids"):
        SearchAds(countries=["DE"]).check()
    SearchAds(countries=["de"], query="x").check()
    assert SearchAds(countries=["de", "DE"], query="x").countries == ["DE"]


@pytest.mark.parametrize("kwargs", [
    {"countries": []},
    {"countries": ["XX"]},
    {"countries": ["DE", "FR", "IT", "ES", "NL", "PL"]},
    {"countries": ["DE"], "query": "x" * 101},
    {"countries": ["DE"], "query": "bad\x00char"},
    {"countries": ["DE"], "query": "zero​width"},
    {"countries": ["DE"], "query": "x", "page_ids": ["12a"]},
    {"countries": ["DE"], "query": "x", "page_ids": [str(i) for i in range(11)]},
    {"countries": ["DE"], "query": "x", "status": "banana"},
    {"countries": ["DE"], "query": "x", "ad_type": "housing"},
    {"countries": ["DE"], "query": "x", "date_from": "2024-13-01"},
    {"countries": ["DE"], "query": "x", "date_from": "01/02/2024"},
    {"countries": ["DE"], "query": "x", "platforms": ["myspace"]},
    {"countries": ["DE"], "query": "x", "languages": ["english"]},
    {"countries": ["DE"], "query": "x", "media": "gif"},
    {"countries": ["DE"], "query": "x", "limit": 0},
    {"countries": ["DE"], "query": "x", "detail": "huge"},
    {"countries": ["DE"], "query": "x", "unknown": 1},
])
def test_search_validation_rejects(kwargs):
    with pytest.raises(ValidationError):
        SearchAds(**kwargs)


def test_date_order_checked():
    with pytest.raises(ValueError, match="date_from"):
        SearchAds(countries=["DE"], query="x", date_from="2025-02-01", date_to="2025-01-01").check()


def test_other_models_validate():
    with pytest.raises(ValidationError):
        FindAdvertiser(name="", countries=["DE"])
    with pytest.raises(ValidationError):
        SummarizeAdvertiser(page_id="abc", countries=["DE"])
    with pytest.raises(ValidationError):
        SummarizeAdvertiser(page_id="1", countries=["DE"], extra=1)
    assert SummarizeAdvertiser(page_id="1", countries=["DE"]).status == "all"


def test_error_sentence_is_short_and_does_not_echo_input():
    try:
        SearchAds(countries=["DE"], query="x" * 500)
    except ValidationError as exc:
        msg = first_error(exc)
    assert msg.endswith(".") and "xxxxx" not in msg and len(msg) < 150
