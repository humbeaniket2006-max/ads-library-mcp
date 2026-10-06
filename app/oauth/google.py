"""Upstream sign-in with Google (OpenID Connect, authorization code flow).

Only these three Google endpoints are ever contacted. Endpoint values are from Google's published OpenID
configuration (https://accounts.google.com/.well-known/openid-configuration).
"""
from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from ..redact import log_event

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
JWKS_TTL = 3600
JWKS_MIN_REFETCH = 60
LEEWAY = 60


class GoogleError(Exception):
    """Sign-in with Google failed. The message never contains upstream response bodies or tokens."""


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    sub: str
    email_verified: bool
    hosted_domain: str | None


class GoogleClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient | None = None, clock=time.time):
        self.client_id, self._secret, self._http, self._clock = client_id, client_secret, http, clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = -1e9

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)
        return self._http

    def authorize_url(self, redirect_uri: str, state: str, nonce: str) -> str:
        q = {
            "client_id": self.client_id, "response_type": "code", "scope": "openid email",
            "redirect_uri": redirect_uri, "state": state, "nonce": nonce, "prompt": "select_account",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(q)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        try:
            r = await self.http.post(GOOGLE_TOKEN_URL, data={
                "code": code, "client_id": self.client_id, "client_secret": self._secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            })
            body = r.json()
        except (httpx.HTTPError, ValueError):
            log_event("google token exchange transport error", logging.WARNING)
            raise GoogleError("token exchange failed") from None
        token = body.get("id_token") if isinstance(body, dict) else None
        if r.status_code != 200 or not isinstance(token, str):
            log_event("google token exchange rejected", logging.WARNING, status=r.status_code)
            raise GoogleError("token exchange failed")
        return token

    async def _refresh_keys(self) -> None:
        try:
            r = await self.http.get(GOOGLE_JWKS_URL)
            keys = r.json().get("keys", [])
            if r.status_code != 200:
                raise ValueError
            self._keys = {k["kid"]: jwt.PyJWK(k) for k in keys if k.get("kty") == "RSA" and "kid" in k}
            self._fetched_at = self._clock()
        except (httpx.HTTPError, ValueError, KeyError, AttributeError, jwt.PyJWTError):
            log_event("google jwks fetch failed", logging.WARNING)
            raise GoogleError("could not load Google signing keys") from None

    async def _key_for(self, kid: str) -> jwt.PyJWK:
        age = self._clock() - self._fetched_at
        if age > JWKS_TTL or (kid not in self._keys and age > JWKS_MIN_REFETCH):
            await self._refresh_keys()
        key = self._keys.get(kid)
        if key is None:
            raise GoogleError("unknown signing key")
        return key

    async def verify_id_token(self, id_token: str, nonce: str) -> GoogleIdentity:
        """Signature (RS256, Google JWKS), issuer, audience, expiry (<= 60 s skew), nonce and email_verified."""
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
            key = await self._key_for(str(kid))
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256"], audience=self.client_id, issuer=GOOGLE_ISSUERS,
                leeway=LEEWAY, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except GoogleError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError):
            raise GoogleError("invalid ID token") from None
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise GoogleError("nonce mismatch")
        email = str(claims.get("email", "")).strip().lower()
        verified = claims.get("email_verified") in (True, "true")
        if not email:
            raise GoogleError("no email in ID token")
        hd = claims.get("hd")
        return GoogleIdentity(email, str(claims["sub"]), verified, str(hd).lower() if hd else None)
