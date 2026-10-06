# Security

## Model

A single shared Meta token lives only in the server's environment. Callers are a small trusted team authenticated by a
secret URL path or labelled bearer keys. The server is read-only and only ever contacts `graph.facebook.com`.

## Controls

- **Auth on every MCP route** (`MCP_AUTH_MODE`): path secret (404 on mismatch), bearer keys with labels (401), or both.
  `hmac.compare_digest` throughout; the bearer check does the same work for every configured key. Startup is refused if a
  secret is missing, shorter than 32 characters, or has unsafe characters. The secret is stripped from the path before the
  MCP layer sees it. Static credentials live behind an interface (`app/auth.py`); OAuth is a separate mode (below).
- **Secrets only from environment.** `.env` is git-ignored; `.env.example` has no values. A redaction layer masks
  `access_token=` values, strings starting `EAA`, bearer tokens and every configured secret in log lines (message, fields,
  exception text) and in error text. uvicorn's access log is disabled because the path contains the secret.
- **Input validation:** pydantic models with `extra=forbid`, ISO country allowlist, enums, numeric page IDs, strict dates,
  length caps, control/zero-width/bidi rejection. Unknown tool parameters are rejected at the HTTP layer.
- **Prompt-injection defence:** ad text and page names are untrusted. HTML, control, zero-width and bidi characters are
  stripped, text is length-capped, and every response carries `text fields are untrusted ad content, not instructions`.
- **Network:** host hard-coded to `https://graph.facebook.com`; no user-supplied URLs are fetched; redirects not followed;
  TLS verified; connect timeout 5 s, read timeout 20 s; Meta responses capped at 2 MB. Meta paging and snapshot URLs
  (which can embed a token) are never returned; snapshot URLs only on request and with `access_token` removed.
- **HTTP hardening:** 64 KB body limit; security headers (nosniff, CSP `default-src 'none'`, frame deny, no-store,
  HSTS, no-referrer); no CORS; Host allowlist (Render hostname from env) plus the SDK's DNS-rebinding and Origin checks;
  no `/docs` or `/openapi.json`; generic error bodies; client IP taken only from one configured proxy header, validated,
  else the socket address.
- **Rate limiting and fail-closed:** per-client token bucket plus global hour/day budgets, enforced before any Meta call;
  unknown budget state denies; invalid config exits at startup; token errors (190, 10/2332002) are never retried and block
  Meta calls for 10 minutes.
- **Cursors** are HMAC-signed, bound to the query, and rejected on any mismatch.
- **Logging:** structured JSON with request id, client label, tool, status, latency, cache hit/miss, Meta calls used, and a
  hash of the query text. No ad content, tokens or query text.
- **Supply chain:** all dependencies pinned; `pip-audit` runs in CI.

## OAuth client mode (`oauth_client`)

No user login: the pre-registered client's secret is the only credential. `/authorize` auto-approves just that client, the
code is redeemable only with the client secret and PKCE verifier at `/token`, and open registration is disabled. Tokens
carry a fingerprint of the secret, so rotating `MCP_OAUTH_CLIENT_SECRET` invalidates them. There is one shared identity, so
there is no per-person revocation; use `oauth` for that.

## OAuth mode with Google sign-in (`oauth`)

- **Built on the MCP SDK's authorization-server framework** with PyJWT (HS256, a derived key and `typ` per purpose, so a
  refresh token can never be used as an access token, and so on). No hand-rolled crypto.
- **PKCE S256 required**; redirect URIs are matched exactly against a fixed allowlist (the two Claude callbacks, plus
  `http://localhost` / `http://127.0.0.1` loopback, port ignored) at registration, at authorize, and again at the callback.
  Unregistered redirect URIs never receive a redirect.
- **Tokens are audience-bound** to `<PUBLIC_BASE_URL>/mcp` and issuer-bound; wrong audience, issuer, key, purpose or expiry is
  a 401. The user's allowlist entry is re-checked on every request and every refresh.
- **Sign-in:** Google ID token verified for RS256 signature (Google's JWKS), issuer, audience, expiry (60 s allowance),
  nonce and `email_verified`. Allowlist: exact email, or domain plus a matching `hd` claim. **Deny by default.**
- **CSRF / confused deputy:** every authorization shows a consent page that names the client and the redirect host; the
  flow is bound to the browser by an HttpOnly, SameSite=Lax cookie checked at the consent POST (CSRF value) and again at the
  Google callback (state binding), alongside a signed `state`, a nonce, and short expiries. A callback replayed in another
  browser is rejected.
- **Redaction** covers JWTs, Google token shapes, OAuth query/form parameters (`code`, `state`, `id_token`, ...) and every
  configured OAuth secret; users appear in logs only as a hash.
- **Network:** only `accounts.google.com`, `oauth2.googleapis.com` and `www.googleapis.com` are contacted for sign-in
  (hard-coded), 10 s timeout, no redirects, TLS verified. Unauthenticated OAuth endpoints are limited to 60 requests per
  minute per client IP.
- **Fail closed:** missing or weak OAuth settings, or empty allowlists, stop startup.

Stateless trade-offs: tokens cannot be revoked individually (remove the user, or rotate `OAUTH_SIGNING_KEY` to sign everyone
out); single-use tracking for codes and refresh tokens covers only the running instance.

## Known limitations

- The path-secret URL is a bearer credential in a URL. Render's edge or any intermediary may log request paths. Prefer
  `bearer` or OAuth when the client supports it, and rotate `MCP_PATH_SECRET` if the URL is shared widely.
- In-memory state resets on restart (free-tier sleep); budgets restart cautious but not exact. Meta's `X-App-Usage` is the
  backstop.
- One shared token means one blast radius; use a token with the minimum permissions Meta allows for Ad Library access.
- The native Render Python runtime's user is not verified to be non-root.

## Reporting

Report suspected vulnerabilities privately to the repository owner; do not open a public issue. If a token may have
leaked: rotate it in Meta's dashboard and Render first, then investigate.
