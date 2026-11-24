"""Search identified prematch football selections across the Fonbet line."""
from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from sys import float_info
from typing import Any

import httpx

from fonbet_mcp.client import (
    FonbetClient,
    FonbetCooldownError,
    FonbetRequestCapacityError,
    FonbetSearchCapacityError,
    RequestMetrics,
    Snapshot,
    request_metrics,
)
from fonbet_mcp.fonbet import (
    _KNOWN_FACTOR_NAMES,
    _VERIFIED_TEAM_CORNER_FACTOR_IDS,
    _VERIFIED_TEAM_GOAL_FACTOR_IDS,
    _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS,
    _factor_blocks_by_event,
    _sport_name,
)
from fonbet_mcp.search_state import Quote, SearchKey, SearchRevision, SearchState
from fonbet_mcp.summary import _EXTRA_SECTION_NAMES, _ZERO_HANDICAP_TEAMS, summarize_match_line

MAX_HOURS_AHEAD = 12
_RECOVERABLE = (httpx.HTTPError, FonbetCooldownError, FonbetRequestCapacityError, ValueError)
_FactorKey = tuple[int, int, Decimal | None]
_SelectionKey = tuple[str, str, str, str, Decimal | None, int | None]

_PREMATCH = {"line", "prematch", "pre_match"}
_UNAVAILABLE = {"live", "inplay", "in_play", "started", "finished", "ended",
                "closed", "suspended", "blocked", "cancelled", "canceled", "deleted"}
_FLAGS = ("b", "blocked", "isBlocked", "live", "isLive", "stale", "expired", "cancelled")
_SIDES = {
    921: "team1", 922: "draw", 923: "team2", 924: "team1_or_draw",
    925: "draw_or_team2", 1571: "team1_or_team2", 927: "team1", 928: "team2",
    2820: "team1", 2821: "team2", 709: "yes", 710: "no",
}
_SUPPORTED_FACTORS = (
    _KNOWN_FACTOR_NAMES.keys()
    | _VERIFIED_TEAM_CORNER_FACTOR_IDS.keys()
    | _VERIFIED_TEAM_GOAL_FACTOR_IDS.keys()
    | _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS.keys()
)
_DOUBLE_CHANCES = {924, 925, 1571}
_STATISTICS = {"goals", *_EXTRA_SECTION_NAMES.values()}
_MARKETS = _STATISTICS | {
    "totals", "individual_totals", "outcomes", "handicaps",
    "double_chance", "both_teams_score", "halves", "statistics",
}


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    return number if number.is_finite() and math.isfinite(float(number)) else None


def _unavailable(item: dict[str, Any]) -> bool:
    if any(item.get(flag) for flag in _FLAGS):
        return True
    for field in ("state", "status"):
        value = item.get(field)
        if isinstance(value, str) and value.casefold() in _UNAVAILABLE:
            return True
    place = item.get("place")
    return isinstance(place, str) and place.casefold() not in _PREMATCH


def _prematch(event: dict[str, Any]) -> bool:
    stage = event.get("place") or event.get("state")
    return isinstance(stage, str) and stage.casefold() in _PREMATCH and not _unavailable(event)


def _snapshot_index(payload: dict[str, Any], key: str) -> dict[int, dict[str, Any]]:
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"Missing Fonbet {key} list")
    indexed: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
            raise ValueError(f"Invalid Fonbet {key} identifier")
        for field_name in ("parentId", "sportId", "startTime"):
            value = row.get(field_name)
            if value is not None and type(value) is not int:
                raise ValueError(f"Invalid Fonbet {key} {field_name}")
        for field_name in ("name", "team1", "team2", "place"):
            value = row.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Invalid Fonbet {key} {field_name}")
        previous = indexed.setdefault(row["id"], row)
        if previous != row:
            raise ValueError(f"Conflicting Fonbet {key} for the same ID")
    return indexed


