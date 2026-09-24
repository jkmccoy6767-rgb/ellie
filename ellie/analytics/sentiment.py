"""News sentiment: headline scoring and per-symbol / sector / market aggregation.

Two scorers share one interface (``score(items) -> list[float]`` in [-1, 1]):

* ``LexiconScorer`` - finance-specific word and phrase lists in the spirit of the
  Loughran-McDonald dictionary, with negation handling. Free, fast, deterministic.
* ``ClaudeScorer`` - asks a Claude model to score each headline from a shareholder's
  point of view. Opt-in (``ELLIE_SENTIMENT_SCORER=claude``) because it costs money;
  any batch it cannot score falls back to the lexicon, so a run never fails on it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- lexicon

POSITIVE_PHRASES = [
    "beats estimates", "beat estimates", "tops estimates", "above estimates", "raises guidance", "raised guidance",
    "lifts guidance", "boosts guidance", "raises outlook", "raises forecast", "record revenue", "record profit",
    "price target raised", "raises price target", "share buyback", "stock buyback", "dividend increase",
    "raises dividend", "upgraded to buy", "strong demand", "all-time high", "wins contract", "fda approval",
]
NEGATIVE_PHRASES = [
    "misses estimates", "missed estimates", "below estimates", "cuts guidance", "cut guidance", "lowers guidance",
    "lowered guidance", "slashes guidance", "cuts outlook", "lowers forecast", "price target cut", "cuts price target",
    "profit warning", "downgraded to sell", "weak demand", "data breach", "class action", "going concern",
    "cuts dividend", "suspends dividend", "job cuts", "sec probe", "under investigation",
]
POSITIVE_WORDS = set("""
beat beats outperform outperforms outperformed surge surges surged soar soars soared jump jumps jumped rally rallies
rallied gain gains gained rise rises rose climb climbs climbed record upgrade upgrades upgraded bullish strong
stronger strongest growth grow grows grew expand expands expansion profit profitable profitability win wins won
boost boosts boosted exceed exceeds exceeded robust momentum breakthrough approval approved accelerate
accelerates accelerating improve improves improved improvement optimistic confident confidence rebound rebounds
rebounded recover recovers recovered recovery upside tailwind tailwinds innovative innovation partnership
milestone buyback raise raises raised higher high resilient solid success successful outpace outpaced
""".split())
NEGATIVE_WORDS = set("""
miss misses missed underperform underperforms underperformed plunge plunges plunged plummet plummets plummeted
sink sinks sank slump slumps slumped drop drops dropped fall falls fell tumble tumbles tumbled decline declines
declined downgrade downgrades downgraded bearish weak weaker weakest loss losses lose loses lost cut cuts
lawsuit lawsuits probe investigation fraud penalty fine fined layoffs layoff bankruptcy default defaults
warning warns warned risk risks concern concerns slowdown slowing slows headwind headwinds shortfall disappoint
disappoints disappointing disappointed volatile volatility halt halts halted delay delays delayed recall
recalls breach lower low pressure pressured struggle struggles struggling crash crashes crashed selloff
downturn scandal resign resigns resigned subpoena antitrust shortage deficit
""".split())
NEGATORS = {"not", "no", "never", "without", "fails", "fail", "failed", "unlikely", "barely"}
_TOKEN = re.compile(r"[a-z][a-z\-']*")


def lexicon_score(text: str) -> float:
    """Net tone in [-1, 1]: (positive - negative) / (positive + negative + 1)."""
    t = text.lower()
    pos = sum(t.count(p) for p in POSITIVE_PHRASES) * 2
    neg = sum(t.count(p) for p in NEGATIVE_PHRASES) * 2
    for p in POSITIVE_PHRASES + NEGATIVE_PHRASES:  # don't double count the phrase's words
        t = t.replace(p, " ")
    tokens = _TOKEN.findall(t)
    for i, tok in enumerate(tokens):
        polarity = 1 if tok in POSITIVE_WORDS else -1 if tok in NEGATIVE_WORDS else 0
        if not polarity:
            continue
        if any(w in NEGATORS for w in tokens[max(0, i - 3):i]):
            polarity = -polarity
        if polarity > 0:
            pos += 1
        else:
            neg += 1
    return (pos - neg) / (pos + neg + 1)


@dataclass
class NewsItem:
    symbol: str
    company: str
    headline: str
    summary: str | None = None

    @property
    def text(self) -> str:
        return f"{self.headline}. {self.summary or ''}"


class LexiconScorer:
    name = "lexicon-v1"

    def score(self, items: Sequence[NewsItem]) -> list[float]:
        return [round(lexicon_score(i.text), 4) for i in items]


# ---------------------------------------------------------------------------- Claude scorer

SCORING_SYSTEM = """You score financial news for an equity research platform.

