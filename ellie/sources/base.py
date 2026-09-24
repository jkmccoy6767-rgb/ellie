"""Shared plumbing for data-source connectors."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import pandas as pd
import requests

log = logging.getLogger(__name__)

PRICE_COLUMNS = ["symbol", "date", "source", "open", "high", "low", "close", "volume"]
NEWS_COLUMNS = ["symbol", "published_at", "source", "headline", "summary", "url"]


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot be reached at all (network policy, outage, auth)."""


@dataclass
class FetchResult:
    """Rows from one source plus per-symbol errors, so partial failures stay visible."""

    source: str
    dataset: str
    frame: pd.DataFrame
    errors: dict[str, str] = field(default_factory=dict)


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 20,
    retries: int = 3,
) -> requests.Response:
    """GET with exponential backoff on transient failures (429 / 5xx / connection)."""
    headers = {"User-Agent": "Mozilla/5.0 (Ellie research platform)", **(headers or {})}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status < 500 and status != 429:
                break  # client error: retrying won't help
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(0.5 * 2**attempt)
    raise SourceUnavailable(f"{url}: {last_exc}") from last_exc


def empty_prices() -> pd.DataFrame:
    return pd.DataFrame(columns=PRICE_COLUMNS)


def empty_news() -> pd.DataFrame:
    return pd.DataFrame(columns=NEWS_COLUMNS)


def news_id(symbol: str, headline: str, published_at: str) -> str:
    """Stable key for a story: the same headline from two feeds on the same UTC day collapses to one row.

    The day is part of the key because formulaic headlines ("X shares fall on weak outlook")
    recur for genuinely different events.
    """
    norm = re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()
    return hashlib.sha1(f"{symbol}|{published_at[:10]}|{norm}".encode()).hexdigest()[:20]


class RateLimiter:
    """Thread-safe minimum spacing between calls (e.g. 60/minute for Finnhub's free tier)."""

    def __init__(self, calls_per_minute: float):
        self.interval = 60.0 / calls_per_minute
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)
