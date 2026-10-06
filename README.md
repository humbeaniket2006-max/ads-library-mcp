# Meta Ad Library MCP server

A read-only remote [MCP](https://modelcontextprotocol.io) server that lets Claude query Meta's **Ad Library API**
(Graph API `/ads_archive`). Official API only, no scraping. Built for a small team: the server holds one Meta token and
users never see it. Hosted on Render's free tier through a Blueprint.

**Coverage limit (also stated in the `search_ads` tool description):** the official API returns only ads delivered in the
EU, plus political and social-issue ads worldwide. Ads outside that scope are not available.

## Tools

| Tool | What it does | Meta calls |
|---|---|---|
| `search_ads` | Search by `query` and/or `page_ids`, with filters. Compact, de-duplicated rows, signed cursor for paging. | 1 (cached) |
| `find_advertiser` | Name to up to 10 `{page_id, page, ads_seen}`. | 1 (cached 24 h) |
| `summarize_advertiser` | Counts by status/platform/language, start-date range, 5 longest-running ads. | up to `MAX_SUMMARY_PAGES` |
| `get_status` | Filter values, budget left, cache hit rate, token health, mock flag, cold start. | 0 |

Every response ends with `meta: {n, next, cached, stale, cc, mock}`. `cc` is the number of Meta calls this response
cost (0 for cache hits and mock mode). `warn` is added only when Meta's app usage is at or above the warn threshold.

### Protecting the two budgets

- **Meta rate limits:** every response is cached (LRU, entries and bytes capped); identical concurrent requests share one
  call (single-flight); stale data up to 24 h old is served (`stale:true`) when the budget is out or Meta fails; global
  hour/day budgets and a per-client per-minute bucket are enforced *before* calling Meta; retries count against the budget;
  `X-App-Usage` warns at 70%, throttles at 85% (hourly cap halved) and stops at 95%; a circuit breaker pauses calls after
  repeated failures; no polling, prefetching or auto-pagination beyond `MAX_PAGES_PER_CALL`.
- **Claude tokens:** four short tool descriptions, compact JSON with short keys and no nulls, text trimmed to
  `AD_TEXT_MAX_CHARS`, identical ads from one page merged (`n`, at most 3 ids), hard cap `MAX_RESPONSE_CHARS`
  (`truncated:true`), no raw Meta JSON, paging URLs or snapshot URLs (unless `include_links`). In mock mode a
  10-ad search is about 1.5 to 2.3 KB.
- On budget exhaustion the model gets one sentence such as
  `The hourly Meta call budget is used up; retry_after_seconds=1200. Do not retry this call.`

## Run locally

Requires Python 3.11+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export META_MOCK=true MCP_AUTH_MODE=path_secret
export MCP_PATH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export CURSOR_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ALLOWED_HOSTS=localhost,127.0.0.1
uvicorn app.main:app --port 8000
# MCP endpoint: http://127.0.0.1:8000/mcp/$MCP_PATH_SECRET     health: http://127.0.0.1:8000/healthz
```

Or copy `.env.example` and load it with your shell/dotenv tool of choice (the app reads the process environment only).

### Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP, URL: http://127.0.0.1:8000/mcp/<MCP_PATH_SECRET>, then Connect > Tools > List Tools
# Headless:
npx @modelcontextprotocol/inspector --cli "http://127.0.0.1:8000/mcp/$MCP_PATH_SECRET" --transport http --method tools/list
```

Mock mode serves fixtures shaped like real `/ads_archive` responses (including ads with HTML, zero-width and bidi
characters, and an injection attempt, to exercise sanitising). Try `query="Nordlicht"`, `page_ids=["1004004004"]`,
`find_advertiser name="FitRoute"`, `summarize_advertiser page_id="1003003003"`.

### Tests and lint

```bash
ruff check .
pytest            # no network; Meta is mocked with respx
pip-audit -r requirements.txt
```

## Deploy via Blueprint (Render)

1. Push this repo to GitHub (branch `main`).
2. In Render: **New > Blueprint**, select the repo. Render reads `render.yaml`.
3. Fill the `sync: false` values: `META_ACCESS_TOKEN` (leave blank while `META_MOCK=true`) and `MCP_API_KEYS`
   (optional; only used with `bearer` or `both`).
4. Deploy. Copy the service URL (`https://<service>.onrender.com`).
5. Open the service's **Environment** tab and copy the generated `MCP_PATH_SECRET`.
6. Your connector URL is `https://<service>.onrender.com/mcp/<MCP_PATH_SECRET>`.

`/healthz` returns `{"ok":true}` with no auth and no details. The app adds Render's `RENDER_EXTERNAL_HOSTNAME` to the
allowed hosts at runtime, so no hostname is hard-coded.

> Render's generated secret is base64 and may contain `+`, `/` or `=`. Paste it into the URL exactly as shown (a raw `/`
> or a `%2F` both work). If a client mangles it, set your own `MCP_PATH_SECRET` (e.g. `python -c "import secrets;
> print(secrets.token_urlsafe(32))"`) in the Environment tab.
>
> Treat the connector URL as a password. Anyone with it can use the server (and your Meta budget).

