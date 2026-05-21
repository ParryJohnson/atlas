"""
ATLAS Signal Engine: Confluence Scorer
=======================================
The brain. Takes signals from all connectors and produces
a single conviction score per ticker.

Score range: -1.0 (strong short) to +1.0 (strong long)
Fire threshold: abs(score) > SIGNAL_FIRE_THRESHOLD from config

Key design decisions:
1. Regime multiplier scales ALL signal weights — don't fight the market
2. Signals must CONFIRM each other to hit fire threshold
3. Contradictory signals (bullish tech + bearish sentiment) = lower score
4. Recency weight: signals older than 24h decay
"""

from datetime import datetime, timedelta
from typing import Optional
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    SIGNAL_WEIGHTS, REGIME_MULTIPLIERS,
    SIGNAL_FIRE_THRESHOLD, SIGNAL_WATCH_THRESHOLD
)
from core.database import get_session, Signal, ConvictionScore

logger = logging.getLogger("atlas.scorer")


def compute_conviction_score(
    ticker: str,
    signals: list[dict],
    regime: str = "neutral",
    regime_multiplier: float = 1.0,
) -> dict:
    """
    Aggregate a list of signals into a conviction score.

    Args:
        ticker: stock ticker
        signals: list of signal dicts (from any connector)
        regime: current market regime string
        regime_multiplier: from classify_regime()

    Returns:
        conviction dict with score, direction, fired signals, recommendation
    """
    if not signals:
        return _empty_conviction(ticker)

    raw_score       = 0.0
    contributing    = []
    bullish_signals = []
    bearish_signals = []

    now = datetime.utcnow()

    for sig in signals:
        signal_type = sig.get("signal_type", "")
        direction   = sig.get("direction", "neutral")
        raw_value   = sig.get("value", 0.0)
        timestamp   = sig.get("timestamp", now)

        # Get base weight from config (or use the score already computed by connector)
        base_weight = sig.get("score", SIGNAL_WEIGHTS.get(signal_type, 0.05))

        # Recency decay: signals older than 6h get reduced weight
        if isinstance(timestamp, datetime):
            hours_old = (now - timestamp.replace(tzinfo=None)).total_seconds() / 3600
        else:
            hours_old = 0

        if hours_old > 48:
            recency_factor = 0.3
        elif hours_old > 24:
            recency_factor = 0.6
        elif hours_old > 6:
            recency_factor = 0.85
        else:
            recency_factor = 1.0

        # Apply regime multiplier to LONG signals (be conservative in bear markets)
        if direction == "bullish":
            regime_factor = regime_multiplier
        elif direction == "bearish":
            regime_factor = (2.0 - regime_multiplier)  # inverse: bearish signals stronger in bear markets
        else:
            regime_factor = 1.0

        weighted_score = base_weight * recency_factor * regime_factor
        raw_score += weighted_score

        signal_summary = {
            "type":           signal_type,
            "direction":      direction,
            "value":          raw_value,
            "weight":         round(base_weight, 4),
            "recency_factor": round(recency_factor, 2),
            "regime_factor":  round(regime_factor, 2),
            "contribution":   round(weighted_score, 4),
            "source":         sig.get("source", "unknown"),
        }
        contributing.append(signal_summary)

        if direction == "bullish":
            bullish_signals.append(signal_type)
        elif direction == "bearish":
            bearish_signals.append(signal_type)

    # Contradiction penalty: if we have both bull and bear signals, reduce confidence
    bull_count = len(bullish_signals)
    bear_count = len(bearish_signals)
    if bull_count > 0 and bear_count > 0:
        contradiction_ratio = min(bull_count, bear_count) / max(bull_count, bear_count)
        penalty = contradiction_ratio * 0.15
        raw_score *= (1.0 - penalty)
        logger.debug(f"[Scorer] {ticker}: contradiction penalty {penalty:.3f} "
                    f"(bull={bull_count}, bear={bear_count})")

    # Clamp to [-1, +1]
    final_score = max(-1.0, min(1.0, raw_score))

    # Determine direction and action
    direction = "long"  if final_score > 0 else "short"
    abs_score = abs(final_score)

    if abs_score >= SIGNAL_FIRE_THRESHOLD:
        action = "FIRE"
        confidence = "high"
    elif abs_score >= SIGNAL_WATCH_THRESHOLD:
        action = "WATCH"
        confidence = "medium"
    else:
        action = "IGNORE"
        confidence = "low"

    return {
        "ticker":           ticker,
        "score":            round(final_score, 4),
        "abs_score":        round(abs_score, 4),
        "direction":        direction,
        "action":           action,
        "confidence":       confidence,
        "regime":           regime,
        "regime_multiplier": regime_multiplier,
        "signal_count":     len(signals),
        "bullish_signals":  bullish_signals,
        "bearish_signals":  bearish_signals,
        "contributing":     contributing,
        "timestamp":        now.isoformat(),
        "above_threshold":  abs_score >= SIGNAL_FIRE_THRESHOLD,
    }


