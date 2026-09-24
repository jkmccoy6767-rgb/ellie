"""Financial Modeling Prep: annual analyst estimates and price-target consensus.

Needs ``ELLIE_FMP_API_KEY``. Used as the second estimates source so consensus can be
cross-checked, the same way prices are.
"""

from __future__ import annotations

import pandas as pd

from .base import http_get

NAME = "fmp"
BASE = "https://financialmodelingprep.com/stable"


def parse_estimates(symbol: str, items: list[dict], as_of: str) -> pd.DataFrame:
    rows = []
    for i in items or []:
        if not i.get("date"):
            continue
        period = f"FY{pd.Timestamp(i['date']).year}"
        if i.get("epsAvg") is not None:
            rows.append({"symbol": symbol, "period": period, "metric": "eps", "as_of": as_of, "mean": i["epsAvg"],
                         "high": i.get("epsHigh"), "low": i.get("epsLow"), "n_analysts": i.get("numAnalystsEps"), "source": NAME})
        if i.get("revenueAvg") is not None:
            rows.append({"symbol": symbol, "period": period, "metric": "revenue", "as_of": as_of, "mean": i["revenueAvg"],
                         "high": i.get("revenueHigh"), "low": i.get("revenueLow"), "n_analysts": i.get("numAnalystsRevenue"),
                         "source": NAME})
    return pd.DataFrame(rows)


def parse_price_target(symbol: str, items: list[dict], as_of: str) -> pd.DataFrame:
    if not items or not items[0].get("targetConsensus"):
        return pd.DataFrame()
    i = items[0]
    return pd.DataFrame([{
        "symbol": symbol, "as_of": as_of, "mean": i["targetConsensus"], "median": i.get("targetMedian"),
        "high": i.get("targetHigh"), "low": i.get("targetLow"), "n_analysts": None, "source": NAME,
    }])


def fetch_analyst(symbol: str, api_key: str, as_of: str, timeout: int = 20) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    try:
        items = http_get(f"{BASE}/analyst-estimates", params={"symbol": symbol, "period": "annual", "apikey": api_key},
                         timeout=timeout).json()
        est = parse_estimates(symbol, items, as_of)
        out["eps"] = est[est["metric"] == "eps"] if not est.empty else est
        out["revenue"] = est[est["metric"] == "revenue"] if not est.empty else est
    except Exception:
        out["eps"] = out["revenue"] = pd.DataFrame()
    try:
        items = http_get(f"{BASE}/price-target-consensus", params={"symbol": symbol, "apikey": api_key}, timeout=timeout).json()
        out["price_targets"] = parse_price_target(symbol, items, as_of)
    except Exception:
        out["price_targets"] = pd.DataFrame()
    return out
