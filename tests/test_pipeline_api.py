import pytest
from fastapi.testclient import TestClient

from ellie import db, ingest
from ellie.api import create_app
from ellie.sources.base import SourceUnavailable


def test_ingest_loads_every_layer(loaded, members):
    conn, s = loaded
    assert s.mode == "synthetic"
    assert s.symbols == len(members)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("prices", "prices_raw", "macro", "fundamentals")}
    assert all(v > 0 for v in counts.values())
    assert counts["prices_raw"] > counts["prices"]  # two sources reconciled into one
    assert conn.execute("SELECT MAX(n_sources) FROM prices").fetchone()[0] == 2


def test_auto_mode_falls_back_when_no_live_source(settings, members, monkeypatch, tmp_path):
    import dataclasses

    from ellie.sources import constituents

    monkeypatch.setattr(constituents, "fetch", lambda timeout=20: (members.head(5), "test"))
    monkeypatch.setattr(ingest, "probe_live_sources", lambda s: [])
    cfg = dataclasses.replace(settings, db_path=tmp_path / "auto.db", history_days=400)
    summary = ingest.run_ingest(db.connect(cfg.db_path), cfg, "auto")
    assert summary.mode == "synthetic"

    with pytest.raises(SourceUnavailable):
        ingest.run_ingest(db.connect(cfg.db_path), cfg, "live")
    status = db.connect(cfg.db_path).execute("SELECT status FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert status == "failed"


@pytest.fixture(scope="module")
def client(loaded, settings):
    return TestClient(create_app(settings))


def test_status_and_overview(client, members):
    st = client.get("/api/status").json()
    assert st["ready"] and st["synthetic"] and st["symbols"] == len(members)
    ov = client.get("/api/overview").json()
    assert ov["index"]["series"]["values"][0] == pytest.approx(100, rel=0.05)
    assert len(ov["sectors"]) == 11


def test_screener_and_stock(client):
    rows = client.get("/api/screener").json()
    sym = rows[0]["symbol"]
    assert {"price", "ret_1Y", "vol_1y", "pe", "sector"} <= set(rows[0])
    s = client.get(f"/api/stocks/{sym}").json()
    assert s["symbol"] == sym and len(s["history"]["dates"]) > 100
    assert client.get("/api/stocks/NOPE").status_code == 404


def test_forecast_endpoint(client):
    sym = client.get("/api/screener").json()[0]["symbol"]
    fc = client.get(f"/api/stocks/{sym}/forecast?horizon=10").json()
    assert len(fc["paths"]) == 10
    assert {r["model"] for r in fc["backtest"]} == {"naive", "drift", "arima", "arima_garch"}
    assert client.get(f"/api/stocks/{sym}/forecast?horizon=500").status_code == 422


@pytest.mark.parametrize("path", ["/api/sectors", "/api/risk", "/api/macro", "/api/alerts", "/api/quality", "/"])
def test_other_endpoints(client, path):
    assert client.get(path).status_code == 200


def test_ingest_endpoint_validates_mode(client):
    assert client.post("/api/ingest?mode=bogus").status_code == 400
