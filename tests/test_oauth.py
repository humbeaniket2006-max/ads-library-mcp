import json
import time

import jwt
import pytest
import respx

from app.config import ConfigError, Settings
from app.main import create_app
from app.oauth.policy import canonical_resource, email_allowed, redirect_allowed, same_redirect
from app.oauth.tokens import TokenCodec, UsedTokens

from .conftest import run_lifespan
from .oauth_helpers import (
    BASE,
    CLAUDE_CB,
    GOOGLE_ID,
    RESOURCE,
    SIGNING,
    STATIC_ID,
    STATIC_SECRET,
    Flow,
    MockGoogle,
    oauth_env,
)


@pytest.fixture(scope="module")
def google():
    return MockGoogle()


@pytest.fixture
async def stack(google):
    """Factory: start an app from env overrides with Google mocked; everything is torn down afterwards."""
    started, flows = [], []
    with respx.mock(assert_all_called=False) as router:
        google.install(router)

        async def make(**over):
            app = create_app(Settings.load(oauth_env(**over)))
            task, done = await run_lifespan(app)
            started.append((task, done))
            flow = Flow(app, google)
            flows.append(flow)
            return app, flow, router

        yield make
        for f in flows:
            await f.aclose()
        for task, done in started:
            done.set()
            await task


async def start(stack, **over):
    return await stack(**over)


# --- discovery ------------------------------------------------------------------------------------

