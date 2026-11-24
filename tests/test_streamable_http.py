from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from fonbet_mcp import server
from fonbet_mcp.client import FonbetClient, RequestPolicy


def _result(response: httpx.Response):
    response.raise_for_status()
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])["result"]
    return response.json()["result"]


@asynccontextmanager
async def _session(app):
    version = "2025-03-26"
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as http:
        response = await http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        })
        _result(response)
        headers["MCP-Session-Id"] = response.headers["mcp-session-id"]
        response = await http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        response.raise_for_status()
        try:
            yield http, headers
        finally:
            (await http.delete("/mcp", headers=headers)).raise_for_status()


async def _search(session, lang="ru"):
    http, headers = session
    response = await http.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "tools/call", "id": 2,
        "params": {"name": "fonbet_search_events", "arguments": {"query": "", "lang": lang}},
    })
    return _result(response)


class StreamableHttpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_share_client_and_cache_until_application_stops(self):
        calls = []
        english_started, release_english = asyncio.Event(), asyncio.Event()

        async def handler(request):
            lang = request.url.params["lang"]
            calls.append(lang)
            if lang == "en":
                english_started.set()
                await release_english.wait()
            return httpx.Response(200, json={"events": []})

        upstream = FonbetClient(
            "https://fonbet.test", transport=httpx.MockTransport(handler),
            policy=RequestPolicy(line_ttl=1, requests_per_second=1000),
        )
        self.addAsyncCleanup(upstream.aclose)
        with patch.object(server, "create_client", return_value=upstream) as create_client:
            app = server.mcp.streamable_http_app()
            async with app.router.lifespan_context(app):
                async with _session(app) as first:
                    first_id = first[1]["MCP-Session-Id"]
                    self.assertFalse((await _search(first))["isError"])
                self.assertFalse(upstream._http.is_closed)

                async with _session(app) as second:
                    self.assertNotEqual(first_id, second[1]["MCP-Session-Id"])
                    self.assertFalse((await _search(second))["isError"])
                    self.assertEqual(calls, ["ru"])

                    pending = asyncio.create_task(_search(second, "en"))
                    try:
                        await asyncio.wait_for(english_started.wait(), 2)
                        async with _session(app) as third:
                            self.assertNotEqual(second[1]["MCP-Session-Id"], third[1]["MCP-Session-Id"])
                            self.assertFalse((await _search(third))["isError"])
                        self.assertFalse(upstream._http.is_closed)
                    finally:
                        release_english.set()
                        outcome = await asyncio.wait_for(pending, 2)
                    self.assertFalse(outcome["isError"])
                    self.assertEqual(calls, ["ru", "en"])

                await asyncio.sleep(1.05)
                async with _session(app) as fourth:
                    self.assertFalse((await _search(fourth))["isError"])
                    self.assertEqual(calls, ["ru", "en", "ru"])
                create_client.assert_called_once_with()
                self.assertIs(server.get_client(), upstream)
            self.assertTrue(upstream._http.is_closed)
            self.assertIsNone(server.client)
