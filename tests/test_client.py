from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from fonbet_mcp.client import (
    FonbetClient, FonbetCooldownError, FonbetRequestCapacityError, RequestPolicy, _retry_after,
)


class FonbetClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler, **policy):
        options = {"requests_per_second": 10000, "retries": 0, **policy}
        client = FonbetClient(
            "https://fonbet.test", policy=RequestPolicy(**options),
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_separate_cache_language_keys_max_size_and_ttl(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"call": count, "lang": request.url.params["lang"]})

        client = self.make_client(handler, line_cache_size=2, event_cache_size=1, line_ttl=1, event_ttl=2)
        line = await client.current_line_snapshot()
        english = await client.current_line_snapshot("en")
        event = await client.event_view_snapshot(10)
        self.assertIs(line, await client.current_line_snapshot())
        self.assertIs(event, await client.event_view_snapshot(10))
        await client.event_view_snapshot(11)
        self.assertIsNot(event, await client.event_view_snapshot(10))
        self.assertIs(english, await client.current_line_snapshot("en"))
        # Set the cached receipt clocks to deterministically expire the line alone.
        tick = asyncio.get_running_loop().time() - 1.2
        client._line_cache["ru"] = replace(line, received_tick=tick)
        recent_event = client._event_cache[(10, "ru")]
        client._event_cache[(10, "ru")] = replace(recent_event, received_tick=tick)
        self.assertIsNot(line, await client.current_line_snapshot())
        self.assertEqual((await client.event_view(10))["call"], recent_event.payload["call"])

    async def test_no_stale_fallback_after_expiry_and_no_error_cache(self):
        statuses = iter((200, 503, 200))

        def handler(_):
            return httpx.Response(next(statuses), json={"events": []})

        client = self.make_client(handler)
        fresh = await client.current_line_snapshot()
        client._line_cache["ru"] = replace(fresh, received_tick=fresh.received_tick - 30)
        with self.assertRaises(httpx.HTTPStatusError):
            await client.current_line()
        self.assertIsNot(await client.current_line_snapshot(), fresh)

    async def test_parallel_identical_requests_coalesce_even_if_one_waiter_cancels(self):
        count = 0
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(_):
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"events": []})

        client = self.make_client(handler, line_ttl=0)
        first = asyncio.create_task(client.current_line_snapshot())
        await asyncio.wait_for(started.wait(), 1)
        others = asyncio.gather(*(client.current_line_snapshot() for _ in range(8)))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        snapshots = await asyncio.wait_for(others, 1)
        self.assertEqual(count, 1)
        self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))
        await client.current_line()
        self.assertEqual(count, 2)

    async def test_unique_request_limit_includes_queue_and_still_coalesces(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            event_id = int(request.url.params["eventId"])
            calls.append(event_id)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"event": event_id})

        client = self.make_client(handler, max_concurrency=1, max_inflight_requests=2)
        first = asyncio.create_task(client.event_view_snapshot(1))
        await asyncio.wait_for(started.wait(), 1)
        queued = asyncio.create_task(client.event_view_snapshot(2))
        await asyncio.sleep(0)
        duplicate = asyncio.create_task(client.event_view_snapshot(2))
        try:
            with self.assertRaises(FonbetRequestCapacityError):
                await client.event_view(3)
            self.assertEqual(calls, [1])
        finally:
            release.set()
        _, queued_snapshot, coalesced = await asyncio.gather(first, queued, duplicate)
        self.assertIs(queued_snapshot, coalesced)
        self.assertEqual((await client.event_view(3))["event"], 3)
        self.assertEqual(calls, [1, 2, 3])

    async def test_rate_limit_and_concurrency_apply_to_distinct_requests(self):
        loop = asyncio.get_running_loop()
        start_times = []
        active = 0
        peak = 0

        async def handler(_):
            nonlocal active, peak
            start_times.append(loop.time())
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.08)
            active -= 1
            return httpx.Response(200, json={})

        client = self.make_client(handler, requests_per_second=40, max_concurrency=2)
        await asyncio.gather(*(client.event_view(i) for i in range(5)))
        self.assertEqual(peak, 2)
        self.assertTrue(all(b - a >= 0.023 for a, b in zip(start_times, start_times[1:])))

    async def test_retry_timeout_429_and_transient_5xx_with_backoff(self):
        calls = []
        loop = asyncio.get_running_loop()

        def handler(request):
            calls.append(loop.time())
            if len(calls) == 1:
                raise httpx.ReadTimeout("read timed out", request=request)
            if len(calls) == 2:
                return httpx.Response(429, headers={"Retry-After": "0.09"})
            if len(calls) == 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"events": []})

        client = self.make_client(handler, retries=3, max_retry_delay=0.12)
        self.assertEqual(await client.current_line(), {"events": []})
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(b - a >= 0.085 for a, b in zip(calls, calls[1:])))

    async def test_retry_after_cooldown_is_shared_without_retrying_early(self):
        count = 0

        def handler(_):
            nonlocal count
            count += 1
            return httpx.Response(429, headers={"Retry-After": "30"})

        client = self.make_client(handler, retries=2, max_retry_delay=1)
        with self.assertRaises(httpx.HTTPStatusError):
            await client.current_line()
        with self.assertRaises(FonbetCooldownError):
            await client.event_view(15)
        self.assertEqual(count, 1)

    async def test_permanent_http_error_not_retried(self):
        count = 0

        def handler(_):
            nonlocal count
            count += 1
            return httpx.Response(403)

        client = self.make_client(handler, retries=2)
        with self.assertRaises(httpx.HTTPStatusError):
            await client.current_line()
        self.assertEqual(count, 1)

    def test_retry_after_date(self):
        date = datetime.now(timezone.utc) + timedelta(seconds=20)
        self.assertGreater(_retry_after(format_datetime(date, usegmt=True)), 18)
        self.assertIsNone(_retry_after("invalid"))
