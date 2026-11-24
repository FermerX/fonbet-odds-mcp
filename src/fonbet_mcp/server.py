from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette

from fonbet_mcp.candidates import MAX_HOURS_AHEAD, search_candidates
from fonbet_mcp.football_stats import FootballDataClient
from fonbet_mcp.summary import find_match_candidates, summarize_match_line
from fonbet_mcp.security import build_auth_kwargs

from fonbet_mcp.config import load_settings
from fonbet_mcp.fonbet import (
    FonbetClient,
    extract_day_specials,
    extract_markets,
    extract_match_markets,
    extract_unlabeled_pairs,
    extract_odds,
    search_events,
    value_check,
)

settings = load_settings()
client: FonbetClient | None = None
stats_client: FootballDataClient | None = None
allowed_hosts = [item.strip() for item in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if item.strip()]
dns_rebinding_protection = os.getenv("MCP_DNS_REBINDING_PROTECTION", "false").lower() == "true"
server_port = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))


def create_client() -> FonbetClient:
    return FonbetClient(
        base_url=settings.base_url,
        scope_market=settings.scope_market,
        timeout=settings.timeout,
        current_line_path=settings.current_line_path,
        event_view_path=settings.event_view_path,
        policy=settings.request_policy,
    )


def get_client() -> FonbetClient:
    if client is None:
        raise RuntimeError("The Fonbet application is not running")
    return client


def get_stats_client() -> FootballDataClient:
    if stats_client is None:
        raise RuntimeError("The football statistics client is not running")
    return stats_client


@asynccontextmanager
async def application_client():
    global client, stats_client
    if client is not None or stats_client is not None:
        raise RuntimeError("The Fonbet application is already running")
    active_client = client = create_client()
    active_stats = stats_client = FootballDataClient(token=os.getenv("FOOTBALL_DATA_API_TOKEN"))
    try:
        yield
    finally:
        try:
            await active_stats.aclose()
        finally:
            try:
                await active_client.aclose()
            finally:
                client = None
                stats_client = None


def _wrap_http_lifespan(app: Starlette) -> Starlette:
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # The client must outlive every session in the SDK's session manager.
        async with application_client(), sdk_lifespan(app) as state:
            yield state

    app.router.lifespan_context = lifespan
    return app


class FonbetMCP(FastMCP):
    def streamable_http_app(self) -> Starlette:
        return _wrap_http_lifespan(super().streamable_http_app())

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        return _wrap_http_lifespan(super().sse_app(mount_path))

    async def run_stdio_async(self) -> None:
        async with application_client():
            await super().run_stdio_async()


mcp = FonbetMCP(
    "fonbet",
    instructions=(
        "Read Fonbet sports events and decimal odds. Use paginated market tools when a match "
        "has many markets. Treat value checks only as analytical signals, not betting guarantees."
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=server_port,
    streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
    sse_path=os.getenv("MCP_SSE_PATH", "/sse"),
    message_path=os.getenv("MCP_MESSAGE_PATH", "/messages/"),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_rebinding_protection,
        allowed_hosts=allowed_hosts,
    ),
    **build_auth_kwargs(),
)


@mcp.tool()
async def fonbet_search_events(query: str, limit: int = 20, lang: str = "ru") -> list[dict[str, Any]]:
    """Search Fonbet line events by team, league, or sport name."""
    payload = await get_client().current_line(lang=lang)
    return [event.__dict__ for event in search_events(payload, query=query, limit=limit)]


@mcp.tool()
async def fonbet_event_odds(event_id: int, lang: str = "ru") -> dict[str, Any]:
    """Get event details and the first page of odds markets for a Fonbet event id."""
    payload = await get_client().event_view(event_id=event_id, lang=lang)
    return extract_odds(payload, event_id=event_id)


@mcp.tool()
async def get_event_odds(
    event_id: int,
    offset: int = 0,
    limit: int = 200,
    query: str = "",
    include_raw: bool = False,
    lang: str = "ru",
) -> dict[str, Any]:
    """Get paginated Fonbet markets for one event. Use next_offset to request the next page."""
    payload = await get_client().event_view(event_id=event_id, lang=lang)
    return extract_markets(
        payload,
        event_id=event_id,
        offset=offset,
        limit=limit,
        query=query or None,
        include_raw=include_raw,
    )


@mcp.tool()
async def get_match_markets(
    event_id: int,
    offset: int = 0,
    limit: int = 200,
    query: str = "",
    include_raw: bool = False,
    lang: str = "ru",
) -> dict[str, Any]:
    """Inspect a football match and linked sub-events (corners, cards, periods).

    Includes parentId, subevent name and paginated markets. Unknown factor IDs
    remain unlabeled; never infer a bet type from odds or parameter alone.
    """
    upstream = get_client()
    payload, line_payload = await asyncio.gather(
        upstream.event_view(event_id=event_id, lang=lang),
        upstream.current_line(lang=lang),
    )
    return extract_match_markets(
        payload,
        event_id=event_id,
        offset=offset,
        limit=limit,
        query=query or None,
        include_raw=include_raw,
        line_payload=line_payload,
    )