## Free tier limits

- **Sleep and cold start:** Render spins a free web service down after 15 minutes without inbound traffic and takes about a
  minute to start it again (per Render's docs). The first call after sleep may time out in the client; retrying works.
- **State is lost:** the cache and budget counters live in memory and start empty after every sleep or deploy. The server
  logs `cold start, cache and budgets reset` and starts cautious: until the process has been up a full window, the hourly
  and daily budgets are scaled by `COLD_START_BUDGET_FACTOR` (default 0.5). Meta's own `X-App-Usage` reading on the first
  call corrects for usage the restart forgot.
- **750 instance hours per month** per workspace on the free plan, one instance, no persistent disk.
- **Keep-alive (optional):** `.github/workflows/keep-awake.yml` pings `/healthz` every 7 minutes so the instance rarely
  sleeps. One always-on service uses about 744 of the 750 free hours per month, so it leaves almost no headroom for a second
  free service. It is external to the app (no self-ping loop). GitHub may delay scheduled runs, and disables scheduled
  workflows after 60 days without repo activity. Delete the file to go back to sleeping when idle. A sleeping instance
  still resets the cache and budgets, so keeping it awake also keeps them.
- **Move to a paid plan:** in `render.yaml`, change `plan: free` to `plan: starter` (the commented line shows where), commit,
  and push. Do this when the team uses it daily or the cold start gets in the way.

## Add the connector

**claude.ai:** Settings > Connectors > Add custom connector, paste the connector URL
(`https://<service>.onrender.com/mcp/<MCP_PATH_SECRET>`). Claude.ai custom connectors may not accept static headers,
which is why the pilot uses `path_secret`. For a login instead of a secret URL, use OAuth mode (see "OAuth sign-in" below).

**Claude Code:**

```bash
claude mcp add --transport http meta-ads https://<service>.onrender.com/mcp/<MCP_PATH_SECRET>
# bearer mode instead (MCP_AUTH_MODE=bearer or both):
claude mcp add --transport http meta-ads https://<service>.onrender.com/mcp --header "Authorization: Bearer <key>"
```

**Claude Desktop:** add it as a custom connector with the same URL.

### Auth modes

`MCP_AUTH_MODE` takes one value or a comma list: `path_secret`, `bearer`, `oauth_client`, `oauth`, `both` (= `path_secret` +
`bearer`, kept for compatibility), or a mix such as `path_secret,oauth_client`.

| Mode | Endpoint | Failure |
|---|---|---|
| `path_secret` | `/mcp/<secret>` | 404 |
| `bearer` | `/mcp` with `Authorization: Bearer <key>` (labelled keys: `MCP_API_KEYS=alice:<key>,bob:<key>`) | 401 |
| `oauth_client` | `/mcp` with an OAuth token from claude.ai's **Client ID + Secret** (simplest, no Google; see below) | 401 + discovery pointer |
| `oauth` | `/mcp` with an OAuth token after each user signs in with Google (per-person access) | 401 + discovery pointer |

The bearer label appears in logs and drives the per-client rate limit; keys are never logged. Rotate or revoke a key by
editing the variable (Render restarts the service). Rotate the path secret by changing `MCP_PATH_SECRET` and re-adding the
connector. In oauth mode the connector URL has **no secret in it**.

## OAuth with just a client ID and secret (`oauth_client`, no Google)

This is the same shape as most connectors: you paste a **Client ID** and **Client Secret** into claude.ai's *Advanced
settings* and the URL has no secret in it. Nothing to create anywhere else; Render generates both values.

1. In `render.yaml` set `MCP_AUTH_MODE` to `path_secret,oauth_client` (keeps your current URL working while you switch), push,
   and let Render redeploy. `OAUTH_SIGNING_KEY`, `MCP_OAUTH_CLIENT_ID` (`claude-meta-ads`) and `MCP_OAUTH_CLIENT_SECRET`
   already exist in the Blueprint; `PUBLIC_BASE_URL` falls back to Render's own URL.
2. In Render > service > **Environment**, copy `MCP_OAUTH_CLIENT_ID` and `MCP_OAUTH_CLIENT_SECRET`.
3. In claude.ai: Settings > Connectors > **Add custom connector**, URL `https://<service>.onrender.com/mcp`, open
   **Advanced settings**, paste the Client ID and Client Secret, then **Connect**. It connects without a login page.
4. Claude Code: open registration is closed in this mode, so Claude Code cannot self-register; it must be given the same
   client ID and secret. Check `claude mcp add --help` for how your version accepts OAuth client credentials. If it has no
   such option, keep `path_secret` in `MCP_AUTH_MODE` (`path_secret,oauth_client`) and use the secret URL from Claude Code
   (`claude mcp add --transport http meta-ads https://<service>.onrender.com/mcp/<MCP_PATH_SECRET>`), or use the Google
   `oauth` mode, which allows self-registration.
5. Once everyone is connected through OAuth, change `MCP_AUTH_MODE` to `oauth_client` to switch the secret URL off.

How it stays safe without a login: the **secret is the credential**. `/authorize` approves automatically only for this one
pre-registered client, and the resulting code can be redeemed only at `/token` with the client secret and the PKCE
verifier, so opening the authorize link gets nobody anything. Open client registration is turned off, so nobody can
register their own client. Redirects are still limited to the Claude callbacks and loopback. Access tokens last 1 hour and
refresh tokens rotate.

**Rotate the secret:** change `MCP_OAUTH_CLIENT_SECRET` in Render and save. All existing tokens stop working immediately
(they are tied to the secret), and you paste the new secret into the connector's Advanced settings. `OAUTH_SIGNING_KEY`
rotation does the same.

Trade-offs: everyone who has the secret is one identity, so there are no per-person accounts, no per-person revocation and
budgets are shared by that identity (the per-minute/hour/day limits apply to the connector as a whole). Use the Google mode
below when you want individual access control. Whoever you give the secret to can connect, so treat it like a password.

## Optional: Google sign-in (`oauth`, per-person access)

> Not part of the Blueprint or `.env.example`. Only use it if you want individual accounts: add `GOOGLE_OAUTH_CLIENT_ID`,
> `GOOGLE_OAUTH_CLIENT_SECRET`, `ALLOWED_EMAILS` and/or `ALLOWED_EMAIL_DOMAINS` by hand in Render's Environment tab and set
> `MCP_AUTH_MODE` to `oauth`. Most teams should use `oauth_client` above.

The server is its own OAuth 2.1 authorization server for Claude, built on the MCP SDK's auth framework (PyJWT for the
signatures; no hand-rolled crypto). Users sign in with Google and only allowed emails get through.

