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
from .analytics import estimates as est_analytics
from .analytics.sentiment import NewsItem, make_scorer
from .config import Settings
from .sources import constituents, finnhub, fmp, fred, sec_edgar, stooq, synthetic, yahoo, yahoo_news
from .sources.base import FetchResult, SourceUnavailable, news_id

log = logging.getLogger(__name__)

PRICE_SOURCES = {yahoo.NAME: yahoo.fetch_symbol, stooq.NAME: stooq.fetch_symbol}
PROBE_SYMBOL = "AAPL"
# Preferred source when the same story or snapshot arrives from several feeds.
NEWS_PRIORITY = [finnhub.NAME, yahoo_news.NAME, synthetic.NEWSWIRE, synthetic.NEWS_RSS]
ANALYST_TABLES = {
    "eps": "estimates", "revenue": "estimates", "recommendations": "recommendations",
    "price_targets": "price_targets", "surprises": "earnings_surprises",
}
DATA_TABLES = ("prices_raw", "prices", "macro", "fundamentals", "news", "estimates", "recommendations",
               "price_targets", "earnings_surprises")


@dataclass
class RunSummary:
    run_id: int
    mode: str
    symbols: int
    price_rows: int
    curated_rows: int
    macro_rows: int
    fundamental_rows: int
    news_rows: int
    new_news_scored: int
    estimate_rows: int
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


def _collect_live_news(settings: Settings, members: pd.DataFrame) -> list[FetchResult]:
    since = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=settings.news_days)
    symbols = members["symbol"].tolist()
    results = [_fetch_many(yahoo_news.NAME, "news", lambda s: yahoo_news.fetch_symbol(s, since, settings.http_timeout),
                           symbols, settings.max_workers)]
    if settings.finnhub_api_key:
        results.append(_fetch_many(finnhub.NAME, "news",
                                   lambda s: finnhub.fetch_news(s, since, settings.finnhub_api_key, settings.http_timeout),
                                   symbols, 4))
    return results


def _collect_live_analyst(settings: Settings, members: pd.DataFrame) -> list[FetchResult]:
    """Per source and dataset. Sources without a configured key are skipped."""
    as_of = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    vendors = []
    if settings.finnhub_api_key:
        vendors.append((finnhub.NAME, lambda s: finnhub.fetch_analyst(s, settings.finnhub_api_key, as_of, settings.http_timeout)))
    if settings.fmp_api_key:
        vendors.append((fmp.NAME, lambda s: fmp.fetch_analyst(s, settings.fmp_api_key, as_of, settings.http_timeout)))
    results = []
    for name, fn in vendors:
        per_symbol: dict[str, list[pd.DataFrame]] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn, sym): sym for sym in members["symbol"]}
            for fut in as_completed(futures):
                try:
                    for dataset, frame in fut.result().items():
                        if not frame.empty:
                            per_symbol.setdefault(dataset, []).append(frame)
                except Exception as exc:
                    errors[futures[fut]] = str(exc)[:200]
        for k, dataset in enumerate(ANALYST_TABLES):
            frames = per_symbol.get(dataset, [])
            # Symbol-level failures are shared by every dataset; report them once.
            results.append(FetchResult(name, dataset, pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(),
                                       errors if k == 0 else {}))
    return results


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


def _collect_synthetic_extras(
    settings: Settings, members: pd.DataFrame, prices: list[FetchResult], funda: FetchResult
) -> tuple[list[FetchResult], list[FetchResult]]:
    primary = prices[0].frame
    news = [FetchResult(src, "news", df) for src, df in synthetic.generate_news(primary, members).items()]
    analyst = [
        FetchResult(src, dataset, frame)
        for src, datasets in synthetic.generate_analyst(primary, funda.frame).items()
        for dataset, frame in datasets.items()
    ]
    return news, analyst


def _load_news(conn: sqlite3.Connection, settings: Settings, members: pd.DataFrame, results: list[FetchResult]) -> tuple[int, int, str]:
    """De-duplicate across feeds, score only stories not already in the warehouse, and upsert."""
    frames = [r.frame for r in results if not r.frame.empty]
    if not frames:
        return 0, 0, "none"
    raw = pd.concat(frames, ignore_index=True)
    raw["id"] = [news_id(sym, h, t) for sym, h, t in zip(raw["symbol"], raw["headline"], raw["published_at"])]
    rank = {s: i for i, s in enumerate(NEWS_PRIORITY)}
    raw["_rank"] = raw["source"].map(rank).fillna(len(rank))
    raw = raw.sort_values(["id", "_rank", "published_at"])
    sources = raw.groupby("id")["source"].agg(lambda s: ",".join(sorted(set(s))))
    news = raw.drop_duplicates("id").set_index("id")
    news["sources"] = sources
    news["published_at"] = raw.groupby("id")["published_at"].min()

    existing = {r[0] for r in conn.execute("SELECT id FROM news")} if len(news) else set()
    fresh = news[~news.index.isin(existing)].copy()
    scorer = make_scorer(settings.sentiment_scorer, settings.sentiment_model)
    names = members.set_index("symbol")["name"]
    items = [NewsItem(r.symbol, names.get(r.symbol, r.symbol), r.headline, r.summary) for r in fresh.itertuples()]
    fresh["sentiment"] = scorer.score(items) if items else []
    fresh["scorer"] = scorer.name

    cols = ["symbol", "published_at", "source", "sources", "headline", "summary", "url", "sentiment", "scorer"]
    db.upsert_df(conn, "news", fresh.reset_index()[["id", *cols]])
    # Stories seen before: only widen the list of feeds that carried them.
    seen = news[news.index.isin(existing)]
    conn.executemany(
        "UPDATE news SET sources = CASE WHEN instr(sources, ?) > 0 THEN sources ELSE sources || ',' || ? END WHERE id = ?",
        [(src, src, i) for i, srcs in seen["sources"].items() for src in srcs.split(",")],
    )
    return len(news), len(fresh), scorer.name


def _load_analyst(conn: sqlite3.Connection, results: list[FetchResult]) -> int:
    rows = 0
    for r in results:
        if r.frame.empty:
            continue
        table = ANALYST_TABLES[r.dataset]
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
        rows += db.upsert_df(conn, table, r.frame[cols])
    return rows


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
                for table in DATA_TABLES:
                    conn.execute(f"DELETE FROM {table}")

        if resolved == "live":
            price_results, macro_res, funda_res = _collect_live(conn, settings, members, live_sources)
            news_results = _collect_live_news(settings, members)
            analyst_results = _collect_live_analyst(settings, members)
        else:
            price_results, macro_res, funda_res = _collect_synthetic(settings, members)
            news_results, analyst_results = _collect_synthetic_extras(settings, members, price_results, funda_res)

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
            news_rows, news_scored, scorer_name = _load_news(conn, settings, members, news_results)
            estimate_rows = _load_analyst(conn, analyst_results)
            issues += est_analytics.mismatch_issues(
                db.read_df(conn, "SELECT * FROM estimates"), db.read_df(conn, "SELECT * FROM price_targets"),
                settings.estimate_tolerance,
            )

            for res in [*price_results, macro_res, funda_res, *news_results, *analyst_results]:
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
                news_rows=news_rows,
                new_news_scored=news_scored,
                estimate_rows=estimate_rows,
                issues=len(issues),
                sources=sorted({r.source for r in [*price_results, macro_res, funda_res, *news_results, *analyst_results]}
                               | {members_source, f"sentiment:{scorer_name}"}),
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
