"""Read-side service: loads the curated warehouse once per ingestion run and caches analytics."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import db
from .analytics import forecast, stats, valuation
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
            alerts=stats.alerts(panel, volume),
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
            "direction": forecast.direction_model(close, s.index),
        }

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
