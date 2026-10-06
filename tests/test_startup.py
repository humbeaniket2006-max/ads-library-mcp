import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(env):
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import app.main as m; m.app"], cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=60,
    )


def test_invalid_config_exits_nonzero_with_one_line_and_no_traceback():
    r = run({"META_MOCK": "true", "MCP_PATH_SECRET": "short", "CURSOR_SIGNING_KEY": "x"})
    assert r.returncode == 1
    assert r.stderr.strip() == "Configuration error: MCP_PATH_SECRET must be at least 32 characters."


def test_valid_config_starts_and_logs_cold_start():
    env = {"META_MOCK": "true", "MCP_PATH_SECRET": "p" * 40, "CURSOR_SIGNING_KEY": "k" * 40}
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert "cold start, cache and budgets reset" in r.stdout and "META_MOCK=true" in r.stdout
    assert "p" * 40 not in r.stdout + r.stderr