def _line_indexes(payload: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    if payload.get("fromVersion", 0) != 0:
        raise ValueError("Expected a full Fonbet line snapshot")
    events = _snapshot_index(payload, "events")
    sports = _snapshot_index(payload, "sports") if events or "sports" in payload else {}
    if events and not sports:
        raise ValueError("Fonbet line has events without sports")
    return events, sports


def _children(events: dict[int, dict[str, Any]]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = defaultdict(list)
    for event_id, event in events.items():
        parent = event.get("parentId")
        if type(parent) is int and parent > 0:
            result[parent].append(event_id)
    return result


def _match_tree(children: dict[int, list[int]], event_id: int) -> set[int]:
    direct = children.get(event_id, [])
    return {event_id, *direct, *(child for pid in direct for child in children.get(pid, []))}


def _feed_blocks(payload: dict[str, Any]) -> dict[int, set[int] | None]:
    """None blocks an entire event. A set blocks only the listed factor IDs."""
    blocked: dict[int, set[int] | None] = {}
    event_blocks = payload.get("eventBlocks", [])
    live_events = payload.get("liveEventInfos", [])
    if not isinstance(event_blocks, list) or not isinstance(live_events, list):
        raise ValueError("Invalid Fonbet availability data")
    for item in event_blocks:
        if not isinstance(item, dict) or type(item.get("eventId")) is not int:
            raise ValueError("Invalid Fonbet event block")
        event_id = item["eventId"]
        if item.get("state") == "partial":
            blocked_ids = item.get("factors")
            if not isinstance(blocked_ids, list) or any(type(value) is not int for value in blocked_ids):
                raise ValueError("Invalid Fonbet partial event block")
            factors = blocked.setdefault(event_id, set())
            if factors is not None:
                factors.update(blocked_ids)
        else:
            blocked[event_id] = None
    for item in live_events:
        if not isinstance(item, dict) or type(item.get("eventId")) is not int:
            raise ValueError("Invalid Fonbet live event metadata")
        blocked[item["eventId"]] = None
    return blocked


@dataclass
class MarketRows:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    processed: int = 0
    invalid: int = 0
    incomplete: bool = False
    price_conflicts: set[_FactorKey] = field(default_factory=set)


def _market_rows(
    payload: dict[str, Any], eligible: set[int],
    line_blocks: dict[int, set[int] | None], detail_blocks: dict[int, set[int] | None],
) -> MarketRows:
    rows = MarketRows()
    selections: dict[_FactorKey, dict[str, dict[str, Any]]] = {}
    unavailable: set[_FactorKey] = set()
    for block in payload["customFactors"]:
        if not isinstance(block, dict) or type(block.get("e")) is not int or block["e"] <= 0:
            rows.incomplete = True
            continue
        event_id = block["e"]
        if event_id not in eligible:
            continue
        factors = block.get("factors")
        if not isinstance(factors, list):
            rows.incomplete = True
            continue
        suppressed = (line_blocks.get(event_id) or set()) | (detail_blocks.get(event_id) or set())
        declared_count = block.get("countAll", len(factors))
        if type(declared_count) is not int or declared_count < 0 or declared_count > len(factors):
            rows.incomplete = True

        for factor in factors:
            rows.processed += 1
            if not isinstance(factor, dict) or type(factor.get("f")) is not int or factor["f"] <= 0:
                rows.invalid += 1
                continue
            factor_id = factor["f"]
            if factor_id not in _SUPPORTED_FACTORS:
                continue
            raw_threshold = factor.get("pt")
            threshold = _number(raw_threshold)
            if raw_threshold not in (None, "") and threshold is None:
                rows.invalid += 1
                continue
            if factor_id in _ZERO_HANDICAP_TEAMS:
                if threshold not in (None, 0):
                    rows.invalid += 1
                    continue
                threshold = Decimal(0)
                factor = {**factor, "pt": "0"}
            key = (event_id, factor_id, threshold)
            if factor_id in suppressed or _unavailable(block) or _unavailable(factor):
                unavailable.add(key)
                continue
            odds = factor.get("v")
            if type(odds) not in (float, int) or not 1 < odds <= float_info.max:
                rows.invalid += 1
                unavailable.add(key)
                continue
            variants = selections.setdefault(key, {})
            previous = next(iter(variants.values()), None)
            if previous is not None and previous["v"] != odds:
                rows.price_conflicts.add(key)
            # Calibrated mappings may require an exact threshold spelling.
            variants.setdefault(str(factor.get("pt")), factor)

    by_event = defaultdict(list)
    for key, variants in selections.items():
        if key not in unavailable:
            by_event[key[0]].extend(variants.values())
    rows.price_conflicts.difference_update(unavailable)
    rows.blocks = [{"e": event_id, "factors": factors} for event_id, factors in by_event.items()]
    return rows


def _match_selections(
    event_id: int, line_events: dict[int, dict[str, Any]], line_sports: dict[int, dict[str, Any]],
    line_blocks: dict[int, set[int] | None], detail: dict[str, Any], window_end: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], MarketRows] | None:
    if not isinstance(detail.get("customFactors"), list) or detail.get("fromVersion", 0) != 0:
        raise ValueError("Expected a full Fonbet event snapshot with factors")
    detail_sports = _snapshot_index(detail, "sports") if "sports" in detail else {}
    detail_events = _snapshot_index(detail, "events")
    if event_id not in detail_events:
        raise ValueError("Match missing from Fonbet details")
    events = {
        eid: {**line_events.get(eid, {}), **detail_events.get(eid, {})}
        for eid in line_events.keys() | detail_events.keys()
    }
    detail_blocks = _feed_blocks(detail)
    full_blocks = {eid for block_index in (line_blocks, detail_blocks) for eid, ids in block_index.items()
                   if ids is None}
    root = events[event_id]
    start = root.get("startTime")
    if (event_id in full_blocks or root.get("parentId") or not _prematch(root)
            or type(start) is not int or not datetime.now(timezone.utc).timestamp() < start <= window_end):
        return None
    if not all(root.get(team) for team in ("team1", "team2")):
        raise ValueError("Missing team names in Fonbet details")
    sports = {**line_sports, **detail_sports}
    if _sport_name(sports, root.get("sportId"), prefer_root=True) != "Футбол":
        return None

    linked = _match_tree(_children(events), event_id)
    eligible = {
        eid for eid in linked
        if eid not in full_blocks and events[eid].get("parentId") not in full_blocks
        and not _unavailable(events[eid])
        and not _unavailable(events.get(events[eid].get("parentId"), {}))
    }
    rows = _market_rows(detail, eligible, line_blocks, detail_blocks)
    if rows.incomplete and not rows.processed:
        raise ValueError("No usable factor rows in incomplete Fonbet details")
    # The line supplies hierarchy only. Every price comes from this detail snapshot.
    metadata = {"events": [events[eid] for eid in sorted(linked)], "sports": list(sports.values())}
    data = {**detail, "events": [], "customFactors": rows.blocks}
    summary = summarize_match_line(event_id, data, metadata, include_zero_handicaps=True)
    return root, _flatten(summary), rows


def _flatten(summary: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for statistic, section in summary["market_sections"].items():
        for kind, selections in section.items():
            for item in selections:
                results.append({
                    **item,
                    "statistic": statistic, "period": "full_time",
                    "category": "individual_totals" if kind == "individual_totals" else "totals",
                    "side": item["direction"],
                    "identification_basis": item.get("mapping_source", "known_factor_id_and_explicit_event_path"),
                })
    categories = {
        "total": "totals", "outcome": "outcomes", "handicap": "handicaps",
        "both_teams_score": "both_teams_score",
    }
    for statistic, periods in summary["expanded_market_sections"].items():
        for period, section in periods.items():
            for kind, selections in section.items():
                for item in selections:
                    factor_id = item["factor_id"]
                    category = "double_chance" if factor_id in _DOUBLE_CHANCES else categories[kind]
                    side = _SIDES.get(factor_id)
                    if kind == "total":
                        side = "over" if item["label"].startswith("Тотал больше") else "under"
                    results.append({
                        **item, "statistic": statistic, "period": period,
                        "category": category, "side": side,
                    })
    return results


def _market_filter(item: dict[str, Any], requested: set[str] | frozenset[str]) -> bool:
    return (
        not requested
        or item["category"] in requested
        or item["statistic"] in requested
        or ("halves" in requested and item["period"] != "full_time")
        or ("statistics" in requested and item["statistic"] not in {"goals", "corners", "yellow_cards"})
    )


def _output_selections(
    items: list[dict[str, Any]], event: dict[str, Any], league: str | None,
    localized: dict[str, Any], received_at: datetime, requested: set[str] | frozenset[str],
    min_odds: float, max_odds: float, price_conflicts: set[_FactorKey],
) -> tuple[list[dict[str, Any]], int]:
    results = []
    groups: dict[_SelectionKey, dict[str, Any]] = {}
    conflicting: set[_SelectionKey] = set()
    start = datetime.fromtimestamp(event["startTime"], tz=timezone.utc).isoformat()
    teams = {
        number: localized.get(f"team{number}") or event.get(f"team{number}")
        for number in (1, 2)
    }
    for item in sorted(items, key=lambda market: (market["event_id"], market["factor_id"])):
        threshold = _number(item.get("threshold"))
        team_number = item.get("team_number")
        if item["category"] in {"handicaps", "outcomes"} and item["side"] in {"team1", "team2"}:
            team_number = int(item["side"][-1])
        key = (item["statistic"], item["period"], item["category"], item["side"], threshold, team_number)
        previous = groups.setdefault(key, item)
        raw_key = (item["event_id"], item["factor_id"], threshold)
        if previous["odds"] != item["odds"] or raw_key in price_conflicts:
            conflicting.add(key)

    for key, item in groups.items():
        if key in conflicting or not min_odds <= item["odds"] <= max_odds or not _market_filter(item, requested):
            continue
        threshold, team_number = key[-2:]
        results.append({
            "event_id": event["id"],
            "market_event_id": item["event_id"],
            "factor_id": item["factor_id"],
            "team1": teams[1],
            "team2": teams[2],
            "league": league,
            "start_time": start,
            "category": item["category"],
            "statistic": item["statistic"],
            "period": item["period"],
            "side": item["side"],
            "team_number": team_number,
            "team": teams.get(team_number),
            "threshold": item.get("threshold"),
            "threshold_is_integer": None if threshold is None else threshold == threshold.to_integral_value(),
            "odds": item["odds"],
            "received_at": received_at.isoformat(),
            "label": item["label"],
            "identification_basis": item["identification_basis"],
        })
    return results, len(conflicting)


def _issue(stage: str, code: str, event_id: int | None = None) -> dict[str, Any]:
    return {"stage": stage, "event_id": event_id, "code": code, "http_status": None}


def _error(stage: str, exc: Exception, event_id: int | None = None) -> dict[str, Any]:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    code = {
        "HTTPStatusError": "http_error",
        "FonbetCooldownError": "retry_after_cooldown",
        "FonbetRequestCapacityError": "request_capacity",
        "FonbetSearchCapacityError": "search_capacity",
    }.get(type(exc).__name__, "invalid_response")
    if isinstance(exc, httpx.TimeoutException):
        code = "timeout"
    elif isinstance(exc, httpx.RequestError):
        code = "network_error"
    return {"stage": stage, "event_id": event_id, "code": code, "http_status": status}


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _coverage(
    state: SearchState, stage_errors: list[dict[str, Any]], *, initial_error: bool = False,
) -> dict[str, Any]:
    errors = sorted([*state.errors, *stage_errors], key=lambda item: (item["stage"], item["event_id"] or 0))
    partial = bool(errors or state.remaining)
    return {
        "line_received_at": state.line_received_at,
        "latest_line_received_at": state.latest_line_received_at,
        "root_events": state.root_events, "eligible_matches": state.eligible,
        "matches_checked": len(state.checked), "matches_completed": state.completed,
        "remaining_matches": state.remaining, "matches_no_longer_eligible": state.no_longer_eligible,
        "markets_processed": state.processed,
        "invalid_market_rows": state.invalid_rows, "conflicting_markets": state.conflicts,
        "skipped_events": state.root_events - state.completed,
        "errors": errors, "error_count": len(errors), "partial": partial,
        "status": "error" if initial_error else "partial" if partial else "ok",
        "search_status": "error" if initial_error else "paused" if state.remaining else "partial" if partial else "complete",
    }


def _expired(client: FonbetClient, search_id: str | None, code: str, limit: int, offset: int) -> dict[str, Any]:
    return {
        "search_id": search_id, "search_status": "expired" if code.endswith("expired") else "error",
        "status": "error", "partial": True,
        "errors": [_issue("search", code)], "error_count": 1, "mode": "prematch",
        "max_snapshot_age_seconds": client.policy.max_snapshot_age, "line_received_at": None,
        "root_events": 0, "eligible_matches": 0, "matches_checked": 0, "matches_completed": 0,
        "remaining_matches": 0, "matches_no_longer_eligible": 0,
        "markets_processed": 0, "invalid_market_rows": 0,
        "conflicting_markets": 0, "skipped_events": 0,
        "candidates": [], "total_candidates": 0, "limit": limit, "offset": offset,
        "next_offset": None, "next_cursor": None, "diagnostics": asdict(RequestMetrics()),
    }


def _search_key(
    hours_ahead: float, min_odds: float, max_odds: float, markets: list[str] | None, lang: str,
) -> SearchKey:
    if not 0 < hours_ahead <= MAX_HOURS_AHEAD:
        raise ValueError(f"hours_ahead must be greater than 0 and at most {MAX_HOURS_AHEAD}")
    if not 1 < min_odds <= max_odds < float("inf"):
        raise ValueError("Expected 1 < min_odds <= max_odds with finite odds")
    requested = frozenset(market.strip().lower() for market in markets or [])
    if unknown := requested - _MARKETS:
        raise ValueError("Unsupported markets: " + ", ".join(sorted(unknown)))
    lang = lang.strip().lower()
    if not lang:
        raise ValueError("lang must not be empty")
    return hours_ahead, min_odds, max_odds, requested, lang


def _parse_cursor(cursor: str) -> tuple[str, int, int]:
    if len(cursor) > 80:
        raise ValueError("Invalid cursor")
    parts = cursor.split(".")
    if len(parts) != 3 or len(parts[0]) != 32 or not all(part.isascii() and part.isdecimal() for part in parts[1:]):
        raise ValueError("Invalid cursor")
    return parts[0], int(parts[1]), int(parts[2])


def _observe_scan(task: asyncio.Task[int]) -> None:
    if not task.cancelled():
        task.exception()


async def search_candidates(
    client: FonbetClient, hours_ahead: float, min_odds: float, max_odds: float,
    markets: list[str] | None, limit: int, offset: int, lang: str,
    search_id: str | None = None, cursor: str | None = None, resume: bool = False, refresh: bool = False,
) -> dict[str, Any]:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("Expected 1 <= limit <= 500 and offset >= 0")
    revision = None
    if cursor is not None:
        cursor_id, revision, cursor_offset = _parse_cursor(cursor)
        if resume or offset or (search_id is not None and search_id != cursor_id):
            raise ValueError("Cursor cannot be combined with resume, an offset or a different search_id")
        search_id, offset = cursor_id, cursor_offset
    if search_id is None and (resume or refresh):
        raise ValueError("Pass a search_id or cursor to continue or refresh a page")

    store = client.searches
    is_new = False
    if search_id is not None:
        state = store.get(search_id)
        if state is None:
            return _expired(client, search_id, "search_expired", limit, offset)
    else:
        key = _search_key(hours_ahead, min_odds, max_odds, markets, lang)
        state = store.find(key)
        if state is None:
            if not client.search_capacity_available():
                return _expired(client, None, "search_capacity", limit, offset)
            state = store.create(key, datetime.now(timezone.utc).timestamp())
            if state is None:
                return _expired(client, None, "search_capacity", limit, offset)
            is_new = True

    own_scan = is_new or (resume and not state.running and (state.matches is None or state.remaining > 0))
    joined = state.running and (resume or revision is None and (search_id is None or not state.revisions))
    if own_scan:
        if not client.search_capacity_available():
            return _expired(client, state.id, "search_capacity", limit, offset)
        state.task = asyncio.create_task(_run_scan(client, state))
        state.task.add_done_callback(_observe_scan)
    if (own_scan or joined) and state.task is not None:
        revision = await asyncio.shield(state.task)
    revision = state.revision if revision is None else revision
    view = state.revisions.get(revision)
    if view is None:
        return _expired(client, state.id, "revision_expired", limit, offset)
    return await _page(client, state, view, revision, limit, offset, own_scan, joined, refresh)


@dataclass
class LineContext:
    snapshot: Snapshot
    events: dict[int, dict[str, Any]]
    sports: dict[int, dict[str, Any]]
    blocks: dict[int, set[int] | None]
    children: dict[int, list[int]]

    def match_events(self, event_id: int) -> dict[int, dict[str, Any]]:
        return {eid: self.events[eid] for eid in _match_tree(self.children, event_id)}


async def _line_context(client: FonbetClient, metrics: RequestMetrics) -> LineContext:
    line = await client.current_line_snapshot("ru")
    tick = asyncio.get_running_loop().time()
    try:
        events, sports = _line_indexes(line.payload)
        return LineContext(line, events, sports, _feed_blocks(line.payload), _children(events))
    finally:
        metrics.processing_seconds += asyncio.get_running_loop().time() - tick


def _eligible(line: LineContext, event_id: int, state: SearchState) -> bool:
    event = line.events.get(event_id)
    if event is None or event.get("parentId") or (event_id in line.blocks and line.blocks[event_id] is None):
        return False
    starts_at = event.get("startTime")
    return (
        type(starts_at) is int
        and max(state.start, datetime.now(timezone.utc).timestamp()) < starts_at <= state.end
        and _prematch(event)
        and _sport_name(line.sports, event.get("sportId"), prefer_root=True) == "Футбол"
        and all(isinstance(event.get(team), str) and event[team].strip() for team in ("team1", "team2"))
    )


def _prioritize(line: LineContext, state: SearchState, matches: list[int]) -> tuple[int, ...]:
    payload = line.snapshot.payload
    factors = _factor_blocks_by_event(payload) if isinstance(payload.get("customFactors"), list) else {}
    minimum, maximum = state.key[1:3]

    def priority(event_id: int) -> tuple[bool, int, int]:
        hint = any(
            isinstance(factor, dict)
            and type(factor.get("f")) is int and factor["f"] in _SUPPORTED_FACTORS
            and type(factor.get("v")) in (float, int) and minimum <= factor["v"] <= maximum
            and not _unavailable(factor)
            for eid in _match_tree(line.children, event_id) for factor in factors.get(eid, ())
        )
        return not hint, line.events[event_id]["startTime"], event_id

    return tuple(sorted(matches, key=priority))


async def _run_scan(client: FonbetClient, state: SearchState) -> int:
    metrics = RequestMetrics()
    token = request_metrics.set(metrics)
    state.reusable_until = 0
    try:
        with client.candidate_search_slot():
            async with asyncio.timeout_at(state.expires_at):
                stage_errors, reusable_until = await _scan(client, state, metrics)
    except TimeoutError:
        stage_errors, reusable_until = [_issue("scan", "scan_deadline")], 0
    except FonbetSearchCapacityError as exc:
        stage_errors, reusable_until = [_error("scan", exc)], 0
    finally:
        request_metrics.reset(token)

    loop = asyncio.get_running_loop()
    processing = loop.time()
    coverage = _coverage(state, stage_errors, initial_error=state.matches is None)
    if not coverage["partial"]:
        state.reusable_until = reusable_until
    state.publish(coverage)
    metrics.processing_seconds += loop.time() - processing
    coverage["scan_diagnostics"] = asdict(metrics)
    return state.revision


async def _scan(
    client: FonbetClient, state: SearchState, metrics: RequestMetrics,
) -> tuple[list[dict[str, Any]], float]:
    loop = asyncio.get_running_loop()
    try:
        line = await _line_context(client, metrics)
    except _RECOVERABLE as exc:
        return [_error("line", exc)], 0
    if line.snapshot.age() >= client.policy.max_snapshot_age:
        return [_issue("line", "stale_snapshot")], 0
    current_receipt = line.snapshot.received_at.isoformat()
    state.latest_line_received_at = current_receipt
    if state.matches is None:
        processing = loop.time()
        state.line_received_at = current_receipt
        roots = [eid for eid, event in line.events.items() if not event.get("parentId")]
        state.root_events = len(roots)
        eligible = [eid for eid in roots if _eligible(line, eid, state)]
        state.eligible = len(eligible)
        if len(eligible) > client.policy.search_max_matches:
            return [_issue("scan", "match_capacity")], 0
        state.matches = _prioritize(line, state, eligible)
        metrics.processing_seconds += loop.time() - processing

    errors: list[dict[str, Any]] = []
    queued = iter(eid for eid in state.matches if eid not in state.finished)
    workers = min(state.remaining, client.policy.max_concurrency)
    local_events: dict[int, dict[str, Any]] = {}
    local_sports: dict[int, dict[str, Any]] = {}
    valid_until = line.snapshot.effective_tick + min(client.policy.line_ttl, client.policy.max_snapshot_age)

    async def localize() -> None:
        nonlocal local_events, local_sports, valid_until
        _, _, _, _, lang = state.key
        if lang == "ru" or not workers:
            return
        try:
            snapshot = await client.current_line_snapshot(lang)
            if snapshot.age() >= client.policy.max_snapshot_age:
                errors.append(_issue("localization", "stale_snapshot"))
            else:
                local_events, local_sports = _line_indexes(snapshot.payload)
                valid_until = min(valid_until, snapshot.effective_tick + client.policy.line_ttl)
        except _RECOVERABLE as exc:
            errors.append(_error("localization", exc))

    async def check_matches() -> None:
        nonlocal valid_until
        for event_id in queued:
            # Resumed searches keep the original universe and recheck current availability.
            if not _eligible(line, event_id, state):
                state.no_longer_eligible += 1
                state.finished.add(event_id)
                continue
            was_checked = event_id in state.checked
            state.checked.add(event_id)
            try:
                detail = await client.event_view_snapshot(event_id, "ru")
            except FonbetRequestCapacityError as exc:
                if not was_checked:
                    state.checked.discard(event_id)
                errors.append(_error("event", exc, event_id))
                return
            except _RECOVERABLE as exc:
                state.errors.append(_error("event", exc, event_id))
                state.finished.add(event_id)
                continue
            processing = loop.time()
            try:
                if detail.age() >= client.policy.max_snapshot_age:
                    state.errors.append(_issue("event", "stale_snapshot", event_id))
                    state.finished.add(event_id)
                    continue
                scanned = _match_selections(
                    event_id, line.match_events(event_id), line.sports, line.blocks, detail.payload, state.end,
                )
                if scanned is None:
                    state.no_longer_eligible += 1
                else:
                    _record(client, state, line, event_id, detail, scanned, local_events, local_sports)
                    valid_until = min(valid_until, detail.effective_tick + client.policy.event_ttl)
                state.finished.add(event_id)
            except _RECOVERABLE as exc:
                state.errors.append(_error("event", exc, event_id))
                state.finished.add(event_id)
            finally:
                metrics.processing_seconds += loop.time() - processing

    try:
        remaining = client.policy.max_snapshot_age - line.snapshot.age()
        async with asyncio.timeout(max(0, remaining)):
            await localize()
            await asyncio.gather(*(check_matches() for _ in range(workers)))
    except TimeoutError:
        errors.append(_issue("scan", "scan_deadline"))

    if state.rows:
        valid_until = min(valid_until, _reuse_until(client, state.rows))
    return errors, valid_until


def _record(
    client: FonbetClient, state: SearchState, line: LineContext, event_id: int, detail: Snapshot,
    scanned: tuple[dict[str, Any], list[dict[str, Any]], MarketRows],
    local_events: dict[int, dict[str, Any]], local_sports: dict[int, dict[str, Any]],
) -> None:
    event, selections, rows = scanned
    state.completed += 1
    state.processed += rows.processed
    state.invalid_rows += rows.invalid
    if rows.incomplete:
        state.errors.append(_issue("event", "incomplete_detail", event_id))
    if rows.invalid:
        state.errors.append(_issue("event", "invalid_market_rows", event_id))
    localized = local_events.get(event_id, {})
    league = _sport_name(local_sports, localized.get("sportId"), prefer_root=False)
    league = league or _sport_name(line.sports, event.get("sportId"), prefer_root=False)
    _, minimum, maximum, markets, _ = state.key
    matched, conflicts = _output_selections(
        selections, event, league, localized, detail.received_at, markets, minimum, maximum, rows.price_conflicts,
    )
    state.conflicts += conflicts
    if conflicts:
        state.errors.append(_issue("event", "conflicting_quotes", event_id))
    room = client.policy.search_max_candidates - len(state.rows)
    if len(matched) > room and not any(item["code"] == "result_capacity" for item in state.errors):
        state.errors.append(_issue("scan", "result_capacity"))
    state.rows.extend(Quote(
        row, event["startTime"], detail.received_tick, detail.effective_tick,
        line.snapshot.received_tick, line.snapshot.effective_tick, state.revision + 1,
    ) for row in matched[:room])


def _reuse_until(client: FonbetClient, quotes: list[Quote]) -> float:
    policy = client.policy
    available_until = min(
        min(quote.detail_effective_tick + min(policy.event_ttl, policy.max_snapshot_age),
            quote.line_effective_tick + min(policy.line_ttl, policy.max_snapshot_age))
        for quote in quotes
    )
    seconds_to_start = min(quote.match_start for quote in quotes) - datetime.now(timezone.utc).timestamp()
    return min(available_until, asyncio.get_running_loop().time() + seconds_to_start)


def _quote_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["market_event_id"], row["factor_id"], _number(row["threshold"]),
        row["category"], row["statistic"], row["period"], row["side"], row["team_number"],
    )


def _superseded(
    client: FonbetClient, state: SearchState, event_id: int, line_tick: float, detail_tick: float,
) -> bool:
    return client.searches.line_tick > line_tick or state.detail_ticks.get(event_id, 0) > detail_tick


def _quote_status(client: FonbetClient, state: SearchState, quote: Quote, scan_revision: int | None) -> str:
    if quote.match_start <= datetime.now(timezone.utc).timestamp():
        return "started"
    tick = asyncio.get_running_loop().time()
    detail_age = tick - quote.detail_effective_tick
    line_age = tick - quote.line_effective_tick
    policy = client.policy
    if max(detail_age, line_age) >= policy.max_snapshot_age:
        return "stale"
    if _superseded(client, state, quote.row["event_id"], quote.line_tick, quote.detail_tick):
        return "superseded"
    if quote.revision != scan_revision and (detail_age >= policy.event_ttl or line_age >= policy.line_ttl):
        return "stale"
    return "snapshot"


@dataclass(frozen=True)
class Verification:
    status: str
    received_at: str | None = None
    detail_tick: float = 0
    detail_effective_tick: float = 0
    line_tick: float = 0
    line_effective_tick: float = 0


async def _refresh_page(
    client: FonbetClient, state: SearchState, quotes: list[Quote], metrics: RequestMetrics,
) -> tuple[dict[tuple[int, tuple[Any, ...]], Verification], list[dict[str, Any]]]:
    if not quotes:
        return {}, []
    policy = client.policy
    by_event: dict[int, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.row["event_id"]].append(quote)
    found: dict[tuple[int, tuple[Any, ...]], Verification] = {}
    errors: list[dict[str, Any]] = []
    queued = iter(by_event)

    async def check(line: LineContext) -> None:
        loop = asyncio.get_running_loop()
        for event_id in queued:
            current: dict[tuple[Any, ...], dict[str, Any]] = {}
            detail = None
            try:
                if _eligible(line, event_id, state):
                    detail = await client.event_view_snapshot(event_id, "ru")
                    processing = loop.time()
                    try:
                        if detail.age() >= min(policy.event_ttl or policy.max_snapshot_age, policy.max_snapshot_age):
                            errors.append(_issue("event", "stale_snapshot", event_id))
                            continue
                        scanned = _match_selections(
                            event_id, line.match_events(event_id), line.sports, line.blocks, detail.payload, state.end,
                        )
                        if scanned is not None:
                            event, items, rows = scanned
                            selections, _ = _output_selections(
                                items, event, None, {}, detail.received_at, set(), 1, float("inf"), rows.price_conflicts,
                            )
                            current = {_quote_identity(row): row for row in selections}
                    finally:
                        metrics.processing_seconds += loop.time() - processing
            except _RECOVERABLE as exc:
                errors.append(_error("event", exc, event_id))
                continue
            for quote in by_event[event_id]:
                identity = _quote_identity(quote.row)
                fresh = current.get(identity)
                if fresh is None:
                    validation = Verification("unavailable")
                elif fresh["odds"] != quote.row["odds"] or fresh["start_time"] != quote.row["start_time"]:
                    validation = Verification("changed")
                else:
                    assert detail is not None
                    validation = Verification(
                        "verified", detail.received_at.isoformat(), detail.received_tick, detail.effective_tick,
                        line.snapshot.received_tick, line.snapshot.effective_tick,
                    )
                found[event_id, identity] = validation

    try:
        with client.candidate_search_slot():
            line = await _line_context(client, metrics)
            freshness = min(policy.line_ttl or policy.max_snapshot_age, policy.max_snapshot_age)
            async with asyncio.timeout(max(0, freshness - line.snapshot.age())):
                await asyncio.gather(*(check(line) for _ in range(min(policy.max_concurrency, len(by_event)))))
    except TimeoutError:
        errors.append(_issue("page", "refresh_deadline"))
    except (FonbetSearchCapacityError, *_RECOVERABLE) as exc:
        errors.append(_error("page", exc))

    return found, errors


async def _page(
    client: FonbetClient, state: SearchState, view: SearchRevision, revision: int,
    limit: int, offset: int, own_scan: bool, joined: bool, refresh: bool,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    start = loop.time()
    page = view.rows[offset:offset + limit]
    scan_revision = revision if own_scan or joined else None
    metrics = RequestMetrics()
    token = request_metrics.set(metrics)
    try:
        if refresh:
            verified, page_errors = await _refresh_page(
                client, state, [quote for quote in page
                                if quote.match_start > datetime.now(timezone.utc).timestamp()], metrics,
            )
        else:
            verified, page_errors = {}, []
    finally:
        request_metrics.reset(token)
    after_refresh = loop.time()
    candidates = []
    ages = []
    for quote in page:
        result = quote.row.copy()
        age = max(0, after_refresh - quote.detail_effective_tick)
        ages.append(age)
        # Status and freshness can change while other visible quotes are being refreshed.
        status = _quote_status(client, state, quote, scan_revision)
        if refresh and status != "started":
            validation = verified.get((quote.row["event_id"], _quote_identity(quote.row)))
            status = validation.status if validation else "unverified"
            if validation and validation.status == "verified":
                maximum_age = client.policy.max_snapshot_age
                verified_age = after_refresh - validation.detail_effective_tick
                line_age = after_refresh - validation.line_effective_tick
                if verified_age >= min(client.policy.event_ttl or maximum_age, maximum_age) or (
                    line_age >= min(client.policy.line_ttl or maximum_age, maximum_age)
                ):
                    status = "stale"
                elif _superseded(client, state, quote.row["event_id"], validation.line_tick, validation.detail_tick):
                    status = "superseded"
                result.update(verified_at=validation.received_at, verified_age_seconds=round(verified_age, 3))
        if status not in {"snapshot", "verified"}:
            result["odds"] = None
        result.update(quote_status=status, quote_age_seconds=round(age, 3))
        candidates.append(result)
    counts = Counter(row["quote_status"] for row in candidates)
    for status in counts.keys() - {"snapshot", "verified"}:
        page_errors.append(_issue("page", f"quote_{status}"))
    page_errors.sort(key=lambda item: (item["stage"], item["code"], item["event_id"] or 0))
    errors = [*view.coverage["errors"], *page_errors]
    partial = bool(view.coverage["partial"] or page_errors)
    next_offset = offset + limit if offset + limit < len(view.rows) else None
    metrics.processing_seconds += (loop.time() - after_refresh) + (after_refresh - start if not refresh else 0)
    diagnostics = asdict(metrics)
    scan_diagnostics = view.coverage["scan_diagnostics"]
    if own_scan:
        diagnostics = {key: value + scan_diagnostics[key] for key, value in diagnostics.items()}
    diagnostics.update(
        result_cache_hits=int(not own_scan and not joined),
        result_cache_misses=int(own_scan),
        search_coalesced=int(joined),
        quote_age_seconds={"min": round(min(ages), 3), "max": round(max(ages), 3)} if ages else None,
    )
    hours, minimum, maximum, markets, language = state.key
    return {
        **view.coverage,
        "status": "error" if view.coverage["status"] == "error" else "partial" if partial else "ok",
        "partial": partial, "errors": errors, "error_count": len(errors),
        "search_id": state.id, "revision": revision, "mode": "prematch",
        "window_start": _timestamp(state.start), "window_end": _timestamp(state.end),
        "filters": {
            "hours_ahead": hours, "min_odds": minimum, "max_odds": maximum,
            "markets": sorted(markets), "lang": language,
        },
        "limit": limit, "offset": offset, "next_offset": next_offset,
        "next_cursor": f"{state.id}.{revision}.{next_offset}" if next_offset is not None else None,
        "max_snapshot_age_seconds": client.policy.max_snapshot_age,
        "total_candidates": len(view.rows), "candidates": candidates,
        "quote_status_counts": dict(counts), "diagnostics": diagnostics,
    }
