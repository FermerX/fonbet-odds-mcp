from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from fonbet_mcp import server
from fonbet_mcp.candidates import _page, search_candidates
from fonbet_mcp.client import FonbetClient, RequestPolicy, Snapshot
from fonbet_mcp.search_state import Quote


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
        self.assertEqual(result["root_events"], 6)
        self.assertEqual(result["eligible_matches"], 1)
        self.assertEqual(result["skipped_events"], 5)
        self.assertEqual(result["matches_checked"], 1)
        self.assertEqual(result["matches_completed"], 1)
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
        self.assertTrue(result["partial"])
        self.assertEqual(result["invalid_market_rows"], 1)
        self.assertEqual(result["conflicting_markets"], 1)

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
        self.assertEqual(result["eligible_matches"], 3)
        self.assertEqual(result["matches_checked"], 3)
        self.assertEqual(result["matches_completed"], 1)
        self.assertEqual(result["skipped_events"], 2)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["errors"][0]["http_status"], 503)
        self.assertEqual(result["errors"][1]["code"], "invalid_response")
        self.assertEqual(result["total_candidates"], 1)

    async def test_conflicting_semantic_aliases_are_excluded_before_filters(self):
        self.details[10] = details(self.match, block(
            10, factor(930, 1.5, "2.5"), factor(1727, 1.7, "2,5"),
            factor(931, 1.5, "2.5"), factor(930, 1.6, "2.50"),
        ))
        client = self.client()
        for minimum, maximum, expected in ((1.4, 1.6, 1), (1.6, 1.8, 0)):
            result = await self.search(client, min_odds=minimum, max_odds=maximum)
            self.assertEqual(result["total_candidates"], expected)
            self.assertTrue(all(row["side"] == "under" for row in result["candidates"]))
            self.assertEqual(result["conflicting_markets"], 1)
            self.assertEqual(result["errors"][0]["code"], "conflicting_quotes")
            self.assertEqual(result["markets_processed"], 4)

    async def test_calibrated_threshold_spelling_and_zero_handicap_alias(self):
        self.details[10] = details(self.match, block(
            10, factor(1812, 1.5, "1.0"), factor(1812, 1.5, "1"),
            factor(927, 1.5, "-0"), factor(2820, 1.6, "0,0"),
        ))
        result = await self.search()
        self.assertEqual([row["factor_id"] for row in result["candidates"]], [1812])
        self.assertEqual(result["conflicting_markets"], 1)
        self.assertEqual(result["invalid_market_rows"], 0)

    async def test_unsupported_text_parameters_are_not_malformed_total_rows(self):
        self.details[10] = details(self.match, block(
            10, factor(1888, 10, "1:0"), factor(589, 5, "1 и менее"), factor(921),
        ))
        result = await self.search()
        self.assertFalse(result["partial"])
        self.assertEqual((result["markets_processed"], result["invalid_market_rows"]), (3, 0))
        self.assertEqual([row["factor_id"] for row in result["candidates"]], [921])

    async def test_malformed_lines_return_structured_error(self):
        for broken in ({"events": [self.match], "sports": []},
                       {"events": [self.match], "sports": [{"id": []}]},
                       {**self.line, "events": [{**self.match, "sportId": []}]},
                       {**self.line, "events": [self.match, {**self.match, "place": "live"}]},
                       {**self.line, "eventBlocks": [{"eventId": 10, "state": "partial", "factors": [{}]}]}):
            with self.subTest(broken=broken):
                self.line = broken
                result = await self.search()
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["errors"][0]["code"], "invalid_response")
                self.assertEqual(result["matches_checked"], 0)

    async def test_malformed_details_preserve_other_matches_and_valid_rows(self):
        other = fixture(11, self.now + 1500)
        self.line["events"].extend([other, self.match])
        self.details[10] = details(self.match, block(
            10, None, factor(930, 1.5, "1e9999"), factor(921, 10 ** 400), factor(923),
        ), {"e": 10, "factors": None})
        self.details[11] = details({**other, "parentId": []}, block(11, factor(921)))
        result = await self.search()
        self.assertEqual((result["root_events"], result["eligible_matches"]), (2, 2))
        self.assertEqual((result["matches_checked"], result["matches_completed"]), (2, 1))
        self.assertEqual((result["skipped_events"], result["markets_processed"]), (1, 4))
        self.assertEqual(result["invalid_market_rows"], 3)
        self.assertEqual([row["factor_id"] for row in result["candidates"]], [923])
        self.assertEqual({issue["code"] for issue in result["errors"]},
                         {"incomplete_detail", "invalid_market_rows", "invalid_response"})

    async def test_horizon_limits_are_checked_before_fetching(self):
        for invalid in (0, -1, 12.1, float("inf"), float("nan")):
            with self.subTest(hours=invalid), self.assertRaisesRegex(ValueError, "at most 12"):
                await self.search(hours_ahead=invalid)
        self.assertEqual(self.requests, [])
        self.assertEqual((await self.search(hours_ahead=12))["status"], "ok")

    async def test_identical_searches_join_and_survive_waiter_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if "eventId" not in request.url.params:
                started.set()
                await release.wait()
            return await self.http(request)

        client = self.client(handler, max_concurrent_searches=1)
        first = asyncio.create_task(self.search(client))
        await asyncio.wait_for(started.wait(), 1)
        joined = asyncio.create_task(self.search(client, limit=1))
        await asyncio.sleep(0)
        overloaded = await self.search(client, hours_ahead=3)
        self.assertEqual(overloaded["status"], "error")
        self.assertEqual(overloaded["errors"][0]["code"], "search_capacity")
        self.assertEqual(overloaded["matches_checked"], 0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        fresh = await joined
        self.assertEqual(fresh["status"], "ok")
        self.assertEqual(fresh["diagnostics"]["search_coalesced"], 1)
        self.assertEqual(fresh["diagnostics"]["http_requests"], 0)
        cached = await self.search(client)
        self.assertEqual(cached["search_id"], fresh["search_id"])
        self.assertEqual(cached["diagnostics"]["result_cache_hits"], 1)
        self.assertEqual(self.requests, [(None, "ru"), (10, "ru")])

    async def test_detail_queue_rejection_does_not_inflate_checked_count(self):
        other = fixture(11, self.now + 1500)
        self.line["events"].append(other)
        self.details[11] = details(other, block(11, factor(921)))
        result = await self.search(self.client(max_inflight_requests=1, max_concurrency=2))
        self.assertEqual((result["eligible_matches"], result["matches_checked"]), (2, 1))
        self.assertEqual((result["matches_completed"], result["skipped_events"]), (1, 1))
        self.assertEqual(result["errors"][0]["code"], "request_capacity")

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
        self.assertEqual(result["remaining_matches"], 1)
        self.assertEqual(result["total_candidates"], 1)
        self.assertEqual(result["skipped_events"], 1)
        self.assertEqual(result["errors"][0]["code"], "scan_deadline")

    async def test_current_revision_uses_both_effective_ages_at_the_snapshot_limit(self):
        client = self.client()
        loop = asyncio.get_running_loop()
        tick = 1000.0
        receipt = datetime.now(timezone.utc)
        state = client.searches.create((4, 1.4, 1.6, frozenset(), "ru"), self.now)

        for line_age, detail_age, expected in (
            (59.9, 59.9, "snapshot"),
            (60.0, 59.9, "stale"),
            (60.1, 59.0, "stale"),
            (59.0, 60.1, "stale"),
            (60.1, 60.2, "stale"),
        ):
            with self.subTest(line_age=line_age, detail_age=detail_age):
                line = Snapshot({}, receipt, tick - line_age + 5, upstream_age=5)
                detail = Snapshot({}, receipt, tick - detail_age + 7, upstream_age=7)
                state.rows[:] = [Quote(
                    {"event_id": 10, "market_event_id": 10, "factor_id": 921,
                     "threshold": None, "odds": 1.5, "received_at": receipt.isoformat()},
                    self.match["startTime"], detail.received_tick, detail.effective_tick,
                    line.received_tick, line.effective_tick, state.revision + 1,
                )]
                state.publish({"errors": [], "status": "ok", "partial": False, "scan_diagnostics": {}})
                with patch.object(loop, "time", return_value=tick):
                    result = await _page(
                        client, state, state.revisions[state.revision], state.revision,
                        limit=1, offset=0, own_scan=False, joined=True, refresh=False,
                    )
                quote = result["candidates"][0]
                self.assertEqual(quote["quote_status"], expected)
                self.assertEqual(quote["odds"], 1.5 if expected == "snapshot" else None)
                self.assertEqual(quote["received_at"], receipt.isoformat())
                self.assertEqual(result["partial"], expected == "stale")

    async def test_deadline_stales_parallel_first_pages_and_preserves_resume_and_refresh(self):
        other = fixture(11, self.now + 1200)
        self.line["events"].append(other)
        self.details[10] = details(self.match, block(10, factor(921), factor(923)))
        self.details[11] = details(other, block(11, factor(921)))
        slow_started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            event_id = request.url.params.get("eventId")
            if event_id == "11":
                slow_started.set()
                await release.wait()
            response = await self.http(request)
            if event_id is None and self.requests.count((None, "ru")) == 1:
                response.headers["Age"] = "5"
            return response

        client = self.client(handler, max_concurrency=1)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        advance = 0
        with (
            patch.object(loop, "time", new=lambda: real_time() + advance),
            patch.object(loop, "slow_callback_duration", float("inf")),
        ):
            owner = asyncio.create_task(self.search(client, limit=1))
            await asyncio.wait_for(slow_started.wait(), 1)
            waiter = asyncio.create_task(self.search(client, limit=1))
            await asyncio.sleep(0)
            advance = 55
            first, joined = await asyncio.gather(owner, waiter)

            self.assertEqual(first["search_id"], joined["search_id"])
            self.assertEqual(joined["diagnostics"]["search_coalesced"], 1)
            self.assertEqual(first["search_status"], "paused")
            self.assertEqual(first["remaining_matches"], 1)
            for page in (first, joined):
                self.assertEqual({error["code"] for error in page["errors"]}, {"scan_deadline", "quote_stale"})
                self.assertEqual(page["candidates"][0]["quote_status"], "stale")
                self.assertIsNone(page["candidates"][0]["odds"])
                self.assertLess(page["candidates"][0]["quote_age_seconds"], 60)

            cursor = first["next_cursor"]
            old = await self.search(client, cursor=cursor)
            release.set()
            resumed = await self.search(client, search_id=first["search_id"], resume=True)
            self.assertEqual(resumed["search_id"], first["search_id"])
            self.assertEqual(resumed["revision"], first["revision"] + 1)
            self.assertEqual(resumed["window_end"], first["window_end"])
            self.assertEqual(resumed["search_status"], "complete")
            self.assertEqual(resumed["remaining_matches"], 0)
            self.assertEqual(resumed["matches_completed"], 2)
            self.assertIsNone(resumed["candidates"][0]["odds"])
            self.assertEqual(resumed["candidates"][-1]["quote_status"], "snapshot")
            self.assertEqual(resumed["candidates"][-1]["odds"], 1.5)
            self.assertEqual(self.requests, [(None, "ru"), (10, "ru"), (11, "ru"), (None, "ru")])

            refreshed = await self.search(client, cursor=cursor, refresh=True)
            self.assertEqual(refreshed["revision"], old["revision"])
            self.assertEqual(refreshed["remaining_matches"], 1)
            quote, stored = refreshed["candidates"][0], old["candidates"][0]
            self.assertEqual(quote["factor_id"], stored["factor_id"])
            self.assertEqual(quote["received_at"], stored["received_at"])
            self.assertEqual(quote["quote_status"], "verified")
            self.assertEqual(quote["odds"], 1.5)
            self.assertGreater(quote["verified_at"], stored["received_at"])
            self.assertEqual(self.requests[-1], (10, "ru"))
            self.assertEqual(self.requests.count((11, "ru")), 1)

    async def test_refresh_suppresses_odds_if_either_source_returns_an_expired_age(self):
        expired_source = None

        async def handler(request):
            response = await self.http(request)
            is_line = request.url.params.get("eventId") is None
            if expired_source == ("line" if is_line else "detail"):
                response.headers["Age"] = "60"
            return response

        client = self.client(handler, line_ttl=0, event_ttl=0)
        first = await self.search(client)
        self.assertEqual(first["candidates"][0]["odds"], 1.5)

        for expired_source, expected_error in (("line", "refresh_deadline"), ("detail", "stale_snapshot")):
            with self.subTest(expired_source=expired_source):
                refreshed = await self.search(client, search_id=first["search_id"], refresh=True)
                self.assertIsNone(refreshed["candidates"][0]["odds"])
                self.assertNotIn(refreshed["candidates"][0]["quote_status"], ("snapshot", "verified"))
                self.assertIn(expected_error, {error["code"] for error in refreshed["errors"]})

    async def test_cursor_and_offset_reuse_frozen_results_with_no_http(self):
        self.details[10] = details(self.match, block(10, factor(930, threshold="2.5"), factor(931, threshold="2.5")))
        client = self.client()
        first = await self.search(client, limit=1)
        page = await self.search(client, cursor=first["next_cursor"])
        offset = await self.search(client, search_id=first["search_id"], offset=1)
        self.assertEqual(page["search_id"], first["search_id"])
        self.assertEqual(page["window_start"], first["window_start"])
        self.assertEqual(page["window_end"], first["window_end"])
        self.assertEqual(page["revision"], first["revision"])
        self.assertEqual(first["matches_completed"], page["matches_completed"])
        self.assertEqual(first["diagnostics"]["http_requests"], 2)
        self.assertEqual(first["diagnostics"]["cache_misses"], 2)
        self.assertEqual(page["diagnostics"]["http_requests"], 0)
        self.assertEqual(page["diagnostics"]["result_cache_hits"], 1)
        self.assertEqual(page["candidates"][0]["factor_id"], 931)
        self.assertEqual(offset["candidates"][0]["factor_id"], 931)
        self.assertEqual(first["candidates"][0]["received_at"], page["candidates"][0]["received_at"])
        self.assertEqual(len(self.requests), 2)

    async def test_expired_prices_and_page_refresh_for_changed_and_blocked_quotes(self):
        self.details[10] = details(self.match, block(10, factor(921), factor(923), factor(924)))
        other = fixture(11, self.now + 1200)
        self.line["events"].append(other)
        self.details[11] = details(other, block(11, factor(921)))
        client = self.client(line_ttl=0.08, event_ttl=0.08)
        initial = await self.search(client, limit=3)
        stamp = initial["candidates"][0]["received_at"]
        await asyncio.sleep(0.10)
        self.details[10] = details(self.match, block(10, factor(921, 1.55), factor(923, b=True), factor(924)))
        stale = await self.search(client, search_id=initial["search_id"], limit=3)
        self.assertEqual(stale["quote_status_counts"], {"stale": 3})
        self.assertTrue(all(row["received_at"] == stamp and row["odds"] is None for row in stale["candidates"]))
        self.assertEqual(stale["diagnostics"]["http_requests"], 0)
        refreshed = await self.search(client, search_id=initial["search_id"], limit=3, refresh=True)
        self.assertEqual(refreshed["quote_status_counts"], {"changed": 1, "unavailable": 1, "verified": 1})
        self.assertEqual([row["factor_id"] for row in refreshed["candidates"]], [921, 923, 924])
        self.assertEqual([row["odds"] for row in refreshed["candidates"]], [None, None, 1.5])
        self.assertTrue(all(row["received_at"] == stamp for row in refreshed["candidates"]))
        self.assertGreater(refreshed["candidates"][2]["verified_at"], stamp)
        self.assertEqual(refreshed["diagnostics"]["http_requests"], 2)
        self.assertEqual(self.requests.count((11, "ru")), 1)

    async def test_newer_detail_invalidates_old_quote_even_after_http_cache_eviction(self):
        client = self.client(event_cache_size=1)
        initial = await self.search(client)
        self.details[10] = details(self.match, block(10, factor(930, 1.55, "2.5")))
        client._event_cache.clear()
        await client.event_view_snapshot(10)
        other = fixture(11, self.now + 1200)
        self.details[11] = details(other, block(11, factor(921)))
        await client.event_view_snapshot(11)
        page = await self.search(client, search_id=initial["search_id"])
        self.assertEqual(page["quote_status_counts"], {"superseded": 1})
        self.assertIsNone(page["candidates"][0]["odds"])

    async def test_resume_keeps_old_cursor_and_only_checks_remaining_matches(self):
        older = self.match
        late1 = fixture(11, self.now + 1200)
        late2 = fixture(12, self.now + 1500)
        self.line["events"].extend([late1, late2])
        self.line["customFactors"] = [block(11, factor(921)), block(12, factor(921))]
        for event in (older, late1, late2):
            self.details[event["id"]] = details(event, block(event["id"], factor(921)))
        attempted = []
        gate = asyncio.Event()

        async def handler(request):
            event_id = request.url.params.get("eventId")
            if event_id:
                attempted.append(int(event_id))
                if event_id == "10":
                    await gate.wait()
            return await self.http(request)

        client = self.client(handler, max_snapshot_age=0.12, line_ttl=0.05, max_concurrency=1)
        first = await self.search(client, limit=1)
        self.assertEqual(first["search_status"], "paused")
        self.assertEqual(first["remaining_matches"], 1)
        self.assertEqual(first["matches_completed"], 2)
        self.assertEqual(attempted, [11, 12, 10])
        self.assertEqual(first["candidates"][0]["event_id"], 11)
        cursor = first["next_cursor"]
        previous = await self.search(client, cursor=cursor)
        gate.set()
        resumed = await self.search(client, search_id=first["search_id"], resume=True, limit=1)
        old_page = await self.search(client, cursor=cursor)
        self.assertEqual(resumed["remaining_matches"], 0)
        self.assertEqual(resumed["matches_completed"], 3)
        self.assertEqual(resumed["candidates"][0]["event_id"], 10)
        self.assertEqual(resumed["window_end"], first["window_end"])
        self.assertGreater(resumed["revision"], first["revision"])
        self.assertEqual(old_page["remaining_matches"], 1)
        self.assertEqual(old_page["candidates"][0]["event_id"], 12)
        self.assertEqual(previous["candidates"][0]["received_at"], old_page["candidates"][0]["received_at"])
        self.assertEqual(attempted, [11, 12, 10])

    async def test_search_store_ttl_and_bounded_result_counts(self):
        self.details[10] = details(self.match, block(10, factor(921), factor(923)))
        client = self.client(search_cache_size=1, search_max_candidates=1, search_state_ttl=0.1)
        first = await self.search(client)
        self.assertEqual(first["total_candidates"], 1)
        self.assertIn("result_capacity", {entry["code"] for entry in first["errors"]})
        second = await self.search(client, hours_ahead=3)
        self.assertEqual(len(client.searches.states), 1)
        evicted = await self.search(client, search_id=first["search_id"])
        self.assertEqual(evicted["search_status"], "expired")
        await asyncio.sleep(0.12)
        expired = await self.search(client, search_id=second["search_id"])
        self.assertEqual(expired["search_status"], "expired")
        self.assertEqual(len(client.searches.states), 0)

    async def test_transport_metrics_include_actual_retry_and_rate_wait(self):
        detail_attempts = 0

        async def handler(request):
            nonlocal detail_attempts
            if request.url.params.get("eventId") == "10":
                detail_attempts += 1
                if detail_attempts == 1:
                    return httpx.Response(503)
            return await self.http(request)

        client = self.client(handler, retries=1, max_retry_delay=0, requests_per_second=50)
        response = await self.search(client)
        metrics = response["diagnostics"]
        self.assertEqual(metrics["http_requests"], 3)
        self.assertEqual(metrics["cache_misses"], 2)
        self.assertEqual(metrics["retries"], 1)
        self.assertGreater(metrics["queue_seconds"], 0)
        self.assertGreater(metrics["network_seconds"], 0)
        self.assertGreater(metrics["processing_seconds"], 0)

    async def test_malformed_preview_hints_do_not_suppress_a_fixture(self):
        self.line["customFactors"] = None
        result = await self.search()
        self.assertEqual(result["matches_completed"], 1)
        self.assertEqual(result["total_candidates"], 1)

    async def test_mocked_load_80_150_and_300_matches(self):
        for size in (80, 150, 300):
            with self.subTest(matches=size):
                events = [fixture(eid, self.now + 900 + eid) for eid in range(1, size + 1)]
                self.line["events"] = events
                self.details = {
                    event["id"]: details(event, block(event["id"], factor(921))) for event in events
                }
                self.requests.clear()
                client = self.client()
                first = await self.search(client, limit=3)
                page = await self.search(client, cursor=first["next_cursor"], limit=3)
                self.assertEqual(first["matches_completed"], size)
                self.assertEqual(first["remaining_matches"], 0)
                self.assertEqual(first["total_candidates"], size)
                self.assertEqual(first["diagnostics"]["http_requests"], size + 1)
                self.assertEqual(page["diagnostics"]["http_requests"], 0)
                self.assertEqual([item["event_id"] for item in page["candidates"]], [4, 5, 6])
                self.assertEqual(len(self.requests), size + 1)

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
