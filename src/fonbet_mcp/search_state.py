"""Bounded, application-local snapshots for candidate scan continuation and paging."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

SearchKey = tuple[float, float, float, frozenset[str], str]


@dataclass(frozen=True)
class Quote:
    row: dict[str, Any]
    match_start: int
    detail_tick: float
    detail_effective_tick: float
    line_tick: float
    line_effective_tick: float
    revision: int


@dataclass(frozen=True)
class SearchRevision:
    rows: tuple[Quote, ...]
    coverage: dict[str, Any]


@dataclass
class SearchState:
    key: SearchKey
    start: float
    end: float
    expires_at: float
    id: str = field(default_factory=lambda: uuid4().hex)
    matches: tuple[int, ...] | None = None
    root_events: int = 0
    eligible: int = 0
    line_received_at: str | None = None
    latest_line_received_at: str | None = None
    detail_ticks: dict[int, float] = field(default_factory=dict)
    checked: set[int] = field(default_factory=set)
    finished: set[int] = field(default_factory=set)
    completed: int = 0
    no_longer_eligible: int = 0
    processed: int = 0
    invalid_rows: int = 0
    conflicts: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    rows: list[Quote] = field(default_factory=list)
    revisions: OrderedDict[int, SearchRevision] = field(default_factory=OrderedDict)
    revision: int = 0
    reusable_until: float = 0
    task: asyncio.Task[int] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def remaining(self) -> int:
        return self.eligible - len(self.finished)

    def publish(self, coverage: dict[str, Any]) -> None:
        self.rows.sort(key=lambda quote: (
            quote.match_start, quote.row["event_id"], quote.row["market_event_id"],
            quote.row["factor_id"], quote.row["threshold"] or "",
        ))
        self.revision += 1
        self.revisions[self.revision] = SearchRevision(tuple(self.rows), coverage)
        if len(self.revisions) > 4:
            self.revisions.popitem(last=False)


class SearchStore:
    def __init__(self, size: int, ttl: float) -> None:
        self.size = size
        self.ttl = ttl
        self.line_tick = 0.0
        self.states: OrderedDict[str, SearchState] = OrderedDict()

    def note_source(self, kind: str, key: str | tuple[int, str], tick: float) -> None:
        if kind == "line" and key == "ru":
            self.line_tick = max(self.line_tick, tick)
        elif isinstance(key, tuple) and key[1] == "ru":
            event_id = key[0]
            for state in self.states.values():
                if event_id in state.checked:
                    state.detail_ticks[event_id] = max(state.detail_ticks.get(event_id, 0), tick)

    def active_count(self) -> int:
        return sum(state.running for state in self.states.values())

    def _prune(self, tick: float) -> None:
        for key, state in tuple(self.states.items()):
            if not state.running and tick >= state.expires_at:
                del self.states[key]

    def get(self, search_id: str) -> SearchState | None:
        tick = asyncio.get_running_loop().time()
        self._prune(tick)
        state = self.states.get(search_id)
        if state is None or tick >= state.expires_at:
            return None
        self.states.move_to_end(search_id)
        return state

    def find(self, key: SearchKey) -> SearchState | None:
        tick = asyncio.get_running_loop().time()
        self._prune(tick)
        for state in reversed(self.states.values()):
            if state.key == key and tick < state.expires_at and (state.running or tick < state.reusable_until):
                self.states.move_to_end(state.id)
                return state
        return None

    def create(self, key: SearchKey, start: float) -> SearchState | None:
        tick = asyncio.get_running_loop().time()
        self._prune(tick)
        if len(self.states) >= self.size:
            victim = next((item.id for item in self.states.values() if not item.running), None)
            if victim is None:
                return None
            del self.states[victim]
        state = SearchState(key, start, start + key[0] * 3600, tick + self.ttl)
        self.states[state.id] = state
        return state

    async def aclose(self) -> None:
        tasks = [state.task for state in self.states.values() if state.task is not None and state.running]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.states.clear()
