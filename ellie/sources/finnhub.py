"""Finnhub: company news, analyst recommendations, price targets, estimates, earnings surprises.

Needs ``ELLIE_FINNHUB_API_KEY``. The free tier allows 60 calls/minute, which every
call here shares; some endpoints (estimates, price targets) need a paid plan and are
skipped cleanly when the key does not cover them.
"""

from __future__ import annotations

import pandas as pd

from .base import RateLimiter, empty_news, http_get

NAME = "finnhub"
BASE = "https://finnhub.io/api/v1"
limiter = RateLimiter(calls_per_minute=55)


def _get(path: str, api_key: str, timeout: int, **params) -> object:
    limiter.wait()
    return http_get(f"{BASE}{path}", params={**params, "token": api_key}, timeout=timeout).json()


def fiscal_period(date: str) -> str:
    return f"FY{pd.Timestamp(date).year}"


def fetch_news(symbol: str, since: pd.Timestamp, api_key: str, timeout: int = 20) -> pd.DataFrame:
    today = pd.Timestamp.now(tz="UTC")
    items = _get("/company-news", api_key, timeout, symbol=symbol,
                 **{"from": since.strftime("%Y-%m-%d"), "to": today.strftime("%Y-%m-%d")})
    rows = [
        {
            "symbol": symbol,
            "published_at": pd.Timestamp(i["datetime"], unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": NAME,
            "headline": i["headline"].strip(),
            "summary": (i.get("summary") or "").strip() or None,
            "url": i.get("url"),
        }
        for i in items or []
        if i.get("headline") and i.get("datetime")
    ]
    return pd.DataFrame(rows) if rows else empty_news()


def parse_recommendations(symbol: str, items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "symbol": symbol, "period": i["period"][:7],
            "strong_buy": int(i.get("strongBuy", 0)), "buy": int(i.get("buy", 0)), "hold": int(i.get("hold", 0)),
            "sell": int(i.get("sell", 0)), "strong_sell": int(i.get("strongSell", 0)), "source": NAME,
        }
        for i in items or []
        if i.get("period")
    ])


def parse_price_target(symbol: str, payload: dict, as_of: str) -> pd.DataFrame:
    if not payload or not payload.get("targetMean"):
        return pd.DataFrame()
    return pd.DataFrame([{
        "symbol": symbol, "as_of": as_of, "mean": payload["targetMean"], "median": payload.get("targetMedian"),
        "high": payload.get("targetHigh"), "low": payload.get("targetLow"),
        "n_analysts": payload.get("numberAnalysts"), "source": NAME,
    }])


def parse_estimates(symbol: str, payload: dict, metric: str, as_of: str) -> pd.DataFrame:
    key = {"eps": "eps", "revenue": "revenue"}[metric]
    rows = [
        {
            "symbol": symbol, "period": fiscal_period(d["period"]), "metric": metric, "as_of": as_of,
            "mean": d[f"{key}Avg"], "high": d.get(f"{key}High"), "low": d.get(f"{key}Low"),
            "n_analysts": d.get("numberAnalysts"), "source": NAME,
        }
        for d in (payload or {}).get("data", [])
        if d.get(f"{key}Avg") is not None and d.get("period")
    ]
    return pd.DataFrame(rows)


def parse_surprises(symbol: str, items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": symbol, "period": i["period"], "actual": i["actual"], "estimate": i["estimate"], "source": NAME}
        for i in items or []
        if i.get("actual") is not None and i.get("estimate") is not None
    ])


def fetch_analyst(symbol: str, api_key: str, as_of: str, timeout: int = 20) -> dict[str, pd.DataFrame]:
    """All analyst datasets for one symbol. Endpoints the plan does not cover return empty frames."""
    out: dict[str, pd.DataFrame] = {}
    calls = {
        "recommendations": lambda: parse_recommendations(symbol, _get("/stock/recommendation", api_key, timeout, symbol=symbol)),
        "surprises": lambda: parse_surprises(symbol, _get("/stock/earnings", api_key, timeout, symbol=symbol)),
        "price_targets": lambda: parse_price_target(symbol, _get("/stock/price-target", api_key, timeout, symbol=symbol), as_of),
        "eps": lambda: parse_estimates(symbol, _get("/stock/eps-estimate", api_key, timeout, symbol=symbol, freq="annual"), "eps", as_of),
        "revenue": lambda: parse_estimates(
            symbol, _get("/stock/revenue-estimate", api_key, timeout, symbol=symbol, freq="annual"), "revenue", as_of
        ),
    }
    for name, call in calls.items():
        try:
            out[name] = call()
        except Exception:  # 403 on paid-only endpoints, or a symbol the vendor doesn't cover
            out[name] = pd.DataFrame()
    return out
