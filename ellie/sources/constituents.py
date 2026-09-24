"""S&P 500 index membership.

Primary: the open `datasets/s-and-p-500-companies` list on GitHub (refreshed from
S&P DJI announcements). Fallback: the copy bundled in ``data/``. A production
deployment should swap in a licensed S&P DJI membership feed so that point-in-time
membership (additions / removals) is exact.
"""

from __future__ import annotations

import io
import logging

import pandas as pd

from ..config import BUNDLED_CONSTITUENTS
from .base import SourceUnavailable, http_get

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

_RENAME = {
    "Symbol": "symbol",
    "Security": "name",
    "GICS Sector": "sector",
    "GICS Sub-Industry": "sub_industry",
    "Headquarters Location": "headquarters",
    "Date added": "date_added",
    "CIK": "cik",
    "Founded": "founded",
}


def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.rename(columns=_RENAME)[list(_RENAME.values())].copy()
    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["cik"] = df["cik"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(10)
    df = df.dropna(subset=["symbol", "name", "sector"]).drop_duplicates("symbol")
    return df.reset_index(drop=True)


def load_bundled() -> pd.DataFrame:
    return _normalise(pd.read_csv(BUNDLED_CONSTITUENTS, dtype=str))


def fetch(timeout: int = 20) -> tuple[pd.DataFrame, str]:
    """Return (constituents, source_name); falls back to the bundled list."""
    try:
        resp = http_get(URL, timeout=timeout)
        df = _normalise(pd.read_csv(io.StringIO(resp.text), dtype=str))
        if len(df) < 450:  # guard against a truncated or malformed download
            raise SourceUnavailable(f"only {len(df)} constituents returned")
        return df, "github-datasets"
    except SourceUnavailable as exc:
        log.warning("Constituents download failed (%s); using bundled list", exc)
        return load_bundled(), "bundled"
