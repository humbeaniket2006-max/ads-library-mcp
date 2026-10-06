"""Per-request context set by the HTTP middleware and read by tools."""
from contextvars import ContextVar

client_key: ContextVar[str] = ContextVar("client_key", default="local")
client_label: ContextVar[str] = ContextVar("client_label", default="local")
request_id: ContextVar[str] = ContextVar("request_id", default="-")
