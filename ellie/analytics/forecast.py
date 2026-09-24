"""Forecasting models with walk-forward backtests.

* Volatility: EWMA (RiskMetrics) and GARCH(1,1) fitted by maximum likelihood.
* Price: ARIMA(1,0,1) mean on log returns + GARCH Monte Carlo intervals, with
  naive (random walk) and drift benchmarks.
* Direction: gradient-boosted classifier on technical features, scored
  walk-forward against a majority-class baseline.

Every model reports its out-of-sample record so users can see how much to trust
it; forecasts are never shown without their error bands.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import optimize
from scipy import stats as sps

TRADING_DAYS = 252


# --------------------------------------------------------------------------- volatility

def ewma_vol(returns: pd.Series, lam: float = 0.94) -> float:
    """Current RiskMetrics EWMA daily volatility."""
    r = returns.dropna().to_numpy()
    var = r[:20].var() if len(r) > 20 else r.var()
    for x in r:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var))


@dataclass
class Garch:
    omega: float
    alpha: float
    beta: float
    mu: float
    last_var: float
    last_resid: float

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    @property
    def long_run_var(self) -> float:
        return self.omega / max(1e-8, 1 - self.persistence)

    def variance_path(self, horizon: int) -> np.ndarray:
        """Expected conditional variance for steps 1..horizon."""
        out = np.empty(horizon)
        v = self.omega + self.alpha * self.last_resid**2 + self.beta * self.last_var
        for h in range(horizon):
            out[h] = v
            v = self.omega + self.persistence * v
        return out


def _garch_filter(eps: np.ndarray, omega: float, alpha: float, beta: float) -> np.ndarray:
    var = np.empty_like(eps)
    var[0] = eps.var()
    for t in range(1, len(eps)):
        var[t] = omega + alpha * eps[t - 1] ** 2 + beta * var[t - 1]
    return var


def fit_garch(returns: pd.Series) -> Garch:
    """Gaussian quasi-MLE GARCH(1,1). Returns are scaled x100 for numerical stability."""
    r = returns.dropna().to_numpy() * 100
    mu = r.mean()
    eps = r - mu
    sample_var = eps.var()

    def nll(theta: np.ndarray) -> float:
        omega, alpha, beta = theta
        if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.999:
            return 1e10
        var = _garch_filter(eps, omega, alpha, beta)
        return 0.5 * np.sum(np.log(var) + eps**2 / var)

    x0 = np.array([sample_var * 0.05, 0.08, 0.9])
    res = optimize.minimize(nll, x0, method="Nelder-Mead", options={"maxiter": 600, "xatol": 1e-6, "fatol": 1e-6})
    omega, alpha, beta = res.x if res.fun < 1e10 else x0
    var = _garch_filter(eps, omega, alpha, beta)
    s2 = 1e-4  # undo the x100 scaling (variance scales by 100^2)
    return Garch(omega * s2, alpha, beta, mu / 100, var[-1] * s2, eps[-1] / 100)


def volatility_forecast(close: pd.Series, horizon: int = 21) -> dict:
    rets = np.log(close).diff().dropna()
    g = fit_garch(rets.iloc[-3 * TRADING_DAYS:])
    path = g.variance_path(horizon)
    ann = np.sqrt(path * TRADING_DAYS)
    return {
        "realized_21d": float(rets.iloc[-21:].std() * np.sqrt(TRADING_DAYS)),
        "ewma": float(ewma_vol(rets) * np.sqrt(TRADING_DAYS)),
        "garch_next_day": float(ann[0]),
        "garch_horizon_avg": float(np.sqrt(path.mean() * TRADING_DAYS)),
        "garch_long_run": float(np.sqrt(g.long_run_var * TRADING_DAYS)),
        "persistence": float(g.persistence),
        "term_structure": [float(v) for v in ann],
    }


# --------------------------------------------------------------------------- price models

def _arima_mean(logret: np.ndarray, horizon: int) -> np.ndarray:
    """Per-step mean log-return forecast from ARIMA(1,0,1); falls back to the sample mean."""
    from statsmodels.tsa.arima.model import ARIMA

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ARIMA(logret, order=(1, 0, 1), trend="c").fit(method_kwargs={"maxiter": 50})
        mean = np.asarray(fit.forecast(horizon))
        if np.all(np.isfinite(mean)):
            # Shrink toward zero: daily equity drift is barely identifiable.
            return 0.5 * mean
    except Exception:
        pass
    return np.full(horizon, 0.5 * logret.mean())


def _point_and_sigma(model: str, logret: np.ndarray, horizon: int) -> tuple[float, float]:
    """Cumulative h-step log-return mean and std for backtest models."""
    sd = logret[-TRADING_DAYS:].std()
    if model == "naive":
        return 0.0, sd * np.sqrt(horizon)
    if model == "drift":
        return logret[-TRADING_DAYS:].mean() * horizon, sd * np.sqrt(horizon)
    mean = float(_arima_mean(logret, horizon).sum())
    if model == "arima":
        return mean, sd * np.sqrt(horizon)
    if model == "arima_garch":
        g = fit_garch(pd.Series(logret))
        return mean, float(np.sqrt(g.variance_path(horizon).sum()))
    raise ValueError(model)


MODELS = ("naive", "drift", "arima", "arima_garch")
MODEL_LABELS = {
    "naive": "Random walk (benchmark)",
    "drift": "Historical drift",
    "arima": "ARIMA(1,0,1)",
    "arima_garch": "ARIMA-GARCH",
}


def backtest(close: pd.Series, horizon: int = 21, n_origins: int = 8, train: int = 3 * TRADING_DAYS) -> pd.DataFrame:
    """Rolling-origin evaluation: refit at each origin, score the h-day-ahead forecast."""
    logp = np.log(close.dropna().to_numpy())
    rows = []
    z80 = sps.norm.ppf(0.9)
    for k in range(n_origins):
        origin = len(logp) - horizon - 1 - k * horizon
        if origin < 126:
            break
        hist = logp[max(0, origin - train): origin + 1]
        logret = np.diff(hist)
        actual = logp[origin + horizon] - logp[origin]
        for m in MODELS:
            mu, sigma = _point_and_sigma(m, logret, horizon)
            rows.append({
                "model": m,
                "origin": k,
                "error": actual - mu,
                "hit": np.sign(mu) == np.sign(actual) if m != "naive" else np.nan,
                "covered": abs(actual - mu) <= z80 * sigma,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    summary = df.groupby("model").agg(
        mae=("error", lambda e: e.abs().mean()),
        rmse=("error", lambda e: np.sqrt((e**2).mean())),
        hit_rate=("hit", "mean"),
        coverage_80=("covered", "mean"),
        n=("error", "size"),
    )
    summary["skill_vs_naive"] = 1 - summary["mae"] / summary.loc["naive", "mae"]
    summary["label"] = [MODEL_LABELS[m] for m in summary.index]
    return summary.reindex([m for m in MODELS if m in summary.index])


def price_forecast(close: pd.Series, horizon: int = 21, n_paths: int = 4000, seed: int = 0) -> pd.DataFrame:
    """ARIMA mean + GARCH(1,1) Monte Carlo with Student-t shocks. Returns quantile paths."""
    close = close.dropna()
    logret = np.log(close).diff().dropna().to_numpy()[-3 * TRADING_DAYS:]
    mean = _arima_mean(logret, horizon)
    g = fit_garch(pd.Series(logret))

    rng = np.random.default_rng(seed)
    nu = 6
    shocks = rng.standard_t(nu, (n_paths, horizon)) / np.sqrt(nu / (nu - 2))
    var = np.full(n_paths, g.omega + g.alpha * g.last_resid**2 + g.beta * g.last_var)
    cum = np.zeros(n_paths)
    paths = np.empty((n_paths, horizon))
    for h in range(horizon):
        eps = np.sqrt(var) * shocks[:, h]
        cum += mean[h] + eps
        paths[:, h] = cum
        var = g.omega + g.alpha * eps**2 + g.beta * var

    last = float(close.iloc[-1])
    qs = {"p05": 5, "p10": 10, "p25": 25, "p50": 50, "p75": 75, "p90": 90, "p95": 95}
    dates = pd.bdate_range(close.index[-1] + pd.Timedelta(days=1), periods=horizon)
    out = pd.DataFrame({k: last * np.exp(np.percentile(paths, q, axis=0)) for k, q in qs.items()}, index=dates)
    out["prob_up"] = (paths > 0).mean(axis=0)
    return out


# --------------------------------------------------------------------------- direction classifier

FEATURES = ["ret_1", "ret_5", "ret_21", "ret_63", "vol_21", "vol_ratio", "rsi_14", "dist_50dma", "mkt_ret_5", "mkt_ret_21"]


def _features(close: pd.Series, market: pd.Series) -> pd.DataFrame:
    r = close.pct_change(fill_method=None)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    vol21 = r.rolling(21).std()
    mkt = market.reindex(close.index).ffill()
    return pd.DataFrame(
        {
            "ret_1": r,
            "ret_5": close.pct_change(5, fill_method=None),
            "ret_21": close.pct_change(21, fill_method=None),
            "ret_63": close.pct_change(63, fill_method=None),
            "vol_21": vol21,
            "vol_ratio": vol21 / r.rolling(63).std(),
            "rsi_14": 100 - 100 / (1 + gain / loss.replace(0, np.nan)),
            "dist_50dma": close / close.rolling(50).mean() - 1,
            "mkt_ret_5": mkt.pct_change(5, fill_method=None),
            "mkt_ret_21": mkt.pct_change(21, fill_method=None),
        }
    )


def direction_model(
    close: pd.Series, market: pd.Series, horizon: int = 5, splits: int = 5, extra: pd.DataFrame | None = None
) -> dict:
    """Probability the stock is higher in ``horizon`` days, with walk-forward accuracy.

    ``extra`` adds daily alternative-data features (news sentiment, estimate revisions)
    indexed by trading day. Each value must be known on that day, or the backtest leaks.
    Rows before the extra data begins are kept, with neutral fill values.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import TimeSeriesSplit

    close = close.dropna()
    X = _features(close, market)
    features = list(FEATURES)
    if extra is not None and not extra.empty:
        extra = extra.reindex(close.index)
        usable = [c for c in extra.columns if extra[c].notna().sum() >= 60]
        X = X.join(extra[usable].fillna(extra[usable].median()))
        features += usable
    y = (close.shift(-horizon) > close).astype(float).where(close.shift(-horizon).notna())
    data = X.join(y.rename("y")).dropna(subset=features)
    labelled = data.dropna(subset=["y"])
    if len(labelled) < 200:
        return {"available": False, "reason": "not enough history"}

    Xl, yl = labelled[features].to_numpy(), labelled["y"].to_numpy()
    make = lambda: GradientBoostingClassifier(n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)  # noqa: E731
    acc, base = [], []
    # gap=horizon stops the training labels from overlapping the test window.
    for tr, te in TimeSeriesSplit(n_splits=splits, gap=horizon).split(Xl):
        clf = make().fit(Xl[tr], yl[tr])
        acc.append((clf.predict(Xl[te]) == yl[te]).mean())
        majority = 1.0 if yl[tr].mean() >= 0.5 else 0.0
        base.append((yl[te] == majority).mean())

    clf = make().fit(Xl, yl)
    latest = data[features].iloc[[-1]].to_numpy()
    importances = sorted(zip(features, clf.feature_importances_), key=lambda t: -t[1])
    return {
        "available": True,
        "horizon_days": horizon,
        "prob_up": float(clf.predict_proba(latest)[0, 1]),
        "walk_forward_accuracy": float(np.mean(acc)),
        "baseline_accuracy": float(np.mean(base)),
        "edge": float(np.mean(acc) - np.mean(base)),
        "folds": len(acc),
        "alt_data_features": features[len(FEATURES):],
        "feature_importance": [{"feature": f, "importance": float(v)} for f, v in importances],
    }
