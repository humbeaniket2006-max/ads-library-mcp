"""Environment configuration. Invalid config raises ConfigError (the process exits at startup)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DEFAULT_GRAPH_VERSION = "v26.0"
MIN_SECRET_LEN = 32


class ConfigError(Exception):
    pass


def _int(env: dict[str, str], key: str, default: int, lo: int = 0, hi: int = 10**12) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer.") from None
    if not lo <= val <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}.")
    return val


def _float(env: dict[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number.") from None
    if not lo <= val <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}.")
    return val


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false.")


def parse_api_keys(raw: str) -> dict[str, str]:
    """Parse 'label:key,label2:key2' into {label: key}. Labels are logged, keys never."""
    out: dict[str, str] = {}
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        label, sep, key = part.partition(":")
        label, key = label.strip(), key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", label):
            raise ConfigError("MCP_API_KEYS entries must look like LABEL:KEY (label A-Z a-z 0-9 _ . -).")
        if len(key) < MIN_SECRET_LEN:
            raise ConfigError(f"MCP_API_KEYS key for '{label}' must be at least {MIN_SECRET_LEN} characters.")
        if label in out:
            raise ConfigError(f"MCP_API_KEYS label '{label}' is duplicated.")
        out[label] = key
    return out


@dataclass(frozen=True)
class Settings:
    meta_access_token: str = field(default="", repr=False)
    graph_api_version: str = DEFAULT_GRAPH_VERSION
    meta_mock: bool = False
    auth_mode: str = "path_secret"
    path_secret: str = field(default="", repr=False)
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    cursor_signing_key: str = field(default="", repr=False)
    client_ip_header: str = "true-client-ip"

    max_calls_per_hour: int = 60
    max_calls_per_day: int = 500
    client_max_calls_per_minute: int = 10
    cold_start_budget_factor: float = 0.5

    cache_ttl_active: int = 900
    cache_ttl_historical: int = 21600
    cache_ttl_advertiser: int = 86400
    cache_stale_max: int = 86400
    cache_max_entries: int = 500
    cache_max_bytes: int = 20_000_000

    default_limit: int = 10
    max_limit: int = 50
    max_pages_per_call: int = 1
    max_summary_pages: int = 2
    ad_text_max_chars: int = 300
    max_response_chars: int = 12000
    max_meta_response_bytes: int = 2_000_000

    usage_warn_pct: int = 70
    usage_throttle_pct: int = 85
    usage_stop_pct: int = 95
    breaker_failures: int = 5
    breaker_cooldown: int = 60
    log_level: str = "INFO"

    @property
    def secrets(self) -> list[str]:
        vals = [self.meta_access_token, self.path_secret, self.cursor_signing_key, *self.api_keys.values()]
        return [v for v in vals if v]

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        mock = _bool(env, "META_MOCK", False)
        token = env.get("META_ACCESS_TOKEN", "").strip()
        if not mock and not token:
            raise ConfigError("META_ACCESS_TOKEN is required unless META_MOCK=true.")

        version = env.get("GRAPH_API_VERSION", "").strip() or DEFAULT_GRAPH_VERSION
        if not re.fullmatch(r"v\d{1,3}\.\d{1,2}", version):
            raise ConfigError("GRAPH_API_VERSION must look like v26.0.")

        mode = env.get("MCP_AUTH_MODE", "path_secret").strip().lower()
        if mode not in ("path_secret", "bearer", "both"):
            raise ConfigError("MCP_AUTH_MODE must be path_secret, bearer or both.")
        secret = env.get("MCP_PATH_SECRET", "").strip()
        if mode in ("path_secret", "both"):
            if len(secret) < MIN_SECRET_LEN:
                raise ConfigError(f"MCP_PATH_SECRET must be at least {MIN_SECRET_LEN} characters.")
            # Render's generateValue emits base64 (letters, digits, + / =), so accept that alphabet too.
            if not re.fullmatch(r"[A-Za-z0-9_\-+/=.~]+", secret):
                raise ConfigError("MCP_PATH_SECRET contains unsupported characters.")
        keys = parse_api_keys(env.get("MCP_API_KEYS", ""))
        if mode in ("bearer", "both") and not keys:
            raise ConfigError("MCP_API_KEYS is required for bearer auth.")

        signing = env.get("CURSOR_SIGNING_KEY", "").strip()
        if len(signing) < MIN_SECRET_LEN:
            raise ConfigError(f"CURSOR_SIGNING_KEY must be at least {MIN_SECRET_LEN} characters.")

        hosts = [h.strip().lower() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
        render_host = env.get("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
        if render_host:
            hosts.append(render_host)
        if not hosts:
            hosts = ["localhost", "127.0.0.1"]
        for h in hosts:
            if not re.fullmatch(r"[a-z0-9.\-:\[\]]+", h) or h == "*":
                raise ConfigError("ALLOWED_HOSTS contains an invalid host.")

        level = env.get("LOG_LEVEL", "INFO").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.")

        max_limit = _int(env, "MAX_LIMIT", 50, 1, 50)
        warn = _int(env, "USAGE_WARN_PCT", 70, 1, 100)
        throttle = _int(env, "USAGE_THROTTLE_PCT", 85, 1, 100)
        stop = _int(env, "USAGE_STOP_PCT", 95, 1, 100)
        if not warn <= throttle <= stop:
            raise ConfigError("USAGE_WARN_PCT <= USAGE_THROTTLE_PCT <= USAGE_STOP_PCT is required.")

        return cls(
            meta_access_token=token,
            graph_api_version=version,
            meta_mock=mock,
            auth_mode=mode,
            path_secret=secret,
            api_keys=keys,
            allowed_hosts=tuple(dict.fromkeys(hosts)),
            cursor_signing_key=signing,
            client_ip_header=env.get("CLIENT_IP_HEADER", "true-client-ip").strip().lower(),
            max_calls_per_hour=_int(env, "META_MAX_CALLS_PER_HOUR", 60, 1, 100_000),
            max_calls_per_day=_int(env, "META_MAX_CALLS_PER_DAY", 500, 1, 1_000_000),
            client_max_calls_per_minute=_int(env, "CLIENT_MAX_CALLS_PER_MINUTE", 10, 1, 10_000),
            cold_start_budget_factor=_float(env, "COLD_START_BUDGET_FACTOR", 0.5, 0.05, 1.0),
            cache_ttl_active=_int(env, "CACHE_TTL_ACTIVE", 900, 0, 86400 * 7),
            cache_ttl_historical=_int(env, "CACHE_TTL_HISTORICAL", 21600, 0, 86400 * 7),
            cache_ttl_advertiser=_int(env, "CACHE_TTL_ADVERTISER", 86400, 0, 86400 * 7),
            cache_stale_max=_int(env, "CACHE_STALE_MAX", 86400, 0, 86400 * 7),
            cache_max_entries=_int(env, "CACHE_MAX_ENTRIES", 500, 1, 100_000),
            cache_max_bytes=_int(env, "CACHE_MAX_BYTES", 20_000_000, 10_000, 500_000_000),
            default_limit=min(_int(env, "DEFAULT_LIMIT", 10, 1, 50), max_limit),
            max_limit=max_limit,
            max_pages_per_call=_int(env, "MAX_PAGES_PER_CALL", 1, 1, 5),
            max_summary_pages=_int(env, "MAX_SUMMARY_PAGES", 2, 1, 10),
            ad_text_max_chars=_int(env, "AD_TEXT_MAX_CHARS", 300, 20, 2000),
            max_response_chars=_int(env, "MAX_RESPONSE_CHARS", 12000, 500, 100_000),
            usage_warn_pct=warn,
            usage_throttle_pct=throttle,
            usage_stop_pct=stop,
            breaker_failures=_int(env, "BREAKER_FAILURES", 5, 1, 100),
            breaker_cooldown=_int(env, "BREAKER_COOLDOWN", 60, 1, 3600),
            log_level=level,
        )
