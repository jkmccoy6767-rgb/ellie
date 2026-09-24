"""Reported fundamentals from SEC EDGAR XBRL company facts (free, authoritative)."""

from __future__ import annotations

import pandas as pd

from .base import http_get

NAME = "sec-edgar"
URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Our metric name -> candidate us-gaap concepts, in preference order.
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "shares_outstanding": ["CommonStockSharesOutstanding"],
}


def parse_company_facts(symbol: str, facts: dict) -> pd.DataFrame:
    """Keep annual (10-K, full fiscal year) values for each metric."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    rows = []
    for metric, candidates in CONCEPTS.items():
        for concept in candidates:
            if concept not in gaap:
                continue
            for unit_rows in gaap[concept]["units"].values():
                for f in unit_rows:
                    if f.get("form") != "10-K" or f.get("fp") != "FY":
                        continue
                    # Flow metrics (income statement) must span ~a year; stock metrics have no start.
                    if "start" in f:
                        days = (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days
                        if not 350 <= days <= 380:
                            continue
                    rows.append((symbol, metric, f["end"], float(f["val"]), f["form"], NAME))
            break  # first concept that exists wins
    df = pd.DataFrame(rows, columns=["symbol", "metric", "period_end", "value", "form", "source"])
    return df.drop_duplicates(["symbol", "metric", "period_end"], keep="last")


def fetch_symbol(symbol: str, cik: str, user_agent: str, timeout: int = 20) -> pd.DataFrame:
    resp = http_get(URL.format(cik=str(cik).zfill(10)), headers={"User-Agent": user_agent}, timeout=timeout)
    return parse_company_facts(symbol, resp.json())
