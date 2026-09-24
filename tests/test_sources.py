import pandas as pd

from ellie.sources import constituents, fred, sec_edgar, stooq, synthetic, yahoo


class FakeResp:
    def __init__(self, text=None, payload=None):
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


def test_bundled_constituents_are_the_full_index():
    df = constituents.load_bundled()
    assert 495 <= len(df) <= 510
    assert df["symbol"].is_unique
    assert df["sector"].nunique() == 11
    assert df["cik"].str.len().eq(10).all()


def test_yahoo_parses_adjusted_close(monkeypatch):
    payload = {"chart": {"result": [{
        "timestamp": [1700000000, 1700086400],
        "indicators": {
            "quote": [{"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20]}],
            "adjclose": [{"adjclose": [1.4, 2.4]}],
        },
    }]}}
    monkeypatch.setattr(yahoo, "http_get", lambda *a, **k: FakeResp(payload=payload))
    df = yahoo.fetch_symbol("BRK.B", pd.Timestamp("2023-01-01", tz="UTC"))
    assert list(df["close"]) == [1.4, 2.4]
    assert (df["source"] == "yahoo").all() and (df["symbol"] == "BRK.B").all()
    assert yahoo.vendor_symbol("BRK.B") == "BRK-B"


def test_stooq_parses_csv(monkeypatch):
    csv = "Date,Open,High,Low,Close,Volume\n2024-01-02,10,11,9,10.5,1000\n2024-01-03,10.5,12,10,11.5,2000\n"
    monkeypatch.setattr(stooq, "http_get", lambda *a, **k: FakeResp(text=csv))
    df = stooq.fetch_symbol("AAPL", pd.Timestamp("2024-01-01"))
    assert list(df["close"]) == [10.5, 11.5]
    assert stooq.vendor_symbol("BF.B") == "bf-b.us"


def test_fred_drops_missing_markers(monkeypatch):
    csv = "observation_date,DGS10\n2024-01-02,3.95\n2024-01-03,.\n2024-01-04,4.01\n"
    monkeypatch.setattr(fred, "http_get", lambda *a, **k: FakeResp(text=csv))
    df = fred.fetch_series("DGS10", pd.Timestamp("2024-01-01"))
    assert list(df["value"]) == [3.95, 4.01]


def test_sec_keeps_annual_full_year_facts_only():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 100, "form": "10-K", "fp": "FY"},
            {"start": "2023-10-01", "end": "2023-12-31", "val": 30, "form": "10-K", "fp": "FY"},  # quarter inside a 10-K
            {"start": "2024-01-01", "end": "2024-03-31", "val": 25, "form": "10-Q", "fp": "Q1"},
        ]}},
        "Assets": {"units": {"USD": [{"end": "2023-12-31", "val": 500, "form": "10-K", "fp": "FY"}]}},
    }}}
    df = sec_edgar.parse_company_facts("XYZ", facts).set_index("metric")
    assert df.loc["revenue", "value"] == 100
    assert df.loc["total_assets", "value"] == 500
    assert len(df) == 2


def test_synthetic_is_deterministic(members):
    a = synthetic.generate_prices(members.head(3), days=200, end=pd.Timestamp("2025-06-30"))
    b = synthetic.generate_prices(members.head(3), days=200, end=pd.Timestamp("2025-06-30"))
    pd.testing.assert_frame_equal(a, b)
    assert (a["high"] >= a["low"]).all() and (a["close"] > 0).all()
