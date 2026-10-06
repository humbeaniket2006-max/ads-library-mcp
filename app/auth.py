"""Authentication behind a small interface. OAuth can be added later as another Authenticator."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol

from .config import Settings


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    status: int = 200          # HTTP status to return when not ok (404 bad path secret, 401 bad bearer)
    label: str | None = None   # key label for bearer auth
    kind: str | None = None    # "path" | "bearer"


class Authenticator(Protocol):
    def authenticate(self, path: str, authorization: str | None) -> AuthResult: ...


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class StaticAuthenticator:
    """Static credentials. path_secret: /mcp/<secret>.  bearer: /mcp with 'Authorization: Bearer <key>' (labelled keys).

    OAuth is a separate mechanism handled by the MCP SDK behind the gateway (see app/oauth)."""

    def __init__(self, s: Settings):
        self.modes, self.secret, self.keys = s.auth_modes, s.path_secret, dict(s.api_keys)

    def _bearer(self, authorization: str | None) -> AuthResult:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return AuthResult(False, 401)
        found = None
        for label, key in self.keys.items():  # no early exit: constant work per request
            if _eq(token.strip(), key):
                found = label
        return AuthResult(True, 200, found, "bearer") if found else AuthResult(False, 401)

    def authenticate(self, path: str, authorization: str | None) -> AuthResult:
        if path == "/mcp" or path == "/mcp/":
            if "bearer" in self.modes:
                return self._bearer(authorization)
            return AuthResult(False, 404)
        if path.startswith("/mcp/") and "path_secret" in self.modes and _eq(path[len("/mcp/"):], self.secret):
            return AuthResult(True, 200, None, "path")
        return AuthResult(False, 404)
