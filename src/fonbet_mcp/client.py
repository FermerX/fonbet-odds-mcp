"""Shared Fonbet HTTP transport and timestamped snapshot cache."""
from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from fonbet_mcp.search_state import SearchStore


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
    max_inflight_requests: int = 32
    max_concurrent_searches: int = 2
    search_cache_size: int = 8
    search_state_ttl: float = 180
    search_max_candidates: int = 10000
    search_max_matches: int = 3000

    def __post_init__(self) -> None:
        nonnegative = (self.line_ttl, self.event_ttl, self.max_retry_delay)
        positive = (self.requests_per_second, self.max_snapshot_age, self.search_state_ttl)
        if any(not math.isfinite(value) or value < 0 for value in nonnegative):
            raise ValueError("Fonbet cache TTLs and retry delay must be finite and nonnegative")
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("Fonbet request rate and snapshot age must be finite and positive")
        if min(self.line_cache_size, self.event_cache_size, self.retries) < 0:
            raise ValueError("Invalid Fonbet cache size or retry count")
        if min(self.max_concurrency, self.max_inflight_requests, self.max_concurrent_searches) < 1:
            raise ValueError("Fonbet concurrency and request limits must be positive")
        if min(self.search_cache_size, self.search_max_candidates, self.search_max_matches) < 1:
            raise ValueError("Fonbet search state capacity must be positive")


@dataclass
class RequestMetrics:
    cache_hits: int = 0
    cache_misses: int = 0
    coalesced_requests: int = 0
    http_requests: int = 0
    retries: int = 0
    queue_seconds: float = 0
    network_seconds: float = 0
    processing_seconds: float = 0


request_metrics: ContextVar[RequestMetrics | None] = ContextVar("request_metrics", default=None)


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    received_at: datetime
    received_tick: float
    upstream_age: float = 0

    @property
    def effective_tick(self) -> float:
        return self.received_tick - self.upstream_age

    def age(self) -> float:
        return asyncio.get_running_loop().time() - self.effective_tick


class FonbetCooldownError(RuntimeError):
    """The upstream asked for a pause longer than a single retry can wait."""


class FonbetRequestCapacityError(RuntimeError):
    """The client reached its limit of active and queued unique requests."""


class FonbetSearchCapacityError(RuntimeError):
    """The application reached its limit of simultaneous candidate scans."""


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
        self._active_searches = 0
        self.searches = SearchStore(self.policy.search_cache_size, self.policy.search_state_ttl)

    @contextmanager
    def candidate_search_slot(self) -> Iterator[None]:
        if self._active_searches >= self.policy.max_concurrent_searches:
            raise FonbetSearchCapacityError("Fonbet candidate search is busy")
        self._active_searches += 1
        try:
            yield
        finally:
            self._active_searches -= 1

    async def aclose(self) -> None:
        await self.searches.aclose()
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

    def search_capacity_available(self) -> bool:
        return max(self._active_searches, self.searches.active_count()) < self.policy.max_concurrent_searches

    async def _snapshot(
        self, kind: str, key: str | tuple[int, str], path: str, params: dict[str, Any],
    ) -> Snapshot:
        if kind == "line":
            cache, ttl, size = self._line_cache, self.policy.line_ttl, self.policy.line_cache_size
        else:
            cache, ttl, size = self._event_cache, self.policy.event_ttl, self.policy.event_cache_size
        metrics = request_metrics.get()
        ttl = min(ttl, self.policy.max_snapshot_age)
        cached = cache.get(key)
        if cached is not None:
            if cached.age() < ttl:
                cache.move_to_end(key)
                if metrics is not None:
                    metrics.cache_hits += 1
                return cached
            del cache[key]

        if metrics is not None:
            metrics.cache_misses += 1
        request_key = (kind, key)
        task = self._inflight.get(request_key)
        if task is None:
            if len(self._inflight) >= self.policy.max_inflight_requests:
                raise FonbetRequestCapacityError("Fonbet request queue is full")

            async def fetch() -> Snapshot:
                try:
                    snapshot = await self._get(path, params)
                    self.searches.note_source(kind, key, snapshot.received_tick)
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
        elif metrics is not None:
            metrics.coalesced_requests += 1
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
        metrics = request_metrics.get()
        for attempt in range(self.policy.retries + 1):
            backoff = min(self.policy.max_retry_delay, 0.25 * 2 ** min(attempt, 10))
            queued_at = loop.time()
            async with self._slots:
                try:
                    await self._wait_for_request_turn()
                finally:
                    if metrics is not None:
                        metrics.queue_seconds += loop.time() - queued_at
                try:
                    response = await self._request(path, params, attempt, metrics)
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


    async def _request(
        self, path: str, params: dict[str, Any], attempt: int, metrics: RequestMetrics | None,
    ) -> httpx.Response:
        loop = asyncio.get_running_loop()
        started = loop.time()
        if metrics is not None:
            metrics.http_requests += 1
            metrics.retries += int(attempt > 0)
        try:
            return await self._http.get(f"{self.base_url}{path}", params=params)
        finally:
            if metrics is not None:
                metrics.network_seconds += loop.time() - started


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
