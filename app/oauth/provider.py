"""Stateless OAuthAuthorizationServerProvider for the MCP SDK.

The SDK supplies /authorize, /token, /register and the discovery metadata; this class supplies the storage
decisions. Nothing is stored: client IDs, codes and tokens are signed JWTs (see tokens.py).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .. import context
from ..config import Settings
from ..format import sanitize_text
from .policy import (
    CLAUDE_CALLBACKS,
    OFFLINE,
    SCOPE,
    VALID_SCOPES,
    canonical_resource,
    email_allowed,
    redirect_allowed,
    same_redirect,
)
from .tokens import TokenCodec, UsedTokens
from .tokens import TokenError as BadToken

CLIENT_TTL = 365 * 86400
AUTHREQ_TTL = 600
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
GRANTS = ["authorization_code", "refresh_token"]


class ClaudeClient(OAuthClientInformationFull):
    """Client record with redirect matching that is exact, except loopback URIs ignore the port."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        registered = [str(u) for u in (self.redirect_uris or [])]
        if redirect_uri is None:
            if len(registered) == 1:
                return AnyUrl(registered[0])
            raise InvalidRedirectUriError("redirect_uri must be specified")
        requested = str(redirect_uri)
        if redirect_allowed(requested) and any(same_redirect(requested, r) for r in registered):
            return redirect_uri
        raise InvalidRedirectUriError("redirect_uri is not registered for this client")


class _Code(AuthorizationCode):
    jti: str
    hd: str | None = None


class _Refresh(RefreshToken):
    jti: str
    hd: str | None = None


def user_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


