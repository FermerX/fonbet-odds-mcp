"""Search identified prematch football selections across the Fonbet line."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass, field
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
    Snapshot,
)
from fonbet_mcp.fonbet import (
    _KNOWN_FACTOR_NAMES,
    _VERIFIED_TEAM_CORNER_FACTOR_IDS,
    _VERIFIED_TEAM_GOAL_FACTOR_IDS,
    _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS,
    _sport_name,
)
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


def _market_filter(item: dict[str, Any], requested: set[str]) -> bool:
    return (
        not requested
        or item["category"] in requested
        or item["statistic"] in requested
        or ("halves" in requested and item["period"] != "full_time")
        or ("statistics" in requested and item["statistic"] not in {"goals", "corners", "yellow_cards"})
    )


def _output_selections(
    items: list[dict[str, Any]], event: dict[str, Any], league: str | None,
    localized: dict[str, Any], received_at: datetime, requested: set[str],
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


async def search_candidates(
    client: FonbetClient, hours_ahead: float, min_odds: float, max_odds: float,
    markets: list[str] | None, limit: int, offset: int, lang: str,
) -> dict[str, Any]:
    if not 0 < hours_ahead <= MAX_HOURS_AHEAD:
        raise ValueError(f"hours_ahead must be greater than 0 and at most {MAX_HOURS_AHEAD}")
    if not (1 < min_odds <= max_odds < float("inf")):
        raise ValueError("Expected 1 < min_odds <= max_odds with finite odds")
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("Expected 1 <= limit <= 500 and offset >= 0")
    requested = {market.strip().lower() for market in markets or []}
    if unknown := requested - _MARKETS:
        raise ValueError("Unsupported markets: " + ", ".join(sorted(unknown)))
    lang = lang.strip().lower()
    if not lang:
        raise ValueError("lang must not be empty")

    result: dict[str, Any] = {
        "mode": "prematch", "limit": limit, "offset": offset, "next_offset": None,
        "line_received_at": None, "max_snapshot_age_seconds": client.policy.max_snapshot_age,
        "root_events": 0, "eligible_matches": 0, "matches_checked": 0, "matches_completed": 0,
        "markets_processed": 0, "invalid_market_rows": 0, "conflicting_markets": 0, "skipped_events": 0,
        "errors": [], "error_count": 0, "partial": False, "status": "ok",
        "total_candidates": 0, "candidates": [],
    }
    try:
        with client.candidate_search_slot():
            return await _scan(client, hours_ahead, min_odds, max_odds, requested, limit, offset, lang, result)
    except FonbetSearchCapacityError as exc:
        return {**result, "status": "error", "partial": True, "error_count": 1, "errors": [_error("scan", exc)]}


async def _scan(
    client: FonbetClient, hours_ahead: float, min_odds: float, max_odds: float,
    requested: set[str], limit: int, offset: int, lang: str, result: dict[str, Any],
) -> dict[str, Any]:
    start = datetime.now(timezone.utc).timestamp()
    end = start + hours_ahead * 3600
    errors = result["errors"]
    selected: list[dict[str, Any]] = []
    try:
        line = await client.current_line_snapshot("ru")
        events, sports = _line_indexes(line.payload)
        line_blocks = _feed_blocks(line.payload)
    except _RECOVERABLE as exc:
        errors.append(_error("line", exc))
        return {**result, "status": "error", "partial": True, "error_count": 1}

    result["line_received_at"] = line.received_at.isoformat()
    if line.age() >= client.policy.max_snapshot_age:
        errors.append(_issue("line", "stale_snapshot"))
        return {**result, "status": "error", "partial": True, "error_count": 1}
    children = _children(events)
    matches = []
    for event_id, event in events.items():
        if event.get("parentId"):
            continue
        result["root_events"] += 1
        starts_at = event.get("startTime")
        if (
            type(starts_at) is int and start < starts_at <= end and _prematch(event)
            and (event_id not in line_blocks or line_blocks[event_id] is not None)
            and _sport_name(sports, event.get("sportId"), prefer_root=True) == "Футбол"
            and all(isinstance(event.get(team), str) and event[team].strip() for team in ("team1", "team2"))
        ):
            matches.append(event_id)

    result["eligible_matches"] = len(matches)
    matches.sort(key=lambda event_id: (events[event_id]["startTime"], event_id))
    queued = iter(matches)
    completed: dict[int, tuple[Snapshot, dict[str, Any], list[dict[str, Any]], set[_FactorKey]]] = {}
    checked: set[int] = set()
    local_events: dict[int, dict[str, Any]] = {}
    local_sports = {}
    local_line = None

    async def check_matches() -> None:
        for event_id in queued:
            checked.add(event_id)
            try:
                detail = await client.event_view_snapshot(event_id, "ru")
                narrowed = {eid: events[eid] for eid in _match_tree(children, event_id)}
                scanned = _match_selections(event_id, narrowed, sports, line_blocks, detail.payload, end)
                if scanned is None:
                    continue
                event, selections, rows = scanned
                result["markets_processed"] += rows.processed
                result["invalid_market_rows"] += rows.invalid
                if rows.incomplete:
                    errors.append(_issue("event", "incomplete_detail", event_id))
                if rows.invalid:
                    errors.append(_issue("event", "invalid_market_rows", event_id))
                completed[event_id] = (detail, event, selections, rows.price_conflicts)
            except FonbetRequestCapacityError as exc:
                checked.discard(event_id)
                errors.append(_error("event", exc, event_id))
                return
            except _RECOVERABLE as exc:
                errors.append(_error("event", exc, event_id))

    async def localize() -> None:
        nonlocal local_events, local_sports, local_line
        try:
            local_line = await client.current_line_snapshot(lang)
            local_events, local_sports = _line_indexes(local_line.payload)
        except _RECOVERABLE as exc:
            errors.append(_error("localization", exc))

    remaining = max(0, client.policy.max_snapshot_age - line.age())
    try:
        async with asyncio.timeout(remaining):
            workers = [check_matches() for _ in range(min(len(matches), client.policy.max_concurrency))]
            if lang != "ru" and matches:
                workers.append(localize())
            await asyncio.gather(*workers)
    except TimeoutError:
        errors.append(_issue("scan", "scan_deadline"))

    result["matches_checked"] = len(checked)
    if local_line is not None and local_line.age() >= client.policy.max_snapshot_age:
        local_events, local_sports = {}, {}
        errors.append(_issue("localization", "stale_snapshot"))
    for event_id, (detail, event, selections, price_conflicts) in completed.items():
        if detail.age() >= client.policy.max_snapshot_age:
            errors.append(_issue("event", "stale_snapshot", event_id))
        elif event["startTime"] > datetime.now(timezone.utc).timestamp():
            localized = local_events.get(event_id, {})
            league = _sport_name(local_sports, localized.get("sportId"), prefer_root=False)
            league = league or _sport_name(sports, event.get("sportId"), prefer_root=False)
            matched, conflicts = _output_selections(
                selections, event, league, localized, detail.received_at, requested, min_odds, max_odds,
                price_conflicts,
            )
            result["matches_completed"] += 1
            result["conflicting_markets"] += conflicts
            if conflicts:
                errors.append(_issue("event", "conflicting_quotes", event_id))
            selected.extend(matched)

    result["skipped_events"] = result["root_events"] - result["matches_completed"]
    selected.sort(key=lambda item: (
        item["start_time"], item["event_id"], item["market_event_id"], item["factor_id"],
        item["threshold"] or "",
    ))
    errors.sort(key=lambda item: (item["stage"], item["event_id"] or 0))
    return {
        **result, "status": "partial" if errors else "ok", "partial": bool(errors),
        "error_count": len(errors), "total_candidates": len(selected),
        "next_offset": offset + limit if offset + limit < len(selected) else None,
        "candidates": selected[offset:offset + limit],
    }
