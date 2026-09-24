"""Analyst estimates: cross-source consensus, revisions, ratings, price targets, surprises."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..quality import Issue

RATING_COLS = ["strong_buy", "buy", "hold", "sell", "strong_sell"]


def _latest_per_source(df: pd.DataFrame, keys: list[str], as_of: str | None = None) -> pd.DataFrame:
    """Latest snapshot per source (optionally as it stood on ``as_of``)."""
    if as_of is not None:
        df = df[df["as_of"] <= as_of]
    if df.empty:
        return df
    return df.sort_values("as_of").groupby([*keys, "source"]).tail(1)


def consensus(estimates: pd.DataFrame, as_of: str | None = None) -> pd.DataFrame:
    """Median across sources of each source's latest mean, per symbol/period/metric."""
    keys = ["symbol", "period", "metric"]
    latest = _latest_per_source(estimates, keys, as_of)
    if latest.empty:
        return pd.DataFrame(columns=[*keys, "mean", "high", "low", "n_analysts", "n_sources", "spread"])
    g = latest.groupby(keys)
    out = g.agg(mean=("mean", "median"), high=("high", "max"), low=("low", "min"),
                n_analysts=("n_analysts", "max"), n_sources=("source", "nunique"))
    out["spread"] = (g["mean"].max() - g["mean"].min()) / g["mean"].median().abs().replace(0, np.nan)
    return out.reset_index()


def mismatch_issues(estimates: pd.DataFrame, targets: pd.DataFrame, tolerance: float) -> list[Issue]:
    issues = []
    cons = consensus(estimates)
    for r in cons[(cons["n_sources"] >= 2) & (cons["spread"] > tolerance)].itertuples():
        issues.append(Issue("estimate_mismatch", "warning", r.symbol, r.period,
                            f"{r.metric} consensus differs {r.spread:.0%} across sources"))
    t = _latest_per_source(targets, ["symbol"])
    if not t.empty:
        g = t.groupby("symbol")["mean"]
        spread = (g.max() - g.min()) / g.median()
        for sym, s in spread[(g.count() >= 2) & (spread > tolerance)].items():
            issues.append(Issue("target_mismatch", "warning", sym, None, f"price-target consensus differs {s:.0%} across sources"))
    return issues


def _fy(period: str) -> int:
    return int(period[2:6])


