"""Shared plumbing for data-source connectors."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import pandas as pd
import requests

log = logging.getLogger(__name__)

PRICE_COLUMNS = ["symbol", "date", "source", "open", "high", "low", "close", "volume"]


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
