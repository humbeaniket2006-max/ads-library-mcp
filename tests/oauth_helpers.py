"""Test doubles for the OAuth flow: a mock Google (real RSA signing) and a client that plays claude.ai."""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.oauth.google import GOOGLE_JWKS_URL, GOOGLE_TOKEN_URL

from .conftest import make_env

BASE = "http://localhost:8000"
RESOURCE = f"{BASE}/mcp"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_ID = "123-abc.apps.googleusercontent.com"
GOOGLE_SECRET = "GOCSPX-" + "g" * 28
SIGNING = "o" * 40
STATIC_ID = "claude-static"
STATIC_SECRET = "c" * 40


def oauth_env(**over):
    env = make_env(
        MCP_AUTH_MODE="oauth", MCP_PATH_SECRET="", PUBLIC_BASE_URL=BASE, ALLOWED_HOSTS="",
        GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_SECRET, OAUTH_SIGNING_KEY=SIGNING,
        MCP_OAUTH_CLIENT_ID=STATIC_ID, MCP_OAUTH_CLIENT_SECRET=STATIC_SECRET,
        ALLOWED_EMAILS="alice@example.com", ALLOWED_EMAIL_DOMAINS="corp.example",
    )
    env.update(over)
    return env


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class MockGoogle:
    """Signs ID tokens with a real RSA key and serves the matching JWKS and token endpoint."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-kid-1"
        self.codes: dict[str, str] = {}  # google auth code -> id_token
        self.token_calls: list[dict] = []
        self.jwks_calls = 0

    def jwks(self) -> dict:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def id_token(self, email, nonce, *, aud=GOOGLE_ID, iss="https://accounts.google.com", verified=True, hd=None,
                 exp_in=3600, sub="1234567890", sign_with=None, kid=None, **extra) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified, "nonce": nonce,
                  "iat": now, "exp": now + exp_in, **extra}
        if hd:
            claims["hd"] = hd
        return jwt.encode(claims, sign_with or self.key, algorithm="RS256", headers={"kid": kid or self.kid})

    def install(self, mock: respx.MockRouter) -> None:
        def token(request: httpx.Request):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append(form)
            if (form.get("client_id") != GOOGLE_ID or form.get("client_secret") != GOOGLE_SECRET
                    or form.get("code") not in self.codes):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.codes[form["code"]], "access_token": "ya29.upstream-unused"})

        def jwks(request):
            self.jwks_calls += 1
            return httpx.Response(200, json=self.jwks())

        mock.post(GOOGLE_TOKEN_URL).mock(side_effect=token)
        mock.get(GOOGLE_JWKS_URL).mock(side_effect=jwks)


class Flow:
    """Drives the full browser + client sequence against the gateway, recording every secret seen."""

    def __init__(self, app, google: MockGoogle, base: str = BASE):
        self.app, self.google = app, google
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=base)
        self.secrets: list[str] = []

    async def aclose(self):
        await self.http.aclose()

    # -- steps ----------------------------------------------------------------------------------
    async def register(self, redirect_uris=(CLAUDE_CB,), name="Claude", **extra) -> httpx.Response:
        body = {"redirect_uris": list(redirect_uris), "client_name": name, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], **extra}
        return await self.http.post("/register", json=body)

    @staticmethod
    def pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        return verifier, b64url(hashlib.sha256(verifier.encode()).digest())

    async def authorize(self, client_id, redirect_uri=CLAUDE_CB, *, challenge, state="client-state-xyz",
                        resource=RESOURCE, method="S256", scope=None, **extra) -> httpx.Response:
        q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state,
             "code_challenge": challenge, "code_challenge_method": method, **extra}
        if resource:
            q["resource"] = resource
        if scope:
            q["scope"] = scope
        return await self.http.get("/authorize", params={k: v for k, v in q.items() if v is not None})

    async def consent_get(self, location: str) -> tuple[httpx.Response, str | None]:
        r = await self.http.get(location.replace(BASE, ""))
        m = re.search(r"name='csrf' value='([0-9a-f]+)'", r.text)
        return r, (m.group(1) if m else None)

    async def consent_post(self, req: str, csrf: str) -> httpx.Response:
        return await self.http.post("/oauth/consent", data={"req": req, "csrf": csrf})

    async def callback(self, state: str, code: str = "gcode") -> httpx.Response:
        return await self.http.get("/oauth/google/callback", params={"code": code, "state": state})

    async def token(self, **form) -> httpx.Response:
        return await self.http.post("/token", data=form)

    # -- whole flow -------------------------------------------------------------------------------
    async def login(self, email="alice@example.com", *, client_id=None, redirect_uri=CLAUDE_CB, hd=None, verified=True,
                    id_token_overrides=None, state="client-state-xyz", resource=RESOURCE, scope=None,
                    client_auth=None) -> dict:
        out: dict = {}
        if client_id is None:
            reg = await self.register(redirect_uris=(redirect_uri,))
            assert reg.status_code == 201, reg.text
            client_id = reg.json()["client_id"]
        out["client_id"] = client_id
        verifier, challenge = self.pkce()
        out["verifier"] = verifier
        a = await self.authorize(client_id, redirect_uri, challenge=challenge, state=state, resource=resource,
                                 scope=scope)
        out["authorize"] = a
        if a.status_code != 302 or "/oauth/consent" not in a.headers.get("location", ""):
            return out
        loc = a.headers["location"]
        out["req"] = parse_qs(urlsplit(loc).query)["req"][0]
        page, csrf = await self.consent_get(loc)
        out["consent_page"] = page
        post = await self.consent_post(out["req"], csrf or "")
        out["consent_post"] = post
        if post.status_code != 303:
            return out
        g = parse_qs(urlsplit(post.headers["location"]).query)
        state_tok, nonce = g["state"][0], g["nonce"][0]
        out["google_state"], out["nonce"] = state_tok, nonce
        extra = dict(id_token_overrides or {})
        idt = self.google.id_token(email, extra.pop("nonce", nonce), verified=verified, hd=hd, **extra)
        gcode = "gcode-" + secrets.token_hex(6)
        self.google.codes[gcode] = idt
        cb = await self.callback(state_tok, gcode)
        out["callback"], out["google_code"], out["id_token"] = cb, gcode, idt
        loc2 = cb.headers.get("location", "")
        q = parse_qs(urlsplit(loc2).query)
        out["final_query"] = q
        if "code" not in q:
            return out
        code = q["code"][0]
        out["code"] = code
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": client_id, "code_verifier": verifier, "resource": resource}
        headers = {}
        if client_auth == "post":
            form["client_secret"] = STATIC_SECRET
        elif client_auth == "basic":
            headers["authorization"] = "Basic " + base64.b64encode(f"{client_id}:{STATIC_SECRET}".encode()).decode()
        t = await self.http.post("/token", data=form, headers=headers)
        out["token_response"] = t
        if t.status_code == 200:
            out["tokens"] = t.json()
            self.secrets += [out["tokens"]["access_token"], out["tokens"]["refresh_token"], code, state_tok, idt]
        return out

    async def mcp(self, access_token: str | None, method="tools/list", params=None, path="/mcp"):
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"
        return await self.http.post(path, headers=headers,
                                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
