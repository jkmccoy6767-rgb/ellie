import dataclasses
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ellie import db, ingest
from ellie.analytics import estimates as est
from ellie.analytics import sentiment as se
from ellie.api import create_app
from ellie.sources import finnhub, fmp, yahoo_news
from ellie.sources.base import news_id

# ---------------------------------------------------------------------------- scoring


@pytest.mark.parametrize("text, sign", [
    ("Acme beats estimates and raises guidance", 1),
    ("Acme shares surge on record profit", 1),
    ("Acme misses estimates, cuts guidance", -1),
    ("Acme plunges after SEC probe", -1),
    ("Acme schedules annual shareholder meeting", 0),
    ("Acme demand is not strong", -1),          # negation flips polarity
    ("Acme does not expect losses", 1),
])
def test_lexicon_polarity(text, sign):
    score = se.lexicon_score(text)
    assert np.sign(round(score, 6)) == sign
    assert -1 < score < 1


class FakeAnthropic:
    """Stands in for anthropic.Anthropic(); records requests and replays canned responses."""

    def __init__(self, responder):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._responder = responder

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


def _reply(scores, stop_reason="end_turn"):
    text = json.dumps({"scores": [{"i": i, "score": s} for i, s in enumerate(scores)]})
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


ITEMS = [se.NewsItem("ACME", "Acme Corp", "Acme wins huge contract"), se.NewsItem("ACME", "Acme Corp", "Acme recalls product")]


def test_claude_scorer_request_shape_and_parsing():
    pytest.importorskip("anthropic")
    fake = FakeAnthropic(lambda kw: _reply([0.8, -0.6]))
    scorer = se.ClaudeScorer(model="claude-opus-5", client=fake)
    assert scorer.score(ITEMS) == [0.8, -0.6]
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Acme Corp (ACME)" in call["messages"][0]["content"]


@pytest.mark.parametrize("reply", [
    lambda kw: _reply([0.5], stop_reason="refusal"),     # declined
    lambda kw: _reply([0.5]),                            # one item missing
    lambda kw: (_ for _ in ()).throw(ValueError("x")),   # any failure
])
def test_claude_scorer_falls_back_to_lexicon(reply):
    pytest.importorskip("anthropic")
    scorer = se.ClaudeScorer(client=FakeAnthropic(reply))
    assert scorer.score(ITEMS) == se.LexiconScorer().score(ITEMS)
    assert scorer.failed_batches == 1


def test_claude_scorer_batches():
    pytest.importorskip("anthropic")
    fake = FakeAnthropic(lambda kw: _reply([0.1] * kw["messages"][0]["content"].count("[Acme")))
    scorer = se.ClaudeScorer(client=fake, batch_size=3)
    assert len(scorer.score(ITEMS * 4)) == 8
    assert len(fake.calls) == 3


# ---------------------------------------------------------------------------- connectors

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Apple beats estimates</title><link>https://x/1</link><description>Strong quarter</description>
<pubDate>Tue, 22 Sep 2026 13:30:00 +0000</pubDate></item>
<item><title>Old story</title><pubDate>Mon, 01 Jun 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_yahoo_rss_parse_filters_by_date():
    df = yahoo_news.parse_rss("AAPL", RSS, pd.Timestamp("2026-09-01", tz="UTC"))
    assert list(df["headline"]) == ["Apple beats estimates"]
    assert df["published_at"].iloc[0] == "2026-09-22T13:30:00Z"


def test_finnhub_parsers():
    recs = finnhub.parse_recommendations("A", [{"period": "2026-09-01", "strongBuy": 5, "buy": 10, "hold": 3, "sell": 1, "strongSell": 0}])
    assert recs.iloc[0][["period", "strong_buy", "hold"]].tolist() == ["2026-09", 5, 3]
    tgt = finnhub.parse_price_target("A", {"targetMean": 120, "targetHigh": 150, "targetLow": 90}, "2026-09-24")
    assert tgt.iloc[0]["mean"] == 120
    assert finnhub.parse_price_target("A", {}, "2026-09-24").empty
    payload = {"data": [{"period": "2027-12-31", "epsAvg": 5.1, "epsHigh": 6, "epsLow": 4, "numberAnalysts": 20}]}
    eps = finnhub.parse_estimates("A", payload, "eps", "d")
    assert eps.iloc[0][["period", "mean", "n_analysts"]].tolist() == ["FY2027", 5.1, 20]
    sur = finnhub.parse_surprises("A", [{"period": "2026-06-30", "actual": 1.1, "estimate": 1.0}, {"period": "x", "actual": None}])
    assert len(sur) == 1


