"""Signed, self-contained tokens (HS256 JWTs via PyJWT). One signing key, a derived key per purpose.

A token minted for one purpose (e.g. a refresh token) can never be accepted for another (e.g. an access token):
each purpose has its own derived key and its own `typ` header, both checked on decode.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

import jwt

# Our own tokens are issued and checked by the same server clock, so no skew allowance is needed (0 s). The MCP SDK's
# bearer check is strict as well. Only Google's ID tokens, minted by another clock, get a 60 s allowance (google.py).
LEEWAY = 0
PURPOSES = ("client", "authreq", "state", "code", "access", "refresh")


class TokenError(Exception):
    """Invalid, expired, tampered or wrong-purpose token (the reason is never shown to callers)."""


class TokenCodec:
    def __init__(self, signing_key: str, issuer: str, clock=time.time):
        self._root = signing_key.encode()
        self.issuer, self._clock = issuer, clock
        self._keys = {p: hmac.new(self._root, f"meta-ads-mcp/{p}".encode(), hashlib.sha256).digest() for p in PURPOSES}

    def mac(self, label: str, data: str) -> str:
        """Keyed MAC for non-token uses (the consent form's CSRF value)."""
        key = hmac.new(self._root, f"meta-ads-mcp/mac/{label}".encode(), hashlib.sha256).digest()
        return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()

    def encode(self, purpose: str, ttl: int, audience: str | None = None, **claims: Any) -> str:
        now = int(self._clock())
        body: dict[str, Any] = {**claims, "iss": self.issuer, "iat": now, "nbf": now, "exp": now + ttl,
                                "jti": secrets.token_urlsafe(16)}
        if audience:
            body["aud"] = audience
        return jwt.encode(body, self._keys[purpose], algorithm="HS256", headers={"typ": purpose})

    def decode(self, purpose: str, token: str, audience: str | None = None) -> dict[str, Any]:
        try:
            if jwt.get_unverified_header(token).get("typ") != purpose:
                raise TokenError("wrong purpose")
            return jwt.decode(
                token, self._keys[purpose], algorithms=["HS256"], issuer=self.issuer, audience=audience,
                leeway=LEEWAY, options={"require": ["exp", "iat", "iss", "jti"], "verify_aud": audience is not None},
            )
        except TokenError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise TokenError("invalid token") from None


class UsedTokens:
    """Best-effort single-use tracking (per process). Stateless tokens cannot be revoked globally, so this only
    catches replays that hit the same running instance; the short code lifetime is the real bound."""

    def __init__(self, max_entries: int = 10_000, clock=time.time):
        self._seen: dict[str, float] = {}
        self._max, self._clock = max_entries, clock

    def use(self, jti: str, exp: float) -> bool:
        """Mark jti used. Returns False if it was already used."""
        now = self._clock()
        if len(self._seen) >= self._max // 2:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        if jti in self._seen and self._seen[jti] > now:
            return False
        if len(self._seen) >= self._max:  # evict the earliest-expiring entry
            self._seen.pop(min(self._seen, key=self._seen.get), None)
        self._seen[jti] = exp + LEEWAY
        return True