```
claude.ai --/authorize (PKCE S256)--> this server --consent page--> Google sign-in --> /oauth/google/callback
   ^                                                                                         |
   +---- redirect to https://claude.ai/api/mcp/auth_callback?code=... <-- email checked -----+
claude.ai --/token--> short-lived access token (1 h) + rotating refresh token --> /mcp
```

What it does: authorization code flow with PKCE (S256 only), Dynamic Client Registration (`/register`) plus one optional
pre-registered static client, discovery metadata at `/.well-known/oauth-protected-resource/mcp` and
`/.well-known/oauth-authorization-server` (URLs built from `PUBLIC_BASE_URL`, which defaults to Render's
`RENDER_EXTERNAL_URL`), tokens bound to this server (`aud` = `<PUBLIC_BASE_URL>/mcp`, checked on every request), refresh
token rotation, a consent page for every sign-in, and exact-match redirect URIs: `https://claude.ai/api/mcp/auth_callback`,
`https://claude.com/api/mcp/auth_callback`, and `http://localhost` / `http://127.0.0.1` loopback on any port (Claude Code).
Everything else is rejected at registration and at authorize.

**Stateless on purpose.** Render's free tier sleeps and forgets memory, so nothing is stored: client registrations,
authorization codes, access tokens and refresh tokens are HMAC-signed JWTs (`OAUTH_SIGNING_KEY`). They survive restarts.
**Rotating `OAUTH_SIGNING_KEY` signs everybody out and invalidates every registered client**; they just reconnect.