For each numbered item, rate how the news is likely to affect the named company's shareholders:
-1.0 = clearly negative (e.g. guidance cut, fraud, major loss), 0 = neutral or purely informational,
+1.0 = clearly positive (e.g. large earnings beat, major contract win). Use intermediate values for
moderate news. Judge the news itself, not the writing style. If an item is about a different company
or is not news about the named company, score it 0."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "score": {"type": "number"}},
                "required": ["i", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


class ClaudeScorer:
    """Language-model scoring with the Anthropic SDK. Batches items; falls back to the lexicon per batch."""

    def __init__(self, model: str = "claude-opus-5", batch_size: int = 40, client=None):
        import anthropic  # optional dependency: pip install anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.batch_size = batch_size
        self.name = f"claude:{model}"
        self.fallback = LexiconScorer()
        self.failed_batches = 0

    def _score_batch(self, batch: Sequence[NewsItem]) -> list[float]:
        listing = "\n".join(f"{n}. [{it.company} ({it.symbol})] {it.text[:600]}" for n, it in enumerate(batch))
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SCORING_SYSTEM,
            messages=[{"role": "user", "content": f"Score these {len(batch)} items:\n\n{listing}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            # Re-run a policy decline on Anthropic's recommended fallback model instead of failing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"unusable response: stop_reason={response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        by_index = {s["i"]: s["score"] for s in json.loads(text)["scores"]}
        if set(by_index) != set(range(len(batch))):
            raise ValueError("model did not score every item")
        return [round(float(np.clip(by_index[n], -1, 1)), 4) for n in range(len(batch))]

    def score(self, items: Sequence[NewsItem]) -> list[float]:
        out: list[float] = []
        for start in range(0, len(items), self.batch_size):
            batch = items[start:start + self.batch_size]
            try:
                out.extend(self._score_batch(batch))
            except (self._anthropic.APIError, ValueError, KeyError, StopIteration) as exc:
                # Rate limits / network errors are retried by the SDK first; after that, keep the run going.
                self.failed_batches += 1
                log.warning("Claude scoring failed for a batch of %d (%s); using lexicon", len(batch), exc)
                out.extend(self.fallback.score(batch))
        return out


def make_scorer(kind: str, model: str):
    if kind == "claude":
        try:
            return ClaudeScorer(model=model)
        except Exception as exc:  # SDK missing or no credentials
            log.warning("Claude scorer unavailable (%s); using lexicon", exc)
    return LexiconScorer()


# ---------------------------------------------------------------------------- aggregation

def daily_sentiment(news: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(mean sentiment, article count), each dates x symbols, by UTC publication date."""
    if news.empty:
        return pd.DataFrame(), pd.DataFrame()
    df = news.assign(day=pd.to_datetime(news["published_at"]).dt.tz_localize(None).dt.normalize())
    g = df.groupby(["day", "symbol"])["sentiment"]
    return g.mean().unstack(), g.size().unstack().fillna(0)


def symbol_summary(news: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Per-symbol 7- and 30-day sentiment, news volume and volume spike ratio."""
    if news.empty:
        return pd.DataFrame()
    t = pd.to_datetime(news["published_at"]).dt.tz_localize(None)
    end = as_of.normalize() + pd.Timedelta(days=1)
    w7 = news[(t >= end - pd.Timedelta(days=7)) & (t < end)]
    w30 = news[(t >= end - pd.Timedelta(days=30)) & (t < end)]
    out = pd.DataFrame({
        "sent_7d": w7.groupby("symbol")["sentiment"].mean(),
        "sent_30d": w30.groupby("symbol")["sentiment"].mean(),
        "news_7d": w7.groupby("symbol").size(),
        "news_30d": w30.groupby("symbol").size(),
    })
    out[["news_7d", "news_30d"]] = out[["news_7d", "news_30d"]].fillna(0)
    # How busy this week is versus the 30-day weekly average.
    out["news_ratio"] = out["news_7d"] / (out["news_30d"] * 7 / 30).replace(0, np.nan)
    out["sent_change"] = out["sent_7d"] - out["sent_30d"]
    return out.rename_axis("symbol")


def feature_frame(news: pd.DataFrame, index: pd.DatetimeIndex, symbol: str) -> pd.DataFrame:
    """Daily sentiment features for one stock, aligned to trading days (used by the direction model)."""
    sub = news[news["symbol"] == symbol]
    if sub.empty:
        return pd.DataFrame(index=index)
    mean, count = daily_sentiment(sub)
    cal = pd.date_range(min(mean.index.min(), index.min()), max(mean.index.max(), index.max()))
    s = mean[symbol].reindex(cal)
    c = count[symbol].reindex(cal).fillna(0)
    # Articles on weekends roll into the next trading day through the rolling windows.
    sent7 = (s.fillna(0) * c).rolling(7).sum() / c.rolling(7).sum().replace(0, np.nan)
    vol_ratio = c.rolling(7).sum() / (c.rolling(30).sum() * 7 / 30).replace(0, np.nan)
    return pd.DataFrame({"sent_7d": sent7.fillna(0), "news_ratio": vol_ratio.fillna(1)}).reindex(index)


def news_alerts(summary: pd.DataFrame, date: str) -> list[dict]:
    out = []
    if summary.empty:
        return out
    busy = summary[(summary["news_7d"] >= 3) & (summary["news_ratio"] >= 2.5)]
    for sym, r in busy.iterrows():
        if r["sent_7d"] <= -0.25:
            out.append({"symbol": sym, "date": date, "rule": "negative_news", "severity": "serious",
                        "detail": f"{int(r['news_7d'])} stories this week ({r['news_ratio']:.1f}× normal), sentiment {r['sent_7d']:+.2f}"})
        elif r["sent_7d"] >= 0.25:
            out.append({"symbol": sym, "date": date, "rule": "positive_news", "severity": "good",
                        "detail": f"{int(r['news_7d'])} stories this week ({r['news_ratio']:.1f}× normal), sentiment {r['sent_7d']:+.2f}"})
    return out
