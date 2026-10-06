"""Browser-facing OAuth routes: the consent page and the Google callback."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..config import Settings
from ..redact import log_event
from .google import GoogleClient, GoogleError
from .policy import email_allowed, is_loopback, redirect_allowed
from .provider import OAuthProvider, user_hash
from .tokens import TokenCodec
from .tokens import TokenError as BadToken

BIND_COOKIE = "mcp_oauth_bind"
STATE_TTL = 600
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; " \
      "frame-ancestors 'none'; base-uri 'none'"
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:30rem;margin:12vh auto;padding:0 1rem;color:#1a1a1a}}
button{{font:inherit;padding:.6rem 1.1rem;border:0;border-radius:.4rem;background:#1a56db;color:#fff;cursor:pointer}}
.n{{color:#555;font-size:.9rem}}</style></head><body>{body}</body></html>"""


@dataclass
class OAuthRuntime:
    settings: Settings
    codec: TokenCodec
    provider: OAuthProvider
    google: GoogleClient | None = None

    @property
    def callback_url(self) -> str:
        return f"{self.settings.public_base_url}/oauth/google/callback"

    @property
    def secure_cookie(self) -> bool:
        return self.settings.public_base_url.startswith("https://")


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})


def _error_page() -> HTMLResponse:  # generic on purpose: no detail about what failed
    return _page("<h1>Sign-in could not continue</h1><p>This sign-in request is invalid or has expired. "
                 "Go back to the app and start again.</p>", 400)


def _clear_cookie(resp: Response, rt: OAuthRuntime) -> None:
    resp.delete_cookie(BIND_COOKIE, path="/oauth", secure=rt.secure_cookie, httponly=True, samesite="lax")


def _bind(cookie: str) -> str:
    return hashlib.sha256(cookie.encode()).hexdigest()


def register_oauth_routes(mcp, rt: OAuthRuntime) -> None:
    @mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
    async def consent(request: Request) -> Response:
        """Shown for every authorization request, so a (dynamically registered) client cannot silently reuse a
        user's existing Google session. Also sets the cookie that ties the whole flow to this browser."""
        form = await request.form() if request.method == "POST" else None
        req = str((form or request.query_params).get("req", ""))
        try:
            claims = rt.codec.decode("authreq", req)
        except BadToken:
            return _error_page()
        if not redirect_allowed(claims["ru"]):
            return _error_page()

        if request.method == "GET":
            cookie = secrets.token_urlsafe(32)
            host = urlsplit(claims["ru"]).hostname or ""
            name = html.escape(claims.get("cn") or "An application")
            local = ("<p class='n'>This app runs on your own computer (localhost). Only continue if you just "
                     "started it yourself.</p>") if is_loopback(claims["ru"]) else ""
            body = (
                f"<h1>Connect to Meta Ad Library</h1><p><b>{name}</b> wants to read ads data on your behalf. "
                f"After sign-in you return to <b>{html.escape(host)}</b>.</p>{local}"
                f"<form method='post' action='/oauth/consent'><input type='hidden' name='req' value='{html.escape(req)}'>"
                f"<input type='hidden' name='csrf' value='{rt.codec.mac('csrf', cookie)}'>"
                "<button type='submit'>Continue with Google</button></form>"
                "<p class='n'>Only approved accounts can sign in.</p>"
            )
            resp = _page(body)
            resp.set_cookie(BIND_COOKIE, cookie, max_age=STATE_TTL, path="/oauth", secure=rt.secure_cookie,
                            httponly=True, samesite="lax")
            return resp

        cookie = request.cookies.get(BIND_COOKIE, "")
        csrf = str((form or {}).get("csrf", ""))
        if not cookie or not hmac.compare_digest(rt.codec.mac("csrf", cookie).encode(), csrf.encode()):
            log_event("oauth csrf check failed", logging.WARNING)
            return _error_page()
        nonce = secrets.token_urlsafe(24)
        state = rt.codec.encode(
            "state", STATE_TTL, bind=_bind(cookie), nonce=nonce,
            req={k: claims[k] for k in ("cid", "ru", "rue", "cc", "st", "sc")},
        )
        url = rt.google.authorize_url(rt.callback_url, state, nonce)
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/oauth/google/callback", methods=["GET"])
    async def google_callback(request: Request) -> Response:
        q = request.query_params
        try:
            claims = rt.codec.decode("state", str(q.get("state", "")))
        except BadToken:
            return _error_page()
        cookie = request.cookies.get(BIND_COOKIE, "")
        if not cookie or not hmac.compare_digest(_bind(cookie).encode(), str(claims["bind"]).encode()):
            log_event("oauth state/cookie binding failed", logging.WARNING)
            return _error_page()  # a callback replayed into a different browser (login CSRF) ends here
        req = claims["req"]
        if not redirect_allowed(req["ru"]):  # defence in depth; the URI was already validated twice
            return _error_page()

        def back(**params: str | None) -> Response:
            resp = RedirectResponse(construct_redirect_uri(req["ru"], state=req["st"], **params), status_code=302,
                                    headers={"Cache-Control": "no-store"})
            _clear_cookie(resp, rt)
            return resp

        code = q.get("code")
        if q.get("error") or not code:
            return back(error="access_denied", error_description="sign-in was cancelled")
        try:
            id_token = await rt.google.exchange_code(str(code), rt.callback_url)
            ident = await rt.google.verify_id_token(id_token, claims["nonce"])
        except GoogleError:
            return back(error="access_denied", error_description="sign-in could not be completed")
        who = user_hash(ident.email)
        s = rt.settings
        if not ident.email_verified or not email_allowed(ident.email, ident.hosted_domain, s.allowed_emails,
                                                         s.allowed_email_domains):
            log_event("oauth sign-in denied", logging.WARNING, user=who, verified=ident.email_verified)
            return back(error="access_denied", error_description="this account is not allowed")
        log_event("oauth sign-in ok", user=who)
        return back(code=rt.provider.issue_code(req, ident.email, ident.hosted_domain))
