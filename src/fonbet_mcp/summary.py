"""Compact, read-only, verified full-time football line by a match query.

Unknown factor IDs, markets from periods, and other stat markets are omitted.
This is a browsing summary, not a claim to show Fonbet's entire betslip.
"""
from __future__ import annotations

import re
from typing import Any

from fonbet_mcp.fonbet import (
    _KNOWN_FACTOR_NAMES,
    _VERIFIED_TEAM_CORNER_FACTOR_IDS,
    _VERIFIED_TEAM_GOAL_FACTOR_IDS,
    _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS,
    _iter_events,
    _sport_name,
    _sports_by_id,
    extract_match_markets,
)

_TOTAL_FACTOR_IDS = frozenset(
    factor_id for factor_id, label in _KNOWN_FACTOR_NAMES.items()
    if label in ("Тотал больше", "Тотал меньше")
)

_SECTIONS = ("goals", "corners", "yellow_cards")


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def find_match_candidates(line_payload: dict[str, Any], query: str) -> dict[str, Any]:
    """Find main football events. Never silently choose between multiple matches."""
    needle = _normalize(query)
    if len(needle) < 3:
        return {"status": "invalid_query", "message": "Укажите название команды или обе команды."}
    sports = _sports_by_id(line_payload)
    exact, partial = [], []
    for event in _iter_events(line_payload):
        event_id = event.get("id")
        team1, team2 = event.get("team1"), event.get("team2")
        if not isinstance(event_id, int) or not isinstance(team1, str) or not isinstance(team2, str):
            continue
        if _sport_name(sports, event.get("sportId"), prefer_root=True) != "Футбол":
            continue
        display_name = f"{team1} — {team2}"
        normalized = _normalize(display_name)
        record = {
            "event_id": event_id,
            "name": display_name,
            "team1": team1,
            "team2": team2,
            "league": _sport_name(sports, event.get("sportId"), prefer_root=False),
            "start_time": event.get("startTime"),
            "status": event.get("place") or event.get("state"),
        }
        if needle == normalized:
            exact.append(record)
        elif needle in normalized:
            partial.append(record)
    # Prefer exact two-team matches over any fuzzy substring match.
    candidates = exact if exact else partial
    if not candidates:
        return {"status": "not_found", "query": query, "candidates": []}
    if len(candidates) != 1:
        return {
            "status": "ambiguous",
            "query": query,
            "candidate_count": len(candidates),
            "candidates": candidates[:12],
            "message": "Уточните матч, турнир или ID события.",
        }
    return {"status": "found", "query": query, "event": candidates[0]}


def _section(market: dict[str, Any], event_id: int) -> str | None:
    subevent_name = market.get("subevent_name")
    if market.get("event_id") == event_id and subevent_name is None:
        return "goals"
    if market.get("parent_id") != event_id or not isinstance(subevent_name, str):
        return None
    name = _normalize(subevent_name)
    if name == "угловые":
        return "corners"
    if name in ("желтые карты", "желтые карточки"):
        return "yellow_cards"
    return None


