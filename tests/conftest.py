import asyncio
import json
import logging

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.redact import redact

SECRET = "s" * 40
SIGN = "k" * 40
TOKEN = "EAAtesttokenvalue1234567890abcdef"
HOST = "example.onrender.com"
KEY_A = "a" * 40
KEY_B = "b" * 40


def make_env(**over):
    env = {
        "META_MOCK": "true", "MCP_AUTH_MODE": "path_secret", "MCP_PATH_SECRET": SECRET,
        "CURSOR_SIGNING_KEY": SIGN, "ALLOWED_HOSTS": HOST, "META_ACCESS_TOKEN": TOKEN,
    }
    env.update(over)
    return env


def make_settings(**over):
    return Settings.load(make_env(**over))


@pytest.fixture(autouse=True)
def _reset_redactor():
    yield
    redact.set_secrets([])
    logging.getLogger().handlers[:] = []


class Rpc:
    def __init__(self, app, path=f"/mcp/{SECRET}", headers=None):
        self.app, self.path = app, path
        self.headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
                        "host": HOST, **(headers or {})}
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=f"http://{HOST}")
        self._id = 0

    async def raw(self, method, params=None, path=None, headers=None):
        self._id += 1
        return await self.http.post(path or self.path, headers={**self.headers, **(headers or {})},
                                    json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})

    async def call(self, tool, **args):
        r = await self.raw("tools/call", {"name": tool, "arguments": args})
        body = r.json()
        if "error" in body:
            return True, body["error"]["message"]
        res = body["result"]
        return res["isError"], res["content"][0]["text"]

    async def json_call(self, tool, **args):
        err, text = await self.call(tool, **args)
        assert not err, text
        return json.loads(text)


async def run_lifespan(app):
    """Run the app's lifespan in its own task (anyio scopes must exit in the task that entered them)."""
    ready, done = asyncio.Event(), asyncio.Event()

    async def _run():
        async with app.inner.router.lifespan_context(app.inner):
            ready.set()
            await done.wait()

    task = asyncio.create_task(_run())
    await ready.wait()
    return task, done


@pytest.fixture
async def mock_app():
    app = create_app(make_settings())
    task, done = await run_lifespan(app)
    yield app
    done.set()
    await task


@pytest.fixture
async def rpc(mock_app):
    r = Rpc(mock_app)
    yield r
    await r.http.aclose()
