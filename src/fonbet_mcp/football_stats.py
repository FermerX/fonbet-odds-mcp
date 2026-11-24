"""Independent, read-only team form from football-data.org API v4.

No bookmaker odds are used. Provider team IDs must be supplied explicitly:
Fonbet event IDs are NOT football-data.org team IDs. The data cannot by
itself justify an estimated probability or a betting recommendation.
"""
from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

BASE_URL = "https://api.football-data.org/v4"
MAX_PROVIDER_MATCHES = 100
LOOKBACK_DAYS = 400


def _parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, str):
        raise ValueError("as_of_utc must be a timezone-aware ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of_utc must be a timezone-aware ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of_utc must include a timezone offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError("Future as_of_utc is not supported")
    return parsed


def _pct(count: int, total: int) -> float | None:
    return round(100 * count / total, 2) if total else None


def _aggregate(games: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(games)
    wins = sum(g["goals_for"] > g["goals_against"] for g in games)
    draws = sum(g["goals_for"] == g["goals_against"] for g in games)
    goals_for = sum(g["goals_for"] for g in games)
    goals_against = sum(g["goals_against"] for g in games)
    over = sum(g["goals_for"] + g["goals_against"] >= 3 for g in games)
    btts = sum(g["goals_for"] > 0 and g["goals_against"] > 0 for g in games)
    return {
        "played": n,
        "wins": wins,
        "draws": draws,
        "losses": n - wins - draws,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goals_for_per_match": round(goals_for / n, 3) if n else None,
        "goals_against_per_match": round(goals_against / n, 3) if n else None,
        "win_percent": _pct(wins, n),
        "over_2_5_percent": _pct(over, n),
        "both_teams_score_percent": _pct(btts, n),
    }


def summarize_form(
    payload: dict[str, Any],
    team_id: int,
    last_n: int,
    as_of: datetime,
) -> dict[str, Any]:
    """Use only completed matches strictly before as_of; fail closed on missing scores."""
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise ValueError("Provider response is missing a matches list")
    games: list[dict[str, Any]] = []
    invalid = 0
    seen_ids: set[int] = set()
    for match in raw_matches:
        if not isinstance(match, dict) or match.get("status") != "FINISHED":
            continue
        match_id = match.get("id")
        if type(match_id) is not int or match_id in seen_ids:
            invalid += 1
            continue
        try:
            played = datetime.fromisoformat(str(match["utcDate"]).replace("Z", "+00:00"))
            if played.tzinfo is None:
                raise ValueError("Naive kickoff")
            played = played.astimezone(timezone.utc)
            # A historical FINISHED flag reflects the current API state, not
            # the match status at an earlier as_of. Apply a conservative
            # four-hour embargo to avoid leaking in-progress/final scores into
            # pre-match backtests (including matches with extra time).
            if played + timedelta(hours=4) >= as_of:
                continue
            home = match["homeTeam"]
            away = match["awayTeam"]
            full_time = match["score"]["fullTime"]
            gh, ga = full_time["home"], full_time["away"]
            if type(gh) is not int or type(ga) is not int or min(gh, ga) < 0:
                raise ValueError("Invalid score")
            if home["id"] == team_id and away["id"] != team_id:
                venue, opponent = "home", away
                scored, conceded = gh, ga
            elif away["id"] == team_id and home["id"] != team_id:
                venue, opponent = "away", home
                scored, conceded = ga, gh
            else:
                raise ValueError("Team mismatch")
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid += 1
            continue
        seen_ids.add(match_id)
        competition = match.get("competition") or {}
        games.append({
            "match_id": match_id,
            "kickoff_utc": played.isoformat(),
            "venue": venue,
            "opponent_id": opponent["id"],
            "opponent_name": opponent.get("name") or opponent.get("shortName"),
            "competition": competition.get("name") if isinstance(competition, dict) else None,
            "goals_for": scored,
            "goals_against": conceded,
        })
    games.sort(key=lambda game: (game["kickoff_utc"], game["match_id"]), reverse=True)
    recent = games[:last_n]
    return {
        "status": "ok" if len(recent) == last_n else "insufficient_data",
        "provider": "football-data.org",
        "provider_api_version": "v4",
        "provider_team_id": team_id,
        "as_of_utc": as_of.isoformat(),
        "requested_matches": last_n,
        "available_completed_matches": len(games),
        "invalid_or_inconsistent_rows": invalid,
        "possible_provider_truncation": len(raw_matches) >= MAX_PROVIDER_MATCHES,
        "summary": _aggregate(recent),
        "home": _aggregate([g for g in recent if g["venue"] == "home"]),
        "away": _aggregate([g for g in recent if g["venue"] == "away"]),
        "matches": recent,
        "limitations": [
            "Only competitions and fixtures available to this API token are included.",
            "The listed matches are filtered to kickoff strictly before as_of_utc.",
            "Full-time scores are provider-reported, not independently settled bets.",
            "No xG, injuries, referees, weather, or model-estimated probabilities are supplied.",
            "A small or incomplete sample must not be interpreted as proof of future results.",
        ],
    }


class FootballDataClient:
    """Single-flight rate-limited provider client; one read-only request per cache miss."""

    def __init__(
        self,
        token: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval_seconds: float = 6.5,
        cache_ttl_seconds: float = 600,
    ) -> None:
        if not math.isfinite(min_interval_seconds) or min_interval_seconds < 0:
            raise ValueError("Invalid provider request interval")
        if not math.isfinite(cache_ttl_seconds) or cache_ttl_seconds < 0:
            raise ValueError("Invalid provider cache TTL")
        self._token = (token or "").strip()
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=12,
            transport=transport,
            headers={"X-Auth-Token": self._token} if self._token else {},
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[int, str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self._min_interval = min_interval_seconds
        self._cache_ttl = cache_ttl_seconds
        self._next_request = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def team_form(
        self, team_id: int, last_n: int = 10, as_of_utc: str | None = None,
    ) -> dict[str, Any]:
        if type(team_id) is not int or team_id <= 0:
            return {"status": "invalid_request", "message": "team_id must be a positive provider team ID"}
        if type(last_n) is not int or not 1 <= last_n <= 20:
            return {"status": "invalid_request", "message": "last_n must be between 1 and 20"}
        try:
            as_of = _parse_as_of(as_of_utc)
        except ValueError as exc:
            return {"status": "invalid_request", "message": str(exc)}
        if not self._token:
            return {
                "status": "not_configured",
                "provider": "football-data.org",
                "message": "Configure FOOTBALL_DATA_API_TOKEN on the server. Do not send the key in chat.",
            }
        start = (as_of.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        # dateTo can be exclusive on this API; choose the following UTC date.
        end = (as_of.date() + timedelta(days=1)).isoformat()
        key = (team_id, start, end)
        clock = asyncio.get_running_loop()
        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None and clock.time() - entry[0] < self._cache_ttl:
                self._cache.move_to_end(key)
                fetched_at, payload = entry[1]["fetched_at"], entry[1]["payload"]
            else:
                delay = self._next_request - clock.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_request = clock.time() + self._min_interval
                try:
                    response = await self._http.get(
                        f"/teams/{team_id}/matches",
                        params={"status": "FINISHED", "dateFrom": start,
                                "dateTo": end, "limit": MAX_PROVIDER_MATCHES},
                    )
                except (httpx.RequestError, httpx.TimeoutException):
                    return {"status": "provider_unavailable", "provider": "football-data.org"}
                if response.status_code == 429:
                    return {"status": "provider_rate_limited", "provider": "football-data.org",
                            "retry_after": response.headers.get("Retry-After")}
                if response.status_code == 403:
                    return {"status": "provider_access_denied", "provider": "football-data.org"}
                if response.status_code == 401:
                    return {"status": "provider_auth_failed", "provider": "football-data.org"}
                if response.status_code != 200:
                    return {"status": "provider_http_error", "provider": "football-data.org",
                            "http_status": response.status_code}
                try:
                    payload = response.json()
                except ValueError:
                    return {"status": "provider_invalid_response", "provider": "football-data.org"}
                if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
                    return {"status": "provider_invalid_response", "provider": "football-data.org"}
                fetched_at = datetime.now(timezone.utc).isoformat()
                self._cache[key] = (clock.time(), {"fetched_at": fetched_at, "payload": payload})
                if len(self._cache) > 128:
                    self._cache.popitem(last=False)
        try:
            result = summarize_form(payload, team_id, last_n, as_of)
        except ValueError:
            return {"status": "provider_invalid_response", "provider": "football-data.org"}
        result["fetched_at_utc"] = fetched_at
        result["window_from_utc_date"] = start
        result["window_to_exclusive_utc_date"] = end
        return result