def summary(
    estimates: pd.DataFrame,
    recs: pd.DataFrame,
    targets: pd.DataFrame,
    surprises: pd.DataFrame,
    prices: pd.Series,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """One row per symbol: forward P/E, estimate growth and revisions, rating, target upside, beat rate."""
    year = as_of.year
    today = as_of.strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []

    if not estimates.empty:
        now = consensus(estimates)
        eps = now[now["metric"] == "eps"].copy()
        eps["fy"] = eps["period"].map(_fy)
        cur = eps[eps["fy"] == year].set_index("symbol")
        nxt = eps[eps["fy"] == year + 1].set_index("symbol")
        rev = now[(now["metric"] == "revenue")].assign(fy=lambda d: d["period"].map(_fy))
        rcur, rnxt = rev[rev["fy"] == year].set_index("symbol"), rev[rev["fy"] == year + 1].set_index("symbol")

        def revision(days: int) -> pd.Series:
            past = consensus(estimates, (as_of - pd.Timedelta(days=days)).strftime("%Y-%m-%d"))
            past = past[(past["metric"] == "eps") & (past["period"] == f"FY{year + 1}")].set_index("symbol")["mean"]
            return nxt["mean"] / past.reindex(nxt.index) - 1

        fwd_eps = cur["mean"].reindex(prices.index)
        parts.append(pd.DataFrame({
            "eps_fy1": fwd_eps,
            "eps_fy2": nxt["mean"].reindex(prices.index),
            "eps_growth_fwd": (nxt["mean"] / cur["mean"] - 1).where(cur["mean"] > 0),
            "revenue_growth_fwd": rnxt["mean"] / rcur["mean"] - 1,
            "fwd_pe": (prices / fwd_eps).where(fwd_eps > 0),
            "eps_rev_30d": revision(30),
            "eps_rev_90d": revision(90),
            "n_analysts": cur["n_analysts"],
        }))

    if not recs.empty:
        latest = recs.sort_values("period").groupby(["symbol", "source"]).tail(1).groupby("symbol")[RATING_COLS].median()
        total = latest.sum(axis=1).replace(0, np.nan)
        score = (latest * [1, 2, 3, 4, 5]).sum(axis=1) / total
        months = sorted(recs["period"].unique())
        prior = pd.Series(dtype=float)
        if len(months) >= 4:
            old = recs[recs["period"] == months[-4]].groupby("symbol")[RATING_COLS].median()
            prior = (old * [1, 2, 3, 4, 5]).sum(axis=1) / old.sum(axis=1).replace(0, np.nan)
        parts.append(pd.DataFrame({
            "rec_score": score,
            "pct_buy": (latest["strong_buy"] + latest["buy"]) / total,
            "rec_change_3m": score - prior.reindex(score.index),
        }))

    if not targets.empty:
        t = _latest_per_source(targets, ["symbol"]).groupby("symbol").agg(
            target_mean=("mean", "median"), target_high=("high", "max"), target_low=("low", "min"))
        t["target_upside"] = t["target_mean"] / prices.reindex(t.index) - 1
        parts.append(t)

    if not surprises.empty:
        s = surprises.sort_values("period").drop_duplicates(["symbol", "period"], keep="last")
        s = s[s["period"] <= today].groupby("symbol").tail(8)
        s = s.assign(beat=s["actual"] > s["estimate"],
                     surprise=(s["actual"] - s["estimate"]) / s["estimate"].abs().replace(0, np.nan))
        g = s.groupby("symbol")
        parts.append(pd.DataFrame({"beat_rate": g["beat"].mean(), "avg_surprise": g["surprise"].mean()}))

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, axis=1).rename_axis("symbol")


def revision_series(estimates: pd.DataFrame, symbol: str, period: str, metric: str = "eps") -> pd.Series:
    """Consensus over time for one symbol/period, from the stored snapshots."""
    sub = estimates[(estimates["symbol"] == symbol) & (estimates["period"] == period) & (estimates["metric"] == metric)]
    if sub.empty:
        return pd.Series(dtype=float)
    wide = sub.pivot_table(index="as_of", columns="source", values="mean").sort_index().ffill()
    s = wide.median(axis=1)
    s.index = pd.to_datetime(s.index)
    return s


def revision_feature(estimates: pd.DataFrame, symbol: str, index: pd.DatetimeIndex, window: int = 30) -> pd.Series:
    """Trailing ``window``-day change in next-year EPS consensus, known as of each trading day."""
    if estimates.empty:
        return pd.Series(0.0, index=index)
    years = sorted({_fy(p) for p in estimates.loc[estimates["symbol"] == symbol, "period"]})
    if not years:
        return pd.Series(0.0, index=index)
    out = pd.Series(np.nan, index=index)
    for y in years:
        s = revision_series(estimates, symbol, f"FY{y}")
        if s.empty:
            continue
        daily = s.reindex(pd.date_range(s.index.min(), index.max())).ffill()
        chg = daily / daily.shift(window) - 1
        # FY(y) is the "next year" estimate during calendar year y-1.
        mask = index.year == y - 1
        out[mask] = chg.reindex(index[mask]).to_numpy()
    return out.fillna(0.0)


def estimate_alerts(summary_df: pd.DataFrame, date: str) -> list[dict]:
    out = []
    if summary_df.empty or "eps_rev_30d" not in summary_df:
        return out
    for sym, v in summary_df["eps_rev_30d"].dropna().items():
        if v <= -0.05:
            out.append({"symbol": sym, "date": date, "rule": "estimate_cut", "severity": "warning",
                        "detail": f"Next-year EPS consensus cut {v:.1%} in 30 days"})
        elif v >= 0.05:
            out.append({"symbol": sym, "date": date, "rule": "estimate_raise", "severity": "good",
                        "detail": f"Next-year EPS consensus raised {v:+.1%} in 30 days"})
    return out
