"""Ingestion pipeline: pull every source, validate, reconcile, load the warehouse."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from . import db, quality
from .config import Settings
from .sources import constituents, fred, sec_edgar, stooq, synthetic, yahoo
from .sources.base import FetchResult, SourceUnavailable

log = logging.getLogger(__name__)

PRICE_SOURCES = {yahoo.NAME: yahoo.fetch_symbol, stooq.NAME: stooq.fetch_symbol}
PROBE_SYMBOL = "AAPL"


@dataclass
class RunSummary:
    run_id: int
    mode: str
    symbols: int
    price_rows: int
    curated_rows: int
    macro_rows: int
    fundamental_rows: int
    issues: int
    sources: list[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_many(
    source: str, dataset: str, fn: Callable[[str], pd.DataFrame], keys: list[str], workers: int
) -> FetchResult:
    frames, errors = [], {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, k): k for k in keys}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                frame = fut.result()
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:  # one bad symbol must not sink the run
                errors[key] = str(exc)[:200]
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return FetchResult(source, dataset, frame, errors)


def probe_live_sources(settings: Settings) -> list[str]:
    """Return price sources that respond right now."""
    start = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=10)
    alive = []
    for name, fn in PRICE_SOURCES.items():
        try:
            if not fn(PROBE_SYMBOL, start, timeout=settings.http_timeout).empty:
                alive.append(name)
        except SourceUnavailable as exc:
            log.warning("Price source %s unavailable: %s", name, exc)
    return alive


def _last_mode(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT mode FROM ingestion_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _price_start(conn: sqlite3.Connection, settings: Settings) -> pd.Timestamp:
    row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    full = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=settings.history_days)
    if row and row[0]:
        # Incremental: re-pull a small overlap so late corrections are picked up.
        return max(full, pd.Timestamp(row[0], tz="UTC") - pd.Timedelta(days=10))
    return full


def _collect_live(
    conn: sqlite3.Connection, settings: Settings, members: pd.DataFrame, price_sources: list[str]
) -> tuple[list[FetchResult], FetchResult, FetchResult]:
    start = _price_start(conn, settings)
    symbols = members["symbol"].tolist()
    prices = [
        _fetch_many(
            name, "prices",
            lambda s, fn=PRICE_SOURCES[name]: fn(s, start, timeout=settings.http_timeout),
            symbols, settings.max_workers,
        )
        for name in price_sources
    ]
    macro_start = pd.Timestamp.now().normalize() - pd.Timedelta(days=settings.history_days)
    macro = _fetch_many(
        fred.NAME, "macro",
        lambda sid: fred.fetch_series(sid, macro_start, settings.fred_api_key, settings.http_timeout),
        list(fred.SERIES), 4,
    )
    ciks = dict(zip(members["symbol"], members["cik"]))
    # SEC fair-access policy: stay under 10 requests/second.
    funda = _fetch_many(
        sec_edgar.NAME, "fundamentals",
        lambda s: sec_edgar.fetch_symbol(s, ciks[s], settings.sec_user_agent, settings.http_timeout),
        symbols, min(settings.max_workers, 4),
    )
    return prices, macro, funda


def _collect_synthetic(settings: Settings, members: pd.DataFrame) -> tuple[list[FetchResult], FetchResult, FetchResult]:
    primary = synthetic.generate_prices(members, days=settings.history_days)
    secondary = synthetic.secondary_from_primary(primary)
    prices = [
        FetchResult(synthetic.PRIMARY, "prices", primary),
        FetchResult(synthetic.SECONDARY, "prices", secondary),
    ]
    macro = FetchResult(synthetic.MACRO, "macro", synthetic.generate_macro(days=settings.history_days))
    funda = FetchResult(synthetic.FUNDAMENTALS, "fundamentals", synthetic.generate_fundamentals(primary))
    return prices, macro, funda


def run_ingest(
    conn: sqlite3.Connection,
    settings: Settings,
    mode: str | None = None,
    limit: int | None = None,
) -> RunSummary:
    """Run one ingestion cycle. ``mode`` is 'live', 'synthetic' or 'auto'."""
    mode = mode or settings.data_mode
    cur = conn.execute(
        "INSERT INTO ingestion_runs (started_at, mode, status) VALUES (?, ?, 'running')", (_now(), mode)
    )
    run_id = cur.lastrowid
    conn.commit()
    try:
        members, members_source = constituents.fetch(settings.http_timeout)
        if limit:
            members = members.head(limit)

        live_sources: list[str] = []
        if mode in ("live", "auto"):
            live_sources = probe_live_sources(settings)
            if not live_sources:
                if mode == "live":
                    raise SourceUnavailable("no live price source reachable")
                log.warning("No live price source reachable; falling back to synthetic demo data")
        resolved = "live" if live_sources else "synthetic"

        # Never mix demo and live data in the same warehouse.
        if _last_mode(conn) not in (None, resolved):
            with db.transaction(conn):
                for table in ("prices_raw", "prices", "macro", "fundamentals"):
                    conn.execute(f"DELETE FROM {table}")

        if resolved == "live":
            price_results, macro_res, funda_res = _collect_live(conn, settings, members, live_sources)
        else:
            price_results, macro_res, funda_res = _collect_synthetic(settings, members)

        raw = pd.concat([r.frame for r in price_results if not r.frame.empty], ignore_index=True)
        clean, issues = quality.validate_prices(raw)

        with db.transaction(conn):
            conn.execute("DELETE FROM constituents")
            db.upsert_df(conn, "constituents", members)
            db.upsert_df(conn, "prices_raw", clean[list(raw.columns)])

            # Reconcile across everything in the raw store so incremental runs stay consistent.
            all_raw = db.read_df(conn, "SELECT symbol, date, source, close, volume FROM prices_raw")
            curated, rec_issues = quality.reconcile(all_raw, settings.reconcile_tolerance)
            issues += rec_issues + quality.coverage_issues(curated, members["symbol"].tolist())
            conn.execute("DELETE FROM prices")
            db.upsert_df(conn, "prices", curated)

            db.upsert_df(conn, "macro", macro_res.frame)
            db.upsert_df(conn, "fundamentals", funda_res.frame)

            for res in [*price_results, macro_res, funda_res]:
                key = "series_id" if res.dataset == "macro" else "symbol"
                n_keys = res.frame[key].nunique() if not res.frame.empty else 0
                conn.execute(
                    "INSERT OR REPLACE INTO source_stats VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, res.source, res.dataset, len(res.frame), n_keys, len(res.errors)),
                )
            conn.execute("INSERT OR REPLACE INTO source_stats VALUES (?, ?, 'constituents', ?, ?, 0)",
                         (run_id, members_source, len(members), len(members)))
            conn.executemany("INSERT INTO quality_issues VALUES (?, ?, ?, ?, ?, ?)", [i.as_row(run_id) for i in issues])

            summary = RunSummary(
                run_id=run_id,
                mode=resolved,
                symbols=len(members),
                price_rows=len(clean),
                curated_rows=len(curated),
                macro_rows=len(macro_res.frame),
                fundamental_rows=len(funda_res.frame),
                issues=len(issues),
                sources=[r.source for r in price_results] + [macro_res.source, funda_res.source, members_source],
            )
            conn.execute(
                "UPDATE ingestion_runs SET finished_at=?, status='success', mode=?, detail=? WHERE id=?",
                (_now(), resolved, json.dumps(summary.__dict__), run_id),
            )
        return summary
    except Exception as exc:
        conn.execute(
            "UPDATE ingestion_runs SET finished_at=?, status='failed', detail=? WHERE id=?",
            (_now(), str(exc)[:500], run_id),
        )
        conn.commit()
        raise
