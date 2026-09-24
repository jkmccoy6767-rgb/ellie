"""Company headlines from Yahoo Finance's public RSS feed (no key required)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd

from .base import empty_news, http_get
from .yahoo import vendor_symbol

NAME = "yahoo-rss"
URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"


def parse_rss(symbol: str, xml_text: str, since: pd.Timestamp) -> pd.DataFrame:
    root = ET.fromstring(xml_text)
    rows = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not pub:
            continue
        ts = pd.Timestamp(parsedate_to_datetime(pub)).tz_convert("UTC")
        if ts < since:
            continue
        rows.append({
            "symbol": symbol,
            "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": NAME,
            "headline": title,
            "summary": (item.findtext("description") or "").strip() or None,
            "url": item.findtext("link"),
        })
    return pd.DataFrame(rows) if rows else empty_news()


def fetch_symbol(symbol: str, since: pd.Timestamp, timeout: int = 20) -> pd.DataFrame:
    params = {"s": vendor_symbol(symbol), "region": "US", "lang": "en-US"}
    return parse_rss(symbol, http_get(URL, params=params, timeout=timeout).text, since)
