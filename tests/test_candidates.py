from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from fonbet_mcp import server
from fonbet_mcp.candidates import search_candidates
from fonbet_mcp.client import FonbetClient, RequestPolicy


SPORTS = [
    {"id": 1, "kind": "sport", "name": "Футбол"},
    {"id": 2, "parentId": 1, "name": "Кубок"},
    {"id": 3, "kind": "sport", "name": "Хоккей"},
]


def fixture(event_id, start, **overrides):
    return {"id": event_id, "team1": "Альфа", "team2": "Бета", "sportId": 2,
            "place": "line", "startTime": start, **overrides}


def factor(factor_id, odds=1.5, threshold=None, **flags):
    return {"f": factor_id, "v": odds, "pt": threshold, **flags}


def block(event_id, *factors):
    return {"e": event_id, "factors": list(factors)}


def details(event, *blocks, subevents=()):
    return {"events": [event, *subevents], "customFactors": list(blocks)}


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = int(time.time())
        self.match = fixture(10, self.now + 900)
        self.line = {"events": [self.match], "sports": SPORTS, "customFactors": []}
        self.details = {10: details(self.match, block(10, factor(930, threshold="2.5")))}
        self.requests = []
        self.localized_line = None

    async def http(self, request):
        event_id = request.url.params.get("eventId")
        lang = request.url.params["lang"]
        key = int(event_id) if event_id is not None else None
        self.requests.append((key, lang))
        if key is None:
            response = self.localized_line if lang != "ru" else self.line
        else:
            response = self.details.get(key)
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)

    def client(self, handler=None, **policy):
        options = {"requests_per_second": 10000, "retries": 0, **policy}
        client = FonbetClient(
            "https://fonbet.test", policy=RequestPolicy(**options),
            transport=httpx.MockTransport(handler or self.http),
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def search(self, client=None, **overrides):
        options = dict(hours_ahead=4, min_odds=1.4, max_odds=1.6, markets=None,
                       limit=50, offset=0, lang="ru")
        options.update(overrides)
        return await search_candidates(client or self.client(), **options)

    async def test_only_upcoming_root_football_matches_within_window(self):
        self.line["events"].extend([
            fixture(11, self.now - 1), fixture(12, self.now + 20000),
            {**fixture(13, self.now + 600), "place": "live"},
            {**fixture(14, self.now + 600), "sportId": 3},
            {"id": 15, "parentId": 10, "name": "угловые"},
            {**fixture(16, self.now + 600), "state": "cancelled"},
        ])
        result = await self.search()
        self.assertEqual(self.requests, [(None, "ru"), (10, "ru")])
        self.assertEqual(result["skipped_events"], 5)
        self.assertEqual(result["matches_checked"], 1)
        self.assertEqual(result["candidates"][0]["event_id"], 10)

    async def test_inclusive_odds_and_valid_prices(self):
        self.details[10] = details(self.match, block(
            10, factor(921, 1.4), factor(922, 1.39), factor(923, 1.6),
            factor(924, 1.61), factor(925, True), factor(1571, "1.5"),
        ))
        result = await self.search()
        self.assertEqual([row["odds"] for row in result["candidates"]], [1.4, 1.6])
        self.assertEqual(result["markets_processed"], 6)
        self.assertEqual(result["candidates"][0]["team"], "Альфа")
        self.assertNotIn("probability", result["candidates"][0])

    async def test_categories_halves_and_supported_statistics(self):
        subevents = [
            {"id": 21, "parentId": 10, "name": "фолы"},
            {"id": 22, "parentId": 21, "name": "1-й тайм фолы"},
            {"id": 23, "parentId": 10, "name": "угловые"},
            {"id": 24, "parentId": 10, "name": "желтые карты"},
        ]
        self.line["events"].extend(subevents)  # Detail price blocks can omit the child objects.
        self.details[10] = details(
            self.match,
            block(10, factor(921), factor(924), factor(927, threshold="-1"),
                  factor(709), factor(2821), factor(1812, threshold="1")),
            block(21, factor(930, threshold="25.5")),
            block(22, factor(931, threshold="12.5")),
            block(23, factor(930, threshold="8.5")),
            block(24, factor(1809, threshold="1.5")),
        )
        client = self.client()
        result = await self.search(client)
        entries = {(row["statistic"], row["period"], row["category"]) for row in result["candidates"]}
        self.assertEqual(entries, {
            ("goals", "full_time", "outcomes"), ("goals", "full_time", "double_chance"),
            ("goals", "full_time", "handicaps"), ("goals", "full_time", "both_teams_score"),
            ("goals", "full_time", "individual_totals"), ("fouls", "full_time", "totals"),
            ("fouls", "first_half", "totals"), ("corners", "full_time", "totals"),
            ("yellow_cards", "full_time", "individual_totals"),
        })
        handicap = next(row for row in result["candidates"] if row["factor_id"] == 2821)
        self.assertEqual((handicap["threshold"], handicap["side"], handicap["team"]), ("0", "team2", "Бета"))
        filtered = await self.search(client, markets=["halves", "double_chance"])
        self.assertEqual({row["factor_id"] for row in filtered["candidates"]}, {931, 924})
        with self.assertRaisesRegex(ValueError, "Unsupported markets"):
            await self.search(client, markets=["unknown"])

    async def test_excludes_unavailable_unknown_and_schedule_only_markets(self):
        self.line["customFactors"] = [block(10, factor(921))]
        self.details[10] = details(
            self.match,
            block(10, factor(9999), factor(709, b=True), factor(710), factor(710, b=True),
                  factor(930, threshold="Inf"), factor(931, threshold="-1"),
                  factor(921, expired=True), factor(922, stale=True),
                  factor(923), factor(923, 1.55)),
            block(30, factor(930, threshold="8.5")),
            block(31, factor(930, threshold="4.5")),
            subevents=[
                {"id": 30, "parentId": 10, "name": "угловые", "place": "live"},
                {"id": 31, "parentId": 30, "name": "1-й тайм угловые"},
            ],
        )
        self.details[10]["factorTypes"] = [{"factorId": 9999, "name": "П1"}]
        result = await self.search()
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["partial"])

    async def test_event_block_index_filters_whole_events_and_individual_factors(self):
        self.line["events"].append(fixture(11, self.now + 1200))
        self.line["eventBlocks"] = [
            {"eventId": 11, "state": "blocked"},
            {"eventId": 10, "state": "partial", "factors": [921]},
        ]
        self.details[10] = details(
            self.match,
            block(10, factor(921), factor(923), factor(924)),
            block(31, factor(930, threshold="4.5")),
            subevents=[
                {"id": 30, "parentId": 10, "name": "угловые"},
                {"id": 31, "parentId": 30, "name": "1-й тайм угловые"},
            ],
        )
        self.details[10]["eventBlocks"] = [
            {"eventId": 10, "state": "partial", "factors": [923]},
            {"eventId": 30, "state": "blocked"},
        ]
        result = await self.search()
        self.assertEqual(self.requests, [(None, "ru"), (10, "ru")])
        self.assertEqual([row["factor_id"] for row in result["candidates"]], [924])

    async def test_incomplete_factor_block_flags_partial_and_preserves_available_quotes(self):
        self.details[10]["customFactors"][0]["countAll"] = 2
        result = await self.search()
        self.assertTrue(result["partial"])
        self.assertEqual(result["errors"][0]["code"], "incomplete_detail")
        self.assertEqual(result["total_candidates"], 1)

    async def test_root_switching_to_live_removes_match(self):
        self.details[10]["events"] = [{**self.match, "place": "live"}]
        result = await self.search()
        self.assertEqual((result["matches_checked"], result["skipped_events"]), (1, 1))
        self.assertEqual(result["candidates"], [])

    async def test_nominal_thresholds_semantic_dedup_and_pagination(self):
        self.details[10] = details(
            self.match,
            block(10, factor(930, threshold="2"), factor(930, threshold="2,0"),
                  factor(1727, threshold="2.0"), factor(931, threshold="2.5")),
            block(10, factor(930, threshold="2")),
        )
        client = self.client()
        first = await self.search(client, limit=1)
        second = await self.search(client, limit=1, offset=first["next_offset"])
        self.assertEqual(first["total_candidates"], 2)
        self.assertEqual(first["markets_processed"], 5)
        self.assertEqual(first["candidates"][0]["threshold"], "2")
        self.assertTrue(first["candidates"][0]["threshold_is_integer"])
        self.assertEqual(second["candidates"][0]["threshold"], "2.5")
        self.assertFalse(second["candidates"][0]["threshold_is_integer"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(first["candidates"][0]["received_at"], second["candidates"][0]["received_at"])

    async def test_empty_line_is_complete(self):
        self.line = {"events": []}
        result = await self.search()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["matches_checked"], 0)
        self.assertEqual(result["candidates"], [])

    async def test_partial_availability_and_error_counts(self):
        for event_id in (11, 12):
            self.line["events"].append(fixture(event_id, self.now + 1800))
        self.details[11] = 503
        self.details[12] = {"events": [self.line["events"][2]]}
        result = await self.search()
        self.assertTrue(result["partial"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches_checked"], 3)
        self.assertEqual(result["skipped_events"], 2)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["errors"][0]["http_status"], 503)
        self.assertEqual(result["errors"][1]["code"], "invalid_response")
        self.assertEqual(result["total_candidates"], 1)

    async def test_line_failure_is_distinct_from_empty_line(self):
        self.line = 429
        result = await self.search()
        self.assertTrue(result["partial"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"][0]["stage"], "line")

    async def test_upstream_age_excludes_stale_detail_and_preserves_fresh_matches(self):
        self.line["events"].append(fixture(11, self.now + 1200))
        self.details[11] = details(self.line["events"][-1], block(11, factor(921)))

        async def handler(request):
            response = await self.http(request)
            if request.url.params.get("eventId") == "11":
                response.headers["Age"] = "120"
            return response

        result = await self.search(self.client(handler))
        self.assertEqual([row["event_id"] for row in result["candidates"]], [10])
        self.assertEqual(result["skipped_events"], 1)
        self.assertEqual(result["errors"][0]["code"], "stale_snapshot")

    async def test_deadline_returns_completed_matches_and_reports_cutoff(self):
        self.line["events"].append(fixture(11, self.now + 1200))

        async def handler(request):
            if request.url.params.get("eventId") == "11":
                await asyncio.Event().wait()
            return await self.http(request)

        result = await self.search(self.client(handler, max_snapshot_age=0.1))
        self.assertEqual(result["total_candidates"], 1)
        self.assertEqual(result["skipped_events"], 1)
        self.assertEqual(result["errors"][0]["code"], "scan_deadline")

    async def test_localized_team_and_league_names_use_same_match_id(self):
        self.localized_line = {
            "events": [{**self.match, "team1": "Alpha", "team2": "Beta"}],
            "sports": [{**SPORTS[1], "name": "Cup"}],
        }
        result = await self.search(lang="en")
        self.assertIn((None, "en"), self.requests)
        self.assertIn((10, "ru"), self.requests)
        row = result["candidates"][0]
        self.assertEqual((row["team1"], row["team2"], row["league"]), ("Alpha", "Beta", "Cup"))

    async def test_mcp_protocol_preserves_legacy_tools_and_accepts_new_call(self):
        calls = {
            "fonbet_search_events": ({"query": "Альфа"}, None),
            "fonbet_event_odds": ({"event_id": 10}, "markets"),
            "get_event_odds": ({"event_id": 10, "offset": 0}, "next_offset"),
            "get_match_markets": ({"event_id": 10}, "subevents"),
            "get_match_line": ({"query": "Альфа"}, "market_sections"),
            "get_unlabeled_pairs": ({"event_id": 10}, "pairs"),
            "get_special_bets": ({}, "specials"),
            "get_same_game_parlay": ({"event_id": 10}, "markets"),
            "get_cross_match_specials": ({}, "specials"),
            "fonbet_value_check": ({"decimal_odds": 1.5, "estimated_probability": 0.7}, "is_value"),
            "fonbet_find_candidates": ({"markets": ["goals"]}, "candidates"),
        }
        client = self.client()
        with patch.object(server, "client", client):
            async with create_connected_server_and_client_session(server.mcp) as session:
                listing = await session.list_tools()
                self.assertEqual({tool.name for tool in listing.tools}, calls.keys())
                for name, (arguments, field) in calls.items():
                    with self.subTest(tool=name):
                        result = await session.call_tool(name, arguments)
                        self.assertFalse(result.isError, result.content)
                        if field is not None:
                            self.assertIn(field, result.structuredContent)
                        if name == "fonbet_find_candidates":
                            self.assertEqual(result.structuredContent["total_candidates"], 1)
