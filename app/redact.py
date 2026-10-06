"""Secret redaction for logs, errors and URLs."""
from __future__ import annotations

import json
import logging
import re
import sys
import time

_TOKEN_PARAM = re.compile(r"(access_token=)[^&\s\"')]+", re.I)
_TOKEN_JSON = re.compile(r"(\"?access_token\"?\s*[:=]\s*\"?)[^\"&\s,}]+", re.I)
_EAA = re.compile(r"\bEAA[A-Za-z0-9_\-]{6,}")
# OAuth: JWTs (our access/refresh/code/state tokens and Google ID tokens), Google token shapes, and
# secret-bearing form/query parameters and JSON keys.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]*")
_GOOGLE = re.compile(r"\b(?:ya29\.[A-Za-z0-9_\-]{10,}|GOCSPX-[A-Za-z0-9_\-]{8,}|1//[A-Za-z0-9_\-]{20,})")
_OAUTH_PARAM = re.compile(
    r"([?&\s](?:code|state|refresh_token|id_token|client_secret|code_verifier|assertion|req)=)[^&\s\"')]+", re.I
)
_OAUTH_JSON = re.compile(
    r"(\"?(?:refresh_token|id_token|client_secret|code_verifier|assertion|access_token_value)\"?\s*[:=]\s*\"?)[^\"&\s,}]+",
    re.I,
)
_BEARER = re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}", re.I)
_MASK = "[redacted]"


class Redactor:
    """Masks configured secrets plus anything that looks like a Meta token."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets: list[str] = []
        self.set_secrets(secrets or [])

    def set_secrets(self, secrets: list[str]) -> None:
        # longest first so a secret that contains another is fully masked
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def __call__(self, text: object) -> str:
        s = str(text)
        for secret in self._secrets:
            s = s.replace(secret, _MASK)
        s = _TOKEN_PARAM.sub(r"\1" + _MASK, s)
        s = _TOKEN_JSON.sub(r"\1" + _MASK, s)
        s = _EAA.sub(_MASK, s)
        s = _JWT.sub(_MASK, s)
        s = _GOOGLE.sub(_MASK, s)
        s = _OAUTH_PARAM.sub(r"\1" + _MASK, s)
        s = _OAUTH_JSON.sub(r"\1" + _MASK, s)
        return _BEARER.sub(r"\1" + _MASK, s)


redact = Redactor()


class RedactingFilter(logging.Filter):
    """Redacts message, args and exception text of every record on the handler it is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            out.update({k: v for k, v in extra.items() if v is not None})
        if record.exc_text:
            out["exc"] = redact(record.exc_text)
        # final defence: redact the whole line, including structured extra fields
        return redact(json.dumps(out, separators=(",", ":"), default=str))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full URLs (incl. access_token) at INFO; keep it quiet and redacted regardless.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True  # access lines contain the secret path
    for name in ("uvicorn", "uvicorn.error", "mcp"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


log = logging.getLogger("meta_ads_mcp")


def log_event(msg: str, level: int = logging.INFO, **fields: object) -> None:
    log.log(level, msg, extra={"fields": fields})