class OAuthProvider:
    def __init__(self, settings: Settings, codec: TokenCodec, used: UsedTokens | None = None):
        self.s, self.codec = settings, codec
        self.used = used or UsedTokens()
        self.base = settings.public_base_url
        self.resource = f"{self.base}/mcp"
        # Presented by the gateway to the SDK after it has authenticated a request by path secret or static key.
        # Random per process, never leaves it.
        self.gateway_token = secrets.token_urlsafe(32)

    # -- policy -------------------------------------------------------------------------------
    def permitted(self, email: str, hd: str | None) -> bool:
        return email_allowed(email, hd, self.s.allowed_emails, self.s.allowed_email_domains)

    @staticmethod
    def _scopes(requested: list[str] | None) -> list[str]:
        out = [SCOPE]
        if requested and OFFLINE in requested:
            out.append(OFFLINE)
        return out

    # -- clients (DCR is stateless: the client_id is a signed token) -----------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        sid = self.s.oauth_client_id
        if sid and hmac.compare_digest(client_id.encode(), sid.encode()):
            return ClaudeClient(
                client_id=sid, client_secret=self.s.oauth_client_secret,
                token_endpoint_auth_method="client_secret_post",
                redirect_uris=[*CLAUDE_CALLBACKS, "http://localhost/callback", "http://127.0.0.1/callback"],
                grant_types=GRANTS, response_types=["code"], scope=" ".join(VALID_SCOPES), client_name="Claude",
            )
        try:
            c = self.codec.decode("client", client_id)
        except BadToken:
            return None
        return ClaudeClient(
            client_id=client_id, client_secret=None, token_endpoint_auth_method="none", redirect_uris=c["ru"],
            grant_types=GRANTS, response_types=["code"], scope=c["sc"], client_name=c.get("cn"),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > 5 or not all(redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed")
        scopes = (client_info.scope or SCOPE).split()
        if not set(scopes) <= set(VALID_SCOPES):
            raise RegistrationError("invalid_client_metadata", "unsupported scope")
        # Public client: PKCE protects the code, so no secret is issued (RFC 7591 lets the server substitute).
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        client_info.token_endpoint_auth_method = "none"
        client_info.grant_types = [g for g in client_info.grant_types if g in GRANTS] or ["authorization_code"]
        name = sanitize_text(client_info.client_name, 60) or None
        client_info.client_id = self.codec.encode("client", CLIENT_TTL, ru=uris, sc=" ".join(scopes), cn=name)

    # -- authorization (hands off to our consent page, then Google) ------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and canonical_resource(params.resource) != canonical_resource(self.resource):
            raise AuthorizeError("invalid_target", "resource does not identify this server")
        if not _CHALLENGE.match(params.code_challenge or ""):
            raise AuthorizeError("invalid_request", "invalid PKCE code_challenge")
        if params.state is not None and len(params.state) > 512:
            raise AuthorizeError("invalid_request", "state is too long")
        ru = str(params.redirect_uri)
        if not redirect_allowed(ru):
            raise AuthorizeError("invalid_request", "redirect_uri is not allowed")
        req = self.codec.encode(
            "authreq", AUTHREQ_TTL, cid=client.client_id, ru=ru, rue=params.redirect_uri_provided_explicitly,
            cc=params.code_challenge, st=params.state, sc=self._scopes(params.scopes),
            cn=sanitize_text(client.client_name, 60) or None,
        )
        return f"{self.base}/oauth/consent?{urlencode({'req': req})}"

    def issue_code(self, req: dict, email: str, hd: str | None) -> str:
        return self.codec.encode(
            "code", self.s.oauth_code_ttl, cid=req["cid"], ru=req["ru"], rue=req["rue"], cc=req["cc"],
            sc=req["sc"], em=email, hd=hd,
        )

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        try:
            c = self.codec.decode("code", authorization_code)
        except BadToken:
            return None
        return _Code(
            code=authorization_code, scopes=c["sc"], expires_at=c["exp"], client_id=c["cid"],
            code_challenge=c["cc"], redirect_uri=c["ru"], redirect_uri_provided_explicitly=c["rue"],
            resource=self.resource, subject=c["em"], jti=c["jti"], hd=c.get("hd"),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if not self.used.use(authorization_code.jti, authorization_code.expires_at):
            raise TokenError("invalid_grant", "authorization code was already used")
        email = authorization_code.subject or ""
        if not self.permitted(email, authorization_code.hd):
            raise TokenError("invalid_grant", "this account is not allowed")
        return self._issue_pair(email, authorization_code.hd, client.client_id, authorization_code.scopes)

    # -- tokens ---------------------------------------------------------------------------------
    def _issue_pair(self, email: str, hd: str | None, client_id: str, scopes: list[str]) -> OAuthToken:
        claims = {"sub": email, "hd": hd, "cid": client_id, "sc": " ".join(scopes)}
        access = self.codec.encode("access", self.s.oauth_access_ttl, audience=self.resource, **claims)
        refresh = self.codec.encode("refresh", self.s.oauth_refresh_ttl, audience=self.resource, **claims)
        return OAuthToken(access_token=access, expires_in=self.s.oauth_access_ttl, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        try:
            c = self.codec.decode("refresh", refresh_token, audience=self.resource)
        except BadToken:
            return None
        if not self.permitted(c["sub"], c.get("hd")):
            return None
        return _Refresh(token=refresh_token, client_id=c["cid"], scopes=c["sc"].split(), expires_at=c["exp"],
                        resource=self.resource, subject=c["sub"], jti=c["jti"], hd=c.get("hd"))

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        if not self.used.use(refresh_token.jti, refresh_token.expires_at or time.time()):
            raise TokenError("invalid_grant", "refresh token was already used")  # rotation: each token works once
        granted = self._scopes(scopes) if scopes else self._scopes(refresh_token.scopes)
        return self._issue_pair(refresh_token.subject or "", refresh_token.hd, client.client_id, granted)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self.gateway_token.encode()):
            return AccessToken(token=token, client_id="gateway", scopes=[SCOPE], resource=self.resource)
        try:
            c = self.codec.decode("access", token, audience=self.resource)
        except BadToken:
            return None
        if not self.permitted(c["sub"], c.get("hd")):  # removing a user takes effect on their next request
            return None
        h = user_hash(c["sub"])
        context.client_key.set(f"user:{h}")
        context.client_label.set(f"u:{h[:10]}")
        return AccessToken(token=token, client_id=c["cid"], scopes=c["sc"].split(), expires_at=c["exp"],
                           resource=self.resource, subject=c["sub"], claims={"iss": self.codec.issuer})

    async def revoke_token(self, token) -> None:
        """Stateless tokens cannot be revoked one by one; rotate OAUTH_SIGNING_KEY or remove the user instead."""
