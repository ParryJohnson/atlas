"""
ATLAS Connector: Macro
======================
Fetches macro economic signals:
- FRED API: yield curve, CPI, fed funds rate, M2, unemployment
- CNN Fear & Greed Index (scrape)
- VIX via yfinance
- Market regime classification

FRED API key: free at https://fred.stlouisfed.org/docs/api/fred/
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import FRED_API_KEY
from core.database import get_session, MarketRegime

logger = logging.getLogger("atlas.macro")

# Key FRED series IDs
FRED_SERIES = {
    "fed_funds_rate":    "FEDFUNDS",       # Federal Funds Rate
    "cpi_yoy":           "CPIAUCSL",       # Consumer Price Index
    "unemployment":      "UNRATE",         # Unemployment Rate
    "yield_10y":         "DGS10",          # 10-Year Treasury
    "yield_2y":          "DGS2",           # 2-Year Treasury
    "yield_3m":          "DGS3MO",         # 3-Month Treasury
    "m2_money_supply":   "M2SL",           # M2 Money Supply
    "gdp_growth":        "A191RL1Q225SBEA", # Real GDP Growth
    "consumer_sentiment":"UMCSENT",         # University of Michigan Sentiment
    "housing_starts":    "HOUST",          # Housing Starts
    "industrial_prod":   "INDPRO",         # Industrial Production Index
    "credit_spread":     "BAMLH0A0HYM2",   # High Yield credit spread (risk-on/off)
}


def fetch_fred_series(series_id: str, limit: int = 12) -> Optional[pd.Series]:
    """Fetch a FRED data series. Returns pandas Series or None."""
    if not FRED_API_KEY:
        logger.warning("[Macro] FRED_API_KEY not set — skipping FRED fetch")
        return None
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "limit": limit,
            "sort_order": "desc",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        obs = data.get("observations", [])
        if not obs:
            return None
        series = pd.Series(
            {o["date"]: float(o["value"]) for o in obs if o["value"] != "."},
            name=series_id
        )
        series.index = pd.to_datetime(series.index)
        return series.sort_index()
    except Exception as e:
        logger.error(f"[Macro] FRED error for {series_id}: {e}")
        return None


def get_yield_curve() -> dict:
    """
    Compute yield curve spread (10Y - 2Y).
    Positive = normal (healthy), Negative = inverted (recession warning).
    """
    y10 = fetch_fred_series("DGS10", limit=5)
    y2  = fetch_fred_series("DGS2",  limit=5)

    if y10 is None or y2 is None:
        return {"spread": None, "inverted": None, "signal": "unavailable"}

    spread = float(y10.iloc[-1]) - float(y2.iloc[-1])
    inverted = spread < 0

    return {
        "yield_10y": round(float(y10.iloc[-1]), 3),
        "yield_2y":  round(float(y2.iloc[-1]), 3),
        "spread":    round(spread, 3),
        "inverted":  inverted,
        "signal":    "bearish" if inverted else "bullish",
        "description": f"Yield curve {'INVERTED' if inverted else 'normal'}: {spread:+.3f}%"
    }


def get_fear_greed_index() -> dict:
    """
    Fetch CNN Fear & Greed Index.
    Score: 0-100 (0=Extreme Fear, 100=Extreme Greed)
    """
    try:
        # CNN's internal API endpoint
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            fg = data.get("fear_and_greed", {})
            score = float(fg.get("score", 50))
            rating = fg.get("rating", "neutral")
            prev_close = float(fg.get("previous_close", score))

            if score <= 20:
                signal = "extreme_fear"
                trade_signal = "bullish"    # Contrarian: extreme fear = buy opportunity
            elif score <= 40:
                signal = "fear"
                trade_signal = "mild_bullish"
            elif score <= 60:
                signal = "neutral"
                trade_signal = "neutral"
            elif score <= 80:
                signal = "greed"
                trade_signal = "mild_bearish"
            else:
                signal = "extreme_greed"
                trade_signal = "bearish"    # Contrarian: extreme greed = caution

            return {
                "score": round(score, 1),
                "rating": rating,
                "signal": signal,
                "trade_signal": trade_signal,
                "prev_close": round(prev_close, 1),
                "change": round(score - prev_close, 1),
                "description": f"Fear & Greed: {score:.0f}/100 ({rating})"
            }
    except Exception as e:
        logger.warning(f"[Macro] Fear & Greed fetch error: {e}")

    # Fallback: neutral
    return {"score": 50, "rating": "neutral", "signal": "neutral",
            "trade_signal": "neutral", "description": "Fear & Greed: unavailable"}


def get_macro_snapshot() -> dict:
    """
    Full macro snapshot. Returns all key indicators.
    """
    snapshot = {
        "timestamp": datetime.utcnow().isoformat(),
        "yield_curve": get_yield_curve(),
        "fear_greed": get_fear_greed_index(),
    }

    # Fetch key FRED series
    for name, series_id in {
        "fed_funds_rate": "FEDFUNDS",
        "cpi_yoy": "CPIAUCSL",
        "unemployment": "UNRATE",
        "credit_spread": "BAMLH0A0HYM2",
    }.items():
        series = fetch_fred_series(series_id, limit=2)
        if series is not None and not series.empty:
            snapshot[name] = {
                "latest": round(float(series.iloc[-1]), 3),
                "prior":  round(float(series.iloc[-2]), 3) if len(series) > 1 else None,
                "series_id": series_id,
            }

    return snapshot


def classify_regime(vix: float, spy_vs_200ma: float,
                    yield_spread: float, fear_greed: float) -> dict:
    """
    Classify current market regime from key indicators.

    Args:
        vix: VIX level
        spy_vs_200ma: SPY % above/below 200-day MA (positive = above)
        yield_spread: 10Y - 2Y yield spread
        fear_greed: CNN Fear & Greed score (0-100)

    Returns:
        dict with regime name, confidence score, and multiplier
    """
    score = 0.0

    # VIX component (higher VIX = more bearish/volatile)
    if vix > 35:
        score -= 0.4
    elif vix > 25:
        score -= 0.2
    elif vix > 18:
        score -= 0.05
    elif vix < 14:
        score += 0.1    # Very low VIX = complacency warning actually
    else:
        score += 0.15

    # SPY vs 200MA (trend)
    if spy_vs_200ma > 5:
        score += 0.3    # Well above 200MA = bull
    elif spy_vs_200ma > 0:
        score += 0.15
    elif spy_vs_200ma > -5:
        score -= 0.15
    else:
        score -= 0.35   # Well below 200MA = bear

    # Yield curve
    if yield_spread > 0.5:
        score += 0.15
    elif yield_spread > 0:
        score += 0.05
    elif yield_spread > -0.5:
        score -= 0.10
    else:
        score -= 0.20   # Deep inversion = serious recession warning

    # Fear & Greed
    if fear_greed < 20:
        score -= 0.10   # Extreme fear = bearish environment (but contrarian buy)
    elif fear_greed < 40:
        score -= 0.05
    elif fear_greed > 80:
        score -= 0.05   # Extreme greed = getting frothy
    else:
        score += 0.05

    # Classify
    if vix > 35 or spy_vs_200ma < -10:
        regime = "crash"
        multiplier = 0.2
    elif score > 0.4:
        regime = "bull"
        multiplier = 1.2
    elif score > 0.1:
        regime = "neutral"
        multiplier = 1.0
    elif vix > 22:
        regime = "high_vol"
        multiplier = 0.7
    else:
        regime = "bear"
        multiplier = 0.5

    return {
        "regime": regime,
        "regime_score": round(score, 3),
        "multiplier": multiplier,
        "inputs": {
            "vix": vix,
            "spy_vs_200ma": spy_vs_200ma,
            "yield_spread": yield_spread,
            "fear_greed": fear_greed,
        },
        "description": f"Regime: {regime.upper()} (score={score:+.2f}, multiplier={multiplier}x)"
    }


def store_regime(regime_data: dict, macro: dict, session):
    """Persist regime snapshot to database."""
    record = MarketRegime(
        date=datetime.utcnow(),
        regime=regime_data["regime"],
        vix_level=regime_data["inputs"]["vix"],
        spy_vs_200ma=regime_data["inputs"]["spy_vs_200ma"],
        yield_spread=macro.get("yield_curve", {}).get("spread"),
        fear_greed=macro.get("fear_greed", {}).get("score"),
        regime_score=regime_data["regime_score"],
    )
    session.add(record)
    session.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Macro Connector Test ===\n")

    # Fear & Greed
    fg = get_fear_greed_index()
    print(f"Fear & Greed: {fg['description']}")
    print(f"  Trade signal: {fg['trade_signal']}")

    # Yield curve
    yc = get_yield_curve()
    print(f"\nYield Curve: {yc['description']}")

    # Regime classification (with example inputs)
    print("\n--- Regime Classification Examples ---")
    scenarios = [
        {"vix": 14, "spy_vs_200ma": 8,  "yield_spread": 0.8,  "fear_greed": 72, "label": "Bull market"},
        {"vix": 20, "spy_vs_200ma": 1,  "yield_spread": -0.3, "fear_greed": 45, "label": "Neutral/mixed"},
        {"vix": 28, "spy_vs_200ma": -6, "yield_spread": -0.8, "fear_greed": 22, "label": "Bear market"},
        {"vix": 42, "spy_vs_200ma":-15, "yield_spread": -1.2, "fear_greed": 8,  "label": "Crash mode"},
    ]
    for s in scenarios:
        r = classify_regime(s["vix"], s["spy_vs_200ma"], s["yield_spread"], s["fear_greed"])
        print(f"  {s['label']:20}: {r['description']}")
