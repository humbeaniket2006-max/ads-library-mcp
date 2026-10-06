import base64
import re
import secrets
from pathlib import Path

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
PLATFORM_PROVIDED = {"RENDER_EXTERNAL_HOSTNAME", "RENDER_EXTERNAL_URL", "PORT"}
# Everything in the spec's Config section must be declared in the Blueprint.
SPEC_CONFIG = """META_ACCESS_TOKEN GRAPH_API_VERSION META_MOCK MCP_AUTH_MODE MCP_PATH_SECRET MCP_API_KEYS CURSOR_SIGNING_KEY
META_MAX_CALLS_PER_HOUR META_MAX_CALLS_PER_DAY CLIENT_MAX_CALLS_PER_MINUTE CACHE_TTL_ACTIVE CACHE_TTL_HISTORICAL
CACHE_TTL_ADVERTISER CACHE_MAX_ENTRIES CACHE_MAX_BYTES DEFAULT_LIMIT MAX_LIMIT MAX_PAGES_PER_CALL MAX_SUMMARY_PAGES
AD_TEXT_MAX_CHARS MAX_RESPONSE_CHARS LOG_LEVEL""".split()


@pytest.fixture(scope="module")
def svc():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    assert list(doc) == ["services"] and len(doc["services"]) == 1
    return doc["services"][0]


def test_service_basics(svc):
    assert svc["type"] == "web" and svc["runtime"] == "python" and svc["plan"] == "free"
    assert svc["healthCheckPath"] == "/healthz" and svc["branch"] == "main"
    assert svc["autoDeployTrigger"] == "commit"
    assert svc["buildCommand"] == "pip install -r requirements.txt"
    cmd = svc["startCommand"]
    assert cmd.startswith("uvicorn app.main:app") and "--host 0.0.0.0" in cmd and "--port $PORT" in cmd


def test_python_version_pinned_fully_qualified(svc):
    env = {e["key"]: e for e in svc["envVars"]}
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(env["PYTHON_VERSION"]["value"]))


def test_paid_alternative_is_commented_out():
    text = (ROOT / "render.yaml").read_text()
    assert re.search(r"^\s*#\s*plan:\s*starter", text, re.M)
    assert not re.search(r"^\s*plan:\s*starter", text, re.M)


def test_secrets_are_not_hardcoded(svc):
    env = {e["key"]: e for e in svc["envVars"]}
    for key in ("MCP_PATH_SECRET", "CURSOR_SIGNING_KEY"):
        assert env[key] == {"key": key, "generateValue": True}
    for key in ("META_ACCESS_TOKEN", "MCP_API_KEYS"):
        assert env[key] == {"key": key, "sync": False}
    text = (ROOT / "render.yaml").read_text()
    assert not re.search(r"EAA[A-Za-z0-9]{8,}", text)
    for e in svc["envVars"]:
        assert not (("generateValue" in e or "sync" in e) and "value" in e)


def test_defaults_and_spec_vars_declared(svc):
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["META_MOCK"]["value"] == "true" and env["MCP_AUTH_MODE"]["value"] == "path_secret"
    assert env["GRAPH_API_VERSION"]["value"] == "v26.0"
    missing = [k for k in SPEC_CONFIG if k not in env]
    assert not missing, missing
    assert "ALLOWED_HOSTS" not in env  # derived from RENDER_EXTERNAL_HOSTNAME at runtime
    for e in svc["envVars"]:
        if "value" in e:
            assert isinstance(e["value"], str)  # Render env values are strings


def test_every_required_env_var_is_declared_and_app_boots_from_blueprint(svc):
    """Build the env Render would give the app and make sure the app accepts it."""
    env = {"RENDER_EXTERNAL_HOSTNAME": "meta-ads-library-mcp.onrender.com", "PORT": "10000"}
    for e in svc["envVars"]:
        if "value" in e:
            env[e["key"]] = e["value"]
        elif e.get("generateValue"):
            env[e["key"]] = base64.b64encode(secrets.token_bytes(32)).decode()  # Render emits base64
        # sync:false vars are left blank, as when a user skips them during mock-mode deploy
    s = Settings.load(env)
    assert s.meta_mock and "meta-ads-library-mcp.onrender.com" in s.allowed_hosts

    # Remove each declared var in turn: only the ones the app truly requires may break loading.
    required = set()
    for key in list(env):
        trial = {k: v for k, v in env.items() if k != key}
        try:
            Settings.load(trial)
        except Exception:
            required.add(key)
    declared = {e["key"] for e in svc["envVars"]} | PLATFORM_PROVIDED
    assert required <= declared, required - declared
    assert {"MCP_PATH_SECRET", "CURSOR_SIGNING_KEY"} <= required


def test_requirements_pinned():
    for line in (ROOT / "requirements.txt").read_text().split():
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==\d+(\.\d+)*", line), line


def test_oauth_vars_declared_without_hardcoded_secrets(svc):
    env = {e["key"]: e for e in svc["envVars"]}
    for key in ("OAUTH_SIGNING_KEY", "MCP_OAUTH_CLIENT_SECRET"):
        assert env[key] == {"key": key, "generateValue": True}
    assert "value" in env["MCP_OAUTH_CLIENT_ID"] and "GOCSPX" not in (ROOT / "render.yaml").read_text()
    for gone in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "ALLOWED_EMAILS", "ALLOWED_EMAIL_DOMAINS"):
        assert gone not in env  # Google sign-in is not part of the Blueprint
    assert env["MCP_AUTH_MODE"]["value"] == "path_secret"  # existing deploys keep working until you opt in
    assert "PUBLIC_BASE_URL" not in env  # derived from RENDER_EXTERNAL_URL


def test_blueprint_boots_in_oauth_client_mode_with_no_manual_values(svc):
    """The simple path (client ID + secret, no Google): Render's own generated values are enough."""
    env = {"RENDER_EXTERNAL_HOSTNAME": "svc.onrender.com", "RENDER_EXTERNAL_URL": "https://svc.onrender.com"}
    for e in svc["envVars"]:
        if "value" in e:
            env[e["key"]] = e["value"]
        elif e.get("generateValue"):
            env[e["key"]] = base64.b64encode(secrets.token_bytes(32)).decode()
        # sync:false variables stay blank
    env["MCP_AUTH_MODE"] = "path_secret,oauth_client"
    s = Settings.load(env)
    assert s.oauth_client_login and not s.oauth_login and s.oauth_client_id == "claude-meta-ads"
    assert s.public_base_url == "https://svc.onrender.com"


def test_blueprint_declares_every_var_oauth_client_mode_requires(svc):
    env = {"RENDER_EXTERNAL_HOSTNAME": "svc.onrender.com", "RENDER_EXTERNAL_URL": "https://svc.onrender.com"}
    for e in svc["envVars"]:
        if "value" in e:
            env[e["key"]] = e["value"]
        elif e.get("generateValue"):
            env[e["key"]] = base64.b64encode(secrets.token_bytes(32)).decode()
    env["MCP_AUTH_MODE"] = "path_secret,oauth_client"
    Settings.load(env)
    required = set()
    for key in list(env):
        try:
            Settings.load({k: v for k, v in env.items() if k != key})
        except Exception:
            required.add(key)
    declared = {e["key"] for e in svc["envVars"]} | PLATFORM_PROVIDED
    assert required <= declared, required - declared
    assert {"OAUTH_SIGNING_KEY", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET"} <= required
