"""Deterministic synthetic market data for demos, tests and offline development.

Prices follow a factor model with volatility clustering:

    r_i,t = beta_i * m_t + gamma_i * s_{sector(i),t} + e_i,t

where the market factor m_t has GARCH(1,1) variance, so the platform's statistics
and forecast models behave as they would on real equities. Two "sources" are
emitted (primary and a lightly-perturbed secondary with a few gaps and injected
breaks) so the reconciliation pipeline is exercised end to end.

Everything produced here is labelled with a ``synthetic-*`` source name and the
dashboard shows a banner whenever synthetic data is in use.
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from .fred import SERIES as MACRO_SERIES

PRIMARY = "synthetic-primary"
SECONDARY = "synthetic-secondary"
MACRO = "synthetic-macro"
FUNDAMENTALS = "synthetic-fundamentals"

# Sector annual drift and beta tilt, loosely shaped on long-run sector behaviour.
_SECTOR_PROFILE: dict[str, tuple[float, float]] = {
    "Information Technology": (0.14, 1.25),
    "Communication Services": (0.10, 1.10),
    "Consumer Discretionary": (0.09, 1.15),
    "Financials": (0.08, 1.10),
    "Industrials": (0.08, 1.05),
    "Health Care": (0.07, 0.85),
    "Materials": (0.06, 1.05),
    "Energy": (0.05, 1.10),
    "Real Estate": (0.04, 0.95),
    "Consumer Staples": (0.05, 0.65),
    "Utilities": (0.05, 0.55),
}


def trading_days(end: pd.Timestamp | None, days: int) -> pd.DatetimeIndex:
    end = (end or pd.Timestamp.today()).normalize()
    return pd.bdate_range(end=end, periods=int(days * 252 / 365))


def _garch_path(rng: np.random.Generator, n: int, omega: float, alpha: float, beta: float) -> np.ndarray:
    var = omega / (1 - alpha - beta)
    out = np.empty(n)
    shock = 0.0
    for t in range(n):
        var = omega + alpha * shock**2 + beta * var
        # Student-t innovations give realistic fat tails.
        shock = np.sqrt(var) * rng.standard_t(5) / np.sqrt(5 / 3)
        out[t] = shock
    return out


def generate_prices(
    constituents: pd.DataFrame, days: int = 3 * 365, end: pd.Timestamp | None = None, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = trading_days(end, days)
    n = len(dates)
    market = 0.0003 + _garch_path(rng, n, omega=1.5e-6, alpha=0.09, beta=0.89)
    sectors = {s: _garch_path(rng, n, omega=4e-7, alpha=0.06, beta=0.9) for s in constituents["sector"].unique()}

    frames = []
    date_str = dates.strftime("%Y-%m-%d")
    for sym, sector in zip(constituents["symbol"], constituents["sector"]):
        srng = np.random.default_rng(zlib.crc32(f"{seed}:{sym}".encode()))
        drift, tilt = _SECTOR_PROFILE.get(sector, (0.07, 1.0))
        beta = max(0.2, srng.normal(tilt, 0.2))
        gamma = srng.uniform(0.6, 1.2)
        idio_vol = srng.uniform(0.008, 0.022)
        alpha = (drift - 0.075) / 252 + srng.normal(0, 0.0002)
        idio = idio_vol * srng.standard_t(4, n) / np.sqrt(2)
        rets = alpha + beta * market + gamma * sectors[sector] + idio
        close = srng.lognormal(np.log(120), 0.8) * np.exp(np.cumsum(rets))
        intraday = np.abs(srng.normal(0, idio_vol, n))
        open_ = close * np.exp(srng.normal(0, idio_vol / 2, n))
        high = np.maximum(open_, close) * (1 + intraday)
        low = np.minimum(open_, close) * (1 - intraday)
        volume = srng.lognormal(np.log(2e6), 0.4, n) * (1 + 20 * np.abs(rets))
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "date": date_str,
                    "source": PRIMARY,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume.round(),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def secondary_from_primary(primary: pd.DataFrame, seed: int = 7, break_rate: float = 2e-4) -> pd.DataFrame:
    """A second vendor's view: tiny rounding noise, ~0.5% missing rows, rare bad prints."""
    rng = np.random.default_rng(seed)
    df = primary.copy()
    df["source"] = SECONDARY
    noise = 1 + rng.normal(0, 2e-4, len(df))
    breaks = rng.random(len(df)) < break_rate
    noise[breaks] *= 1 + rng.choice([-1, 1], breaks.sum()) * rng.uniform(0.02, 0.08, breaks.sum())
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] * noise).round(2)
    keep = rng.random(len(df)) > 0.005
    return df[keep].reset_index(drop=True)