def test_fmp_parsers():
    df = fmp.parse_estimates("A", [{"date": "2027-09-27", "epsAvg": 7.0, "revenueAvg": 4e11, "numAnalystsEps": 30}], "d")
    assert set(df["metric"]) == {"eps", "revenue"} and set(df["period"]) == {"FY2027"}
    t = fmp.parse_price_target("A", [{"targetConsensus": 250, "targetHigh": 300}], "d")
    assert t.iloc[0]["mean"] == 250


def test_news_id_dedupes_same_story_same_day_only():
    a = news_id("AAPL", "Apple beats estimates!", "2026-09-22T13:30:00Z")
    b = news_id("AAPL", "apple  beats estimates", "2026-09-22T19:00:00Z")
    c = news_id("AAPL", "Apple beats estimates", "2026-10-22T13:30:00Z")
    assert a == b and a != c


# ---------------------------------------------------------------------------- estimates analytics


def _est(rows):
    return pd.DataFrame(rows, columns=["symbol", "period", "metric", "as_of", "mean", "high", "low", "n_analysts", "source"])


def test_consensus_median_and_mismatch():
    e = _est([
        ("A", "FY2026", "eps", "2026-09-01", 5.0, 6, 4, 20, "s1"),
        ("A", "FY2026", "eps", "2026-09-20", 5.2, 6, 4, 20, "s1"),   # latest s1 snapshot wins
        ("A", "FY2026", "eps", "2026-09-20", 6.5, 7, 5, 18, "s2"),
    ])
    c = est.consensus(e).iloc[0]
    assert c["mean"] == pytest.approx((5.2 + 6.5) / 2) and c["n_sources"] == 2
    issues = est.mismatch_issues(e, pd.DataFrame(columns=["symbol", "as_of", "mean", "source"]), 0.1)
    assert [i.check_name for i in issues] == ["estimate_mismatch"]


def test_summary_metrics():
    asof = pd.Timestamp("2026-09-24")
    e = _est([
        ("A", "FY2026", "eps", "2026-09-24", 5.0, None, None, 10, "s1"),
        ("A", "FY2027", "eps", "2026-06-20", 5.0, None, None, 10, "s1"),
        ("A", "FY2027", "eps", "2026-08-20", 5.5, None, None, 10, "s1"),
        ("A", "FY2027", "eps", "2026-09-24", 6.0, None, None, 10, "s1"),
    ])
    recs = pd.DataFrame([{"symbol": "A", "period": "2026-09", "strong_buy": 2, "buy": 2, "hold": 0, "sell": 0, "strong_sell": 0, "source": "s1"}])
    tg = pd.DataFrame([{"symbol": "A", "as_of": "2026-09-24", "mean": 120.0, "high": 150, "low": 90, "source": "s1"}])
    sur = pd.DataFrame([{"symbol": "A", "period": f"2026-0{m}-30", "actual": a, "estimate": 1.0, "source": "s1"}
                        for m, a in ((3, 1.1), (6, 0.9))])
    out = est.summary(e, recs, tg, sur, pd.Series({"A": 100.0}), asof).loc["A"]
    assert out["fwd_pe"] == 20
    assert out["eps_growth_fwd"] == pytest.approx(0.2)
    assert out["eps_rev_30d"] == pytest.approx(6.0 / 5.5 - 1)
    assert out["eps_rev_90d"] == pytest.approx(0.2)
    assert out["rec_score"] == 1.5 and out["pct_buy"] == 1
    assert out["target_upside"] == pytest.approx(0.2)
    assert out["beat_rate"] == 0.5


