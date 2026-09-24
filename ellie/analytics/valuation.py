"""Valuation multiples from reported fundamentals, relative to sector peers."""

from __future__ import annotations

import numpy as np
import pandas as pd


def latest_fundamentals(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """Wide table (symbol x metric) of the latest annual value plus prior-year value."""
    if fundamentals.empty:
        return pd.DataFrame()
    f = fundamentals.sort_values("period_end")
    latest = f.groupby(["symbol", "metric"]).tail(1).pivot(index="symbol", columns="metric", values="value")
    prior = (
        f.groupby(["symbol", "metric"]).nth(-2).pivot(index="symbol", columns="metric", values="value")
        if len(f) else pd.DataFrame()
    )
    return latest.join(prior.add_suffix("_prior"), how="left")


def multiples(fundamentals: pd.DataFrame, prices: pd.Series, sectors: pd.Series) -> pd.DataFrame:
    w = latest_fundamentals(fundamentals)
    if w.empty:
        return w
    px = prices.reindex(w.index)
    get = lambda c: w[c] if c in w else pd.Series(np.nan, index=w.index)  # noqa: E731
    eps = get("eps_diluted")
    shares = get("shares_outstanding")
    mcap = px * shares
    out = pd.DataFrame(
        {
            "market_cap": mcap,
            "pe": (px / eps).where(eps > 0),
            "earnings_yield": eps / px,
            "ps": (mcap / get("revenue")).where(get("revenue") > 0),
            "pb": (mcap / get("equity")).where(get("equity") > 0),
            "net_margin": get("net_income") / get("revenue"),
            "roe": (get("net_income") / get("equity")).where(get("equity") > 0),
            "debt_to_equity": (get("total_liabilities") / get("equity")).where(get("equity") > 0),
            "revenue_growth": get("revenue") / get("revenue_prior") - 1,
            "eps_growth": (eps / get("eps_diluted_prior") - 1).where(get("eps_diluted_prior") > 0),
        }
    )
    out["sector"] = sectors.reindex(out.index)
    med = out.groupby("sector")["pe"].transform("median")
    out["pe_vs_sector"] = out["pe"] / med - 1
    return out