def _empty_conviction(ticker: str) -> dict:
    return {
        "ticker": ticker, "score": 0.0, "abs_score": 0.0,
        "direction": "neutral", "action": "IGNORE", "confidence": "none",
        "regime": "neutral", "signal_count": 0,
        "bullish_signals": [], "bearish_signals": [],
        "contributing": [], "above_threshold": False,
    }


def score_all_tickers(all_signals: dict, regime_data: dict) -> list[dict]:
    """
    Score every ticker in the universe.
    all_signals: {ticker: [signal, ...]} from combined connector output
    regime_data: from classify_regime()

    Returns list of conviction dicts, sorted by abs(score) desc.
    """
    regime        = regime_data.get("regime", "neutral")
    multiplier    = regime_data.get("multiplier", 1.0)
    session       = get_session()
    scored        = []

    for ticker, signals in all_signals.items():
        conviction = compute_conviction_score(ticker, signals, regime, multiplier)
        scored.append(conviction)

        # Persist to DB
        record = ConvictionScore(
            ticker=ticker,
            score=conviction["score"],
            direction=conviction["direction"],
            regime=regime,
            signals_fired=conviction["contributing"],
            above_threshold=conviction["above_threshold"],
            timestamp=datetime.utcnow(),
        )
        session.add(record)

        # Also store individual signals
        for sig in signals:
            signal_record = Signal(
                ticker=ticker,
                signal_type=sig.get("signal_type", "unknown"),
                value=sig.get("value", 0.0),
                score=sig.get("score", 0.0),
                direction=sig.get("direction", "neutral"),
                source=sig.get("source", "unknown"),
                raw_data=sig.get("raw_data", {}),
                is_fired=conviction["above_threshold"],
            )
            session.add(signal_record)

    session.commit()
    session.close()

    # Sort: FIRE first, then WATCH, then by score magnitude
    priority = {"FIRE": 0, "WATCH": 1, "IGNORE": 2}
    scored.sort(key=lambda x: (priority.get(x["action"], 3), -x["abs_score"]))

    fires  = [s for s in scored if s["action"] == "FIRE"]
    watches = [s for s in scored if s["action"] == "WATCH"]
    logger.info(f"[Scorer] Scored {len(scored)} tickers: "
               f"{len(fires)} FIRE, {len(watches)} WATCH — regime={regime.upper()}")

    return scored


