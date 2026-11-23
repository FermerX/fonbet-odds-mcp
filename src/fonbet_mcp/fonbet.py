from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class EventSummary:
    event_id: int
    name: str
    sport: str | None
    league: str | None
    start_time: int | None
    status: str | None


class FonbetClient:
    def __init__(
        self,
        base_url: str,
        scope_market: str = "1600",
        timeout: float = 20,
        current_line_path: str = "/events/listBase",
        event_view_path: str = "/events/event",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.scope_market = scope_market
        self.timeout = timeout
        self.current_line_path = current_line_path
        self.event_view_path = event_view_path

    async def current_line(self, lang: str = "ru") -> dict[str, Any]:
        return await self._get(
            self.current_line_path.format(lang=lang),
            params={"scopeMarket": self.scope_market, "lang": lang},
        )

    async def event_view(self, event_id: int, lang: str = "ru") -> dict[str, Any]:
        return await self._get(
            self.event_view_path,
            params={"eventId": event_id, "lang": lang, "scopeMarket": self.scope_market},
        )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"user-agent": "Mozilla/5.0", "referer": "https://fon.bet/"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Fonbet returned a non-object JSON payload")
        return payload


def search_events(payload: dict[str, Any], query: str, limit: int = 20) -> list[EventSummary]:
    needle = query.casefold().strip()
    sports_by_id = _sports_by_id(payload)
    results: list[EventSummary] = []

    for event in _iter_events(payload):
        event_id = event.get("id")
        if not isinstance(event_id, int):
            continue
        name = _event_name(event)
        sport = _sport_name(sports_by_id, event.get("sportId"), prefer_root=True)
        league = _sport_name(sports_by_id, event.get("sportId"), prefer_root=False)
        haystack = " ".join(part for part in [name, league, sport, event.get("place")] if part).casefold()
        if needle and needle not in haystack:
            continue
        results.append(
            EventSummary(
                event_id=event_id,
                name=name,
                sport=sport,
                league=league,
                start_time=event.get("startTime") if isinstance(event.get("startTime"), int) else None,
                status=event.get("place") or event.get("state"),
            )
        )
        if len(results) >= limit:
            break
    return results


def extract_odds(payload: dict[str, Any], event_id: int | None = None) -> dict[str, Any]:
    return extract_markets(payload, event_id=event_id, offset=0, limit=120, query=None, include_raw=False)


def extract_markets(
    payload: dict[str, Any],
    event_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    query: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    events = list(_iter_events(payload))
    if event_id is not None:
        event = next((item for item in events if item.get("id") == event_id), events[0] if events else {})
    else:
        event = events[0] if events else {}
        event_id = event.get("id") if isinstance(event.get("id"), int) else None

    factor_blocks = _factor_blocks_by_event(payload)
    factors = factor_blocks.get(event_id, []) if event_id is not None else []
    sports_by_id = _sports_by_id(payload)
    catalog = _factor_type_catalog(payload)
    normalized = [_normalize_factor(factor, catalog, include_raw=include_raw) for factor in factors if isinstance(factor, dict)]

    if query:
        needle = query.casefold().strip()
        normalized = [market for market in normalized if needle in _market_search_text(market).casefold()]

    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    page = normalized[offset : offset + limit]

    return {
        "event": {
            "id": event.get("id"),
            "name": _event_name(event),
            "sport": _sport_name(sports_by_id, event.get("sportId"), prefer_root=True),
            "league": _sport_name(sports_by_id, event.get("sportId"), prefer_root=False),
            "start_time": event.get("startTime"),
            "status": event.get("place"),
        },
        "markets": page,
        "market_count": len(normalized),
        "total_market_count": len(factors),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(normalized) else None,
        "packet_version": payload.get("packetVersion"),
    }


# Calibrated against the two Fonbet UI screenshots for the EPL fixture
# Brentford vs Everton. Both teams, exact thresholds and both directions
# were matched to the same fixture's factor IDs + odds.
# WARNING: do not apply this map to goals, cards, halves, or other sports.
# Validation on additional fixtures should precede widening its scope.
_VERIFIED_TEAM_CORNER_FACTOR_IDS: dict[int, tuple[int, str]] = {
    1809: (1, "ИТБ"),
    1810: (1, "ИТМ"),
    1812: (1, "ИТБ"),
    1813: (1, "ИТМ"),
    1815: (1, "ИТБ"),
    1816: (1, "ИТМ"),
    # The 3.5 pair for team two is NONADJACENT.
    1854: (2, "ИТБ"),
    1871: (2, "ИТМ"),
    1873: (2, "ИТБ"),
    1874: (2, "ИТМ"),
    1880: (2, "ИТБ"),
    1881: (2, "ИТМ"),
}


def _verified_team_corner_label(
    factor_id: Any,
    param_text: Any,
    root: dict[str, Any],
    event: dict[str, Any],
    event_id: int,
    section_name: str,
    sport_name: str | None,
) -> str | None:
    """Apply conservative screenshot-calibrated labels to full-time corners."""
    if sport_name != "Футбол":
        return None
    if section_name.strip().casefold() != "угловые":
        return None
    if event.get("parentId") != event_id:
        return None
    if not isinstance(factor_id, int) or param_text in (None, ""):
        return None
    spec = _VERIFIED_TEAM_CORNER_FACTOR_IDS.get(factor_id)
    if spec is None:
        return None
    team_number, direction = spec
    team_name = root.get("team1") if team_number == 1 else root.get("team2")
    if not isinstance(team_name, str) or not team_name.strip():
        return None
    return f"{team_name} {direction} угловых ({param_text})"


# Screenshot calibration: Brentford (team 1) and Everton (team 2), full match
# "Индивидуальные тоталы голов", thresholds 0.5, 1, 1.5, 2, 2.5.
# All 20 decimal odds and factor IDs matched the same Fonbet MCP event.
# Never use these mappings on card, corner, halftime, or other sports markets.
_VERIFIED_TEAM_GOAL_FACTOR_IDS: dict[int, tuple[int, str, str]] = {
    1809: (1, "ИТБ", "0.5"),
    1810: (1, "ИТМ", "0.5"),
    1812: (1, "ИТБ", "1"),
    1813: (1, "ИТМ", "1"),
    1815: (1, "ИТБ", "1.5"),
    1816: (1, "ИТМ", "1.5"),
    1818: (1, "ИТБ", "2"),
    1819: (1, "ИТМ", "2"),
    1821: (1, "ИТБ", "2.5"),
    1822: (1, "ИТМ", "2.5"),
    1854: (2, "ИТБ", "0.5"),
    1871: (2, "ИТМ", "0.5"),
    1873: (2, "ИТБ", "1"),
    1874: (2, "ИТМ", "1"),
    1880: (2, "ИТБ", "1.5"),
    1881: (2, "ИТМ", "1.5"),
    1883: (2, "ИТБ", "2"),
    1884: (2, "ИТМ", "2"),
    1886: (2, "ИТБ", "2.5"),
    1887: (2, "ИТМ", "2.5"),
}


def _verified_team_goal_label(
    factor_id: Any,
    param_text: Any,
    root: dict[str, Any],
    sport_name: str | None,
) -> str | None:
    """Label individual full-time goals only for screenshot-confirmed IDs."""
    if sport_name != "Футбол" or not isinstance(factor_id, int):
        return None
    spec = _VERIFIED_TEAM_GOAL_FACTOR_IDS.get(factor_id)
    if spec is None:
        return None
    team_number, direction, verified_threshold = spec
    # Factor IDs can be reused in submarkets. Keep the verified threshold.
    if str(param_text) != verified_threshold:
        return None
    team = root.get("team1") if team_number == 1 else root.get("team2")
    if not isinstance(team, str) or not team.strip():
        return None
    return f"{team} {direction} голов ({verified_threshold})"


# Fonbet UI screenshot calibration: Brentford-Everton, full-match yellow cards.
# Confirmed all 6 factor IDs, sides, thresholds, prices and both teams.
# Deliberately restricted to the exact full-match "желтые карты" child event;
# it must NOT be extended to first-half cards, goals, corners or other sports.
_VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS: dict[int, tuple[int, str, str]] = {
    1809: (1, "ИТБ", "1.5"),
    1810: (1, "ИТМ", "1.5"),
    1854: (2, "ИТБ", "1.5"),
    1871: (2, "ИТМ", "1.5"),
    1873: (2, "ИТБ", "2.5"),
    1874: (2, "ИТМ", "2.5"),
}


def _verified_team_yellow_card_label(
    factor_id: Any,
    param_text: Any,
    root: dict[str, Any],
    event: dict[str, Any],
    event_id: int,
    section_name: str,
    sport_name: str | None,
) -> str | None:
    """Return screenshot-verified card totals only in the full-match cards child."""
    if sport_name != "Футбол":
        return None
    if section_name.strip().casefold() != "желтые карты":
        return None
    if event.get("parentId") != event_id or not isinstance(factor_id, int):
        return None
    spec = _VERIFIED_TEAM_YELLOW_CARD_FACTOR_IDS.get(factor_id)
    if spec is None:
        return None
    team_number, direction, threshold = spec
    if str(param_text) != threshold:
        return None
    team = root.get("team1") if team_number == 1 else root.get("team2")
    if not isinstance(team, str) or not team.strip():
        return None
    return f"{team} {direction} ЖК ({threshold})"


def extract_match_markets(
    payload: dict[str, Any],
    event_id: int,
    offset: int = 0,
    limit: int = 200,
    query: str | None = None,
    include_raw: bool = False,
    line_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a match's own markets and markets of all nested sub-events.

    Fonbet represents corners, cards, periods and other statistical markets as
    separate events linked by parentId. Factor IDs are not descriptive by
    themselves: preserve unknown labels rather than guessing them.
    """
    # Match detail data can omit siblings/children; the current line contains
    # parentId links. Details take precedence where both feeds have an event.
    events_by_id: dict[int, dict[str, Any]] = {}
    if line_payload is not None:
        events_by_id.update({
            event["id"]: event
            for event in _iter_events(line_payload)
            if isinstance(event.get("id"), int)
        })
    events_by_id.update({
        event["id"]: event
        for event in _iter_events(payload)
        if isinstance(event.get("id"), int)
    })
    root = events_by_id.get(event_id)
    if root is None:
        raise ValueError(f"Event {event_id} not found in Fonbet response")

    selected_ids = {event_id}
    # This also captures grandchildren such as first-half corners.
    for _ in range(len(events_by_id)):
        added = {
            eid for eid, event in events_by_id.items()
            if event.get("parentId") in selected_ids
        }
        if added.issubset(selected_ids):
            break
        selected_ids.update(added)

    factor_blocks = _factor_blocks_by_event(line_payload or {})
    factor_blocks.update(_factor_blocks_by_event(payload))
    catalog = _factor_type_catalog(line_payload or {})
    catalog.update(_factor_type_catalog(payload))
    sports = _sports_by_id(line_payload or {})
    sports.update(_sports_by_id(payload))
    all_markets: list[dict[str, Any]] = []
    subevents: list[dict[str, Any]] = []

    root_sport_name = _sport_name(sports, root.get("sportId"), prefer_root=True)

    for eid in [event_id, *(eid for eid in events_by_id if eid != event_id and eid in selected_ids)]:
        event = events_by_id[eid]
        # The event's own name/caption is usually "Угловые", "ЖК", etc.
        section = str(event.get("name") or event.get("caption") or _event_name(event))
        factors = factor_blocks.get(eid, [])
        if eid != event_id:
            subevents.append({
                "event_id": eid,
                "parent_id": event.get("parentId"),
                "name": section,
                "level": event.get("level"),
                "market_count": len(factors),
            })
        for factor in factors:
            if not isinstance(factor, dict):
                continue
            market = _normalize_factor(factor, catalog, include_raw=include_raw)
            market["event_id"] = eid
            market["parent_id"] = event.get("parentId")
            market["event_level"] = event.get("level")
            market["subevent_name"] = section if eid != event_id else None
            if eid != event_id and not market["group"]:
                market["group"] = section
            # Root football event = full-match goals only. Do not apply these
            # factor IDs to cards, corners, halves or any child event.
            if market.get("name") is None and eid == event_id:
                goal_label = _verified_team_goal_label(
                    market.get("factor_id"), market.get("param_text"),
                    root, root_sport_name,
                )
                if goal_label:
                    market["name"] = goal_label
                    market["label"] = goal_label
                    market["mapping_source"] = "calibrated_fonbet_ui_bre_ev_goals"
            # Only full-match football corners have screenshot-verified IDs.
            if market.get("name") is None:
                verified_label = _verified_team_corner_label(
                    market.get("factor_id"), market.get("param_text"),
                    root, event, event_id, section, root_sport_name,
                )
                if verified_label:
                    market["name"] = verified_label
                    market["label"] = verified_label
                    market["mapping_source"] = "calibrated_fonbet_ui_bre_ev_corners"
            # Calibrated with Fonbet UI; only the direct full-match yellow-cards
            # event, never halftime, generic goals or unrelated market sections.
            if market.get("name") is None:
                card_label = _verified_team_yellow_card_label(
                    market.get("factor_id"), market.get("param_text"),
                    root, event, event_id, section, root_sport_name,
                )
                if card_label:
                    market["name"] = card_label
                    market["label"] = card_label
                    market["mapping_source"] = "calibrated_fonbet_ui_bre_ev_cards"
            all_markets.append(market)

    needle = (query or "").casefold().strip()
    filtered = [
        market for market in all_markets
        if not needle or needle in _market_search_text(market).casefold()
        or needle in str(market.get("subevent_name") or "").casefold()
    ]
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    page = filtered[offset: offset + limit]
    return {
        "event": {
            "id": event_id,
            "name": _event_name(root),
            "sport": _sport_name(sports, root.get("sportId"), prefer_root=True),
            "league": _sport_name(sports, root.get("sportId"), prefer_root=False),
            "start_time": root.get("startTime"),
            "status": root.get("place") or root.get("state"),
        },
        "subevents": subevents,
        "source_counts": {
            "event_view_events": len(list(_iter_events(payload))),
            "line_events": len(list(_iter_events(line_payload or {}))),
            "linked_events": len(selected_ids),
            "linked_subevents_with_factors": sum(1 for item in subevents if item["market_count"] > 0),
        },
        "markets": page,
        "market_count": len(filtered),
        "total_market_count": len(all_markets),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(filtered) else None,
        "packet_version": payload.get("packetVersion"),
    }


def _team_total_calibration_hypotheses(
    markets: list[dict[str, Any]],
    event: dict[str, Any],
    event_id: int,
    root: dict[str, Any],
    section_filter: str,
) -> list[dict[str, Any]]:
    """Suggest, but NEVER assert, interpretations of goal/card totals.

    Observed ID/team/direction correspondence was *verified only for*
    full-match CORNERS by two Fonbet UI screenshots. For goals and cards,
    the same IDs are a hypothesis that requires independent UI validation.
    Unlike the verified corner mapping, this function does NOT change
    market["name"] or market["label"].
    """
    if event.get("sport") != "Футбол":
        return []
    teams = (root.get("team1"), root.get("team2"))
    if not all(isinstance(t, str) and t.strip() for t in teams):
        return []
    # These pairs include the nonadjacent IDs 1854/1871, missed by the
    # generic adjacent-pair diagnostic.
    factor_pairs = (
        (1809, 1810), (1812, 1813), (1815, 1816),
        (1854, 1871), (1873, 1874), (1880, 1881),
    )
    needle = section_filter.casefold().strip()
    buckets: dict[tuple[int, str, str], dict[int, dict[str, Any]]] = {}
    for market in markets:
        section = str(market.get("subevent_name") or "Матч")
        # Only full match goals in root event, or direct full-match yellow
        # cards child; never first-half markets or other child events.
        if section == "Матч":
            if market.get("event_id") != event_id:
                continue
            market_type = "голы"
        elif section.casefold() == "желтые карты":
            if market.get("parent_id") != event_id:
                continue
            market_type = "желтые карты"
        else:
            continue
        if needle and needle not in section.casefold():
            continue
        if market.get("name") is not None or market.get("blocked"):
            continue
        fid = market.get("factor_id")
        param_text = market.get("param_text")
        odds = market.get("odds")
        if fid not in _VERIFIED_TEAM_CORNER_FACTOR_IDS or param_text in ("", None):
            continue
        if not isinstance(odds, (int, float)) or isinstance(odds, bool) or odds <= 1:
            continue
        key = (market["event_id"], market_type, str(param_text))
        buckets.setdefault(key, {})[fid] = market

    hypotheses: list[dict[str, Any]] = []
    for (subevent_id, market_type, threshold), by_id in buckets.items():
        for over_id, under_id in factor_pairs:
            over, under = by_id.get(over_id), by_id.get(under_id)
            if over is None or under is None:
                continue
            team_no, over_direction = _VERIFIED_TEAM_CORNER_FACTOR_IDS[over_id]
            team_no_u, under_direction = _VERIFIED_TEAM_CORNER_FACTOR_IDS[under_id]
            if team_no != team_no_u or (over_direction, under_direction) != ("ИТБ", "ИТМ"):
                continue
            hypotheses.append({
                "event_id": subevent_id,
                "market_section": market_type,
                "hypothesized_team": teams[team_no - 1],
                "hypothesized_team_number": team_no,
                "threshold": threshold,
                "hypothesized_over": {"factor_id": over_id, "odds": over["odds"]},
                "hypothesized_under": {"factor_id": under_id, "odds": under["odds"]},
                "verified_for_this_market": False,
                "evidence": "Only full-match CORNERS IDs were calibrated against Fonbet UI",
            })
    return sorted(hypotheses, key=lambda h: (h["event_id"], h["hypothesized_team_number"], h["hypothesized_over"]["factor_id"]))


def extract_unlabeled_pairs(
    payload: dict[str, Any],
    event_id: int,
    section: str = "",
    offset: int = 0,
    limit: int = 100,
    line_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit unidentified adjacent factor IDs at the same line.

    Returns *candidates* only. An adjacent ID is not proof of an over/under
    pair, nor proof that it is an individual team total. The API deliberately
    does not label the first or second selection as "over", "under", "team 1"
    or "team 2" until metadata from Fonbet confirms it.
    """
    markets: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = extract_match_markets(
            payload, event_id=event_id, offset=cursor, limit=500,
            line_payload=line_payload,
        )
        markets.extend(page["markets"])
        next_cursor = page["next_offset"]
        if next_cursor is None:
            break
        cursor = next_cursor

    needle = section.casefold().strip()
    all_events = list(_iter_events(payload)) + list(_iter_events(line_payload or {}))
    root = next((e for e in all_events if e.get("id") == event_id and e.get("team1") and e.get("team2")), {})
    hypotheses = _team_total_calibration_hypotheses(
        markets, page["event"], event_id, root, section,
    )
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for market in markets:
        # Use only *unidentified* selections with a numeric factor id,
        # a quoted price and an explicit line parameter.
        if market.get("name") is not None:
            continue
        factor_id = market.get("factor_id")
        param_text = market.get("param_text")
        price = market.get("odds")
        if not isinstance(factor_id, int) or param_text in (None, ""):
            continue
        if not isinstance(price, (int, float)) or price <= 1:
            continue
        section_name = market.get("subevent_name") or "Матч"
        if needle and needle not in str(section_name).casefold():
            continue
        buckets.setdefault((market["event_id"], str(param_text)), []).append(market)

    pairs: list[dict[str, Any]] = []
    for (_, param_text), group in buckets.items():
        factors = sorted(group, key=lambda m: m["factor_id"])
        used: set[int] = set()
        for idx in range(len(factors) - 1):
            left = factors[idx]
            right = factors[idx + 1]
            fid = left["factor_id"]
            rid = right["factor_id"]
            if fid in used or rid in used or rid != fid + 1:
                continue
            used.update((fid, rid))
            pairs.append({
                "event_id": left["event_id"],
                "subevent_name": left.get("subevent_name") or "Матч",
                "param_text": param_text,
                "selections": [
                    {"factor_id": fid, "odds": left["odds"]},
                    {"factor_id": rid, "odds": right["odds"]},
                ],
                "verified_market_type": False,
                "verified_team": False,
                "verified_direction": False,
            })
    pairs.sort(key=lambda p: (p["event_id"], p["selections"][0]["factor_id"]))
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    return {
        "event": page["event"],
        "section_filter": section,
        "pair_count": len(pairs),
        "pairs": pairs[offset:offset + limit],
        "calibration_hypothesis_count": len(hypotheses),
        "calibration_hypotheses": hypotheses,
        "calibration_warning": (
            "Goal/card hypotheses are extrapolated from CORNERS screenshots only; "
            "team and over/under direction are NOT confirmed for goals/cards."
        ),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(pairs) else None,
        "warning": (
            "Pairs are unverified factor-ID neighbors. Do not interpret them "
            "as team totals or over/under bets without separate verification."
        ),
    }


def extract_day_specials(
    payload: dict[str, Any],
    query: str = "",
    limit: int = 100,
    include_raw: bool = False,
) -> dict[str, Any]:
    sports_by_id = _sports_by_id(payload)
    catalog = _factor_type_catalog(payload)
    events_by_id = {
        event.get("id"): event
        for event in _iter_events(payload)
        if isinstance(event.get("id"), int)
    }
    base_keywords = [
        "спец",
        "игрок",
        "пенальти",
        "карточ",
        "углов",
        "офсайд",
        "удар",
        "сэйв",
        "гол",
        "тайм",
        "побед",
        "фора",
        "тотал",
    ]
    keywords = [item.casefold() for item in base_keywords]
    query_text = query.casefold().strip()

    results: list[dict[str, Any]] = []
    max_items = max(1, min(limit, 500))
    for event_id, factors in _factor_blocks_by_event(payload).items():
        event = events_by_id.get(event_id, {})
        for factor in factors:
            if not isinstance(factor, dict):
                continue
            market = _normalize_factor(factor, catalog, include_raw=include_raw)
            text = _market_search_text(market).casefold()
            if query_text and query_text not in text:
                continue
            if not query_text and keywords and not any(keyword in text for keyword in keywords):
                continue
            results.append(
                {
                    "event": {
                        "id": event_id,
                        "name": _event_name(event),
                        "sport": _sport_name(sports_by_id, event.get("sportId"), prefer_root=True),
                        "league": _sport_name(sports_by_id, event.get("sportId"), prefer_root=False),
                        "start_time": event.get("startTime"),
                    },
                    "market": market,
                }
            )
            if len(results) >= max_items:
                return {"specials": results, "count": len(results), "limit": max_items}
    return {"specials": results, "count": len(results), "limit": max_items}


def value_check(decimal_odds: float, estimated_probability: float) -> dict[str, float | bool]:
    if decimal_odds <= 1:
        raise ValueError("decimal_odds must be greater than 1")
    if not 0 < estimated_probability < 1:
        raise ValueError("estimated_probability must be between 0 and 1")

    implied_probability = 1 / decimal_odds
    expected_roi = (decimal_odds * estimated_probability) - 1
    return {
        "decimal_odds": decimal_odds,
        "estimated_probability": estimated_probability,
        "implied_probability": implied_probability,
        "edge": estimated_probability - implied_probability,
        "expected_roi": expected_roi,
        "is_value": expected_roi > 0,
    }


def _sports_by_id(payload: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {
        item.get("id"): item
        for item in payload.get("sports", [])
        if isinstance(item, dict) and item.get("id") is not None
    }


def _sport_name(sports_by_id: dict[Any, dict[str, Any]], sport_id: Any, prefer_root: bool) -> str | None:
    sport = sports_by_id.get(sport_id)
    if not sport:
        return None
    if not prefer_root:
        return sport.get("name")

    current = sport
    visited: set[Any] = set()
    while current and current.get("id") not in visited:
        visited.add(current.get("id"))
        if current.get("kind") == "sport":
            return current.get("name")
        parent = sports_by_id.get(current.get("parentId"))
        if not parent:
            break
        current = parent
    return sport.get("name")


def _factor_blocks_by_event(payload: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    blocks: dict[int, list[dict[str, Any]]] = {}
    for block in payload.get("customFactors", []):
        if not isinstance(block, dict):
            continue
        event_id = block.get("e")
        factors = block.get("factors")
        if isinstance(event_id, int) and isinstance(factors, list):
            blocks[event_id] = factors
    return blocks


def _iter_events(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    value = payload.get("events", [])
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _event_name(event: dict[str, Any]) -> str:
    team1 = event.get("team1")
    team2 = event.get("team2")
    if team1 and team2:
        return f"{team1} - {team2}"
    return str(event.get("name") or event.get("caption") or event.get("id") or "unknown")


# Factor IDs confirmed in a historical Fonbet CFX market-cell example,
# including quote names (not inferred from odds or position).
# https://gist.github.com/netwood/eb27a27abe154531fd434795cff2ea3e
# Event subtypes (goals, corners, cards) come from event.parentId and event.name.
_KNOWN_FACTOR_NAMES: dict[int, str] = {
    921: "П1",
    922: "Ничья",
    923: "П2",
    924: "1X",
    925: "X2",
    1571: "12",
    927: "Фора 1",
    928: "Фора 2",
    930: "Тотал больше",
    931: "Тотал меньше",
    # Alternate total markets. Pairs are >P / <P for the same parameter.
    1696: "Тотал больше",
    1697: "Тотал меньше",
    1727: "Тотал больше",
    1728: "Тотал меньше",
    1730: "Тотал больше",
    1731: "Тотал меньше",
    1733: "Тотал больше",
    1734: "Тотал меньше",
    1736: "Тотал больше",
    1737: "Тотал меньше",
    2820: "П1 с форой 0",
    2821: "П2 с форой 0",
    709: "Обе забьют - да",
    710: "Обе забьют - нет",
}


def _known_factor_label(factor_id: Any, param_text: Any) -> str | None:
    if not isinstance(factor_id, int):
        return None
    name = _KNOWN_FACTOR_NAMES.get(factor_id)
    if not name:
        return None
    if param_text not in (None, "") and name in {"Фора 1", "Фора 2", "Тотал больше", "Тотал меньше"}:
        return f"{name} ({param_text})"
    return name
def _factor_type_catalog(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Find labels only in explicit factor/market metadata collections.

    Event and sport objects also contain (id, name), but their IDs belong to
    different namespaces. The former generic scan could falsely assign a
    cricket league name to a football corner-bet factor ID.
    """
    catalog: dict[int, dict[str, Any]] = {}
    for key in ("factorTypes", "marketTypes", "factorNames"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            factor_id = item.get("factorId", item.get("id"))
            name = item.get("name") or item.get("title") or item.get("caption")
            if isinstance(factor_id, int) and isinstance(name, str) and name.strip():
                catalog[factor_id] = item
    return catalog

def _normalize_factor(factor: dict[str, Any], catalog: dict[Any, dict[str, Any]], include_raw: bool = False) -> dict[str, Any]:
    factor_id = factor.get("f")
    meta = catalog.get(factor_id, {})
    known_label = _known_factor_label(factor_id, factor.get("pt"))
    result: dict[str, Any] = {
        "factor_id": factor_id,
        "label": known_label,
        "name": known_label or meta.get("name") or meta.get("title") or meta.get("caption"),
        "group": factor.get("g") or meta.get("group") or meta.get("groupName"),
        "odds": factor.get("v"),
        "param": factor.get("p"),
        "param_text": factor.get("pt"),
        "score": factor.get("s"),
        "blocked": factor.get("b"),
    }
    if include_raw:
        result["raw"] = factor
        if meta:
            result["metadata"] = meta
    return result


def _market_search_text(market: dict[str, Any]) -> str:
    values = [
        market.get("label"),
        market.get("name"),
        market.get("group"),
        market.get("param"),
        market.get("param_text"),
        market.get("factor_id"),
    ]
    return " ".join(str(value) for value in values if value is not None)
