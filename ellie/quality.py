"""Validation checks and cross-source reconciliation for price data."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Preferred order when sources disagree and there is no majority.
SOURCE_PRIORITY = ["yahoo", "stooq", "synthetic-primary", "synthetic-secondary"]


@dataclass
class Issue:
    check_name: str
    severity: str  # "info" | "warning" | "critical"
    symbol: str | None
    date: str | None
    detail: str

    def as_row(self, run_id: int) -> tuple:
        return (run_id, self.check_name, self.severity, self.symbol, self.date, self.detail)


def validate_prices(raw: pd.DataFrame, max_abs_return: float = 0.4) -> tuple[pd.DataFrame, list[Issue]]:
    """Drop impossible rows and flag suspicious ones. Returns (clean_rows, issues)."""
    issues: list[Issue] = []
    if raw.empty:
        return raw, issues
    df = raw.drop_duplicates(["symbol", "date", "source"], keep="last").copy()

    bad = ~(df["close"] > 0)
    for r in df[bad].itertuples():
        issues.append(Issue("non_positive_close", "critical", r.symbol, r.date, f"{r.source}: close={r.close}"))
    df = df[~bad]

    has_range = df["high"].notna() & df["low"].notna()
    inverted = has_range & (df["high"] < df["low"])
    for r in df[inverted].itertuples():
        issues.append(Issue("high_below_low", "warning", r.symbol, r.date, f"{r.source}: high {r.high:.2f} < low {r.low:.2f}"))

    df = df.sort_values(["source", "symbol", "date"])
    ret = df.groupby(["source", "symbol"])["close"].pct_change()
    jumps = ret.abs() > max_abs_return
    for r, v in zip(df[jumps].itertuples(), ret[jumps]):
        issues.append(Issue("extreme_move", "warning", r.symbol, r.date, f"{r.source}: {v:+.1%} one-day move"))

    return df.reset_index(drop=True), issues


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.index(source) if source in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def reconcile(raw: pd.DataFrame, tolerance: float = 0.005) -> tuple[pd.DataFrame, list[Issue]]:
    """Merge per-source prices into one curated close per symbol/date.

    * one source      -> take it
    * two sources     -> take the higher-priority source; flag if they differ > tolerance
    * three or more   -> take the median (majority vote); flag outliers
    """
    issues: list[Issue] = []
    if raw.empty:
        return pd.DataFrame(columns=["symbol", "date", "close", "volume", "n_sources"]), issues

    closes = raw.pivot_table(index=["symbol", "date"], columns="source", values="close", aggfunc="last")
    volumes = raw.pivot_table(index=["symbol", "date"], columns="source", values="volume", aggfunc="last")
    ordered = sorted(closes.columns, key=_priority)
    closes = closes[ordered]
    n_sources = closes.notna().sum(axis=1)

    # First non-null in priority order.
    preferred = closes.bfill(axis=1).iloc[:, 0]
    median = closes.median(axis=1)
    curated = np.where(n_sources >= 3, median, preferred)

    spread = (closes.max(axis=1) - closes.min(axis=1)) / closes.min(axis=1)
    flagged = spread[(n_sources >= 2) & (spread > tolerance)]
    for (sym, date), s in flagged.items():
        row = closes.loc[(sym, date)].dropna()
        detail = ", ".join(f"{src}={v:.2f}" for src, v in row.items())
        issues.append(Issue("source_mismatch", "warning" if s < 0.05 else "critical", sym, date, f"{s:.2%} spread ({detail})"))

    volume = volumes.reindex(columns=sorted(volumes.columns, key=_priority)).bfill(axis=1).iloc[:, 0]
    out = pd.DataFrame(
        {"close": curated, "volume": volume.reindex(closes.index).to_numpy(), "n_sources": n_sources.to_numpy()},
        index=closes.index,
    ).reset_index()
    return out, issues


def coverage_issues(curated: pd.DataFrame, symbols: list[str], min_ratio: float = 0.8) -> list[Issue]:
    issues: list[Issue] = []
    if curated.empty:
        return [Issue("no_price_data", "critical", None, None, "curated price table is empty")]
    counts = curated.groupby("symbol").size()
    expected = curated["date"].nunique()
    for sym in symbols:
        n = int(counts.get(sym, 0))
        if n == 0:
            issues.append(Issue("missing_symbol", "critical", sym, None, "no price history from any source"))
        elif n < min_ratio * expected:
            issues.append(Issue("sparse_history", "info", sym, None, f"{n}/{expected} trading days (recent IPO or data gap)"))
    return issues