def format_conviction_report(scored: list[dict]) -> str:
    """
    Human-readable conviction report.
    """
    fires  = [s for s in scored if s["action"] == "FIRE"]
    watches = [s for s in scored if s["action"] == "WATCH"]

    lines = [
        "=" * 65,
        f"  ATLAS CONVICTION REPORT  —  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 65,
    ]

    if fires:
        lines.append(f"\n🔥 FIRE ({len(fires)} tickers — above execution threshold)\n")
        for s in fires:
            bar = "█" * int(s["abs_score"] * 20)
            lines.append(f"  {s['ticker']:<8} [{s['direction'].upper():<5}] "
                         f"score={s['score']:+.3f}  {bar}")
            lines.append(f"           signals: {', '.join(s['bullish_signals'] + s['bearish_signals'])}")

    if watches:
        lines.append(f"\n👁  WATCH ({len(watches)} tickers — building conviction)\n")
        for s in watches[:8]:  # Show top 8 watches
            lines.append(f"  {s['ticker']:<8} [{s['direction'].upper():<5}] "
                         f"score={s['score']:+.3f}  "
                         f"signals: {len(s['signal_count'] if isinstance(s.get('signal_count'), list) else s.get('bullish_signals',[]) + s.get('bearish_signals',[]))}")

    lines.append(f"\n  Regime multiplier applied: {scored[0]['regime_multiplier']}x" if scored else "")
    lines.append("=" * 65)

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Confluence Scorer Test ===\n")

    # Simulate signals for multiple tickers
    mock_signals = {
        "NVDA": [
            {"signal_type": "rsi_oversold",       "value": 28.5, "score": 0.15, "direction": "bullish", "source": "technicals"},
            {"signal_type": "macd_bullish_cross",  "value": 0.42, "score": 0.12, "direction": "bullish", "source": "technicals"},
            {"signal_type": "insider_buy",         "value": 1.0,  "score": 0.18, "direction": "bullish", "source": "edgar"},
            {"signal_type": "news_sentiment_bullish","value": 0.7, "score": 0.10, "direction": "bullish", "source": "finnhub"},
            {"signal_type": "volume_spike",        "value": 2.3,  "score": 0.08, "direction": "bullish", "source": "technicals"},
        ],
        "TSLA": [
            {"signal_type": "rsi_overbought",      "value": 74.2, "score": -0.15, "direction": "bearish", "source": "technicals"},
            {"signal_type": "macd_bearish_cross",  "value": -0.3, "score": -0.12, "direction": "bearish", "source": "technicals"},
            {"signal_type": "news_sentiment_bearish","value":-0.6, "score": -0.10, "direction": "bearish", "source": "finnhub"},
        ],
        "AAPL": [
            {"signal_type": "bb_lower_touch",      "value": 165.2,"score": 0.10, "direction": "bullish", "source": "technicals"},
            {"signal_type": "google_trends_spike", "value": 1.8,  "score": 0.08, "direction": "bullish", "source": "google_trends"},
            {"signal_type": "insider_sell",        "value": 1.0,  "score": -0.10,"direction": "bearish", "source": "edgar"},
        ],
        "META": [
            {"signal_type": "fear_greed_extreme_fear","value": 15,"score": 0.12, "direction": "bullish", "source": "macro"},
            {"signal_type": "rsi_oversold",        "value": 26.0, "score": 0.15, "direction": "bullish", "source": "technicals"},
            {"signal_type": "congress_buy",        "value": 1.0,  "score": 0.12, "direction": "bullish", "source": "capitol_trades"},
            {"signal_type": "stocktwits_bullish",  "value": 0.72, "score": 0.07, "direction": "bullish", "source": "stocktwits"},
        ],
        "SPY": [
            {"signal_type": "golden_cross",        "value": 1.0,  "score": 0.14, "direction": "bullish", "source": "technicals"},
        ],
    }

    regime_data = {"regime": "neutral", "multiplier": 1.0}
    scored = score_all_tickers(mock_signals, regime_data)

    print(f"{'Ticker':<8} {'Score':>8} {'Action':<8} {'Direction':<8} {'Confidence'}")
    print("-" * 55)
    for s in scored:
        print(f"{s['ticker']:<8} {s['score']:>+8.4f} {s['action']:<8} "
              f"{s['direction']:<8} {s['confidence']}")

    print("\n")
    print(format_conviction_report(scored))
