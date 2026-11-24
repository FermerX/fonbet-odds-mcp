"""Shared Fonbet HTTP transport and timestamped snapshot cache."""
from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class RequestPolicy:
    line_ttl: float = 10
    event_ttl: float = 15
    line_cache_size: int = 8
    event_cache_size: int = 256
    requests_per_second: float = 5
    max_concurrency: int = 4
    retries: int = 2
    max_retry_delay: float = 5
    max_snapshot_age: float = 60

    def __post_init__(self) -> None:
        nonnegative = (self.line_ttl, self.event_ttl, self.max_retry_delay)
        positive = (self.requests_per_second, self.max_snapshot_age)
        if any(not math.isfinite(value) or value < 0 for value in nonnegative):
            raise ValueError("Fonbet cache TTLs and retry delay must be finite and nonnegative")
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("Fonbet request rate and snapshot age must be finite and positive")
        if min(self.line_cache_size, self.event_cache_size, self.retries) < 0 or self.max_concurrency < 1:
            raise ValueError("Invalid Fonbet cache size, retry count or concurrency")


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    received_at: datetime
    received_tick: float
    upstream_age: float = 0

    def age(self) -> float:
        return self.upstream_age + asyncio.get_running_loop().time() - self.received_tick


class FonbetCooldownError(RuntimeError):
    """The upstream asked for a pause longer than a single retry can wait."""


class FonbetClient:
    def __init__(
        self,
        base_url: str,
        scope_market: str = "1600",
        timeout: float = 20,
        current_line_path: str = "/events/listBase",
        event_view_path: str = "/events/event",
        *,
        policy: RequestPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.scope_market = scope_market
        self.timeout = timeout
        self.current_line_path = current_line_path
        self.event_view_path = event_view_path
        self.policy = policy or RequestPolicy()
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"user-agent": "Mozilla/5.0", "referer": "https://fon.bet/"},
            limits=httpx.Limits(
                max_connections=self.policy.max_concurrency,
                max_keepalive_connections=self.policy.max_concurrency,
            ),
            transport=transport,
        )
        self._line_cache: OrderedDict[str | tuple[int, str], Snapshot] = OrderedDict()
        self._event_cache: OrderedDict[str | tuple[int, str], Snapshot] = OrderedDict()
        self._inflight: dict[tuple[str, str | tuple[int, str]], asyncio.Task[Snapshot]] = {}
        self._slots = asyncio.Semaphore(self.policy.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0
        self._retry_until = 0.0

    async def aclose(self) -> None:
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._http.aclose()

    async def current_line(self, lang: str = "ru") -> dict[str, Any]:
        return self._fresh_payload(await self.current_line_snapshot(lang))

    async def event_view(self, event_id: int, lang: str = "ru") -> dict[str, Any]:
        return self._fresh_payload(await self.event_view_snapshot(event_id, lang))

    def _fresh_payload(self, snapshot: Snapshot) -> dict[str, Any]:
        if snapshot.age() >= self.policy.max_snapshot_age:
            raise ValueError("Fonbet returned an expired snapshot")
        return snapshot.payload

    async def current_line_snapshot(self, lang: str = "ru") -> Snapshot:
        return await self._snapshot(
            "line", lang, self.current_line_path.format(lang=lang),
            {"scopeMarket": self.scope_market, "lang": lang},
        )

    async def event_view_snapshot(self, event_id: int, lang: str = "ru") -> Snapshot:
        return await self._snapshot(
            "event", (event_id, lang), self.event_view_path,
            {"eventId": event_id, "lang": lang, "scopeMarket": self.scope_market},
        )

    async def _snapshot(
        self, kind: str, key: str | tuple[int, str], path: str, params: dict[str, Any],
    ) -> Snapshot:
        if kind == "line":
            cache, ttl, size = self._line_cache, self.policy.line_ttl, self.policy.line_cache_size
        else:
            cache, ttl, size = self._event_cache, self.policy.event_ttl, self.policy.event_cache_size
        ttl = min(ttl, self.policy.max_snapshot_age)
        cached = cache.get(key)
        if cached is not None:
            if cached.age() < ttl:
                cache.move_to_end(key)
                return cached
            del cache[key]

        request_key = (kind, key)
        task = self._inflight.get(request_key)
        if task is None:
            async def fetch() -> Snapshot:
                try:
                    snapshot = await self._get(path, params)
                    if size and snapshot.age() < ttl:
                        cache[key] = snapshot
                        if len(cache) > size:
                            cache.popitem(last=False)
                    return snapshot
                finally:
                    self._inflight.pop(request_key, None)

            task = asyncio.create_task(fetch())
            self._inflight[request_key] = task
            # The shared request can outlive every waiter after cancellation.
            task.add_done_callback(self._observe_error)
        return await asyncio.shield(task)

    @staticmethod
    def _observe_error(task: asyncio.Task[Snapshot]) -> None:
        if not task.cancelled():
            task.exception()

    async def _wait_for_request_turn(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            while True:
                now = loop.time()
                if self._retry_until - now > self.policy.max_retry_delay:
                    raise FonbetCooldownError("Fonbet requested a longer Retry-After pause")
                delay = max(self._next_request, self._retry_until) - now
                if delay <= 0:
                    self._next_request = now + 1 / self.policy.requests_per_second
                    return
                await asyncio.sleep(delay)

    async def _get(self, path: str, params: dict[str, Any]) -> Snapshot:
        loop = asyncio.get_running_loop()
        for attempt in range(self.policy.retries + 1):
            backoff = min(self.policy.max_retry_delay, 0.25 * 2 ** min(attempt, 10))
            async with self._slots:
                await self._wait_for_request_turn()
                try:
                    response = await self._http.get(f"{self.base_url}{path}", params=params)
                except httpx.TransportError:
                    if attempt == self.policy.retries:
                        raise
                else:
                    if response.status_code not in (429, 500, 502, 503, 504):
                        response.raise_for_status()
                        received_at, received_tick = datetime.now(timezone.utc), loop.time()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("Fonbet returned a non-object JSON payload")
                        age = response.headers.get("Age", "")
                        upstream_age = float(age) if age.isascii() and age.isdecimal() else 0
                        return Snapshot(payload, received_at, received_tick, upstream_age)

                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    if response.status_code == 429 or retry_after is not None:
                        pause = backoff if retry_after is None else retry_after
                        self._retry_until = max(self._retry_until, loop.time() + pause)
                    if attempt == self.policy.retries or (retry_after or 0) > self.policy.max_retry_delay:
                        response.raise_for_status()
                    backoff = max(backoff, retry_after or 0)
            await asyncio.sleep(backoff)
        raise AssertionError("Retry loop exhausted")


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None