async def test_discovery_metadata_and_401_pointer(stack):
    app, flow, _ = await stack()
    prm = (await flow.http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [BASE]  # exact, no trailing slash
    asm = (await flow.http.get("/.well-known/oauth-authorization-server")).json()
    assert asm["issuer"] == BASE  # RFC 8414 compares issuers as exact strings
    assert asm["authorization_endpoint"] == f"{BASE}/authorize" and asm["token_endpoint"] == f"{BASE}/token"
    assert asm["registration_endpoint"] == f"{BASE}/register"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in asm["grant_types_supported"] and "refresh_token" in asm["grant_types_supported"]
    r = await flow.mcp(None)
    assert r.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    assert (await flow.mcp("not-a-token")).status_code == 401
    assert "access-control-allow-origin" not in (await flow.http.get("/.well-known/oauth-authorization-server")).headers


# --- the full flow ----------------------------------------------------------------------------------

async def test_full_flow_with_dcr_and_tool_call(stack, google, capsys):
    app, flow, _ = await stack()
    res = await flow.login()
    assert res["authorize"].status_code == 302 and "Continue with Google" in res["consent_page"].text
    assert "access_denied" not in str(res["final_query"]) and res["final_query"]["state"] == ["client-state-xyz"]
    tokens = res["tokens"]
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["refresh_token"]
    # the upstream (Google) code was exchanged with our secret, once
    assert len(google.token_calls) >= 1 and google.token_calls[-1]["redirect_uri"] == f"{BASE}/oauth/google/callback"
    r = await flow.mcp(tokens["access_token"])
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 4
    call = await flow.mcp(tokens["access_token"], "tools/call", {"name": "get_status", "arguments": {}})
    assert json.loads(call.json()["result"]["content"][0]["text"])["mock"] is True
    await flow.mcp(tokens["access_token"], "tools/call",
                   {"name": "search_ads", "arguments": {"countries": ["DE"], "query": "Nordlicht"}})
    out = capsys.readouterr().out
    for secret in flow.secrets:
        assert secret not in out
    assert "alice@example.com" not in out
    assert '"client":"u:' in out  # per-user label in logs is a hash, not the email


async def test_consent_page_shows_redirect_host_and_sets_binding_cookie(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    page = res["consent_page"]
    assert "claude.ai" in page.text and "set-cookie" in page.headers
    assert "HttpOnly" in page.headers["set-cookie"] and "samesite=lax" in page.headers["set-cookie"].lower()
    assert "form-action 'self' https://accounts.google.com" in page.headers["content-security-policy"]


async def test_static_client_with_secret_post_and_basic(stack):
    app, flow, _ = await stack()
    for mode in ("post", "basic"):
        res = await flow.login(client_id=STATIC_ID, client_auth=mode)
        assert "tokens" in res, (mode, res["token_response"].text)
        assert (await flow.mcp(res["tokens"]["access_token"])).status_code == 200
    res = await flow.login(client_id=STATIC_ID)  # no secret at all
    assert res["token_response"].status_code == 401
    assert res["token_response"].json()["error"] == "invalid_client"


async def test_static_client_wrong_secret_rejected(stack):
    app, flow, _ = await stack()
    r = await flow.token(grant_type="authorization_code", code="x", client_id=STATIC_ID, client_secret="w" * 40,
                         code_verifier="v" * 50, redirect_uri=CLAUDE_CB)
    assert r.status_code == 401


# --- PKCE ---------------------------------------------------------------------------------------------

async def test_pkce_is_required_and_s256_only(stack):
    app, flow, _ = await stack()
    cid = (await flow.register()).json()["client_id"]
    _, challenge = flow.pkce()
    no_challenge = await flow.http.get("/authorize", params={
        "client_id": cid, "redirect_uri": CLAUDE_CB, "response_type": "code", "state": "s"})
    assert no_challenge.status_code in (302, 400) and "/oauth/consent" not in no_challenge.headers.get("location", "")
    plain = await flow.authorize(cid, challenge=challenge, method="plain")
    assert "/oauth/consent" not in plain.headers.get("location", "")
    short = await flow.authorize(cid, challenge="tooshort")
    assert "/oauth/consent" not in short.headers.get("location", "")


async def test_wrong_code_verifier_is_rejected(stack):
    app, flow, _ = await stack()
    assert "tokens" in await flow.login()
    cid = (await flow.register()).json()["client_id"]
    verifier, challenge = flow.pkce()
    a = await flow.authorize(cid, challenge=challenge)
    assert a.status_code == 302
    # drive to a code manually, then present the wrong verifier
    from urllib.parse import parse_qs, urlsplit
    loc = a.headers["location"]
    req = parse_qs(urlsplit(loc).query)["req"][0]
    _, csrf = await flow.consent_get(loc)
    post = await flow.consent_post(req, csrf)
    g = parse_qs(urlsplit(post.headers["location"]).query)
    flow.google.codes["gc"] = flow.google.id_token("alice@example.com", g["nonce"][0])
    cb = await flow.callback(g["state"][0], "gc")
    code = parse_qs(urlsplit(cb.headers["location"]).query)["code"][0]
    bad = await flow.token(grant_type="authorization_code", code=code, client_id=cid, redirect_uri=CLAUDE_CB,
                           code_verifier="x" * 60)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # and the right verifier then still works (a failed attempt does not burn the code in this design)
    good = await flow.token(grant_type="authorization_code", code=code, client_id=cid, redirect_uri=CLAUDE_CB,
                            code_verifier=verifier)
    assert good.status_code == 200
    again = await flow.token(grant_type="authorization_code", code=code, client_id=cid, redirect_uri=CLAUDE_CB,
                             code_verifier=verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"  # single use (same instance)


async def test_authorization_code_expires(stack):
    app, flow, _ = await stack(OAUTH_CODE_TTL="10")
    res = await flow.login()
    assert "tokens" in res
    old = TokenCodec(SIGNING, BASE, clock=lambda: time.time() - 500).encode(
        "code", 10, cid=res["client_id"], ru=CLAUDE_CB, rue=True, cc="c" * 43, sc=["ads:read"], em="alice@example.com")
    r = await flow.token(grant_type="authorization_code", code=old, client_id=res["client_id"], redirect_uri=CLAUDE_CB,
                         code_verifier="v" * 60)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


# --- redirect URIs -------------------------------------------------------------------------------------

@pytest.mark.parametrize("uri", [
    "https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
    "http://localhost:3118/callback", "http://127.0.0.1:54321/callback", "http://localhost/callback",
])
async def test_dcr_accepts_allowed_redirects(stack, uri):
    app, flow, _ = await stack()
    r = await flow.register(redirect_uris=(uri,))
    assert r.status_code == 201 and r.json()["redirect_uris"] == [uri]
    assert r.json().get("client_secret") in (None, "") and r.json()["token_endpoint_auth_method"] == "none"


@pytest.mark.parametrize("uri", [
    "https://evil.example/cb", "http://claude.ai/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback/",
    "https://claude.ai/api/mcp/auth_callback?x=1", "https://claude.ai.evil.com/api/mcp/auth_callback",
    "https://evil.com/https://claude.ai/api/mcp/auth_callback", "https://claude.ai@evil.com/api/mcp/auth_callback",
    "http://localhost.evil.com/cb", "http://localhost@evil.com/cb", "https://localhost/cb", "http://[::1]:8080/cb",
    "http://127.0.0.1.evil.com/cb", "javascript:alert(1)", "myapp://callback", "https://claude.ai/other",
])
async def test_dcr_rejects_other_redirects(stack, uri):
    app, flow, _ = await stack()
    r = await flow.register(redirect_uris=(uri,))
    assert r.status_code == 400 and r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


async def test_dcr_rejects_mixed_list(stack):
    app, flow, _ = await stack()
    assert (await flow.register(redirect_uris=(CLAUDE_CB, "https://evil.example/cb"))).status_code == 400


async def test_authorize_with_unregistered_redirect_shows_error_without_redirecting(stack):
    app, flow, _ = await stack()
    cid = (await flow.register()).json()["client_id"]
    _, challenge = flow.pkce()
    r = await flow.authorize(cid, "https://evil.example/cb", challenge=challenge)
    assert r.status_code == 400 and "location" not in r.headers  # never redirect to an unregistered URI
    r = await flow.authorize(cid, "http://localhost:9/cb", challenge=challenge)  # allowed shape but not registered
    assert r.status_code == 400


async def test_static_client_loopback_any_port_but_exact_path(stack):
    app, flow, _ = await stack()
    _, challenge = flow.pkce()
    ok = await flow.authorize(STATIC_ID, "http://localhost:49152/callback", challenge=challenge)
    assert ok.status_code == 302 and "/oauth/consent" in ok.headers["location"]
    ok2 = await flow.authorize(STATIC_ID, "http://127.0.0.1:7/callback", challenge=challenge)
    assert ok2.status_code == 302
    bad_path = await flow.authorize(STATIC_ID, "http://localhost:49152/other", challenge=challenge)
    assert bad_path.status_code == 400
    bad_host = await flow.authorize(STATIC_ID, "http://localhost.evil.com:49152/callback", challenge=challenge)
    assert bad_host.status_code == 400


def test_redirect_policy_unit():
    assert redirect_allowed("https://claude.ai/api/mcp/auth_callback")
    assert not redirect_allowed("https://claude.ai/api/mcp/auth_callback#frag")
    assert same_redirect("http://localhost:1/cb", "http://localhost:2/cb")
    assert not same_redirect("http://localhost:1/cb", "http://127.0.0.1:1/cb")
    assert not same_redirect("https://claude.ai/x", "https://claude.com/x")
    assert canonical_resource("HTTPS://Host.Example/mcp/") == "https://host.example/mcp"
    assert canonical_resource("https://h/mcp?x=1") == ""


# --- state / CSRF / nonce ------------------------------------------------------------------------------

async def test_callback_without_binding_cookie_is_rejected(stack, google):
    app, flow, _ = await stack()
    res = await flow.login()
    # a second browser (no cookie jar) replaying the callback must fail: login CSRF
    from urllib.parse import parse_qs, urlsplit

    import httpx
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=BASE)
    try:
        st = parse_qs(urlsplit(res["consent_post"].headers["location"]).query)["state"][0]
        google.codes["gx"] = google.id_token("alice@example.com", res["nonce"])
        r = await other.get("/oauth/google/callback", params={"code": "gx", "state": st})
        assert r.status_code == 400 and "location" not in r.headers
    finally:
        await other.aclose()


async def test_tampered_or_expired_state_and_req_rejected(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    st = res["google_state"]
    for bad in (st[:-3] + "AAA", "garbage", ""):
        r = await flow.callback(bad)
        assert r.status_code == 400
    r = await flow.http.get("/oauth/consent", params={"req": res["req"][:-3] + "AAA"})
    assert r.status_code == 400
    past = TokenCodec(SIGNING, BASE, clock=lambda: time.time() - 5000)
    old_req = past.encode("authreq", 600, cid="x", ru=CLAUDE_CB, rue=True, cc="c" * 43, st="s", sc=["ads:read"])
    assert (await flow.http.get("/oauth/consent", params={"req": old_req})).status_code == 400
    old_state = past.encode("state", 600, bind="b", nonce="n", req={})
    assert (await flow.callback(old_state)).status_code == 400


async def test_consent_post_requires_matching_csrf(stack):
    app, flow, _ = await stack()
    cid = (await flow.register()).json()["client_id"]
    _, challenge = flow.pkce()
    a = await flow.authorize(cid, challenge=challenge)
    from urllib.parse import parse_qs, urlsplit
    req = parse_qs(urlsplit(a.headers["location"]).query)["req"][0]
    page, csrf = await flow.consent_get(a.headers["location"])
    assert csrf
    assert (await flow.consent_post(req, "0" * 64)).status_code == 400
    assert (await flow.consent_post(req, "")).status_code == 400
    flow.http.cookies.clear()
    assert (await flow.consent_post(req, csrf)).status_code == 400  # right csrf, no cookie
    page, csrf2 = await flow.consent_get(a.headers["location"])
    assert (await flow.consent_post(req, csrf2)).status_code == 303


async def test_client_state_is_echoed_and_error_redirects_carry_it(stack):
    app, flow, _ = await stack()
    res = await flow.login("mallory@evil.example", state="abc-123")
    q = res["final_query"]
    assert q["error"] == ["access_denied"] and q["state"] == ["abc-123"] and "code" not in q
    assert res["callback"].headers["location"].startswith(CLAUDE_CB)


async def test_nonce_mismatch_denied(stack):
    app, flow, _ = await stack()
    res = await flow.login(id_token_overrides={"nonce": "attacker-nonce"})
    assert res["final_query"]["error"] == ["access_denied"]


# --- who may sign in ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("email", "verified", "hd", "allowed"), [
    ("alice@example.com", True, None, True),
    ("ALICE@example.com", True, None, True),
    ("alice@example.com", False, None, False),             # unverified email
    ("bob@example.com", True, None, False),                # not on the list
    ("bob@corp.example", True, "corp.example", True),      # domain list + matching hd
    ("bob@corp.example", True, None, False),               # domain claim without hd is not enough
    ("bob@corp.example", True, "other.example", False),
    ("bob@corp.example", False, "corp.example", False),
    ("x@evil.corp.example", True, "evil.corp.example", False),
])
async def test_access_control(stack, email, verified, hd, allowed):
    app, flow, _ = await stack()
    res = await flow.login(email, verified=verified, hd=hd)
    assert ("tokens" in res) is allowed, res.get("final_query")
    if not allowed:
        assert res["final_query"]["error"] == ["access_denied"] and "code" not in res["final_query"]


async def test_google_id_token_validation(stack, google):
    app, flow, _ = await stack()
    cases = {
        "wrong audience": {"aud": "other-client"},
        "wrong issuer": {"iss": "https://evil.example"},
        "expired": {"exp_in": -300},
        "bad signature": {"sign_with": google.other_key},
        "unknown kid": {"kid": "nope"},
        "unsigned": None,
    }
    for name, override in cases.items():
        if override is None:
            continue
        res = await flow.login(id_token_overrides=override)
        assert res["final_query"].get("error") == ["access_denied"], name
    ok = await flow.login(id_token_overrides={"exp_in": -30})  # inside the 60 s skew allowance
    assert "tokens" in ok
    ok = await flow.login(id_token_overrides={"iss": "accounts.google.com"})  # Google's second legal issuer form
    assert "tokens" in ok


async def test_google_token_endpoint_failure_denies(stack, google):
    app, flow, router = await stack()
    router.post("https://oauth2.googleapis.com/token").respond(500, text="boom ya29.x")
    res = await flow.login()
    assert res["final_query"]["error"] == ["access_denied"]


async def test_user_cancelled_at_google(stack):
    app, flow, _ = await stack()
    cid = (await flow.register()).json()["client_id"]
    _, ch = flow.pkce()
    a = await flow.authorize(cid, challenge=ch, state="st9")
    from urllib.parse import parse_qs, urlsplit
    loc = a.headers["location"]
    req = parse_qs(urlsplit(loc).query)["req"][0]
    _, csrf = await flow.consent_get(loc)
    post = await flow.consent_post(req, csrf)
    st = parse_qs(urlsplit(post.headers["location"]).query)["state"][0]
    r = await flow.http.get("/oauth/google/callback", params={"error": "access_denied", "state": st})
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st9"]


# --- tokens ---------------------------------------------------------------------------------------------------

async def test_token_validation_matrix(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    at, rt = res["tokens"]["access_token"], res["tokens"]["refresh_token"]
    codec = TokenCodec(SIGNING, BASE)
    now = time.time()
    claims = {"sub": "alice@example.com", "cid": res["client_id"], "sc": "ads:read"}
    past = TokenCodec(SIGNING, BASE, clock=lambda: now - 7200)
    cases = {
        "valid": (at, 200),
        "tampered": (at[:-4] + ("AAAA" if not at.endswith("AAAA") else "BBBB"), 401),
        "refresh token as access": (rt, 401),
        "expired": (past.encode("access", 3600, audience=RESOURCE, **claims), 401),
        "wrong audience": (codec.encode("access", 3600, audience="https://other.example/mcp", **claims), 401),
        "no audience": (codec.encode("access", 3600, **claims), 401),
        "wrong issuer": (TokenCodec(SIGNING, "https://evil.example").encode("access", 3600, audience=RESOURCE, **claims), 401),
        "other signing key": (TokenCodec("z" * 40, BASE).encode("access", 3600, audience=RESOURCE, **claims), 401),
        "alg none": (jwt.encode({**claims, "aud": RESOURCE, "iss": BASE, "exp": int(now) + 600, "iat": int(now),
                                 "jti": "j"}, key=None, algorithm="none", headers={"typ": "access"}), 401),
        "not a jwt": ("hello", 401),
        "user removed from list": (codec.encode("access", 3600, audience=RESOURCE, **{**claims, "sub": "gone@example.com"}), 401),
    }
    for name, (tok, want) in cases.items():
        assert (await flow.mcp(tok)).status_code == want, name


async def test_clock_skew_policy(stack):
    """Own tokens: strict (same clock as the checker). Google ID tokens: at most 60 s of allowance."""
    app, flow, _ = await stack()
    res = await flow.login()
    now = time.time()
    claims = {"sub": "alice@example.com", "cid": res["client_id"], "sc": "ads:read"}
    just_expired = TokenCodec(SIGNING, BASE, clock=lambda: now - 3600 - 30).encode(
        "access", 3600, audience=RESOURCE, **claims)
    assert (await flow.mcp(just_expired)).status_code == 401
    future = TokenCodec(SIGNING, BASE, clock=lambda: now + 600).encode("access", 3600, audience=RESOURCE, **claims)
    assert (await flow.mcp(future)).status_code == 401  # not yet valid
    assert "tokens" in await flow.login(id_token_overrides={"exp_in": -30})        # Google token, inside 60 s
    assert "tokens" not in await flow.login(id_token_overrides={"exp_in": -120})   # Google token, beyond 60 s


async def test_refresh_rotation_and_reuse_detection(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    cid, rt1 = res["client_id"], res["tokens"]["refresh_token"]
    r = await flow.token(grant_type="refresh_token", refresh_token=rt1, client_id=cid)
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != rt1 and t2["access_token"] != res["tokens"]["access_token"]
    assert (await flow.mcp(t2["access_token"])).status_code == 200
    reuse = await flow.token(grant_type="refresh_token", refresh_token=rt1, client_id=cid)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    r3 = await flow.token(grant_type="refresh_token", refresh_token=t2["refresh_token"], client_id=cid)
    assert r3.status_code == 200 and r3.json()["refresh_token"] != t2["refresh_token"]
    # a refresh token presented by a different client is refused
    other = (await flow.register()).json()["client_id"]
    stolen = await flow.token(grant_type="refresh_token", refresh_token=r3.json()["refresh_token"], client_id=other)
    assert stolen.status_code == 400
    # an access token is not a refresh token
    bad = await flow.token(grant_type="refresh_token", refresh_token=t2["access_token"], client_id=cid)
    assert bad.status_code == 400


async def test_resource_binding_at_authorize(stack):
    app, flow, _ = await stack()
    cid = (await flow.register()).json()["client_id"]
    _, ch = flow.pkce()
    wrong = await flow.authorize(cid, challenge=ch, resource="https://evil.example/mcp")
    assert wrong.status_code == 302 and "error=invalid_target" in wrong.headers["location"]
    assert wrong.headers["location"].startswith(CLAUDE_CB)
    slash = await flow.authorize(cid, challenge=ch, resource=RESOURCE + "/")
    assert "/oauth/consent" in slash.headers["location"]
    none = await flow.authorize(cid, challenge=ch, resource=None)
    assert "/oauth/consent" in none.headers["location"]


# --- restart survival, revocation, key rotation ---------------------------------------------------------------

async def test_tokens_and_clients_survive_a_restart(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    at, rt, cid = res["tokens"]["access_token"], res["tokens"]["refresh_token"], res["client_id"]
    app2, flow2, _ = await stack()  # brand-new process state: new app object, empty memory
    assert app2 is not app and app2.oauth.provider is not app.oauth.provider
    assert (await flow2.mcp(at)).status_code == 200
    r = await flow2.token(grant_type="refresh_token", refresh_token=rt, client_id=cid)
    assert r.status_code == 200
    _, ch = flow2.pkce()
    a = await flow2.authorize(cid, challenge=ch)  # the dynamically registered client is still known
    assert a.status_code == 302 and "/oauth/consent" in a.headers["location"]
    full = await flow2.login(client_id=cid)
    assert "tokens" in full


async def test_removing_a_user_revokes_access_and_refresh(stack):
    app, flow, _ = await stack(ALLOWED_EMAILS="alice@example.com,carol@example.com")
    res = await flow.login("carol@example.com")
    at, rt, cid = res["tokens"]["access_token"], res["tokens"]["refresh_token"], res["client_id"]
    assert (await flow.mcp(at)).status_code == 200
    app2, flow2, _ = await stack(ALLOWED_EMAILS="alice@example.com")  # carol removed
    assert (await flow2.mcp(at)).status_code == 401
    r = await flow2.token(grant_type="refresh_token", refresh_token=rt, client_id=cid)
    assert r.status_code == 400
    assert "tokens" in await flow2.login("alice@example.com")  # others are unaffected


async def test_rotating_the_signing_key_revokes_everyone(stack):
    app, flow, _ = await stack()
    res = await flow.login()
    at, rt, cid = res["tokens"]["access_token"], res["tokens"]["refresh_token"], res["client_id"]
    app2, flow2, _ = await stack(OAUTH_SIGNING_KEY="n" * 40)
    assert (await flow2.mcp(at)).status_code == 401
    assert (await flow2.token(grant_type="refresh_token", refresh_token=rt, client_id=cid)).status_code in (400, 401)
    _, ch = flow2.pkce()
    unknown = await flow2.authorize(cid, challenge=ch)
    assert unknown.status_code == 400  # the old client registration no longer verifies


def test_used_tokens_tracker():
    u = UsedTokens(max_entries=4)
    assert u.use("a", time.time() + 100) and not u.use("a", time.time() + 100)
    for i in range(10):
        u.use(f"k{i}", time.time() + 100)
    assert len(u._seen) <= 4


# --- per-user limits and modes ------------------------------------------------------------------------------------

async def test_per_user_budgets_keyed_by_email(stack):
    import httpx
    app, flow, router = await stack(META_MOCK="false", META_ACCESS_TOKEN="EAA" + "t" * 30,
                                    CLIENT_MAX_CALLS_PER_HOUR="2", COLD_START_BUDGET_FACTOR="1",
                                    META_MAX_CALLS_PER_HOUR="50", CLIENT_MAX_CALLS_PER_MINUTE="20")
    router.get("https://graph.facebook.com/v26.0/ads_archive").mock(
        return_value=httpx.Response(200, json={"data": []}))
    alice = (await flow.login("alice@example.com"))["tokens"]["access_token"]
    bob = (await flow.login("bob@corp.example", hd="corp.example"))["tokens"]["access_token"]

    async def search(tok, q):
        r = await flow.mcp(tok, "tools/call", {"name": "search_ads", "arguments": {"countries": ["DE"], "query": q}})
        res = r.json()["result"]
        return res["isError"], res["content"][0]["text"]

    assert not (await search(alice, "a1"))[0] and not (await search(alice, "a2"))[0]
    err, msg = await search(alice, "a3")
    assert err and "hourly call limit" in msg and "Do not retry this call." in msg
    assert not (await search(bob, "b1"))[0]  # another user is not affected


async def test_oauth_endpoints_are_rate_limited_per_ip(stack):
    app, flow, _ = await stack()
    codes = [(await flow.http.get("/.well-known/oauth-authorization-server")).status_code for _ in range(62)]
    assert codes[:60] == [200] * 60 and codes[60:] == [429, 429]


async def test_oauth_only_mode_rejects_static_credentials(stack):
    app, flow, _ = await stack()
    assert (await flow.mcp(None, path="/mcp/" + "s" * 40)).status_code == 404
    assert (await flow.mcp("a" * 40)).status_code == 401


async def test_path_secret_plus_oauth(stack):
    secret = "p" * 40
    app, flow, _ = await stack(MCP_AUTH_MODE="path_secret,oauth", MCP_PATH_SECRET=secret)
    assert (await flow.mcp(None, path=f"/mcp/{secret}")).status_code == 200
    assert (await flow.mcp(None, path="/mcp/" + "x" * 40)).status_code == 404
    unauth = await flow.mcp(None)
    assert unauth.status_code == 401 and "resource_metadata" in unauth.headers["www-authenticate"]
    at = (await flow.login())["tokens"]["access_token"]
    assert (await flow.mcp(at)).status_code == 200


async def test_bearer_plus_oauth(stack):
    key = "k" * 40
    app, flow, _ = await stack(MCP_AUTH_MODE="bearer,oauth", MCP_API_KEYS=f"alice:{key}")
    assert (await flow.mcp(key)).status_code == 200  # static key still works
    assert (await flow.mcp("w" * 40)).status_code == 401
    at = (await flow.login())["tokens"]["access_token"]
    assert (await flow.mcp(at)).status_code == 200


async def test_both_alias_still_means_path_secret_and_bearer():
    s = Settings.load(oauth_env(MCP_AUTH_MODE="both", MCP_PATH_SECRET="p" * 40, MCP_API_KEYS="a:" + "k" * 40))
    assert s.auth_modes == frozenset({"path_secret", "bearer"})


# --- config: refuse to start with missing or weak OAuth settings ----------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"GOOGLE_OAUTH_CLIENT_ID": ""},
    {"GOOGLE_OAUTH_CLIENT_SECRET": ""},
    {"OAUTH_SIGNING_KEY": ""},
    {"OAUTH_SIGNING_KEY": "short"},
    {"ALLOWED_EMAILS": "", "ALLOWED_EMAIL_DOMAINS": ""},      # deny by default
    {"ALLOWED_EMAILS": "not-an-email"},
    {"ALLOWED_EMAIL_DOMAINS": "@corp.example", "ALLOWED_EMAILS": ""},
    {"PUBLIC_BASE_URL": "http://app.example.com"},             # http only for localhost
    {"PUBLIC_BASE_URL": "https://app.example.com/mcp"},
    {"PUBLIC_BASE_URL": ""},
    {"MCP_OAUTH_CLIENT_ID": "id-only", "MCP_OAUTH_CLIENT_SECRET": ""},
    {"MCP_OAUTH_CLIENT_SECRET": "short"},
    {"OAUTH_ACCESS_TOKEN_TTL": "7200", "OAUTH_REFRESH_TOKEN_TTL": "3600"},
    {"OAUTH_ACCESS_TOKEN_TTL": "5"},
    {"MCP_AUTH_MODE": "oauth,magic"},
])
def test_invalid_oauth_config_fails_closed(over):
    with pytest.raises(ConfigError):
        Settings.load(oauth_env(**over))


def test_public_base_url_falls_back_to_render_external_url():
    env = oauth_env(PUBLIC_BASE_URL="", RENDER_EXTERNAL_URL="https://svc.onrender.com")
    s = Settings.load(env)
    assert s.public_base_url == "https://svc.onrender.com" and "svc.onrender.com" in s.allowed_hosts


def test_oauth_secrets_never_in_repr():
    s = Settings.load(oauth_env())
    text = repr(s)
    for secret in (SIGNING, STATIC_SECRET, "GOCSPX"):
        assert secret not in text
    assert GOOGLE_ID not in {*s.secrets}


# --- policy -------------------------------------------------------------------------------------------------------------------

def test_email_policy():
    emails, domains = frozenset({"a@x.com"}), frozenset({"corp.example", "gmail.com"})
    assert email_allowed("a@x.com", None, emails, domains)
    assert not email_allowed("a@x.com", None, frozenset(), frozenset())          # deny by default
    assert email_allowed("u@gmail.com", None, emails, domains)
    assert not email_allowed("u@gmail.com", "evil.example", emails, domains)
    assert email_allowed("u@corp.example", "corp.example", emails, domains)
    assert not email_allowed("u@corp.example", None, emails, domains)
    assert not email_allowed("nonsense", "corp.example", emails, domains)
