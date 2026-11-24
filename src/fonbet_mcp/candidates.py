"""Search identified prematch football selections across the Fonbet line."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from fonbet_mcp.client import FonbetClient, FonbetCooldownError, Snapshot
from fonbet_mcp.fonbet import _iter_events, _sport_name, _sports_by_id
from fonbet_mcp.summary import _EXTRA_SECTION_NAMES, summarize_match_line

_PREMATCH = {"line", "prematch", "pre_match"}
_UNAVAILABLE = {"live", "inplay", "in_play", "started", "finished", "ended",
                "closed", "suspended", "blocked", "cancelled", "canceled", "deleted"}
_FLAGS = ("b", "blocked", "isBlocked", "live", "isLive", "stale", "expired", "cancelled")
_SIDES = {
    921: "team1", 922: "draw", 923: "team2", 924: "team1_or_draw",
    925: "draw_or_team2", 1571: "team1_or_team2", 927: "team1", 928: "team2",
    2820: "team1", 2821: "team2", 709: "yes", 710: "no",
}
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
    return number if number.is_finite() else None


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


def _by_id(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        event["id"]: event for event in _iter_events(payload)
        if type(event.get("id")) is int and event["id"] > 0
    }


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
            continue
        event_id = item["eventId"]
        if item.get("state") == "partial" and isinstance(item.get("factors"), list):
            factors = blocked.setdefault(event_id, set())
            if factors is not None:
                factors.update(factor_id for factor_id in item["factors"] if type(factor_id) is int)
        else:
            blocked[event_id] = None
    for item in live_events:
        if isinstance(item, dict) and type(item.get("eventId")) is int:
            blocked[item["eventId"]] = None
    return blocked


def _market_rows(
    payload: dict[str, Any], eligible: set[int],
    line_blocks: dict[int, set[int] | None], detail_blocks: dict[int, set[int] | None],
) -> tuple[list[dict[str, Any]], int, bool]:
    rows: dict[int, dict[tuple[int, Decimal | None], dict[str, Any] | None]] = defaultdict(dict)
    count = 0
    incomplete = False
    for block in payload["customFactors"]:
        if not isinstance(block, dict) or type(block.get("e")) is not int or block["e"] not in eligible:
            continue
        factors = block.get("factors")
        if not isinstance(factors, list):
            raise ValueError("Invalid Fonbet factor list")
        event_id = block["e"]
        selections = rows[event_id]
        suppressed = (line_blocks.get(event_id) or set()) | (detail_blocks.get(event_id) or set())
        declared_count = block.get("countAll")
        if type(declared_count) is int and declared_count > len(factors):
            incomplete = True
        for factor in factors:
            count += 1
            if not isinstance(factor, dict) or type(factor.get("f")) is not int:
                continue
            param = factor.get("pt")
            threshold = _number(param)
            if param not in (None, "") and threshold is None:
                continue
            key = (factor["f"], threshold)
            odds = factor.get("v")
            available = (
                factor["f"] not in suppressed and not _unavailable(block) and not _unavailable(factor)
                and type(odds) in (float, int) and 1 < odds < float("inf")
            )
            if not available:
                selections[key] = None
            elif key not in selections:
                selections[key] = factor
            elif selections[key] is not None and selections[key]["v"] != odds:
                # Conflicting prices for the same factor and threshold are ambiguous.
                selections[key] = None
    return [
        {"e": event_id, "factors": [factor for factor in selections.values() if factor is not None]}
        for event_id, selections in rows.items()
    ], count, incomplete


def _match_selections(
    event_id: int, line_events: dict[int, dict[str, Any]], line_sports: list[Any],
    line_blocks: dict[int, set[int] | None], detail: dict[str, Any], window_end: float,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, bool]:
    if not isinstance(detail.get("events"), list) or not isinstance(detail.get("customFactors"), list):
        raise ValueError("Missing Fonbet event or factor list")
    if detail.get("fromVersion"):
        raise ValueError("Expected a full Fonbet event snapshot")
    if "sports" in detail and not isinstance(detail["sports"], list):
        raise ValueError("Invalid Fonbet sports list")
    detail_events = _by_id(detail)
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
        return None, [], 0, False

    linked = _match_tree(_children(events), event_id)
    eligible = {
        eid for eid in linked
        if eid not in full_blocks and events[eid].get("parentId") not in full_blocks
        and not _unavailable(events[eid])
        and not _unavailable(events.get(events[eid].get("parentId"), {}))
    }
    blocks, count, incomplete = _market_rows(detail, eligible, line_blocks, detail_blocks)
    # The line supplies hierarchy only. Every price comes from this detail snapshot.
    metadata = {"events": [events[eid] for eid in sorted(linked)], "sports": line_sports}
    data = {**detail, "events": [], "customFactors": blocks}
    summary = summarize_match_line(event_id, data, metadata, include_zero_handicaps=True)
    return root, _flatten(summary), count, incomplete


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
    min_odds: float, max_odds: float,
) -> list[dict[str, Any]]:
    results = []
    seen: set[tuple[str, str, str, str, Decimal | None, int | None]] = set()
    start = datetime.fromtimestamp(event["startTime"], tz=timezone.utc).isoformat()
    teams = {
        number: localized.get(f"team{number}") or event.get(f"team{number}")
        for number in (1, 2)
    }
    for item in sorted(items, key=lambda market: (market["event_id"], market["factor_id"])):
        if not min_odds <= item["odds"] <= max_odds or not _market_filter(item, requested):
            continue
        threshold = _number(item.get("threshold"))
        team_number = item.get("team_number")
        if item["category"] in {"handicaps", "outcomes"} and item["side"] in {"team1", "team2"}:
            team_number = int(item["side"][-1])
        key = (item["statistic"], item["period"], item["category"], item["side"], threshold, team_number)
        if key in seen:
            continue
        seen.add(key)
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
    return results


def _error(stage: str, exc: Exception, event_id: int | None = None) -> dict[str, Any]:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    code = {
        "HTTPStatusError": "http_error",
        "FonbetCooldownError": "retry_after_cooldown",
    }.get(type(exc).__name__, "invalid_response")
    if isinstance(exc, httpx.TimeoutException):
        code = "timeout"
    elif isinstance(exc, httpx.RequestError):
        code = "network_error"
    return {"stage": stage, "event_id": event_id, "code": code, "http_status": status}


def _validate_line(payload: dict[str, Any]) -> None:
    if payload.get("fromVersion") or not isinstance(payload.get("events"), list) or (
        payload["events"] and not isinstance(payload.get("sports"), list)
    ):
        raise ValueError("Missing Fonbet line events or sports")


async def search_candidates(
    client: FonbetClient, hours_ahead: float, min_odds: float, max_odds: float,
    markets: list[str] | None, limit: int, offset: int, lang: str,
) -> dict[str, Any]:
    if not math.isfinite(hours_ahead) or hours_ahead <= 0:
        raise ValueError("hours_ahead must be finite and positive")
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

    start = datetime.now(timezone.utc).timestamp()
    end = start + hours_ahead * 3600
    errors: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "mode": "prematch", "limit": limit, "offset": offset, "next_offset": None,
        "line_received_at": None, "max_snapshot_age_seconds": client.policy.max_snapshot_age,
        "matches_checked": 0, "markets_processed": 0, "skipped_events": 0,
        "errors": errors, "error_count": 0, "partial": False, "status": "ok",
        "total_candidates": 0, "candidates": [],
    }
    recoverable = (httpx.HTTPError, FonbetCooldownError, ValueError)
    try:
        line = await client.current_line_snapshot("ru")
        _validate_line(line.payload)
        line_blocks = _feed_blocks(line.payload)
    except recoverable as exc:
        errors.append(_error("line", exc))
        return {**result, "status": "error", "partial": True, "error_count": 1}

    result["line_received_at"] = line.received_at.isoformat()
    if line.age() >= client.policy.max_snapshot_age:
        errors.append({"stage": "line", "event_id": None, "code": "stale_snapshot", "http_status": None})
        return {**result, "status": "error", "partial": True, "error_count": 1}
    events = _by_id(line.payload)
    sports = _sports_by_id(line.payload)
    children = _children(events)
    matches = []
    for event_id, event in events.items():
        if event.get("parentId"):
            continue
        starts_at = event.get("startTime")
        if (
            type(starts_at) is int and start < starts_at <= end and _prematch(event)
            and (event_id not in line_blocks or line_blocks[event_id] is not None)
            and _sport_name(sports, event.get("sportId"), prefer_root=True) == "Футбол"
            and all(isinstance(event.get(team), str) and event[team].strip() for team in ("team1", "team2"))
        ):
            matches.append(event_id)
        else:
            result["skipped_events"] += 1

    matches.sort(key=lambda event_id: (events[event_id]["startTime"], event_id))
    queued = iter(matches)
    completed: dict[int, tuple[Snapshot, dict[str, Any], list[dict[str, Any]]]] = {}
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
                event, selections, count, incomplete = _match_selections(
                    event_id, narrowed, line.payload.get("sports", []), line_blocks, detail.payload, end,
                )
                result["markets_processed"] += count
                if incomplete:
                    errors.append({
                        "stage": "event", "event_id": event_id,
                        "code": "incomplete_detail", "http_status": None,
                    })
                if event is not None:
                    completed[event_id] = (detail, event, selections)
            except recoverable as exc:
                errors.append(_error("event", exc, event_id))

    async def localize() -> None:
        nonlocal local_events, local_sports, local_line
        try:
            local_line = await client.current_line_snapshot(lang)
            _validate_line(local_line.payload)
            local_events = _by_id(local_line.payload)
            local_sports = _sports_by_id(local_line.payload)
        except recoverable as exc:
            errors.append(_error("localization", exc))

    remaining = max(0, client.policy.max_snapshot_age - line.age())
    try:
        async with asyncio.timeout(remaining):
            workers = [check_matches() for _ in range(min(len(matches), client.policy.max_concurrency))]
            if lang != "ru" and matches:
                workers.append(localize())
            await asyncio.gather(*workers)
    except TimeoutError:
        errors.append({"stage": "scan", "event_id": None, "code": "scan_deadline", "http_status": None})

    result["matches_checked"] = len(checked)
    result["skipped_events"] += len(matches) - len(completed)
    if local_line is not None and local_line.age() >= client.policy.max_snapshot_age:
        local_events, local_sports = {}, {}
        errors.append({"stage": "localization", "event_id": None, "code": "stale_snapshot", "http_status": None})
    for event_id, (detail, event, selections) in completed.items():
        if detail.age() >= client.policy.max_snapshot_age:
            errors.append({"stage": "event", "event_id": event_id, "code": "stale_snapshot", "http_status": None})
        elif event["startTime"] > datetime.now(timezone.utc).timestamp():
            localized = local_events.get(event_id, {})
            league = _sport_name(local_sports, localized.get("sportId"), prefer_root=False)
            league = league or _sport_name(sports, event.get("sportId"), prefer_root=False)
            selected.extend(_output_selections(
                selections, event, league, localized, detail.received_at, requested, min_odds, max_odds,
            ))
            continue
        result["skipped_events"] += 1

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
