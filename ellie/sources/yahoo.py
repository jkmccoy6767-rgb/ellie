"""Daily prices from Yahoo Finance's public chart endpoint (primary price source)."""

from __future__ import annotations

import pandas as pd

from .base import empty_prices, http_get

NAME = "yahoo"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def vendor_symbol(symbol: str) -> str:
    # Yahoo uses '-' for share classes: BRK.B -> BRK-B
    return symbol.replace(".", "-")


def fetch_symbol(symbol: str, start: pd.Timestamp, timeout: int = 20) -> pd.DataFrame:
    params = {
        "period1": int(start.timestamp()),
        "period2": int(pd.Timestamp.now(tz="UTC").timestamp()),
        "interval": "1d",
        "events": "div,splits",
    }
    payload = http_get(URL.format(symbol=vendor_symbol(symbol)), params=params, timeout=timeout).json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0].get("timestamp"):
        return empty_prices()
    r = result[0]
    quote = r["indicators"]["quote"][0]
    # Adjusted close keeps return series consistent across splits and dividends.
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(r["timestamp"], unit="s").strftime("%Y-%m-%d"),
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": adj,
            "volume": quote["volume"],
        }
    ).dropna(subset=["close"])
    df.insert(0, "symbol", symbol)
    df.insert(2, "source", NAME)
    return df.drop_duplicates("date", keep="last")
