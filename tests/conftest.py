from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from ellie import db
from ellie.config import Settings
from ellie.ingest import run_ingest
from ellie.sources import constituents


@pytest.fixture(scope="session")
def members() -> pd.DataFrame:
    # A cross-sector sample keeps tests fast while exercising sector logic.
    df = constituents.load_bundled()
    return df.groupby("sector").head(4).reset_index(drop=True)


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    path = tmp_path_factory.mktemp("wh") / "test.db"
    return dataclasses.replace(Settings(), db_path=path, data_mode="synthetic", history_days=600)


@pytest.fixture(scope="session")
def loaded(settings, members, monkeypatch_session):
    monkeypatch_session.setattr(constituents, "fetch", lambda timeout=20: (members, "test-fixture"))
    conn = db.connect(settings.db_path)
    summary = run_ingest(conn, settings, "synthetic")
    return conn, summary


@pytest.fixture(scope="session")
def monkeypatch_session():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture
def gbm_panel() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2023-01-02", periods=600)
    rets = rng.normal(0.0003, 0.015, (600, 3))
    return pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=["A", "B", "C"])