@mcp.tool()
async def get_match_line(query: str, lang: str = "ru") -> dict[str, Any]:
    """One-call football line: verified totals plus extended identified markets.

    market_sections preserves full-time goal/corner/card totals.
    expanded_market_sections includes known-ID outcomes, handicaps and totals
    from explicitly linked full-time and period football statistic events:
    fouls, shots, shots on target, offsides, throw-ins, goal kicks, woodwork.
    Unknown IDs remain excluded. Prices are Fonbet odds; 1/odds is a
    break-even threshold, not an independent probability prediction.
    Ambiguous names return candidates instead of an arbitrary match.
    """
    upstream = get_client()
    line = await upstream.current_line(lang=lang)
    search = find_match_candidates(line, query)
    if search["status"] != "found":
        return search
    event_id = search["event"]["event_id"]
    payload = await upstream.event_view(event_id=event_id, lang=lang)
    return summarize_match_line(event_id, payload, line)


@mcp.tool()
async def get_unlabeled_pairs(
    event_id: int,
    section: str = "",
    offset: int = 0,
    limit: int = 100,
    lang: str = "ru",
) -> dict[str, Any]:
    """Audit unlabeled adjacent-ID pairs for a match or a market section.

    Evidence only: no selection is identified as team 1/2 or over/under.
    Use to prioritize factor IDs for manual verification against the betslip.
    """
    upstream = get_client()
    payload, line_payload = await asyncio.gather(
        upstream.event_view(event_id=event_id, lang=lang),
        upstream.current_line(lang=lang),
    )
    return extract_unlabeled_pairs(
        payload, event_id=event_id, section=section,
        offset=offset, limit=limit, line_payload=line_payload,
    )


@mcp.tool()
async def get_special_bets(
    query: str = "",
    limit: int = 100,
    include_raw: bool = False,
    lang: str = "ru",
) -> dict[str, Any]:
    """Search the current Fonbet day line for special/player/stat-style markets."""
    payload = await get_client().current_line(lang=lang)
    return extract_day_specials(payload, query=query, limit=limit, include_raw=include_raw)


@mcp.tool()
async def get_same_game_parlay(
    event_id: int,
    query: str = "",
    limit: int = 200,
    lang: str = "ru",
) -> dict[str, Any]:
    """Return same-event markets that can be inspected for a same-game parlay idea."""
    payload = await get_client().event_view(event_id=event_id, lang=lang)
    return extract_markets(payload, event_id=event_id, offset=0, limit=limit, query=query or None, include_raw=False)


@mcp.tool()
async def get_cross_match_specials(
    query: str = "",
    limit: int = 100,
    lang: str = "ru",
) -> dict[str, Any]:
    """Return special-style markets across the current Fonbet line for cross-match analysis."""
    payload = await get_client().current_line(lang=lang)
    return extract_day_specials(payload, query=query, limit=limit, include_raw=False)


@mcp.tool()
async def fonbet_value_check(decimal_odds: float, estimated_probability: float) -> dict[str, Any]:
    """Check whether decimal odds have positive expected value for your estimated probability."""
    return value_check(decimal_odds=decimal_odds, estimated_probability=estimated_probability)


@mcp.tool()
async def fonbet_find_candidates(
    hours_ahead: Annotated[float, Field(gt=0, le=MAX_HOURS_AHEAD)] = 4,
    min_odds: float = 1.40,
    max_odds: float = 1.60,
    markets: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    lang: str = "ru",
    search_id: str | None = None,
    cursor: str | None = None,
    resume: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """Find identified prematch football selections within an odds range and start-time window.

    Market filters (OR): goals, corners, yellow_cards, totals, individual_totals,
    outcomes, handicaps, double_chance, both_teams_score, halves, statistics,
    fouls, shots_on_target, offsides, throw_ins, goal_kicks, shots, woodwork.
    hours_ahead must be greater than 0 and at most 12. Use next_cursor for a stable
    page or next_offset for legacy paging. Pass search_id and resume=true to check
    remaining matches. Use refresh=true with a search_id or cursor to verify only
    the displayed page. Check partial, quote_status and received_at before use.
    """
    return await search_candidates(
        get_client(), hours_ahead, min_odds, max_odds, markets, limit, offset, lang,
        search_id, cursor, resume, refresh,
    )


@mcp.tool()
async def football_team_form(
    team_id: int,
    last_n: int = 10,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Independent completed-match form from football-data.org (optional API token).

    team_id is a football-data.org ID, not a Fonbet ID. Never silently
    match teams across providers. Uses only provider-reported full-time scores
    strictly before as_of_utc. Returns samples and home/away splits, not xG,
    injury data, estimated probabilities, value claims, or bet instructions.
    """
    return await get_stats_client().team_form(team_id, last_n, as_of_utc)


def main() -> None:
    transport = cast(
        Literal["stdio", "sse", "streamable-http"],
        os.getenv("MCP_TRANSPORT", "sse"),
    )
    if os.getenv("MCP_AUTH_MODE", "none").strip().lower() == "oauth" and transport != "streamable-http":
        raise ValueError("OAuth mode requires MCP_TRANSPORT=streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
