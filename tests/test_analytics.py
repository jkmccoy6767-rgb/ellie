import numpy as np
import pandas as pd
import pytest

from ellie.analytics import forecast, stats, valuation


def test_beta_recovers_known_exposure():
    rng = np.random.default_rng(0)
    mkt = pd.Series(rng.normal(0, 0.01, 2000))
    rets = pd.DataFrame({"hi": 1.5 * mkt + rng.normal(0, 0.002, 2000), "lo": 0.5 * mkt + rng.normal(0, 0.002, 2000)})
    b = stats.beta(rets, mkt)
    assert b["hi"] == pytest.approx(1.5, abs=0.05)
    assert b["lo"] == pytest.approx(0.5, abs=0.05)


def test_drawdown_and_var():
    s = pd.Series([100, 120, 90, 110, 60, 80.0])
    assert stats.max_drawdown(s) == pytest.approx(-0.5)
    rets = pd.DataFrame({"x": np.linspace(-0.05, 0.05, 101)})
    var, es = stats.historical_var(rets, 0.95)
    assert var["x"] == pytest.approx(0.045, abs=1e-3)
    assert es["x"] > var["x"]


def test_stock_metrics_shape(gbm_panel):
    idx = stats.equal_weight_index(gbm_panel)
    m = stats.stock_metrics(gbm_panel, gbm_panel * 0 + 1e6, idx)
    assert list(m.index) == ["A", "B", "C"] and m.index.name == "symbol"
    assert m["vol_1y"].between(0.15, 0.35).all()      # simulated at 1.5%/day ≈ 24% annualized
    assert {"ret_1D", "ret_1Y", "ret_YTD", "beta", "var95_1d"} <= set(m.columns)


def test_breadth_counts(gbm_panel):
    b = stats.breadth(gbm_panel)
    assert b["advancers"] + b["decliners"] + b["unchanged"] == 3
    assert 0 <= b["pct_above_200dma"] <= 1


def test_alerts_detect_outsized_move(gbm_panel):
    p = gbm_panel.copy()
    p.iloc[-1, 0] = p.iloc[-2, 0] * 1.25
    rules = {(a["symbol"], a["rule"]) for a in stats.alerts(p, p * 0 + 1e6)}
    assert ("A", "outsized_move") in rules


def test_garch_recovers_persistence():
    rng = np.random.default_rng(3)
    n, omega, alpha, beta = 4000, 2e-6, 0.08, 0.9
    r, var, eps = np.empty(n), omega / (1 - alpha - beta), 0.0
    for t in range(n):
        var = omega + alpha * eps**2 + beta * var
        eps = np.sqrt(var) * rng.standard_normal()
        r[t] = eps
    g = forecast.fit_garch(pd.Series(r))
    assert g.persistence == pytest.approx(0.98, abs=0.03)
    assert np.sqrt(g.long_run_var) == pytest.approx(np.sqrt(omega / (1 - alpha - beta)), rel=0.35)


def test_price_forecast_quantiles_are_ordered(gbm_panel):
    fc = forecast.price_forecast(gbm_panel["A"], horizon=10, n_paths=500)
    assert len(fc) == 10
    cols = ["p05", "p10", "p25", "p50", "p75", "p90", "p95"]
    assert (fc[cols].diff(axis=1).iloc[:, 1:] >= 0).all().all()
    # Uncertainty grows with horizon.
    assert (fc["p95"] - fc["p05"]).is_monotonic_increasing


def test_backtest_scores_every_model(gbm_panel):
    bt = forecast.backtest(gbm_panel["A"], horizon=10, n_origins=4)
    assert list(bt.index) == list(forecast.MODELS)
    assert bt.loc["naive", "skill_vs_naive"] == 0
    assert bt["coverage_80"].between(0, 1).all()


def test_direction_model_reports_out_of_sample(gbm_panel):
    idx = stats.equal_weight_index(gbm_panel)
    d = forecast.direction_model(gbm_panel["B"], idx)
    assert d["available"]
    assert 0 <= d["prob_up"] <= 1
    # Random-walk data: no real edge should appear.
    assert abs(d["edge"]) < 0.15


def test_valuation_multiples():
    f = pd.DataFrame([
        ("A", "eps_diluted", "2023-12-31", 4.0), ("A", "eps_diluted", "2024-12-31", 5.0),
        ("A", "shares_outstanding", "2024-12-31", 100.0), ("A", "revenue", "2024-12-31", 1000.0),
        ("A", "revenue", "2023-12-31", 800.0), ("A", "equity", "2024-12-31", 500.0),
        ("A", "net_income", "2024-12-31", 500.0),
    ], columns=["symbol", "metric", "period_end", "value"])
    m = valuation.multiples(f, pd.Series({"A": 50.0}), pd.Series({"A": "Tech"})).loc["A"]
    assert m["pe"] == 10
    assert m["market_cap"] == 5000
    assert m["revenue_growth"] == pytest.approx(0.25)
    assert m["eps_growth"] == pytest.approx(0.25)