_MACRO_START: dict[str, tuple[float, float, float]] = {
    # series: (start level, daily vol, mean-reversion target)
    "DGS10": (3.8, 0.05, 4.2),
    "DGS3MO": (4.9, 0.02, 4.3),
    "T10Y2Y": (-0.4, 0.03, 0.3),
    "VIXCLS": (17.0, 1.0, 17.0),
    "CPIAUCSL": (300.0, 0.0, 0.0),
    "UNRATE": (3.7, 0.0, 0.0),
    "FEDFUNDS": (5.3, 0.0, 0.0),
    "BAMLH0A0HYM2": (4.0, 0.05, 3.6),
}


def generate_macro(days: int = 3 * 365, end: pd.Timestamp | None = None, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    daily = trading_days(end, days)
    frames = []
    for sid in MACRO_SERIES:
        start, vol, target = _MACRO_START[sid]
        if sid in ("CPIAUCSL", "UNRATE", "FEDFUNDS"):
            dates = pd.date_range(daily[0], daily[-1], freq="MS")
            if sid == "CPIAUCSL":
                vals = start * np.exp(np.cumsum(rng.normal(0.0025, 0.002, len(dates))))
            elif sid == "UNRATE":
                vals = np.clip(start + np.cumsum(rng.normal(0.01, 0.08, len(dates))), 3.0, 8.0)
            else:
                vals = np.clip(start + np.cumsum(rng.choice([0, 0, 0, -0.25], len(dates))), 2.0, 6.0)
        else:
            dates = daily
            vals = np.empty(len(dates))
            x = start
            for i in range(len(dates)):
                x += 0.01 * (target - x) + vol * rng.standard_normal()
                vals[i] = max(x, 9.0) if sid == "VIXCLS" else x
        frames.append(
            pd.DataFrame(
                {"series_id": sid, "date": dates.strftime("%Y-%m-%d"), "value": np.round(vals, 3), "source": MACRO}
            )
        )
    return pd.concat(frames, ignore_index=True)


def generate_fundamentals(prices: pd.DataFrame, seed: int = 5) -> pd.DataFrame:
    """Annual statements consistent with each symbol's latest price (P/E ~ 8–45)."""
    rng = np.random.default_rng(seed)
    last = prices.sort_values("date").groupby("symbol")["close"].last()
    year_end = pd.Timestamp(prices["date"].max()).year - 1
    rows = []
    for sym, px in last.items():
        shares = rng.lognormal(np.log(4e8), 0.8)
        pe = rng.lognormal(np.log(20), 0.35)
        eps = px / pe
        margin = rng.uniform(0.05, 0.3)
        growth = rng.normal(0.07, 0.08)
        for k in range(4):
            yr = year_end - k
            scale = (1 + growth) ** -k
            e = eps * scale * rng.normal(1, 0.05)
            ni = e * shares
            rev = ni / margin
            assets = rev * rng.uniform(0.8, 2.5)
            liab = assets * rng.uniform(0.3, 0.75)
            for metric, val in (
                ("revenue", rev),
                ("net_income", ni),
                ("eps_diluted", e),
                ("total_assets", assets),
                ("total_liabilities", liab),
                ("equity", assets - liab),
                ("operating_cash_flow", ni * rng.uniform(1.0, 1.4)),
                ("shares_outstanding", shares),
            ):
                rows.append((sym, metric, f"{yr}-12-31", float(val), "10-K", FUNDAMENTALS))
    return pd.DataFrame(rows, columns=["symbol", "metric", "period_end", "value", "form", "source"])


# ---------------------------------------------------------------------------- news & analyst data

NEWSWIRE = "synthetic-newswire"
NEWS_RSS = "synthetic-rss"
ESTIMATES_A = "synthetic-estimates-a"
ESTIMATES_B = "synthetic-estimates-b"

_POSITIVE_TEMPLATES = [
    "{name} beats estimates as {noun} demand stays strong",
    "{name} raises guidance after record quarter",
    "{name} shares surge {pct}% on upbeat outlook",
    "Analysts upgrade {name}, citing improving margins",
    "{name} wins major contract, boosting growth outlook",
    "{name} announces share buyback and dividend increase",
    "{name} rallies as {noun} sales accelerate",
    "{name} stock climbs after strong earnings momentum",
    "Price target raised on {name} as profit outlook improves",
    "{name} gains on robust {noun} orders",
]
_NEGATIVE_TEMPLATES = [
    "{name} misses estimates as {noun} demand weakens",
    "{name} cuts guidance, warns of slowdown",
    "{name} shares plunge {pct}% after disappointing results",
    "Analysts downgrade {name} on margin pressure",
    "{name} faces probe over {noun} practices",
    "{name} slumps as {noun} headwinds mount",
    "{name} stock falls after weak outlook",
    "Price target cut on {name} amid rising risks",
    "{name} drops on lawsuit concerns",
    "{name} announces layoffs as losses widen",
]
_NEUTRAL_TEMPLATES = [
    "{name} to present at industry conference next week",
    "{name} schedules quarterly earnings call",
    "{name} names new head of {noun} division",
    "What to watch in {name}'s next report",
    "{name} files annual proxy statement",
    "{name} completes previously announced {noun} transaction",
    "{name} holds annual shareholder meeting",
    "Options activity picks up in {name} ahead of data",
]
_SECTOR_NOUNS = {
    "Information Technology": "cloud", "Communication Services": "advertising", "Consumer Discretionary": "consumer",
    "Financials": "lending", "Industrials": "industrial", "Health Care": "drug", "Materials": "chemicals",
    "Energy": "oil", "Real Estate": "leasing", "Consumer Staples": "grocery", "Utilities": "power",
}


def generate_news(
    primary: pd.DataFrame, members: pd.DataFrame, days: int = 180, seed: int = 21
) -> dict[str, pd.DataFrame]:
    """Headlines whose volume and tone follow each stock's actual daily moves (no look-ahead)."""
    closes = primary.pivot(index="date", columns="symbol", values="close").sort_index()
    rets = closes.pct_change()
    z = (rets / rets.rolling(63, min_periods=20).std()).clip(-4, 4)
    z = z.iloc[-int(days * 252 / 365):]
    info = members.set_index("symbol")
    wire, rss = [], []
    for sym in z.columns:
        name = info.at[sym, "name"] if sym in info.index else sym
        noun = _SECTOR_NOUNS.get(info.at[sym, "sector"] if sym in info.index else "", "business")
        srng = np.random.default_rng(zlib.crc32(f"news:{seed}:{sym}".encode()))
        for date, zv in z[sym].dropna().items():
            n = srng.poisson(0.3 + 0.9 * abs(zv))
            for _ in range(n):
                p_pos = 1 / (1 + np.exp(-1.4 * zv))
                roll = srng.random()
                if roll < 0.3:
                    tmpl = _NEUTRAL_TEMPLATES[srng.integers(len(_NEUTRAL_TEMPLATES))]
                elif srng.random() < p_pos:
                    tmpl = _POSITIVE_TEMPLATES[srng.integers(len(_POSITIVE_TEMPLATES))]
                else:
                    tmpl = _NEGATIVE_TEMPLATES[srng.integers(len(_NEGATIVE_TEMPLATES))]
                headline = tmpl.format(name=name, noun=noun, pct=max(1, round(abs(zv) * 1.5)))
                ts = pd.Timestamp(date) + pd.Timedelta(hours=int(srng.integers(13, 21)), minutes=int(srng.integers(0, 60)))
                row = {"symbol": sym, "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": headline,
                       "summary": None, "url": None}
                wire.append({**row, "source": NEWSWIRE})
                if srng.random() < 0.55:  # the same story syndicated to the second feed
                    rss.append({**row, "source": NEWS_RSS})
    cols = ["symbol", "published_at", "source", "headline", "summary", "url"]
    return {NEWSWIRE: pd.DataFrame(wire, columns=cols), NEWS_RSS: pd.DataFrame(rss, columns=cols)}


def generate_analyst(
    primary: pd.DataFrame, fundamentals: pd.DataFrame, days: int = 180, seed: int = 33
) -> dict[str, dict[str, pd.DataFrame]]:
    """Two estimate sources plus ratings, targets and surprises consistent with prices and filings."""
    closes = primary.pivot(index="date", columns="symbol", values="close").sort_index()
    closes.index = pd.to_datetime(closes.index)
    end = closes.index[-1]
    snaps = [d for d in pd.date_range(end - pd.Timedelta(days=days), end, freq="7D")]
    if snaps[-1] != end:
        snaps.append(end)
    f = fundamentals.sort_values("period_end")
    last = f.groupby(["symbol", "metric"])["value"].last().unstack()
    fy1, fy2 = end.year, end.year + 1

    est_a, est_b, recs, targets, surprises = [], [], [], [], []
    for sym in closes.columns:
        if sym not in last.index:
            continue
        srng = np.random.default_rng(zlib.crc32(f"est:{seed}:{sym}".encode()))
        px = closes[sym].ffill()
        eps0, rev0 = last.at[sym, "eps_diluted"], last.at[sym, "revenue"]
        g1, g2 = srng.normal(0.08, 0.06), srng.normal(0.09, 0.05)
        n_an = int(srng.integers(8, 42))
        disagree = srng.random() < 0.02  # a few symbols where the two vendors genuinely disagree
        for d in snaps:
            # Analysts partially follow the stock: consensus drifts with the log price change.
            drift = float(np.exp(0.35 * np.log(px.asof(d) / px.asof(snaps[0]))))
            as_of = d.strftime("%Y-%m-%d")
            for period, base in ((f"FY{fy1}", 1 + g1), (f"FY{fy2}", (1 + g1) * (1 + g2))):
                e = eps0 * base * drift
                r = rev0 * base ** 0.8 * drift ** 0.5
                disp = srng.uniform(0.05, 0.2)
                for metric, mean in (("eps", e), ("revenue", r)):
                    row = {"symbol": sym, "period": period, "metric": metric, "as_of": as_of, "mean": mean,
                           "high": mean * (1 + disp), "low": mean * (1 - disp), "n_analysts": n_an}
                    est_a.append({**row, "source": ESTIMATES_A})
                    off = 1.15 if disagree else 1 + srng.normal(0, 0.01)
                    est_b.append({**row, "mean": mean * off, "high": row["high"] * off, "low": row["low"] * off,
                                  "n_analysts": max(3, n_an - int(srng.integers(0, 6))), "source": ESTIMATES_B})
            upside = srng.normal(0.12, 0.06) - 0.3 * np.log(px.asof(d) / px.asof(d - pd.Timedelta(days=90)))
            mean_t = px.asof(d) * (1 + upside)
            for src, noise in ((ESTIMATES_A, 1.0), (ESTIMATES_B, 1 + srng.normal(0, 0.015))):
                targets.append({"symbol": sym, "as_of": as_of, "mean": mean_t * noise, "median": mean_t * noise * 1.01,
                                "high": mean_t * noise * 1.35, "low": mean_t * noise * 0.7, "n_analysts": n_an, "source": src})
        for k in range(6):
            month = (end - pd.DateOffset(months=5 - k)).strftime("%Y-%m")
            mom = float(np.log(px.asof(end - pd.DateOffset(months=5 - k)) / px.asof(end - pd.DateOffset(months=8 - k))))
            p_buy = float(np.clip(0.55 + 0.8 * mom + srng.normal(0, 0.05), 0.1, 0.9))
            buys = srng.binomial(n_an, p_buy)
            sells = srng.binomial(n_an - buys, 0.15)
            strong_b = srng.binomial(buys, 0.4)
            strong_s = srng.binomial(sells, 0.3)
            recs.append({"symbol": sym, "period": month, "strong_buy": strong_b, "buy": buys - strong_b,
                         "hold": n_an - buys - sells, "sell": sells - strong_s, "strong_sell": strong_s, "source": ESTIMATES_A})
        beat_bias = srng.normal(0.03, 0.03)
        for q in range(8):
            qend = (end - pd.DateOffset(months=3 * (q + 1))).to_period("Q").end_time.normalize()
            est = eps0 / 4 * (1 + srng.normal(0, 0.1))
            surprises.append({"symbol": sym, "period": qend.strftime("%Y-%m-%d"), "estimate": est,
                              "actual": est * (1 + srng.normal(beat_bias, 0.05)), "source": ESTIMATES_A})

    def split(rows):
        df = pd.DataFrame(rows)
        return {"eps": df[df.metric == "eps"], "revenue": df[df.metric == "revenue"]}

    a, b = split(est_a), split(est_b)
    tdf = pd.DataFrame(targets)
    return {
        ESTIMATES_A: {**a, "recommendations": pd.DataFrame(recs), "price_targets": tdf[tdf.source == ESTIMATES_A],
                      "surprises": pd.DataFrame(surprises)},
        ESTIMATES_B: {**b, "price_targets": tdf[tdf.source == ESTIMATES_B]},
    }
