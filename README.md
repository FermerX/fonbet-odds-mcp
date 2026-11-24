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
- `get_match_markets` - paginated match and linked sub-event markets, including unlabeled factors.
- `get_match_line` - compact, identified football markets for a match.
- `get_unlabeled_pairs` - inspect unlabeled factor pairs for manual verification.
- `fonbet_find_candidates` - find identified prematch football selections by start time, odds, and market category.
- `get_special_bets` - search current line for special/player/stat-style markets.
- `get_same_game_parlay` - inspect one event for same-game parlay ideas.
- `get_cross_match_specials` - inspect special-style markets across multiple events.
- `fonbet_value_check` - simple value-bet check from your estimated probability.

## Find prematch football candidates

Call `fonbet_find_candidates` with these MCP tool arguments:

```json
{
  "hours_ahead": 4,
  "min_odds": 1.40,
  "max_odds": 1.60,
  "markets": ["goals", "individual_totals", "double_chance"],
  "limit": 20,
  "offset": 0,
  "lang": "ru"
}
```

An empty argument object uses a four-hour window, the inclusive odds range 1.40–1.60,
all identified categories, a page of 50, and Russian. The window starts at the
time of the call. `hours_ahead` accepts values greater than zero and up to 12 hours.
`limit` accepts 1–500 and `offset` starts at zero.
The tool sorts by kickoff, event ID, market event ID, factor ID and threshold.
Each search returns an opaque `search_id`, its absolute `window_start` and
`window_end`, frozen `filters`, the coverage counters, and a `revision`.
Follow `next_cursor` to read the same revision without a new scan:

```json
{"cursor": "<next_cursor>", "limit": 20}
```

Legacy `offset` and `next_offset` are still accepted. With `search_id`,
an offset addresses the latest revision. Without an ID, an offset applies
to the currently resolved search for those filters. Use the cursor to pin a
revision across calls. Search filters and the absolute time window come from
the stored search when an ID or cursor is supplied. Pagination parameters do
not affect coalescing of identical searches.

When the scan reaches its deadline, `search_status` is `paused` and
`remaining_matches` counts fixtures still awaiting a terminal check. Resume the
same window with:

```json
{"search_id": "<search_id>", "resume": true, "limit": 20}
```

Continuation publishes a new sorted revision and checks only pending matches.
Existing cursors keep their earlier order and coverage counters. The server
rechecks pending fixtures against a current line and reports fixtures that have
left the window or become unavailable in `matches_no_longer_eligible`.
The initial line's identified price hints determine scan priority; matches
without hints remain in the queue. Terminal upstream errors are reported with
the original coverage. Start a new search to retry those fixtures.

Market filters match any listed value. Use `totals`, `individual_totals`,
`outcomes`, `handicaps`, `double_chance`, or `both_teams_score` for market
types. Use `goals`, `corners`, `yellow_cards`, `fouls`, `shots_on_target`,
`offsides`, `throw_ins`, `goal_kicks`, `shots`, or `woodwork` for the
underlying statistic. `halves` includes identified first- and second-half
markets. `statistics` includes the additional statistics from fouls through
woodwork. Each candidate has a `category`, `statistic`, `period`, `side`,
and match, market event and factor IDs. Russian metadata drives recognition.
For other languages the tool requests localized team and league names and uses
the Russian names where the feed has no translation. Market labels remain Russian.

Quote `received_at` and `line_received_at` are UTC receipt timestamps from
this server. Odds come from the event detail response. The effective age
includes the upstream HTTP `Age` header when present. Page reads preserve
`received_at`. A quote marked `snapshot` is eligible for display under the
current freshness limits. During replay, both source cache TTLs must still be
valid. Stale or superseded quotes and quotes past their recorded kickoff retain
their position and have `odds: null`.

