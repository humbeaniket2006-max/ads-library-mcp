"""ASGI entrypoint: `uvicorn app.main:app`. Heavy imports are lazy so the process starts fast."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from base64 import b64decode
from collections import deque
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode

from . import context
from .auth import Authenticator, StaticAuthenticator
from .config import ConfigError, Settings
from .models import TOOL_PARAMS
from .redact import log_event, redact, setup_logging

MAX_BODY = 64 * 1024
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000"),
]


OAUTH_PATHS = frozenset({
    "/authorize", "/token", "/register", "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp", "/oauth/consent", "/oauth/google/callback",
})


class RateWindow:
    """Small per-key sliding window limiter (used per client IP on the unauthenticated OAuth endpoints)."""

    def __init__(self, limit: int, seconds: int, clock=time.monotonic):
        self.limit, self.seconds, self._clock = limit, seconds, clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.seconds:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 2000:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.seconds}
        return True


class Gateway:
    """Pure-ASGI front door: health, host check, auth, body limit, unknown-param rejection, OAuth routing."""

    def __init__(self, inner: Any, settings: Settings, auth: Authenticator | None = None, oauth: Any = None):
        self.inner, self.s, self.oauth = inner, settings, oauth
        self.auth = auth or StaticAuthenticator(settings)
        self.hosts = {h.lower() for h in settings.allowed_hosts}
        self._oauth_limit = RateWindow(60, 60)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.inner(scope, receive, send)
        if scope["type"] != "http":
            return
        started = False

        async def send_h(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                kept = [(k, v) for k, v in msg.get("headers", []) if not k.lower().startswith(b"access-control-")]
                present = {k.lower() for k, _ in kept}  # no CORS: the SDK's wildcard CORS headers are dropped
                msg = {**msg, "headers": [*kept, *[h for h in SECURITY_HEADERS if h[0] not in present]]}
            await send(msg)

        try:
            await self._handle(scope, receive, send_h)
        except Exception as exc:  # generic body; details only in redacted logs
            log_event("unhandled error", logging.ERROR, error=redact(type(exc).__name__))
            if not started:
                await self._json(send_h, 500, {"error": "internal error"})

    @staticmethod
    async def _json(send, status: int, body: Any, extra: list | None = None) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
                                *(extra or [])]})
        await send({"type": "http.response.body", "body": raw})

    def _ip(self, scope, headers: dict[bytes, bytes]) -> str:
        v = headers.get(self.s.client_ip_header.encode(), b"").decode("latin-1").strip()
        if v and len(v) <= 64 and all(c.isalnum() or c in ".:" for c in v):
            return v
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def _handle(self, scope, receive, send):
        path = scope.get("path", "")
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if path == "/healthz" and scope["method"] in ("GET", "HEAD"):
            return await self._json(send, 200, {"ok": True})  # no auth, no Meta call, no details

        host = headers.get(b"host", b"").decode("latin-1").lower()
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if hostname not in self.hosts and host not in self.hosts:
            return await self._json(send, 421, {"error": "misdirected request"})

        ip = self._ip(scope, headers)
        oauth_on = self.oauth is not None
        context.request_id.set(uuid.uuid4().hex[:12])

        if oauth_on and path in OAUTH_PATHS:  # unauthenticated by design: discovery, DCR, authorize, token, callbacks
            if not self._oauth_limit.allow(ip):
                return await self._json(send, 429, {"error": "too many requests"}, [(b"retry-after", b"60")])
            body = await self._read_body(headers, receive)
            if body is None:
                return await self._json(send, 413, {"error": "request too large"})
            if path == "/token":
                scope, body = self._basic_to_form(scope, headers, body)
            return await self.inner(scope, self._replay(body, receive), send)

        authz = headers.get(b"authorization", b"").decode("latin-1") or None
        res = self.auth.authenticate(path, authz)
        passthrough = False
        if not res.ok:
            if oauth_on and path in ("/mcp", "/mcp/"):
                passthrough = True  # the SDK validates OAuth bearer tokens and issues the 401 + discovery pointer
            else:
                log_event("auth failed", logging.WARNING, status=res.status, ip=ip)
                extra = [(b"www-authenticate", b"Bearer")] if res.status == 401 else None
                body = {"error": "unauthorized"} if res.status == 401 else {"error": "not found"}
                return await self._json(send, res.status, body, extra)

        body = await self._read_body(headers, receive)
        if body is None:
            return await self._json(send, 413, {"error": "request too large"})
        if not passthrough or authz:
            bad = self._unknown_params(body)
            if bad is not None:
                return await self._json(send, 200, bad)
        if self._is_tool_call(body):
            send = self._shorten_sdk_errors(send)

        inner_scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}  # never pass the secret inward
        if passthrough:
            context.client_label.set("anonymous")
            context.client_key.set(f"ip:{ip}")  # replaced by the user's identity once the SDK verifies the token
        else:
            context.client_label.set(res.label or "path")
            context.client_key.set(res.label or f"ip:{ip}")
            if oauth_on:  # the SDK now requires a token on /mcp; vouch for this already-authenticated request
                hdrs = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"authorization"]
                hdrs.append((b"authorization", b"Bearer " + self.oauth.provider.gateway_token.encode()))
                inner_scope["headers"] = hdrs
        await self.inner(inner_scope, self._replay(body, receive), send)

    @staticmethod
    def _replay(body: bytes, receive):
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    @staticmethod
    def _basic_to_form(scope, headers: dict[bytes, bytes], body: bytes):
        """Some clients send client_secret_basic. The SDK reads the secret per the client's registered method, so
        fold HTTP Basic credentials into the form body (and drop the header) before it sees the request."""
        value = headers.get(b"authorization", b"").decode("latin-1")
        ctype = headers.get(b"content-type", b"").decode("latin-1").lower()
        if not value.lower().startswith("basic ") or not ctype.startswith("application/x-www-form-urlencoded"):
            return scope, body
        try:
            cid, _, secret = b64decode(value[6:].strip(), validate=True).decode().partition(":")
        except ValueError:
            return scope, body
        params = parse_qsl(body.decode("latin-1"), keep_blank_values=True)
        keys = {k for k, _ in params}
        if "client_id" not in keys:
            params.append(("client_id", unquote(cid)))
        if "client_secret" not in keys and secret:
            params.append(("client_secret", unquote(secret)))
        new = urlencode(params).encode()
        hdrs = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in (b"authorization", b"content-length")]
        hdrs.append((b"content-length", str(len(new)).encode()))
        return {**scope, "headers": hdrs}, new

    async def _read_body(self, headers, receive) -> bytes | None:
        try:
            if int(headers.get(b"content-length", b"0") or 0) > MAX_BODY:
                return None
        except ValueError:
            return None
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY:
                return None
            chunks.append(chunk)
            if not msg.get("more_body"):
                break
        return b"".join(chunks)

    @staticmethod
    def _is_tool_call(body: bytes) -> bool:
        try:
            msg = json.loads(body)
        except ValueError:
            return False
        return isinstance(msg, dict) and msg.get("method") == "tools/call"

    @staticmethod
    def _shorten_sdk_errors(send):
        """The SDK reports argument type errors as a multi-line pydantic dump; make it one sentence."""
        held: list[dict] = []

        async def wrapped(msg):
            if msg["type"] == "http.response.start":
                held.append(msg)
                return
            if msg["type"] != "http.response.body" or not held:
                return await send(msg)
            held.append(msg)
            if msg.get("more_body"):
                return
            start, chunks = held[0], [m.get("body", b"") for m in held[1:]]
            raw = b"".join(chunks)
            try:
                doc = json.loads(raw)
                block = doc["result"]["content"][0]
                text = block["text"]
                if doc["result"].get("isError") and "validation error" in text:
                    lines = text.splitlines()
                    param = lines[1].strip()[:40] if len(lines) > 1 else "input"
                    block["text"] = f"Error executing tool: invalid {param}: wrong type or value."
                    raw = json.dumps(doc, separators=(",", ":")).encode()
            except (ValueError, KeyError, IndexError, TypeError):
                pass
            hdrs = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
            hdrs.append((b"content-length", str(len(raw)).encode()))
            await send({**start, "headers": hdrs})
            await send({"type": "http.response.body", "body": raw})

        return wrapped

    @staticmethod
    def _unknown_params(body: bytes) -> dict | None:
        """Reject tools/call with arguments the tool does not declare (JSON-RPC -32602)."""
        try:
            msg = json.loads(body) if body else None
        except ValueError:
            return None  # let the MCP layer produce its parse error
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if not isinstance(m, dict) or m.get("method") != "tools/call":
                continue
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            allowed = TOOL_PARAMS.get(params.get("name"))
            args = params.get("arguments") or {}
            if allowed is None or not isinstance(args, dict):
                continue
            extra = sorted(str(k) for k in args if k not in allowed)
            if extra:
                shown = extra[0][:40].encode("ascii", "replace").decode()
                return {"jsonrpc": "2.0", "id": m.get("id"),
                        "error": {"code": -32602, "message": f"Unknown parameter '{shown}'."}}
        return None


def create_app(settings: Settings | None = None) -> Gateway:
    from mcp.server.transport_security import TransportSecuritySettings

    from .redact import redact as redactor
    from .service import Service
    from .tools import build_server

    s = settings or Settings.load()
    redactor.set_secrets(s.secrets)
    setup_logging(s.log_level)
    svc = Service(s)
    hosts = [*s.allowed_hosts, *[f"{h}:*" for h in s.allowed_hosts if ":" not in h]]
    oauth = None
    if s.oauth_enabled:
        from .oauth.google import GoogleClient
        from .oauth.provider import OAuthProvider
        from .oauth.routes import OAuthRuntime
        from .oauth.tokens import TokenCodec

        codec = TokenCodec(s.oauth_signing_key, s.public_base_url)
        google = GoogleClient(s.google_client_id, s.google_client_secret) if s.oauth_login else None
        oauth = OAuthRuntime(s, codec, OAuthProvider(s, codec), google)
    inner = build_server(svc, oauth).streamable_http_app(
        stateless_http=True, json_response=True, streamable_http_path="/mcp",
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts),
    )
    if s.meta_mock:
        log_event("META_MOCK=true: serving fixtures, no calls to Meta", logging.WARNING)
    log_event("cold start, cache and budgets reset")
    gw = Gateway(inner, s, oauth=oauth)
    gw.service = svc  # exposed for tests
    return gw


_app: Gateway | None = None


def __getattr__(name: str):
    """`uvicorn app.main:app` builds the app lazily; invalid config exits with one line."""
    global _app
    if name == "app":
        if _app is None:
            try:
                _app = create_app()
            except ConfigError as exc:
                sys.stderr.write(f"Configuration error: {exc}\n")
                sys.exit(1)
        return _app
    raise AttributeError(name)