def _selection(market: dict[str, Any], category: str, event: dict[str, Any]) -> dict[str, Any] | None:
    if market.get("blocked"):
        return None
    odds = market.get("odds")
    if isinstance(odds, bool) or not isinstance(odds, (float, int)) or odds <= 1:
        return None
    threshold = market.get("param_text")
    if threshold in ("", None):
        return None
    try:
        threshold_number = float(str(threshold).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if threshold_number < 0 or threshold_number != threshold_number:
        return None
    factor_id = market.get("factor_id")
    name = market.get("name")
    if not isinstance(name, str):
        return None

    common_base = {
        "factor_id": factor_id,
        "event_id": market.get("event_id"),
        "threshold": str(threshold),
        "odds": odds,
        "break_even_probability": round(1 / odds, 6),
        "break_even_percent": round(100 / odds, 2),
        "label": name,
    }
    if factor_id in _TOTAL_FACTOR_IDS and name in (
        f"Тотал больше ({threshold})",
        f"Тотал меньше ({threshold})",
    ):
        direction = "over" if name.startswith("Тотал больше") else "under"
        return {**common_base, "type": "general_total", "direction": direction, "team": None}

    source = market.get("mapping_source")
    expected_source = {
        "goals": "calibrated_fonbet_ui_bre_ev_goals",
        "corners": "calibrated_fonbet_ui_bre_ev_corners",
        "yellow_cards": "calibrated_fonbet_ui_bre_ev_cards",
    }[category]
    if source != expected_source:
        return None
    mappings = {
        "goals": _VERIFIED_TEAM_GOAL_FACTOR_IDS,
        "corners": _VERIFIED_TEAM_CORNER_FACTOR_IDS,
        "yellow_cards": _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS,
    }[category]
    spec = mappings.get(factor_id)
    if not spec:
        return None
    team_number, direction = spec[:2]
    team_name = event.get("team1") if team_number == 1 else event.get("team2")
    if not isinstance(team_name, str):
        return None
    return {
        **common_base,
        "type": "individual_total",
        "direction": "over" if direction == "ИТБ" else "under",
        "team": team_name,
        "team_number": team_number,
        "mapping_source": source,
    }



# Each key must equal a direct, explicitly named full-match football sub-event.
# The parent chain is checked below: factor IDs alone never determine the market.
_EXTRA_SECTION_NAMES: dict[str, str] = {
    "угловые": "corners",
    "желтые карты": "yellow_cards",
    "желтые карточки": "yellow_cards",
    "фолы": "fouls",
    "удары в створ": "shots_on_target",
    "офсайды": "offsides",
    "вброс аутов": "throw_ins",
    "удары от ворот": "goal_kicks",
    "удары всего": "shots",
    "штанги или перекладины": "woodwork",
}
_PERIOD_NAMES: dict[str, str] = {"1-й тайм": "first_half", "2-й тайм": "second_half"}
_OUTCOME_IDS = frozenset((921, 922, 923, 924, 925, 1571))
_HANDICAP_IDS = frozenset((927, 928))
_BOTH_SCORE_IDS = frozenset((709, 710))


def _identified_market_context(
    market: dict[str, Any], events: dict[int, dict[str, Any]], root_id: int
) -> tuple[str, str] | None:
    """Classify only an exact root/child/grandchild path, never fuzzy text."""
    eid = market.get("event_id")
    if eid == root_id and market.get("subevent_name") is None:
        return "goals", "full_time"
    event = events.get(eid)
    if event is None:
        return None
    name = str(event.get("name") or event.get("caption") or "").strip().casefold()
    parent_id = event.get("parentId")
    if parent_id == root_id:
        if name in _PERIOD_NAMES:
            return "goals", _PERIOD_NAMES[name]
        if name in _EXTRA_SECTION_NAMES:
            return _EXTRA_SECTION_NAMES[name], "full_time"
        return None
    parent = events.get(parent_id)
    if parent is None or parent.get("parentId") != root_id:
        return None
    parent_name = str(parent.get("name") or parent.get("caption") or "").strip().casefold()
    category = _EXTRA_SECTION_NAMES.get(parent_name)
    if category is None:
        return None
    for prefix, period in _PERIOD_NAMES.items():
        if name == f"{prefix} {parent_name}":
            return category, period
    return None


def _expanded_identified_sections(
    markets: list[dict[str, Any]],
    events: dict[int, dict[str, Any]],
    root_id: int,
) -> tuple[dict[str, dict[str, dict[str, list[dict[str, Any]]]]], int]:
    """Append safe, context-aware markets without re-labelling unknown factor IDs.

    The old verified full-match goal/corner/card totals are retained unchanged in
    market_sections. Only known, explicitly named factor IDs can enter this
    secondary index. Identification does not independently validate settlement.
    """
    root = events.get(root_id, {})
    result: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    seen: set[tuple[int, int, str]] = set()
    for market in markets:
        context = _identified_market_context(market, events, root_id)
        if context is None or market.get("blocked"):
            continue
        category, period = context
        factor_id = market.get("factor_id")
        if not isinstance(factor_id, int):
            continue
        known_name = _KNOWN_FACTOR_NAMES.get(factor_id)
        if known_name is None:
            continue
        odds = market.get("odds")
        if isinstance(odds, bool) or not isinstance(odds, (int, float)) or not 1 < odds < float("inf"):
            continue

        threshold: str | None = None
        kind: str
        team: str | None = None
        if factor_id in _TOTAL_FACTOR_IDS:
            # Already exposed as verified full-time totals in the original response.
            if period == "full_time" and category in _SECTIONS:
                continue
            kind = "total"
        elif factor_id in _HANDICAP_IDS:
            kind = "handicap"
            team_num = 1 if factor_id == 927 else 2
            possible_team = root.get(f"team{team_num}")
            team = possible_team if isinstance(possible_team, str) and possible_team.strip() else None
        elif factor_id in _OUTCOME_IDS:
            kind = "outcome"
        elif factor_id in _BOTH_SCORE_IDS and category == "goals" and period == "full_time":
            kind = "both_teams_score"
        else:
            continue

        if kind in ("total", "handicap"):
            raw_threshold = market.get("param_text")
            if raw_threshold in (None, ""):
                continue
            threshold = str(raw_threshold)
            try:
                numeric = float(threshold.replace(",", "."))
            except ValueError:
                continue
            if not float("-inf") < numeric < float("inf") or (kind == "total" and numeric < 0):
                continue
            expected_name = f"{known_name} ({threshold})"
            if market.get("name") != expected_name:
                continue
        elif market.get("name") != known_name or market.get("param_text") not in (None, ""):
            continue

        dedupe_key = (market["event_id"], factor_id, threshold or "")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry: dict[str, Any] = {
            "event_id": market["event_id"],
            "factor_id": factor_id,
            "category": category,
            "period": period,
            "kind": kind,
            "label": known_name if threshold is None else f"{known_name} ({threshold})",
            "odds": odds,
            "break_even_percent": round(100 / odds, 2),
            "identification_basis": "known_factor_id_and_explicit_event_path",
        }
        if threshold is not None:
            entry["threshold"] = threshold
        if team is not None:
            entry["team"] = team

        period_buckets = result.setdefault(category, {}).setdefault(period, {})
        period_buckets.setdefault(kind, []).append(entry)

    for periods in result.values():
        for buckets in periods.values():
            for entries in buckets.values():
                entries.sort(key=lambda item: (
                    item.get("threshold", ""),
                    item["factor_id"],
                ))
    return result, len(seen)

def summarize_match_line(
    event_id: int,
    event_view_payload: dict[str, Any],
    line_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return compact verified totals, without false probabilities or predictions."""
    all_markets: list[dict[str, Any]] = []
    cursor = 0
    first_page = None
    while True:
        page = extract_match_markets(
            event_view_payload, event_id=event_id, offset=cursor, limit=500,
            line_payload=line_payload,
        )
        if first_page is None:
            first_page = page
        all_markets.extend(page["markets"])
        if page["next_offset"] is None:
            break
        cursor = page["next_offset"]

    events = {
        e["id"]: e for e in (
            *list(_iter_events(line_payload)), *list(_iter_events(event_view_payload))
        ) if isinstance(e.get("id"), int)
    }
    root = events.get(event_id, {})
    if not root or first_page is None:
        raise ValueError(f"Unknown match event ID: {event_id}")
    result: dict[str, dict[str, list[dict[str, Any]]]] = {
        section: {"general_totals": [], "individual_totals": []} for section in _SECTIONS
    }
    seen: set[tuple[int, int, str]] = set()
    for market in all_markets:
        section = _section(market, event_id)
        if section is None:
            continue
        selection = _selection(market, section, root)
        if selection is None:
            continue
        key = (selection["event_id"], selection["factor_id"], selection["threshold"])
        if key in seen:
            continue
        seen.add(key)
        bucket = "general_totals" if selection["type"] == "general_total" else "individual_totals"
        result[section][bucket].append(selection)

    for section in _SECTIONS:
        for bucket in ("general_totals", "individual_totals"):
            result[section][bucket].sort(key=lambda x: (
                float(x["threshold"].replace(",", ".")),
                str(x.get("team") or ""),
                0 if x["direction"] == "over" else 1,
                x["factor_id"],
            ))
    expanded, expanded_count = _expanded_identified_sections(all_markets, events, event_id)
    return {
        "status": "ok",
        "event": {
            **first_page["event"],
            "team1": root.get("team1"),
            "team2": root.get("team2"),
        },
        "market_sections": result,
        "expanded_market_sections": expanded,
        "expanded_market_count": expanded_count,
        "source_market_count": first_page["total_market_count"],
        "selected_market_count": len(seen) + expanded_count,
        "excluded_market_count": first_page["total_market_count"] - len(seen) - expanded_count,
        "scope": ("Verified full-match goal/corner/card totals in market_sections; "
                  "known factor IDs in explicitly linked full-match and period "
                  "events in expanded_market_sections. Unknown IDs excluded."),
        "probability_note": (
            "break_even_probability = 1 / decimal odds. This is the break-even "
            "threshold, NOT an independent prediction of outcome probability."
        ),
    }