To verify only the current page, pass `refresh: true` with a cursor or
`search_id`. An unchanged and available quote gets `quote_status: "verified"`
and a separate `verified_at`. A changed, blocked, missing or unverifiable
quote keeps its position, gets an explicit status, and has `odds: null`.
A timeout or upstream error can leave a partial page, with `partial: true`
and structured `errors`. A failed line request returns `status: "error"`.
An empty valid line returns `status: "ok"`. `search_status` reports scan
coverage separately from the current page's quote availability.
`root_events` counts distinct root events in the line. `eligible_matches` counts
prematch football fixtures in the requested window. `matches_checked` counts
admitted detail checks, including checks that fail or run out of time.
`matches_completed` counts fixtures with fresh, processed details, including
usable parts of incomplete responses. `skipped_events` is the difference
between `root_events` and `matches_completed`. `markets_processed` counts all
factor rows examined in eligible detail blocks, including invalid rows
(`invalid_market_rows`) and duplicates. `total_candidates` counts selections
before pagination. Identified aliases at equal prices count once. Conflicting
prices for the same selection suppress that selection before applying the odds
filter and increment `conflicting_markets`.

At capacity, the tool reports `search_capacity` for a refused scan,
`request_capacity` when a detail check cannot be queued, and explicit
`match_capacity` or `result_capacity` when a search exceeds its storage limits.
The application keeps up to four revisions per search. Expired or evicted
searches and revisions return `search_expired` or `revision_expired`.

`diagnostics` reports lookup hits and misses, coalesced detail and search
requests, actual HTTP attempts and retries, queue wait, network and processing
seconds, and quote age. The queue and network durations sum time across
workers. An HTTP attempt shared by multiple calls is attributed to its
originating operation. `scan_diagnostics` preserves the counters at the point
a revision was published. Results are in memory and expire on process restart.

`threshold_is_integer` distinguishes whole from fractional parameters.
Settlement details such as refunds, quarter-line splits, and extra time require
confirmation in the Fonbet betslip. This tool screens prices and makes no
probability or return forecast.

### HTTP client settings

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `FONBET_LINE_TTL` | 10 | Cache TTL for the general line, in seconds |
| `FONBET_EVENT_TTL` | 15 | Cache TTL for event details, in seconds |
| `FONBET_LINE_CACHE_SIZE` | 8 | Maximum cached line responses |
| `FONBET_EVENT_CACHE_SIZE` | 256 | Maximum cached event responses |
| `FONBET_REQUESTS_PER_SECOND` | 5 | Maximum outbound request start rate per server process |
| `FONBET_MAX_CONCURRENCY` | 4 | Maximum concurrent upstream requests per server process |
| `FONBET_MAX_INFLIGHT_REQUESTS` | 32 | Maximum distinct upstream requests active, queued, or waiting to retry per server process |
| `FONBET_MAX_CONCURRENT_SEARCHES` | 2 | Maximum simultaneous candidate scans or page verifications per server process |
| `FONBET_SEARCH_CACHE_SIZE` | 8 | Maximum retained searches per server process |
| `FONBET_SEARCH_STATE_TTL` | 180 | Absolute retention of search metadata and cursors, in seconds |
| `FONBET_SEARCH_MAX_CANDIDATES` | 10000 | Maximum stored selections per search |
| `FONBET_SEARCH_MAX_MATCHES` | 3000 | Maximum frozen fixtures per search |
| `FONBET_RETRIES` | 2 | Additional attempts on network errors, timeouts, HTTP 429, and HTTP 500/502/503/504 |
| `FONBET_MAX_RETRY_DELAY` | 5 | Maximum retry wait, in seconds |
| `FONBET_MAX_SNAPSHOT_AGE` | 60 | Maximum snapshot age and scan deadline, measured from receipt of the line |

Set the line or event TTL or cache size to zero to disable reuse for that source.
Search metadata retention is independent of quote freshness. Concurrent
identical requests share one upstream operation. All MCP sessions served by the
same process share the client, caches and rate limits. A longer upstream
`Retry-After` pauses new upstream requests and returns a visible error to the
caller. Expired cache entries are never used as a fallback.

Run the full suite with `python -m unittest discover -s tests -v`.

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
