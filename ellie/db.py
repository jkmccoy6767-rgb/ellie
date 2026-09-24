"""SQLite warehouse: raw per-source tables, curated tables and run metadata.

SQLite keeps the MVP dependency-free; the schema maps one-to-one onto a cloud
warehouse (Snowflake / Databricks) for the scale-out phase.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS constituents (
    symbol      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    sector      TEXT NOT NULL,
    sub_industry TEXT,
    headquarters TEXT,
    date_added  TEXT,
    cik         TEXT,
    founded     TEXT
);

-- Raw prices, one row per symbol/date/source. Never overwritten by other sources.
CREATE TABLE IF NOT EXISTS prices_raw (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    source  TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL NOT NULL,
    volume  REAL,
    PRIMARY KEY (symbol, date, source)
);

-- Curated prices after validation and cross-source reconciliation.
CREATE TABLE IF NOT EXISTS prices (
    symbol    TEXT NOT NULL,
    date      TEXT NOT NULL,
    close     REAL NOT NULL,
    volume    REAL,
    n_sources INTEGER NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS macro (
    series_id TEXT NOT NULL,
    date      TEXT NOT NULL,
    value     REAL NOT NULL,
    source    TEXT NOT NULL,
    PRIMARY KEY (series_id, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol     TEXT NOT NULL,
    metric     TEXT NOT NULL,
    period_end TEXT NOT NULL,
    value      REAL NOT NULL,
    form       TEXT,
    source     TEXT NOT NULL,
    PRIMARY KEY (symbol, metric, period_end)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS source_stats (
    run_id  INTEGER NOT NULL,
    source  TEXT NOT NULL,
    dataset TEXT NOT NULL,
    rows    INTEGER NOT NULL,
    symbols INTEGER NOT NULL,
    errors  INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, dataset)
);

CREATE TABLE IF NOT EXISTS quality_issues (
    run_id   INTEGER NOT NULL,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    symbol   TEXT,
    date     TEXT,
    detail   TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """Insert-or-replace every row of ``df`` into ``table`` (columns must match)."""
    if df.empty:
        return 0
    cols = list(df.columns)
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    rows = df.astype(object).where(pd.notna(df), None).itertuples(index=False, name=None)
    conn.executemany(sql, rows)
    return len(df)


def read_df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)
