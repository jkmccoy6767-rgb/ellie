"""Daily prices from Stooq's CSV endpoint (secondary price source for reconciliation)."""

from __future__ import annotations

import io

import pandas as pd

from .base import empty_prices, http_get

NAME = "stooq"
URL = "https://stooq.com/q/d/l/"


def vendor_symbol(symbol: str) -> str:
    return symbol.replace(".", "-").lower() + ".us"


def fetch_symbol(symbol: str, start: pd.Timestamp, timeout: int = 20) -> pd.DataFrame:
    params = {"s": vendor_symbol(symbol), "i": "d", "d1": start.strftime("%Y%m%d")}
    text = http_get(URL, params=params, timeout=timeout).text
    if not text.startswith("Date"):
        return empty_prices()
    raw = pd.read_csv(io.StringIO(text))
    df = pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d"),
            "source": NAME,
            "open": raw["Open"],
            "high": raw["High"],
            "low": raw["Low"],
            "close": raw["Close"],
            "volume": raw.get("Volume"),
        }
    )
    return df.dropna(subset=["close"])
