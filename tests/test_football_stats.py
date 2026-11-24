"""Offline contract tests for the optional football-data.org team-form integration."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from fonbet_mcp.football_stats import FootballDataClient, summarize_form


def game(mid, when, gh, ga, home=10, away=20, status="FINISHED"):
    return {
        "id": mid,
        "utcDate": when.isoformat(),
        "status": status,
        "homeTeam": {"id": home, "name": f"Team {home}"},
        "awayTeam": {"id": away, "name": f"Team {away}"},
        "score": {"fullTime": {"home": gh, "away": ga}},
        "competition": {"name": "Example League"},
    }


class TeamFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.payload = {"matches": [
            game(1, self.now - timedelta(days=5), 2, 0),
            game(2, self.now - timedelta(days=3), 1, 1, home=20, away=10),
            game(3, self.now - timedelta(days=1), 0, 3),
        ]}
        self.requests = []

        async def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.payload)

        self.client = FootballDataClient(
            "test-token", transport=httpx.MockTransport(handler),
            min_interval_seconds=0,
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_computed_form_and_venue_splits(self):
        result = await self.client.team_form(10, last_n=3, as_of_utc=self.now.isoformat())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["wins"], 1)
        self.assertEqual(result["summary"]["draws"], 1)
        self.assertEqual(result["summary"]["losses"], 1)
        self.assertEqual(result["summary"]["goals_for"], 3)
        self.assertEqual(result["summary"]["goals_against"], 4)
        self.assertEqual(result["home"]["played"], 2)
        self.assertEqual(result["away"]["played"], 1)
        self.assertEqual(result["away"]["goals_for"], 1)
        self.assertEqual(result["matches"][0]["match_id"], 3)
        self.assertEqual(result["matches"][1]["venue"], "away")
        self.assertNotIn("estimated_probability", result)
        self.assertNotIn("odds", result)

    async def test_api_contract_with_authenticated_request(self):
        await self.client.team_form(10, last_n=2)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/v4/teams/10/matches")
        self.assertEqual(request.url.params["status"], "FINISHED")
        self.assertEqual(request.url.params["limit"], "100")
        self.assertEqual(request.headers["X-Auth-Token"], "test-token")

    async def test_cache_and_coalesced_requests(self):
        a, b = await asyncio.gather(
            self.client.team_form(10, 2, self.now.isoformat()),
            self.client.team_form(10, 3, self.now.isoformat()),
        )
        self.assertEqual(a["summary"]["played"], 2)
        self.assertEqual(b["summary"]["played"], 3)
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_rows_duplicates_and_cutoff(self):
        self.payload["matches"].extend([
            game(1, self.now - timedelta(hours=1), 8, 0),
            game(4, self.now + timedelta(minutes=10), 4, 2),
            game(5, self.now - timedelta(days=2), None, 1),
            game(6, self.now - timedelta(days=2), 1, 0, home=99, away=98),
            game(7, self.now - timedelta(days=2), 3, 2, status="IN_PLAY"),
            game(8, self.now - timedelta(hours=2), 4, 0, status="FINISHED"),
        ])
        r = await self.client.team_form(10, last_n=10, as_of_utc=self.now.isoformat())
        self.assertEqual(r["status"], "insufficient_data")
        self.assertEqual(r["summary"]["played"], 3)
        self.assertEqual(r["invalid_or_inconsistent_rows"], 3)
        self.assertEqual(r["available_completed_matches"], 3)

    async def test_missing_key_never_calls_external_api(self):
        other = FootballDataClient(None, transport=httpx.MockTransport(
            lambda request: self.fail("HTTP must not be called")
        ))
        self.addAsyncCleanup(other.aclose)
        result = await other.team_form(10)
        self.assertEqual(result["status"], "not_configured")
        self.assertNotIn("test-token", str(result))

    async def test_invalid_input_and_naive_timestamp(self):
        self.assertEqual((await self.client.team_form(0))["status"], "invalid_request")
        self.assertEqual((await self.client.team_form(True))["status"], "invalid_request")
        self.assertEqual((await self.client.team_form(10, 21))["status"], "invalid_request")
        self.assertEqual((await self.client.team_form(10, 3, "2026-11-25T12:00:00"))["status"], "invalid_request")
        self.assertEqual(len(self.requests), 0)

    async def test_unauthorized_rate_limited_and_invalid_json(self):
        for status_code, expected in [(401, "provider_auth_failed"), (403, "provider_access_denied"),
                                      (429, "provider_rate_limited"), (500, "provider_http_error")]:
            async def handler(request):
                return httpx.Response(status_code)
            client = FootballDataClient("bad-key", transport=httpx.MockTransport(handler),
                                        min_interval_seconds=0)
            try:
                result = await client.team_form(10)
            finally:
                await client.aclose()
            self.assertEqual(result["status"], expected)
            self.assertNotIn("bad-key", str(result))
        other = FootballDataClient("key", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="broken JSON")
        ))
        try:
            self.assertEqual((await other.team_form(10))["status"], "provider_invalid_response")
        finally:
            await other.aclose()

    async def test_truncation_cannot_be_claimed_complete(self):
        payload = {"matches": [
            game(i + 1, self.now - timedelta(days=i + 1), 1, 0)
            for i in range(100)
        ]}
        result = summarize_form(payload, 10, 10, self.now)
        self.assertTrue(result["possible_provider_truncation"])
        self.assertEqual(result["summary"]["played"], 10)


if __name__ == "__main__":
    unittest.main()
