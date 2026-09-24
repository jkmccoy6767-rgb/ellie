import pandas as pd

from ellie import quality


def _raw(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "source", "open", "high", "low", "close", "volume"])


def test_validate_drops_non_positive_and_flags_jumps():
    raw = _raw([
        ("A", "2024-01-02", "yahoo", 1, 1, 1, 10.0, 1),
        ("A", "2024-01-03", "yahoo", 1, 1, 1, 0.0, 1),
        ("A", "2024-01-04", "yahoo", 1, 1, 1, 20.0, 1),
    ])
    clean, issues = quality.validate_prices(raw)
    assert len(clean) == 2
    names = {i.check_name for i in issues}
    assert {"non_positive_close", "extreme_move"} <= names


def test_reconcile_prefers_primary_and_flags_mismatch():
    raw = _raw([
        ("A", "2024-01-02", "stooq", None, None, None, 10.0, 5),
        ("A", "2024-01-02", "yahoo", None, None, None, 10.01, 7),
        ("A", "2024-01-03", "stooq", None, None, None, 11.0, 5),
        ("A", "2024-01-03", "yahoo", None, None, None, 12.0, 7),
        ("A", "2024-01-04", "stooq", None, None, None, 13.0, 5),  # only secondary has it
    ])
    curated, issues = quality.reconcile(raw, tolerance=0.005)
    c = curated.set_index("date")
    assert c.loc["2024-01-02", "close"] == 10.01          # yahoo outranks stooq
    assert c.loc["2024-01-04", "close"] == 13.0           # gap filled from secondary
    assert c.loc["2024-01-02", "n_sources"] == 2
    assert [i.date for i in issues] == ["2024-01-03"]     # 9% spread flagged, 0.1% is not
    assert issues[0].severity == "critical"


def test_reconcile_takes_median_with_three_sources():
    raw = _raw([
        ("A", "2024-01-02", "yahoo", None, None, None, 50.0, 1),
        ("A", "2024-01-02", "stooq", None, None, None, 10.0, 1),
        ("A", "2024-01-02", "synthetic-primary", None, None, None, 10.2, 1),
    ])
    curated, _ = quality.reconcile(raw)
    assert curated["close"].iloc[0] == 10.2


def test_coverage_flags_missing_symbols():
    curated = pd.DataFrame({"symbol": ["A"] * 10, "date": [f"d{i}" for i in range(10)], "close": 1.0})
    issues = quality.coverage_issues(curated, ["A", "B"])
    assert [(i.check_name, i.symbol) for i in issues] == [("missing_symbol", "B")]
