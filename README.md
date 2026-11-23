# Fonbet MCP

MCP server for reading Fonbet sports events and odds.

Important: Fonbet does not provide a stable public API contract for this use case. Use this server only where it complies with local law, the bookmaker rules, and request limits. Betting forecasts are not financial guarantees: the server helps analyze odds, probability, and value, but it cannot promise profit.

## Local install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

## Local ChatGPT run

```powershell
$env:MCP_TRANSPORT="streamable-http"
$env:MCP_HOST="127.0.0.1"
$env:MCP_PORT="8000"
python -m fonbet_mcp.server
```

Local endpoint:

```text
http://127.0.0.1:8000/mcp
```

## Render Free deploy

This repo includes `render.yaml`.

1. Push this folder to a GitHub repository.
2. Open Render: https://render.com
3. New + -> Blueprint.
4. Connect the GitHub repository.
5. Render will read `render.yaml` and create `fonbet-odds-mcp`.
6. After deploy, use this ChatGPT connector URL:

```text
https://YOUR-RENDER-SERVICE.onrender.com/mcp
```

If you create a Web Service manually instead of Blueprint:

- Build command: `pip install -e .`
- Start command: `python -m fonbet_mcp.server`
- Environment variables:
  - `MCP_TRANSPORT=streamable-http`
  - `MCP_HOST=0.0.0.0`
  - `MCP_PATH=/mcp`
  - `MCP_MESSAGE_PATH=/messages/`
  - `MCP_DNS_REBINDING_PROTECTION=false`
  - `FONBET_BASE_URL=https://line-lb51.bk6bba-resources.com`
  - `FONBET_CURRENT_LINE_PATH=/events/listBase`
  - `FONBET_EVENT_VIEW_PATH=/events/event`
  - `FONBET_SCOPE_MARKET=1600`
  - `FONBET_TIMEOUT=20`

## MCP tools

- `fonbet_search_events` - search events by team, league, or sport.
- `fonbet_event_odds` - compatibility tool: first large page of event odds by `event_id`.
- `get_event_odds` - paginated full event markets; use `offset`, `limit`, `query`, and `next_offset`.
- `get_special_bets` - search current line for special/player/stat-style markets.
- `get_same_game_parlay` - inspect one event for same-game parlay ideas.
- `get_cross_match_specials` - inspect special-style markets across multiple events.
- `fonbet_value_check` - simple value-bet check from your estimated probability.

## Extended football line (opt-in branch)

After deploying this branch, the existing `get_match_line("Сантос - Гремио")`
tool preserves `market_sections` (verified full-time goal, corner and yellow-card
totals) and adds `expanded_market_sections`, grouped by statistic and period.

Additional explicitly named statistics: fouls, shots on target, offsides,
throw-ins, goal kicks, all shots, and shots hitting the woodwork. It also lists
known factor-ID result, double-chance and handicap selections, plus
first-/second-half totals when the corresponding sub-event exists.

**Safety/accuracy:** The new output includes only known factor IDs in matching
root/child/grandchild football sub-events. It does not infer unknown factors
from price, factor adjacency, or numerical parameter. Period markets are kept
separate and returned odds are not win-probability forecasts. The
`identification_basis` flag means an identified mapping, not fresh validation
against the Fonbet user interface. Before placing a bet, confirm the
selection and settlement rules in the Fonbet betslip.

`expanded_market_count` and `selected_market_count` reflect extra identified
selections. `get_match_markets` remains available for all unrecognized factors.
No account, login, or bet-placement functionality is added.
OAuth and deployment configuration remain unchanged.

Tests: `python -m unittest discover -s tests -p 'test_expanded_summary.py'`.

## Current Fonbet endpoints

- `GET /events/listBase?scopeMarket=1600&lang=ru`
- `GET /events/event?eventId=...&scopeMarket=1600&lang=ru`

Odds are read from `customFactors[].factors[]`; `f` is the factor id and `v` is the decimal odds value.


## Security / optional OAuth 2.1

**Until enabled, this public Render MCP endpoint remains unauthenticated.**
It reads public Fonbet odds and has no account-login or betting tools, but
other people can invoke the tools and consume Render capacity.

ChatGPT custom MCP plugins support OAuth or no authentication, not custom
API-key request headers. A secret in a public URL is not a reliable substitute.

### Setup (only after an OAuth provider is ready)

1. Configure an OAuth/OIDC authorization server supporting authorization-code
   flow with PKCE S256, ChatGPT-compatible CIMD/DCR client onboarding, and
   discoverable issuer metadata. Configure its allowed users and token audience.
2. Configure access tokens as RS256/ES256 signed JWTs with JWKS, and claims
   `iss`, `aud`, `sub`, `exp`, plus scope `fonbet:read` (in `scope` or `scp`).
   Support refresh tokens / `offline_access` for stable ChatGPT connections.
3. In the **Render dashboard > your service > Environment**, set:

   - `MCP_AUTH_MODE=oauth`
   - `MCP_OAUTH_ISSUER=https://EXACT-OAUTH-ISSUER`
   - `MCP_OAUTH_JWKS_URL=https://PROVIDER-JWKS-ENDPOINT`
   - `MCP_OAUTH_RESOURCE_URL=https://SERVICE.onrender.com/mcp`
   - `MCP_OAUTH_AUDIENCE=https://SERVICE.onrender.com/mcp` (or provider's API audience)
   - `MCP_OAUTH_REQUIRED_SCOPES=fonbet:read`

   Do not put secret credentials in GitHub or `render.yaml`. The provider
   must issue tokens with the same audience and scopes as the server expects.
4. Switch your **ChatGPT > Plugins > Fonbet Odds** authentication setting
   to OAuth / reconnect with OAuth; complete provider login and consent.

**Safe rollout:** Leave `MCP_AUTH_MODE=none` (or unset) until the provider
and ChatGPT login are ready; otherwise the existing plugin will stop working.
For zero disruption, test using a separate protected Render service first.
A rollback to `none` restores old access **but makes the endpoint public again**.

Invalid OAuth configuration fails on startup instead of silently falling back
to no authentication. Valid mode checks JWT signatures, issuer, audience,
expiration and scopes. This server verifies tokens but does not issue them.
