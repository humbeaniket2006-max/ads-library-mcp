import json
import logging

import httpx

from app.main import create_app

from .conftest import HOST, KEY_A, KEY_B, SECRET, Rpc, make_settings, run_lifespan


async def started(**env):
    app = create_app(make_settings(**env))
    task, done = await run_lifespan(app)
    return app, task, done


async def stop(task, done, *clients):
    for c in clients:
        await c.http.aclose()
    done.set()
    await task


async def status_of(rpc, path=None, headers=None):
    return (await rpc.raw("tools/list", path=path, headers=headers)).status_code


async def test_path_secret_mode(mock_app):
    ok = Rpc(mock_app)
    assert await status_of(ok) == 200
    assert await status_of(ok, path="/mcp/" + "x" * 40) == 404          # bad secret
    assert await status_of(ok, path="/mcp") == 404                        # no secret, no bearer in this mode
    assert await status_of(ok, path=f"/mcp/{SECRET}/extra") == 404
    assert await status_of(ok, path="/other") == 404
    assert await status_of(ok, headers={"authorization": f"Bearer {KEY_A}"}, path="/mcp") == 404


async def test_bearer_mode_with_rotation_and_revocation(caplog):
    env = {"MCP_AUTH_MODE": "bearer", "MCP_PATH_SECRET": "", "MCP_API_KEYS": f"alice:{KEY_A},bob:{KEY_B}"}
    app, task, done = await started(**env)
    r = Rpc(app, path="/mcp", headers={"authorization": f"Bearer {KEY_A}"})
    assert await status_of(r) == 200
    assert await status_of(r, headers={"authorization": f"Bearer {KEY_B}"}) == 200
    bad = await r.raw("tools/list", headers={"authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
    assert await status_of(r, headers={"authorization": ""}) == 401
    assert await status_of(r, path=f"/mcp/{SECRET}", headers={"authorization": f"Bearer {KEY_A}"}) == 404
    assert (await r.json_call("get_status"))["mock"] is True
    await stop(task, done, r)

    # revoke alice by removing her key and restarting: her key stops working, bob's still works
    app2, task2, done2 = await started(**{**env, "MCP_API_KEYS": f"bob:{KEY_B}"})
    r2 = Rpc(app2, path="/mcp")
    assert await status_of(r2, headers={"authorization": f"Bearer {KEY_A}"}) == 401
    assert await status_of(r2, headers={"authorization": f"Bearer {KEY_B}"}) == 200
    await stop(task2, done2, r2)


async def test_both_mode_accepts_either():
    app, task, done = await started(MCP_AUTH_MODE="both", MCP_API_KEYS=f"alice:{KEY_A}")
    path = Rpc(app)
    bearer = Rpc(app, path="/mcp", headers={"authorization": f"Bearer {KEY_A}"})
    assert await status_of(path) == 200 and await status_of(bearer) == 200
    assert await status_of(Rpc(app, path="/mcp")) == 401
    await stop(task, done, path, bearer)


async def test_client_label_is_logged_and_secrets_are_not(caplog, capsys):
    app, task, done = await started(MCP_AUTH_MODE="bearer", MCP_PATH_SECRET="", MCP_API_KEYS=f"alice:{KEY_A}")
    r = Rpc(app, path="/mcp", headers={"authorization": f"Bearer {KEY_A}"})
    await r.call("search_ads", countries=["DE"], query="secret query text")
    await Rpc(app, path="/mcp", headers={"authorization": "Bearer wrong"}).raw("tools/list")
    await stop(task, done, r)
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    call = next(line for line in lines if line.get("tool") == "search_ads")
    assert call["client"] == "alice" and call["status"] == "ok" and "latency_ms" in call and "request_id" in call
    assert call["cache"] == "miss" and "meta_calls" in call
    assert "secret query text" not in out and KEY_A not in out and "wrong" not in out
    assert call["query_sha"] and len(call["query_sha"]) == 12


async def test_healthz_needs_no_auth_or_host(mock_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(mock_app), base_url="http://evil.example") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert (await c.post("/healthz")).status_code == 421  # only GET/HEAD are health checks


async def test_wrong_host_rejected_dns_rebinding(mock_app):
    r = Rpc(mock_app, headers={"host": "evil.example"})
    assert await status_of(r) == 421


async def test_security_headers_no_cors_generic_bodies(mock_app):
    r = Rpc(mock_app)
    resp = await r.raw("tools/list", path="/mcp/" + "q" * 40)
    assert resp.json() == {"error": "not found"}
    for h in ("x-content-type-options", "content-security-policy", "x-frame-options", "cache-control",
              "strict-transport-security", "referrer-policy"):
        assert h in resp.headers
    assert "access-control-allow-origin" not in resp.headers
    pre = await r.http.options(r.path, headers={**r.headers, "origin": "https://evil.example",
                                                "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in pre.headers
    for p in ("/docs", "/openapi.json", "/redoc"):
        assert (await r.http.get(p, headers={"host": HOST})).status_code == 404


async def test_body_limit_64kb(mock_app):
    r = Rpc(mock_app)
    big = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "get_status", "arguments": {}, "pad": "x" * 70_000}}
    resp = await r.http.post(r.path, headers=r.headers, content=json.dumps(big))
    assert resp.status_code == 413


async def test_client_ip_only_from_trusted_header(mock_app):
    gw = mock_app
    scope = {"client": ("10.0.0.1", 1)}
    assert gw._ip(scope, {b"true-client-ip": b"203.0.113.9"}) == "203.0.113.9"
    assert gw._ip(scope, {b"x-forwarded-for": b"6.6.6.6"}) == "10.0.0.1"          # untrusted header ignored
    assert gw._ip(scope, {b"true-client-ip": b"bad value; drop"}) == "10.0.0.1"


async def test_internal_error_is_generic(mock_app, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError(f"db password={SECRET}")

    monkeypatch.setattr(mock_app, "inner", boom)
    r = Rpc(mock_app)
    resp = await r.raw("tools/list")
    assert resp.status_code == 500 and resp.json() == {"error": "internal error"}
    assert SECRET not in resp.text


async def test_unhandled_error_logging_has_no_secret(capsys):
    app, task, done = await started()
    await stop(task, done)
    logging.getLogger("x").error("oops")
    assert SECRET not in capsys.readouterr().out


async def test_base64_path_secret_as_generated_by_render():
    secret = "ab+/cd=" * 6  # contains + / = like Render's generateValue output
    app, task, done = await started(MCP_PATH_SECRET=secret)
    r = Rpc(app, path=f"/mcp/{secret}")
    assert await status_of(r) == 200
    assert await status_of(r, path="/mcp/" + secret.replace("+", "-")) == 404
    enc = Rpc(app, path="/mcp/" + secret.replace("/", "%2F"))
    assert await status_of(enc) == 200  # clients that percent-encode the slash also work
    await stop(task, done, r, enc)
