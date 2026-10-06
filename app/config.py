"""Environment configuration. Invalid config raises ConfigError (the process exits at startup)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DEFAULT_GRAPH_VERSION = "v26.0"
MIN_SECRET_LEN = 32
AUTH_MODES = ("path_secret", "bearer", "oauth", "oauth_client")


def parse_auth_modes(raw: str) -> frozenset[str]:
    """MCP_AUTH_MODE: path_secret | bearer | oauth | oauth_client | both (= path_secret+bearer) or a comma list."""
    modes: set[str] = set()
    for part in [p.strip().lower() for p in raw.split(",") if p.strip()]:
        if part == "both":
            modes |= {"path_secret", "bearer"}
        elif part in AUTH_MODES:
            modes.add(part)
        else:
            raise ConfigError("MCP_AUTH_MODE must be path_secret, bearer, oauth, oauth_client, both, or a comma list.")
    if not modes:
        raise ConfigError("MCP_AUTH_MODE must not be empty.")
    return frozenset(modes)


def _list(env: dict[str, str], key: str) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in env.get(key, "").replace(";", ",").split(",") if x.strip())


def _base_url(raw: str) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(raw.strip())
    local = u.hostname in ("localhost", "127.0.0.1")
    if (u.scheme != "https" and not (u.scheme == "http" and local)) or not u.hostname:
        raise ConfigError("PUBLIC_BASE_URL must be an https URL (http only for localhost).")
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        raise ConfigError("PUBLIC_BASE_URL must be a bare origin like https://host (no path, query or credentials).")
    return f"{u.scheme}://{u.netloc.lower()}"


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
    auth_modes: frozenset[str] = frozenset({"path_secret"})
    path_secret: str = field(default="", repr=False)
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    cursor_signing_key: str = field(default="", repr=False)
    client_ip_header: str = "true-client-ip"

    # OAuth (only used when "oauth" is in auth_modes)
    public_base_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    oauth_signing_key: str = field(default="", repr=False)
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    allowed_emails: frozenset[str] = frozenset()
    allowed_email_domains: frozenset[str] = frozenset()
    oauth_access_ttl: int = 3600
    oauth_refresh_ttl: int = 30 * 86400
    oauth_code_ttl: int = 60

    max_calls_per_hour: int = 60
    max_calls_per_day: int = 500
    client_max_calls_per_minute: int = 10
    client_max_calls_per_hour: int = 30
    client_max_calls_per_day: int = 200
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
    def oauth_enabled(self) -> bool:
        return bool({"oauth", "oauth_client"} & self.auth_modes)

    @property
    def oauth_login(self) -> bool:
        """Users sign in with Google (and DCR is open to Claude)."""
        return "oauth" in self.auth_modes

    @property
    def oauth_client_login(self) -> bool:
        """The pre-registered client is approved automatically; its secret is the only credential."""
        return "oauth_client" in self.auth_modes

    @property
    def secrets(self) -> list[str]:
        vals = [self.meta_access_token, self.path_secret, self.cursor_signing_key, self.google_client_secret,
                self.oauth_signing_key, self.oauth_client_secret, *self.api_keys.values()]
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

        modes = parse_auth_modes(env.get("MCP_AUTH_MODE", "path_secret"))
        secret = env.get("MCP_PATH_SECRET", "").strip()
        if "path_secret" in modes:
            if len(secret) < MIN_SECRET_LEN:
                raise ConfigError(f"MCP_PATH_SECRET must be at least {MIN_SECRET_LEN} characters.")
            # Render's generateValue emits base64 (letters, digits, + / =), so accept that alphabet too.
            if not re.fullmatch(r"[A-Za-z0-9_\-+/=.~]+", secret):
                raise ConfigError("MCP_PATH_SECRET contains unsupported characters.")
        keys = parse_api_keys(env.get("MCP_API_KEYS", ""))
        if "bearer" in modes and not keys:
            raise ConfigError("MCP_API_KEYS is required for bearer auth.")

        signing = env.get("CURSOR_SIGNING_KEY", "").strip()
        if len(signing) < MIN_SECRET_LEN:
            raise ConfigError(f"CURSOR_SIGNING_KEY must be at least {MIN_SECRET_LEN} characters.")

        oauth = _load_oauth(env, modes)

        hosts = [h.strip().lower() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
        if oauth["public_base_url"]:
            from urllib.parse import urlsplit

            hosts.append(urlsplit(oauth["public_base_url"]).netloc)
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
            auth_modes=modes,
            path_secret=secret,
            api_keys=keys,
            allowed_hosts=tuple(dict.fromkeys(hosts)),
            cursor_signing_key=signing,
            client_ip_header=env.get("CLIENT_IP_HEADER", "true-client-ip").strip().lower(),
            **oauth,
            client_max_calls_per_hour=_int(env, "CLIENT_MAX_CALLS_PER_HOUR", 30, 1, 100_000),
            client_max_calls_per_day=_int(env, "CLIENT_MAX_CALLS_PER_DAY", 200, 1, 1_000_000),
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


def _load_oauth(env: dict[str, str], modes: frozenset[str]) -> dict:
    """Validate OAuth settings. Required and strict when an oauth mode is enabled; ignored otherwise.

    oauth        = users sign in with Google, checked against an allowlist; Dynamic Client Registration is open.
    oauth_client = no user login: the pre-registered client (ID + secret) is the credential; DCR is closed.
    """
    out: dict = {}
    login, client_mode = "oauth" in modes, "oauth_client" in modes
    if not (login or client_mode):
        return {"public_base_url": ""}
    base = env.get("PUBLIC_BASE_URL", "").strip() or env.get("RENDER_EXTERNAL_URL", "").strip()
    if not base:
        raise ConfigError("PUBLIC_BASE_URL (or Render's RENDER_EXTERNAL_URL) is required for oauth.")
    out["public_base_url"] = _base_url(base)

    key = env.get("OAUTH_SIGNING_KEY", "").strip()
    if len(key) < MIN_SECRET_LEN:
        raise ConfigError(f"OAUTH_SIGNING_KEY must be at least {MIN_SECRET_LEN} characters.")
    out["oauth_signing_key"] = key

    cid, csecret = env.get("MCP_OAUTH_CLIENT_ID", "").strip(), env.get("MCP_OAUTH_CLIENT_SECRET", "").strip()
    if (client_mode or cid or csecret) and (not cid or not csecret):
        raise ConfigError("MCP_OAUTH_CLIENT_ID and MCP_OAUTH_CLIENT_SECRET must be set together.")
    if csecret and len(csecret) < MIN_SECRET_LEN:
        raise ConfigError(f"MCP_OAUTH_CLIENT_SECRET must be at least {MIN_SECRET_LEN} characters.")
    if cid and not re.fullmatch(r"[A-Za-z0-9._~\-]{3,100}", cid):
        raise ConfigError("MCP_OAUTH_CLIENT_ID may only contain letters, digits and . _ ~ -")
    out["oauth_client_id"], out["oauth_client_secret"] = cid, csecret

    if login:
        gid, gsecret = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(), env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        if not gid or not gsecret:
            raise ConfigError("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are required for oauth.")
        out["google_client_id"], out["google_client_secret"] = gid, gsecret
        emails, domains = _list(env, "ALLOWED_EMAILS"), _list(env, "ALLOWED_EMAIL_DOMAINS")
        if not emails and not domains:  # deny by default
            raise ConfigError("Set ALLOWED_EMAILS and/or ALLOWED_EMAIL_DOMAINS; oauth denies everyone by default.")
        if any("@" not in e or e.startswith("@") or e.endswith("@") for e in emails):
            raise ConfigError("ALLOWED_EMAILS entries must be full email addresses.")
        if any("@" in d or "." not in d for d in domains):
            raise ConfigError("ALLOWED_EMAIL_DOMAINS entries must be bare domains like example.com.")
        out["allowed_emails"], out["allowed_email_domains"] = emails, domains

    access = _int(env, "OAUTH_ACCESS_TOKEN_TTL", 3600, 60, 86400)
    refresh = _int(env, "OAUTH_REFRESH_TOKEN_TTL", 30 * 86400, 3600, 90 * 86400)
    code = _int(env, "OAUTH_CODE_TTL", 60, 10, 300)
    if refresh <= access:
        raise ConfigError("OAUTH_REFRESH_TOKEN_TTL must be longer than OAUTH_ACCESS_TOKEN_TTL.")
    out.update(oauth_access_ttl=access, oauth_refresh_ttl=refresh, oauth_code_ttl=code)
    return out
