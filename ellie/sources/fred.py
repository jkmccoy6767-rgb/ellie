"""Macro series from the Federal Reserve Bank of St. Louis (FRED)."""

from __future__ import annotations

import io

import pandas as pd

from .base import http_get

NAME = "fred"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_URL = "https://api.stlouisfed.org/fred/series/observations"

# series_id -> (label, unit)
SERIES: dict[str, tuple[str, str]] = {
    "DGS10": ("10-year Treasury yield", "%"),
    "DGS3MO": ("3-month Treasury yield", "%"),
    "T10Y2Y": ("10y–2y yield spread", "%"),
    "VIXCLS": ("CBOE VIX", "index"),
    "CPIAUCSL": ("CPI, all urban consumers", "index"),
    "UNRATE": ("Unemployment rate", "%"),
    "FEDFUNDS": ("Effective fed funds rate", "%"),
    "BAMLH0A0HYM2": ("High-yield credit spread", "%"),
}


def fetch_series(series_id: str, start: pd.Timestamp, api_key: str | None = None, timeout: int = 20) -> pd.DataFrame:
    if api_key:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start.strftime("%Y-%m-%d"),
        }
        obs = http_get(API_URL, params=params, timeout=timeout).json()["observations"]
        raw = pd.DataFrame(obs)[["date", "value"]]
    else:
        text = http_get(CSV_URL, params={"id": series_id, "cosd": start.strftime("%Y-%m-%d")}, timeout=timeout).text
        raw = pd.read_csv(io.StringIO(text))
        raw.columns = ["date", "value"]
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")  # FRED uses "." for missing
    raw = raw.dropna(subset=["value"])
    raw = raw[pd.to_datetime(raw["date"]) >= start]
    return pd.DataFrame({"series_id": series_id, "date": raw["date"], "value": raw["value"], "source": NAME})
