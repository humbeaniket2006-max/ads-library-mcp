"""Pure policy rules: redirect URI allowlist, resource canonicalisation, who may sign in."""
from __future__ import annotations

from urllib.parse import urlsplit

CLAUDE_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
SCOPE = "ads:read"
OFFLINE = "offline_access"
VALID_SCOPES = [SCOPE, OFFLINE]
# Google addresses on these domains have no `hd` claim; every other domain must prove itself with `hd`.
CONSUMER_DOMAINS = ("gmail.com", "googlemail.com")


def is_loopback(uri: str) -> bool:
    try:
        p = urlsplit(uri)
        _ = p.port  # raises ValueError on a bad port
    except ValueError:
        return False
    return (
        p.scheme == "http" and p.hostname in LOOPBACK_HOSTS and not p.username and not p.password
        and not p.query and not p.fragment and p.path.startswith("/") and "@" not in p.netloc
    )


def redirect_allowed(uri: str) -> bool:
    """Exact match for the hosted Claude callbacks; http loopback (localhost / 127.0.0.1) on any port."""
    return uri in CLAUDE_CALLBACKS or is_loopback(uri)


def same_redirect(requested: str, registered: str) -> bool:
    """Exact match, except that loopback URIs compare with the port ignored (RFC 8252 section 7.3)."""
    if requested == registered:
        return True
    if not (is_loopback(requested) and is_loopback(registered)):
        return False
    a, b = urlsplit(requested), urlsplit(registered)
    return (a.hostname, a.path) == (b.hostname, b.path)


def canonical_resource(url: str) -> str:
    """RFC 8707 resource identifier compared without case in scheme/host and without a trailing slash."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    if p.fragment or p.query:
        return ""
    return f"{p.scheme.lower()}://{(p.netloc or '').lower()}{p.path.rstrip('/')}"


def email_allowed(email: str, hosted_domain: str | None, emails: frozenset[str], domains: frozenset[str]) -> bool:
    """Deny by default. Exact verified email, or a domain on the list.

    Google advises using the `hd` claim, not the email suffix, for organisation membership. So a domain entry matches
    only when the address ends with that domain AND (hd equals it, or it is a consumer domain with no hd).
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    if email in emails:
        return True
    domain = email.rsplit("@", 1)[1]
    if domain not in domains:
        return False
    if domain in CONSUMER_DOMAINS:
        return not hosted_domain
    return (hosted_domain or "").lower() == domain
