"""Read-side service: loads the curated warehouse once per ingestion run and caches analytics."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import db
from .analytics import estimates as est
from .analytics import forecast, sentiment, stats, valuation
from .sources.fred import SERIES as MACRO_SERIES


class UnknownSymbol(LookupError):
    """Requested symbol is not an index member with price history."""


class NoData(LookupError):
    """The warehouse has not been loaded yet."""


@dataclass
class Snapshot:
    run_id: int
    panel: pd.DataFrame
    volume: pd.DataFrame
    index: pd.Series
    members: pd.DataFrame
    metrics: pd.DataFrame
    sector_idx: pd.DataFrame
    valuation: pd.DataFrame
    macro: pd.DataFrame
    alerts: list[dict]
    news: pd.DataFrame
    analyst: dict[str, pd.DataFrame]
    news_summary: pd.DataFrame
    analyst_summary: pd.DataFrame


class Platform:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._forecasts: dict[tuple[str, int], dict] = {}
        self.ingest_running = False

    # ------------------------------------------------------------------ loading

    def latest_run(self) -> dict | None:
        row = self.conn.execute(
            "SELECT id, started_at, finished_at, mode, status, detail FROM ingestion_runs "
            "WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        keys = ["id", "started_at", "finished_at", "mode", "status", "detail"]
        run = dict(zip(keys, row))
        run["detail"] = json.loads(run["detail"]) if run["detail"] else None
        return run

    def snapshot(self) -> Snapshot:
        run = self.latest_run()
        if run is None:
            raise NoData("No data loaded yet. Run `python -m ellie ingest` first.")
        with self._lock:
            if self._snapshot is None or self._snapshot.run_id != run["id"]:
                self._snapshot = self._build(run["id"])
                self._forecasts.clear()
            return self._snapshot

    def _build(self, run_id: int) -> Snapshot:
        prices = db.read_df(self.conn, "SELECT symbol, date, close, volume FROM prices")
        prices["date"] = pd.to_datetime(prices["date"])
        panel = prices.pivot(index="date", columns="symbol", values="close").sort_index()
        volume = prices.pivot(index="date", columns="symbol", values="volume").sort_index()
        members = db.read_df(self.conn, "SELECT * FROM constituents").set_index("symbol")
        members = members[members.index.isin(panel.columns)]
        panel, volume = panel[members.index], volume[members.index]

        macro = db.read_df(self.conn, "SELECT series_id, date, value FROM macro ORDER BY date")
        rf = macro[macro.series_id == "DGS3MO"]["value"]
        risk_free = float(rf.iloc[-1]) / 100 if len(rf) else 0.0

        index = stats.equal_weight_index(panel)
        metrics = stats.stock_metrics(panel, volume, index, risk_free)
        funda = db.read_df(self.conn, "SELECT symbol, metric, period_end, value FROM fundamentals")
        val = valuation.multiples(funda, metrics["price"], members["sector"])
        metrics = metrics.join(members[["name", "sector", "sub_industry"]])
        if not val.empty:
            metrics = metrics.join(val.drop(columns="sector"))

        as_of = panel.index[-1]
        news = db.read_df(self.conn, "SELECT id, symbol, published_at, source, sources, headline, url, sentiment, scorer FROM news")
        news = news[news["symbol"].isin(members.index)]
        analyst = {t: db.read_df(self.conn, f"SELECT * FROM {t}")
                   for t in ("estimates", "recommendations", "price_targets", "earnings_surprises")}
        news_summary = sentiment.symbol_summary(news, as_of)
        analyst_summary = est.summary(analyst["estimates"], analyst["recommendations"], analyst["price_targets"],
                                      analyst["earnings_surprises"], metrics["price"], as_of)
        for extra in (news_summary, analyst_summary):
            if not extra.empty:
                metrics = metrics.join(extra)
        date = as_of.strftime("%Y-%m-%d")
        alerts = stats.alerts(panel, volume) + sentiment.news_alerts(news_summary, date) + est.estimate_alerts(analyst_summary, date)
        rank = {"critical": 0, "serious": 1, "warning": 2, "good": 3}
        alerts.sort(key=lambda a: (rank[a["severity"]], a["symbol"]))
        return Snapshot(
            run_id=run_id,
            panel=panel,
            volume=volume,
            index=index,
            members=members,
            metrics=metrics,
            sector_idx=stats.sector_indices(panel, members["sector"]),
            valuation=val,
            macro=macro,
            alerts=alerts,
            news=news,
            analyst=analyst,
            news_summary=news_summary,
            analyst_summary=analyst_summary,
        )

    # ------------------------------------------------------------------ views

    def status(self) -> dict:
        run = self.latest_run()
        out = {"ready": run is not None, "ingest_running": self.ingest_running, "run": run}
        if run:
            snap = self.snapshot()
            out.update(
                as_of=snap.panel.index[-1].strftime("%Y-%m-%d"),
                symbols=int(snap.panel.shape[1]),
                trading_days=int(snap.panel.shape[0]),
                synthetic=run["mode"] == "synthetic",
            )
        return out

    def overview(self) -> dict:
        s = self.snapshot()
        idx = s.index
        index_returns = stats.period_returns(idx.to_frame()).iloc[0].to_dict()
        sector_returns = stats.period_returns(s.sector_idx)
        counts = s.members["sector"].value_counts()
        sector_returns["members"] = counts.reindex(sector_returns.index)
        by_sev: dict[str, int] = {}
        for a in s.alerts:
            by_sev[a["severity"]] = by_sev.get(a["severity"], 0) + 1
        return {
            "as_of": idx.index[-1].strftime("%Y-%m-%d"),
            "index": {
                "name": "S&P 500 equal-weight (computed)",
                "level": float(idx.iloc[-1]),
                "returns": index_returns,
                "series": series_json(idx),
                "drawdown": series_json(stats.drawdown_series(idx)),
                "vol_1y": float(idx.pct_change().iloc[-252:].std() * np.sqrt(252)),
            },
            "breadth": stats.breadth(s.panel),
            "breadth_history": series_json(stats.breadth_history(s.panel)),
            "sectors": records(sector_returns.rename_axis("sector").reset_index()),
            "alert_counts": by_sev,
            "top_movers": {
                "gainers": records(self._movers(ascending=False)),
                "losers": records(self._movers(ascending=True)),
            },
        }

    def _movers(self, ascending: bool, n: int = 8) -> pd.DataFrame:
        m = self.snapshot().metrics
        return m.sort_values("ret_1D", ascending=ascending).head(n).reset_index()[["symbol", "name", "sector", "price", "ret_1D"]]

    def screener(self) -> list[dict]:
        m = self.snapshot().metrics.reset_index()
        return records(m)

    def stock(self, symbol: str) -> dict:
        s = self.snapshot()
        symbol = symbol.upper()
        if symbol not in s.panel.columns:
            raise UnknownSymbol(f"{symbol} is not a tracked S&P 500 member")
        close = s.panel[symbol].dropna()
        member = s.members.loc[symbol]
        funda = db.read_df(
            self.conn,
            "SELECT metric, period_end, value, source FROM fundamentals WHERE symbol=? ORDER BY period_end",
            (symbol,),
        )
        funda_wide = (
            funda.pivot(index="period_end", columns="metric", values="value").reset_index()
            if not funda.empty else pd.DataFrame()
        )
        peers = s.metrics[s.metrics["sector"] == member["sector"]]
        return {
            "symbol": symbol,
            "profile": {k: none_if_nan(v) for k, v in member.to_dict().items()},
            "metrics": {k: none_if_nan(v) for k, v in s.metrics.loc[symbol].to_dict().items()},
            "sector_median": {k: none_if_nan(v) for k, v in peers.median(numeric_only=True).to_dict().items()},
            "history": series_json(close),
            "relative": series_json((close / close.iloc[0]) / (s.index.reindex(close.index) / s.index.reindex(close.index).iloc[0]) * 100),
            "drawdown": series_json(stats.drawdown_series(close)),
            "fundamentals": records(funda_wide),
            "fundamentals_source": funda["source"].iloc[0] if not funda.empty else None,
        }

    def forecast(self, symbol: str, horizon: int = 21) -> dict:
        s = self.snapshot()
        symbol = symbol.upper()
        if symbol not in s.panel.columns:
            raise UnknownSymbol(f"{symbol} is not a tracked S&P 500 member")
        key = (symbol, horizon)
        if key not in self._forecasts:
            if len(self._forecasts) >= 256:
                self._forecasts.pop(next(iter(self._forecasts)))
            self._forecasts[key] = self._forecast_bundle(s, symbol, horizon)
        return self._forecasts[key]

    @staticmethod
    def _forecast_bundle(s: Snapshot, symbol: str, horizon: int) -> dict:
        close = s.panel[symbol].dropna()
        fc = forecast.price_forecast(close, horizon)
        bt = forecast.backtest(close, horizon)
        return {
            "symbol": symbol,
            "horizon_days": horizon,
            "model": "ARIMA(1,0,1) mean + GARCH(1,1) Monte Carlo, Student-t shocks",
            "last_close": float(close.iloc[-1]),
            "last_date": close.index[-1].strftime("%Y-%m-%d"),
            "paths": records(fc.rename_axis("date").reset_index().assign(date=lambda d: d["date"].dt.strftime("%Y-%m-%d"))),
            "volatility": forecast.volatility_forecast(close, horizon),
            "backtest": records(bt.rename_axis("model").reset_index()) if not bt.empty else [],
            "direction": forecast.direction_model(close, s.index, extra=Platform._alt_features(s, symbol, close.index)),
        }

    @staticmethod
    def _alt_features(s: Snapshot, symbol: str, index: pd.DatetimeIndex) -> pd.DataFrame:
        """News sentiment and estimate revisions as they were known on each trading day."""
        feats = sentiment.feature_frame(s.news, index, symbol) if not s.news.empty else pd.DataFrame(index=index)
        if not s.analyst["estimates"].empty:
            rev = est.revision_feature(s.analyst["estimates"], symbol, index)
            # Snapshots only exist for part of the history; mark the rest unknown so it is filled neutrally.
            first = s.analyst["estimates"]["as_of"].min()
            feats["eps_rev_30d"] = rev.where(index >= pd.Timestamp(first) + pd.Timedelta(days=30))
        if "sent_7d" in feats:
            first_news = pd.Timestamp(s.news["published_at"].min()[:10]) + pd.Timedelta(days=7)
            feats.loc[feats.index < first_news, ["sent_7d", "news_ratio"]] = np.nan
        return feats

    # ------------------------------------------------------------------ news & analysts

    def sentiment_overview(self) -> dict:
        s = self.snapshot()
        if s.news.empty:
            return {"available": False}
        news = s.news.assign(sector=s.news["symbol"].map(s.members["sector"]))
        mean, count = sentiment.daily_sentiment(news)
        total = count.sum(axis=1)
        daily = (mean.fillna(0) * count).sum(axis=1) / total.replace(0, np.nan)
        weekly = (daily.fillna(0) * total).rolling(7, min_periods=1).sum() / total.rolling(7, min_periods=1).sum()
        ns = s.news_summary.join(s.members[["name", "sector"]])
        t = pd.to_datetime(news["published_at"]).dt.tz_localize(None)
        end = s.panel.index[-1] + pd.Timedelta(days=1)
        last7 = news[t >= end - pd.Timedelta(days=7)]
        last30 = news[t >= end - pd.Timedelta(days=30)]
        sectors = pd.DataFrame({
            "sent_7d": last7.groupby("sector")["sentiment"].mean(),
            "sent_30d": last30.groupby("sector")["sentiment"].mean(),
            "news_7d": last7.groupby("sector").size(),
        }).rename_axis("sector").reset_index()
        keep = ["symbol", "name", "sector", "sent_7d", "sent_30d", "news_7d", "news_ratio", "sent_change"]
        active = ns[ns["news_7d"] >= 3].reset_index()
        latest = news.sort_values("published_at", ascending=False).head(150)
        return {
            "available": True,
            "scorer": news["scorer"].mode().iloc[0],
            "sentiment_7d": float(last7["sentiment"].mean()) if len(last7) else None,
            "sentiment_30d": float(last30["sentiment"].mean()) if len(last30) else None,
            "articles_7d": int(len(last7)),
            "pct_positive_7d": float((last7["sentiment"] > 0.1).mean()) if len(last7) else None,
            "pct_negative_7d": float((last7["sentiment"] < -0.1).mean()) if len(last7) else None,
            "multi_source_share": float(news["sources"].str.contains(",").mean()),
            "series": series_json(weekly),
            "volume": series_json(total.astype(float)),
            "sectors": records(sectors),
            "most_positive": records(active.nlargest(12, "sent_7d")[keep]),
            "most_negative": records(active.nsmallest(12, "sent_7d")[keep]),
            "busiest": records(ns.reset_index().nlargest(12, "news_ratio")[keep]),
            "latest": records(latest.assign(name=latest["symbol"].map(s.members["name"]))[
                ["symbol", "name", "published_at", "headline", "url", "sentiment", "sources"]]),
        }

    def stock_news(self, symbol: str) -> dict:
        s = self.snapshot()
        symbol = self._check(symbol)
        sub = s.news[s.news["symbol"] == symbol].sort_values("published_at", ascending=False)
        feats = sentiment.feature_frame(s.news, s.panel.index[-126:], symbol) if not sub.empty else pd.DataFrame()
        return {
            "symbol": symbol,
            "summary": {k: none_if_nan(v) for k, v in (s.news_summary.loc[symbol].to_dict().items()
                                                         if symbol in s.news_summary.index else [])},
            "sentiment_7d": series_json(feats["sent_7d"]) if "sent_7d" in feats else {"dates": [], "values": []},
            "items": records(sub.head(60)[["published_at", "headline", "url", "sentiment", "sources", "scorer"]]),
        }

    def stock_estimates(self, symbol: str) -> dict:
        s = self.snapshot()
        symbol = self._check(symbol)
        a = {k: v[v["symbol"] == symbol] for k, v in s.analyst.items()}
        cons = est.consensus(a["estimates"])
        year = s.panel.index[-1].year
        revisions = {}
        for period in (f"FY{year}", f"FY{year + 1}"):
            ser = est.revision_series(a["estimates"], symbol, period)
            if not ser.empty:
                revisions[period] = series_json(ser)
        recs = a["recommendations"].groupby("period")[est.RATING_COLS].median().reset_index() if not a["recommendations"].empty else pd.DataFrame()
        tgt = a["price_targets"].sort_values("as_of").groupby("source").tail(1)
        surprises = a["earnings_surprises"].sort_values("period").drop_duplicates("period", keep="last").tail(8)
        surprises = surprises.assign(surprise=(surprises["actual"] - surprises["estimate"]) / surprises["estimate"].abs())
        return {
            "symbol": symbol,
            "price": float(s.metrics.at[symbol, "price"]),
            "summary": {k: none_if_nan(v) for k, v in (s.analyst_summary.loc[symbol].to_dict().items()
                                                         if symbol in s.analyst_summary.index else [])},
            "consensus": records(cons.drop(columns="symbol")) if not cons.empty else [],
            "revisions": revisions,
            "recommendations": records(recs),
            "targets": records(tgt[["source", "as_of", "mean", "high", "low", "n_analysts"]]),
            "surprises": records(surprises[["period", "estimate", "actual", "surprise"]]),
            "sources": sorted(set().union(*[set(v["source"]) for v in a.values() if not v.empty])),
        }

    def _check(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol not in self.snapshot().panel.columns:
            raise UnknownSymbol(f"{symbol} is not a tracked S&P 500 member")
        return symbol

    def sectors(self) -> dict:
        s = self.snapshot()
        corr = stats.sector_correlation(s.sector_idx)
        med = s.metrics.groupby("sector").median(numeric_only=True)
        cols = [c for c in ["vol_1y", "beta", "sharpe_1y", "max_dd_1y", "var95_1d", "pe", "ret_YTD", "ret_1Y"] if c in med]
        return {
            "series": {c: series_json(s.sector_idx[c]) for c in s.sector_idx.columns},
            "correlation": {"labels": list(corr.columns), "matrix": [[round(float(v), 3) for v in row] for row in corr.to_numpy()]},
            "medians": records(med[cols].rename_axis("sector").reset_index()),
        }

    def risk(self) -> dict:
        m = self.snapshot().metrics.reset_index()
        keep = ["symbol", "name", "sector", "vol_1y", "beta", "var95_1d", "es95_1d", "max_dd_1y"]
        return {
            "highest_var": records(m.nlargest(15, "var95_1d")[keep]),
            "deepest_drawdown": records(m.nsmallest(15, "max_dd_1y")[keep]),
            "scatter": records(m[["symbol", "sector", "vol_1y", "ret_1Y", "beta"]]),
        }

    def macro(self) -> list[dict]:
        mac = self.snapshot().macro
        out = []
        for sid, (label, unit) in MACRO_SERIES.items():
            ser = mac[mac.series_id == sid]
            if ser.empty:
                continue
            out.append({
                "id": sid, "label": label, "unit": unit,
                "latest": float(ser["value"].iloc[-1]), "latest_date": ser["date"].iloc[-1],
                "series": {"dates": ser["date"].tolist(), "values": [float(v) for v in ser["value"]]},
            })
        return out

    def alerts(self) -> list[dict]:
        s = self.snapshot()
        names = s.members["name"]
        return [{**a, "name": names.get(a["symbol"])} for a in s.alerts]

    def quality(self) -> dict:
        runs = db.read_df(self.conn, "SELECT id, started_at, finished_at, mode, status FROM ingestion_runs ORDER BY id DESC LIMIT 20")
        run = self.latest_run()
        rid = run["id"] if run else -1
        return {
            "runs": records(runs),
            "sources": records(db.read_df(self.conn, "SELECT source, dataset, rows, symbols, errors FROM source_stats WHERE run_id=?", (rid,))),
            "issue_summary": records(db.read_df(
                self.conn,
                "SELECT check_name, severity, COUNT(*) AS n FROM quality_issues WHERE run_id=? GROUP BY 1, 2 ORDER BY n DESC",
                (rid,),
            )),
            "issues": records(db.read_df(
                self.conn,
                "SELECT check_name, severity, symbol, date, detail FROM quality_issues WHERE run_id=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, date DESC LIMIT 200",
                (rid,),
            )),
            "coverage": {
                "multi_source_share": float(db.read_df(self.conn, "SELECT AVG(n_sources >= 2) AS v FROM prices")["v"].iloc[0] or 0),
            },
        }


# ---------------------------------------------------------------------- JSON helpers

def none_if_nan(v):
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else float(v)
    return v


def records(df: pd.DataFrame) -> list[dict]:
    cols = list(df.columns)
    return [{c: none_if_nan(v) for c, v in zip(cols, row)} for row in df.itertuples(index=False, name=None)]


def series_json(s: pd.Series) -> dict:
    s = s.dropna()
    return {"dates": s.index.strftime("%Y-%m-%d").tolist(), "values": [round(float(v), 6) for v in s.to_numpy()]}
