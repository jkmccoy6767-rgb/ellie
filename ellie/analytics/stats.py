"""Descriptive statistics, risk metrics, market breadth and alert rules.

All functions take a wide price panel (index = trading dates, columns = symbols)
so they vectorise across the whole index at once.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as sps

TRADING_DAYS = 252
PERIODS = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}


def daily_returns(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.pct_change(fill_method=None)


def equal_weight_index(panel: pd.DataFrame, base: float = 100.0) -> pd.Series:
    """Equal-weighted index of all members (rebalanced daily)."""
    rets = daily_returns(panel).mean(axis=1, skipna=True).fillna(0.0)
    return (base * (1 + rets).cumprod()).rename("index")


def period_returns(panel: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for label, n in PERIODS.items():
        if len(panel) > n:
            out[label] = panel.iloc[-1] / panel.iloc[-1 - n] - 1
    last_date = panel.index[-1]
    ytd_base = panel[panel.index < pd.Timestamp(year=last_date.year, month=1, day=1)]
    if not ytd_base.empty:
        out["YTD"] = panel.iloc[-1] / ytd_base.iloc[-1] - 1
    return pd.DataFrame(out)


def max_drawdown(prices: pd.DataFrame | pd.Series) -> pd.Series | float:
    peak = prices.cummax()
    return (prices / peak - 1).min()


def drawdown_series(prices: pd.Series) -> pd.Series:
    return prices / prices.cummax() - 1


def historical_var(returns: pd.DataFrame, level: float = 0.95) -> tuple[pd.Series, pd.Series]:
    """One-day historical VaR and expected shortfall, reported as positive losses."""
    q = returns.quantile(1 - level)
    es = returns.where(returns.le(q)).mean()
    return -q, -es


def parametric_var(returns: pd.DataFrame, level: float = 0.95) -> pd.Series:
    z = sps.norm.ppf(1 - level)
    return -(returns.mean() + z * returns.std())


def beta(returns: pd.DataFrame, market: pd.Series) -> pd.Series:
    aligned = returns.join(market.rename("__mkt"), how="inner").dropna(subset=["__mkt"])
    mkt = aligned.pop("__mkt")
    cov = aligned.apply(lambda col: col.cov(mkt))
    return cov / mkt.var()


def rsi(panel: pd.DataFrame, window: int = 14) -> pd.Series:
    delta = panel.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).iloc[-1]


def stock_metrics(
    panel: pd.DataFrame, volume: pd.DataFrame, market: pd.Series, risk_free: float = 0.0
) -> pd.DataFrame:
    """One row per symbol with return, risk, momentum and liquidity statistics."""
    rets = daily_returns(panel)
    last_year = rets.iloc[-TRADING_DAYS:]
    mkt_rets = market.pct_change().iloc[-TRADING_DAYS:]
    window_prices = panel.iloc[-TRADING_DAYS:]

    ann_vol = last_year.std() * np.sqrt(TRADING_DAYS)
    ann_ret = (1 + last_year.mean()) ** TRADING_DAYS - 1
    var95, es95 = historical_var(last_year)
    hi52, lo52 = window_prices.max(), window_prices.min()
    last = panel.ffill().iloc[-1]

    df = pd.DataFrame(
        {
            "price": last,
            "vol_1y": ann_vol,
            "vol_3m": rets.iloc[-63:].std() * np.sqrt(TRADING_DAYS),
            "beta": beta(last_year, mkt_rets),
            "sharpe_1y": (ann_ret - risk_free) / ann_vol.replace(0, np.nan),
            "max_dd_1y": max_drawdown(window_prices),
            "var95_1d": var95,
            "es95_1d": es95,
            "pct_from_52w_high": last / hi52 - 1,
            "pct_from_52w_low": last / lo52 - 1,
            "momentum_12_1": panel.iloc[-21] / panel.iloc[-TRADING_DAYS] - 1 if len(panel) > TRADING_DAYS else np.nan,
            "rsi_14": rsi(panel),
            "above_50dma": last > panel.iloc[-50:].mean(),
            "above_200dma": last > panel.iloc[-200:].mean(),
            "avg_volume_20d": volume.iloc[-20:].mean(),
        }
    )
    return df.join(period_returns(panel).add_prefix("ret_")).rename_axis("symbol")


def sector_indices(panel: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """Equal-weighted index per sector (columns = sector)."""
    rets = daily_returns(panel)
    grouped = rets.T.groupby(sectors.reindex(rets.columns)).mean().T.fillna(0.0)
    return 100 * (1 + grouped).cumprod()


def sector_correlation(sector_idx: pd.DataFrame, window: int = TRADING_DAYS) -> pd.DataFrame:
    return sector_idx.pct_change().iloc[-window:].corr()


def breadth(panel: pd.DataFrame) -> dict:
    last = panel.ffill().iloc[-1]
    prev = panel.ffill().iloc[-2]
    live = last.notna() & prev.notna()
    window = panel.iloc[-TRADING_DAYS:]
    return {
        "advancers": int((last[live] > prev[live]).sum()),
        "decliners": int((last[live] < prev[live]).sum()),
        "unchanged": int((last[live] == prev[live]).sum()),
        "pct_above_50dma": float((last > panel.iloc[-50:].mean()).mean()),
        "pct_above_200dma": float((last > panel.iloc[-200:].mean()).mean()),
        "new_52w_highs": int((last >= window.max()).sum()),
        "new_52w_lows": int((last <= window.min()).sum()),
    }


def breadth_history(panel: pd.DataFrame, window: int = 200) -> pd.Series:
    """Share of members trading above their own ``window``-day moving average."""
    ma = panel.rolling(window, min_periods=window).mean()
    valid = ma.notna()
    return ((panel > ma) & valid).sum(axis=1).div(valid.sum(axis=1).replace(0, np.nan)).dropna()


def alerts(panel: pd.DataFrame, volume: pd.DataFrame, sigma: float = 3.0) -> list[dict]:
    """Rule-based alerts on the latest session. Severity follows the size of the signal."""
    rets = daily_returns(panel)
    last_ret = rets.iloc[-1]
    vol63 = rets.iloc[-64:-1].std()
    z = last_ret / vol63
    window = panel.iloc[-TRADING_DAYS:]
    last = panel.iloc[-1]
    vol_ratio = volume.iloc[-1] / volume.iloc[-21:-1].mean()
    date = panel.index[-1].strftime("%Y-%m-%d")

    out: list[dict] = []
    for sym, zv in z[z.abs() >= sigma].items():
        out.append({
            "symbol": sym, "date": date, "rule": "outsized_move",
            "severity": "critical" if abs(zv) >= 5 else "serious",
            "detail": f"{last_ret[sym]:+.1%} move ({zv:+.1f}σ vs 3-month volatility)",
        })
    for sym in last[last >= window.max()].index:
        out.append({"symbol": sym, "date": date, "rule": "new_52w_high", "severity": "good",
                    "detail": f"New 52-week high at {last[sym]:,.2f}"})
    for sym in last[last <= window.min()].index:
        out.append({"symbol": sym, "date": date, "rule": "new_52w_low", "severity": "warning",
                    "detail": f"New 52-week low at {last[sym]:,.2f}"})
    for sym, r in vol_ratio[vol_ratio >= 3].items():
        out.append({"symbol": sym, "date": date, "rule": "volume_spike", "severity": "warning",
                    "detail": f"Volume {r:.1f}× the 20-day average"})
    rank = {"critical": 0, "serious": 1, "warning": 2, "good": 3}
    return sorted(out, key=lambda a: (rank[a["severity"]], a["symbol"]))