# ---------------------------------------------------------------------------- sentiment aggregation


def _news(rows):
    return pd.DataFrame(rows, columns=["symbol", "published_at", "sentiment"])


def test_symbol_summary_and_alerts():
    asof = pd.Timestamp("2026-09-24")
    rows = [("A", f"2026-09-{d:02d}T15:00:00Z", -0.6) for d in range(18, 25) for _ in range(2)]
    rows += [("A", "2026-09-01T15:00:00Z", 0.0)]
    s = se.symbol_summary(_news(rows), asof).loc["A"]
    assert s["news_7d"] == 14 and s["sent_7d"] == pytest.approx(-0.6)
    alerts = se.news_alerts(se.symbol_summary(_news(rows), asof), "2026-09-24")
    assert [a["rule"] for a in alerts] == ["negative_news"]


def test_sentiment_features_have_no_look_ahead():
    idx = pd.bdate_range("2026-08-03", "2026-09-24")
    base = [("A", f"{d.date()}T15:00:00Z", 0.2) for d in idx[:20]]
    future = [("A", "2026-09-20T15:00:00Z", -1.0)]
    f1 = se.feature_frame(_news(base), idx, "A")
    f2 = se.feature_frame(_news(base + future), idx, "A")
    cut = pd.Timestamp("2026-09-18")
    pd.testing.assert_frame_equal(f1[f1.index <= cut], f2[f2.index <= cut])
    assert f2.loc["2026-09-21", "sent_7d"] < 0


# ---------------------------------------------------------------------------- pipeline & API


def test_ingest_loads_news_and_analyst_tables(loaded):
    conn, s = loaded
    assert s.news_rows > 0 and s.new_news_scored == s.news_rows and s.estimate_rows > 0
    for table in ("news", "estimates", "recommendations", "price_targets", "earnings_surprises"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    # Syndicated stories were merged, not duplicated.
    assert conn.execute("SELECT COUNT(*) FROM news WHERE sources LIKE '%,%'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(DISTINCT scorer) FROM news").fetchone()[0] == 1


def test_reingest_does_not_rescore_existing_news(settings, members, monkeypatch, tmp_path):
    from ellie.sources import constituents

    monkeypatch.setattr(constituents, "fetch", lambda timeout=20: (members.head(6), "test"))
    cfg = dataclasses.replace(settings, db_path=tmp_path / "twice.db", history_days=400)
    first = ingest.run_ingest(db.connect(cfg.db_path), cfg, "synthetic")
    second = ingest.run_ingest(db.connect(cfg.db_path), cfg, "synthetic")
    assert first.new_news_scored > 0 and second.new_news_scored == 0
    assert second.news_rows == first.news_rows


@pytest.fixture(scope="module")
def client(loaded, settings):
    return TestClient(create_app(settings))


def test_sentiment_and_stock_endpoints(client):
    ov = client.get("/api/sentiment").json()
    assert ov["available"] and len(ov["sectors"]) == 11 and ov["latest"]
    sym = ov["latest"][0]["symbol"]
    news = client.get(f"/api/stocks/{sym}/news").json()
    assert news["items"] and "sent_7d" in news["summary"]
    e = client.get(f"/api/stocks/{sym}/estimates").json()
    assert e["consensus"] and e["recommendations"] and e["targets"] and e["surprises"]
    assert {"fwd_pe", "rec_score", "target_upside", "beat_rate"} <= set(e["summary"])
    assert client.get("/api/stocks/NOPE/news").status_code == 404


def test_screener_carries_alt_data_columns(client):
    row = client.get("/api/screener").json()[0]
    assert {"sent_7d", "news_7d", "fwd_pe", "eps_rev_30d", "rec_score", "target_upside"} <= set(row)


def test_direction_model_uses_alt_data(client):
    sym = client.get("/api/screener").json()[0]["symbol"]
    d = client.get(f"/api/stocks/{sym}/forecast?horizon=10").json()["direction"]
    assert d["available"] and set(d["alt_data_features"]) <= {"sent_7d", "news_ratio", "eps_rev_30d"}
