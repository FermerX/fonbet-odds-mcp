from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from fonbet_mcp.client import RequestPolicy


@dataclass(frozen=True)
class Settings:
    base_url: str
    current_line_path: str
    event_view_path: str
    scope_market: str
    timeout: float
    request_policy: RequestPolicy = RequestPolicy()


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        base_url=os.getenv("FONBET_BASE_URL", "https://line-lb51.bk6bba-resources.com").rstrip("/"),
        current_line_path=os.getenv("FONBET_CURRENT_LINE_PATH", "/events/listBase"),
        event_view_path=os.getenv("FONBET_EVENT_VIEW_PATH", "/events/event"),
        scope_market=os.getenv("FONBET_SCOPE_MARKET", "1600"),
        timeout=float(os.getenv("FONBET_TIMEOUT", "20")),
        request_policy=RequestPolicy(
            line_ttl=float(os.getenv("FONBET_LINE_TTL", "10")),
            event_ttl=float(os.getenv("FONBET_EVENT_TTL", "15")),
            line_cache_size=int(os.getenv("FONBET_LINE_CACHE_SIZE", "8")),
            event_cache_size=int(os.getenv("FONBET_EVENT_CACHE_SIZE", "256")),
            requests_per_second=float(os.getenv("FONBET_REQUESTS_PER_SECOND", "5")),
            max_concurrency=int(os.getenv("FONBET_MAX_CONCURRENCY", "4")),
            retries=int(os.getenv("FONBET_RETRIES", "2")),
            max_retry_delay=float(os.getenv("FONBET_MAX_RETRY_DELAY", "5")),
            max_snapshot_age=float(os.getenv("FONBET_MAX_SNAPSHOT_AGE", "60")),
        ),
    )