### 1. Create the Google OAuth client

1. In [Google Cloud Console](https://console.cloud.google.com/) pick or create a project.
2. **APIs & Services > OAuth consent screen**: configure it. Choose *Internal* if everyone is in your Google Workspace
   organisation, otherwise *External* (in *Testing* mode add each user under *Test users*). The only scopes used are
   `openid` and `email`.
3. **APIs & Services > Credentials > Create credentials > OAuth client ID**, application type **Web application**.
4. Under **Authorised redirect URIs** add exactly (no trailing slash):
   `https://<your-service>.onrender.com/oauth/google/callback`
   (for local testing also `http://localhost:8000/oauth/google/callback`).
5. Copy the client ID and secret.

### 2. Set the variables on Render

In the service's **Environment** tab (or `.env` locally; see `.env.example`):

| Variable | Value |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | from step 1 |
| `ALLOWED_EMAILS` | comma-separated full addresses, e.g. `ana@example.com,ben@example.com` |
| `ALLOWED_EMAIL_DOMAINS` | optional, e.g. `example.com` (Workspace domains must match Google's `hd` claim; `gmail.com` addresses match without it) |
| `OAUTH_SIGNING_KEY` | generated by the Blueprint (at least 32 characters) |
| `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` | the static client for claude.ai Advanced settings (ID `claude-meta-ads`, secret generated) |
| `PUBLIC_BASE_URL` | leave empty on Render (uses `RENDER_EXTERNAL_URL`) |

At least one of `ALLOWED_EMAILS` / `ALLOWED_EMAIL_DOMAINS` is required: with both empty the server **refuses to start in
oauth mode** (deny by default). It also refuses to start if the Google values or signing key are missing or weak.

Then turn OAuth on by editing `MCP_AUTH_MODE` in `render.yaml` (`path_secret,oauth` while you migrate, `oauth` afterwards)
and pushing. The Blueprint keeps `path_secret` as the default so existing deployments do not break. If the new
`sync: false` variables are not present on an existing service, add them by hand in the Environment tab.

### 3. Add the connector in claude.ai

Settings > Connectors > **Add custom connector**. Name it, and set the URL to
`https://<your-service>.onrender.com/mcp` (no secret). Open **Advanced settings**:

- **Leave Client ID and Client Secret blank** (recommended). Claude registers itself through Dynamic Client Registration.
- Or enter `MCP_OAUTH_CLIENT_ID` and `MCP_OAUTH_CLIENT_SECRET` from the Environment tab to use the pre-registered client.

Click **Connect**: you see this server's consent page ("Continue with Google"), sign in with an allowed account, and
return to Claude. Each person signs in with their own Google account.

### 4. Add it in Claude Code

```bash
claude mcp add --transport http meta-ads https://<your-service>.onrender.com/mcp
# then in Claude Code run /mcp, pick meta-ads and choose Authenticate (a browser window opens)
```

Claude Code redirects to a loopback address on a random port; that is allowed. The consent page warns that the app runs on
your own computer.

### 5. Operate it

- **Rotate the signing key** (everyone signs out; no data lost): edit `OAUTH_SIGNING_KEY` in Render, save. Users reconnect.
- **Rotate the static client secret:** edit `MCP_OAUTH_CLIENT_SECRET`, then update the connector's Advanced settings.
- **Rotate the Google secret:** create a new secret in Google Cloud, update `GOOGLE_OAUTH_CLIENT_SECRET`, delete the old one.
- **Revoke one user:** remove their address (or domain) from `ALLOWED_EMAILS` / `ALLOWED_EMAIL_DOMAINS` and save. After the
  restart their access token is refused on the next request and their refresh token no longer works. Nobody else is
  affected. For an emergency "everyone out", rotate the signing key.
- **Limits per user:** budgets and the per-minute bucket are keyed by the signed-in email (shown only as a hash in logs):
  `CLIENT_MAX_CALLS_PER_MINUTE`, `CLIENT_MAX_CALLS_PER_HOUR` (30), `CLIENT_MAX_CALLS_PER_DAY` (200), on top of the global
  Meta budget.
- **Cold start:** Claude gives the discovery, registration and token endpoints 10 seconds. A free instance that is asleep
  takes about a minute to start, so the *first* connect attempt after sleep can fail. Retry once it is awake (the keep-awake
  workflow helps).
- **Token lifetimes:** `OAUTH_ACCESS_TOKEN_TTL` (3600 s), `OAUTH_REFRESH_TOKEN_TTL` (30 days, sliding), `OAUTH_CODE_TTL` (60 s).

Because tokens are stateless there is no server-side list to revoke from. Authorization codes and refresh tokens are
single use *per running instance* (a replay is caught only while the process stays up); the short code lifetime and the
refresh rotation bound the rest.

## Meta token: renewal and revoke runbook

1. **Short-lived to long-lived:** from a trusted machine (never from a browser), call
   `GET https://graph.facebook.com/oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<SHORT_LIVED_TOKEN>`.
   Meta documents long-lived user tokens as lasting about 60 days. Put a calendar reminder at about 50 days.
2. **Update Render:** service > Environment > edit `META_ACCESS_TOKEN` > Save (Render redeploys). Call `get_status`:
   `token` is `unknown` until the first successful call, then `ok`.
3. **Error 190 (or code 10 with subcode 2332002):** the server never retries, sets `token` to `expired` or `unauthorised`
   and blocks Meta calls for 10 minutes, returning a clear message. Fix: renew as above, or check that your identity
   confirmation / Ad Library API access is complete. Cached data keeps being served (`stale:true`) meanwhile.
4. **Revoke:** remove the token in Render (set `META_ACCESS_TOKEN` empty and `META_MOCK=true`), then revoke the token or
   reset the app secret in the Meta developer dashboard. If the token ever appeared in a log, treat it as leaked and
   rotate it.

## Going live

Once Meta approves your identity confirmation: set `META_ACCESS_TOKEN`, set `META_MOCK=false` in Render's Environment tab,
and save. The server refuses to start with `META_MOCK=false` and no token. Check `get_status` (`mock:false`), then run a
small `search_ads`. Keep the first week's budgets conservative.

## Tuning

| Variable | Default | Effect |
|---|---|---|
| `META_MAX_CALLS_PER_HOUR` / `_PER_DAY` | 60 / 500 | Global Meta call budgets (sliding windows). |
| `CLIENT_MAX_CALLS_PER_MINUTE` / `_PER_HOUR` / `_PER_DAY` | 10 / 30 / 200 | Per key label, signed-in user or IP. |
| `CACHE_TTL_ACTIVE` / `_HISTORICAL` / `_ADVERTISER` | 900 / 21600 / 86400 | Seconds. Raise to spend fewer calls. |
| `CACHE_MAX_ENTRIES` / `CACHE_MAX_BYTES` | 500 / 20 MB | LRU limits. |
| `MAX_PAGES_PER_CALL` / `MAX_SUMMARY_PAGES` | 1 / 2 | Pages fetched per tool call. |
| `DEFAULT_LIMIT` / `MAX_LIMIT` | 10 / 50 | Ads per page. |
| `AD_TEXT_MAX_CHARS` / `MAX_RESPONSE_CHARS` | 300 / 12000 | Claude token controls. |
| `COLD_START_BUDGET_FACTOR` | 0.5 | Budget scaling after a restart. |
| `CACHE_STALE_MAX`, `USAGE_WARN_PCT`, `USAGE_THROTTLE_PCT`, `USAGE_STOP_PCT`, `BREAKER_FAILURES`, `BREAKER_COOLDOWN`, `CLIENT_IP_HEADER` | see `.env.example` | Extras. |

Rule of thumb: watch `get_status` for a week; if `hour.left` rarely drops below half and the cache hit rate is above 50%,
you have headroom. Raise TTLs before raising budgets.

## Layout

```
app/config.py    env config, fails closed        app/store.py    LRU cache, budgets, buckets (swap for Redis)
app/meta.py      Graph client, backoff, breaker  app/service.py  the four tools' logic
app/format.py    sanitise, compact, cursor       app/tools.py    MCP tool registration
app/main.py      ASGI gateway (auth, limits)     app/redact.py   secret redaction + JSON logs
app/fixtures.py  mock data                       render.yaml     Blueprint
```

## Unverified assumptions

Verified against current docs or the installed package: the `/ads_archive` parameter and `ArchivedAd` field names, the
`X-App-Usage` header format, Render Blueprint fields, `RENDER_EXTERNAL_HOSTNAME`, free-tier spin-down (15 min, about 1 min
to restart), the fully qualified `PYTHON_VERSION` rule, the long-lived token exchange, and the `mcp` 2.3.0 API (read from
the installed source, because the docs site excerpts did not cover the HTTP options).

**Could not verify:**

- **`mcp` 2.x naming:** the SDK renamed `FastMCP` to `MCPServer` in 2.x (confirmed by the package's own import error and
  source). The brief says FastMCP; this uses the 2.x equivalent, pinned at `mcp==2.3.0`.
- **Token error handling:** code 190 and code 10 with subcode 2332002 come from your brief; I could not load Meta's error
  reference. The `expired` vs `unauthorised` split (subcodes 463/467 or "expired" in the message) is my assumption.
- **Rate-limit errors:** codes 4, 17, 32, 613 and 80000-80014 are from Meta's rate-limiting page, but I did not confirm
  `/ads_archive` returns them. Whether `/ads_archive` sends `X-App-Usage` is also unconfirmed; the code only reacts if it
  is present.
- **Array encoding:** array parameters are sent as JSON (`ad_reached_countries=["DE"]`, `search_page_ids=[123]`). I did not
  find an official example of the wire format.
- **Fields on non-political ads:** `spend`, `impressions`, `currency` and `demographic_distribution` are documented as
  political/issue-only, `eu_total_reach` as EU-only. I assume requesting them for other ads returns them absent rather than
  erroring. If Meta rejects that, `detail=full` needs a field split.
- **"reach" = `eu_total_reach`** and "demographics" = `demographic_distribution`; that is my mapping.
- **Snapshot URLs:** I assume `ad_snapshot_url` can embed an access token, so the server strips `access_token` from it. The
  stripped link may need a signed-in viewer to render.
- **Ad Library access requirements** (identity confirmation) and any Ad Library-specific rate limit: Meta's page would not
  load. Your "no per-call fee" assumption is carried over unverified.
- **Graph version v26.0:** your default; I did not confirm it is the current version.
- **Render:**
  - The client-IP header: `True-Client-IP` is from community posts, not official docs. It is configurable via
    `CLIENT_IP_HEADER` and falls back to the socket address.
  - `generateValue` output: observed to be base64 (so `+ / =` are accepted in `MCP_PATH_SECRET`); its length is still
    unconfirmed, and the app refuses to start if it is under 32 characters.
  - Whether a blank `sync: false` entry is accepted at creation.
  - Whether the native Python runtime runs as non-root.
  - The Host header Render's health checker sends: `/healthz` skips the host check for this reason.
  - Client connector timeouts after sleep.
- **`autoDeploy`:** the brief says `autoDeploy: true`; Render's spec marks it deprecated, so `render.yaml` uses
  `autoDeployTrigger: commit`. I could only validate `render.yaml` structurally (tests parse it); I could not run Render's
  own validator.
- **`cc`** in the response meta is interpreted as "Meta calls charged for this response".
- **Claude.ai static headers:** per your note, not confirmed.

### OAuth: unverified assumptions

- **`oauth_client` mode:** that claude.ai's Advanced settings send the pasted client ID and secret via `client_secret_post` or
  Basic (both work), and whether Claude Code can be given a client ID and secret (I did not verify any flag for it). Not run against a real client.

Verified: Claude's connector authentication page (callback URLs, S256 PKCE, DCR, `401` + `resource_metadata`, refresh
rotation, 10 s endpoint limit, loopback redirects), the MCP authorization spec (RFC 9728/8414/7591/8707 requirements),
Google's OpenID configuration (endpoints, RS256, issuer), and the `mcp` 2.3.0 auth framework (read from the installed source).

Not verified:

- **Real Claude clients were not run against this.** The full flow is tested end to end against a mock Google with real RSA
  signing, and the HTTP surface was exercised with curl. Whether claude.ai picks DCR, a static `client_secret_post`, or
  `client_secret_basic` is unconfirmed; all three work (Basic credentials are folded into the form body at the gateway).
- **Advertised token auth methods:** the SDK's authorization-server metadata lists `client_secret_post` and
  `client_secret_basic` but not `none`, and does not set `client_id_metadata_document_supported`. DCR clients register as
  public clients (`none`) and work, and Claude falls back to DCR when CIMD is not advertised, but I could not confirm a
  strict client accepts that. Claude Code is expected to use DCR here rather than its Client ID Metadata Document.
- **`claude.com` callback:** Claude's page documents `https://claude.ai/api/mcp/auth_callback`; the `claude.com` URL is
  from your brief.
- **`offline_access`:** advertised in `scopes_supported`; refresh tokens are issued whether or not Claude requests it.
- **Resource indicator:** a missing `resource` is accepted (treated as this server); a different one is refused
  (`invalid_target`). The exact value Claude sends was not observed.
- **Google:** the Cloud Console menu names above, whether an *External* app in *Testing* mode needs test users for `openid
  email` only, that `http://localhost` redirect URIs are accepted for local tests, the `hd` claim being present for every
  Workspace account, and `prompt=select_account` behaviour. Google ID tokens get a 60 s clock allowance; this server's own
  tokens get none (the SDK's bearer check is strict).
- **Render:** whether editing `MCP_AUTH_MODE` in the dashboard survives the next Blueprint sync (change it in `render.yaml`
  to be safe), and whether new `sync: false` keys appear on an existing service.
- **Replay and revocation limits** described above are inherent to stateless tokens; a store (Redis) behind the same
  provider would close them.
- **No CORS:** the SDK's wildcard CORS headers are removed at the gateway to keep the "no CORS" rule, so a browser-based
  MCP Inspector cannot complete OAuth; use the CLI mode or a real client.

