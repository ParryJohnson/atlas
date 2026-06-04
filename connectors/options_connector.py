"""
ATLAS Options Flow Connector
==============================
Options chain analysis via yfinance (free, no key required).

Signals generated:
  - unusual_call_volume / unusual_put_volume  (flow vs open interest)
  - pc_ratio_extreme_bullish / bearish        (put/call ratio extremes)
  - iv_expansion / iv_low                     (volatility environment)
  - skew_bullish / skew_bearish               (put IV vs call IV premium)

All signals feed into the same conviction scoring engine.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import sys, os, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
logger = logging.getLogger("atlas.options")

# ETFs and instruments where options flow is less meaningful
_SKIP_TICKERS = {"^VIX", "TLT", "GLD", "IWM"}


def get_iv_rank(ticker: str) -> float | None:
    """
    Estimate IV rank (0–100) using 1-year rolling realized volatility.
    High rank (>75) = elevated IV.  Low rank (<25) = compressed IV.
    """
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty or len(hist) < 60:
            return None
        rets = hist["Close"].pct_change().dropna()
        rolling_vol = rets.rolling(21).std() * (252 ** 0.5) * 100
        rolling_vol = rolling_vol.dropna()
        if rolling_vol.empty:
            return None
        lo, hi, cur = rolling_vol.min(), rolling_vol.max(), rolling_vol.iloc[-1]
        if hi == lo:
            return 50.0
        return round(float((cur - lo) / (hi - lo) * 100), 1)
    except Exception as e:
        logger.debug(f"[Options] IV rank error {ticker}: {e}")
        return None


def analyze_options_chain(ticker: str) -> dict:
    """
    Full options chain analysis across the 3 nearest expiries.
    Returns a structured summary dict.
    """
    base = {
        "ticker": ticker, "has_options": False,
        "put_call_ratio": None,
        "unusual_call_volume": False, "unusual_put_volume": False,
        "avg_call_iv": None, "avg_put_iv": None, "skew": None,
        "total_call_vol": None, "total_put_vol": None,
        "total_call_oi": None, "total_put_oi": None,
        "iv_rank": None, "nearest_expiry": None,
    }
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return base

        base["has_options"]    = True
        base["nearest_expiry"] = expirations[0]

        calls_list, puts_list = [], []
        for expiry in expirations[:3]:
            try:
                chain = t.option_chain(expiry)
                calls_list.append(chain.calls)
                puts_list.append(chain.puts)
            except Exception:
                continue

        if not calls_list:
            return base

        calls = pd.concat(calls_list, ignore_index=True)
        puts  = pd.concat(puts_list,  ignore_index=True)

        call_vol = float(calls["volume"].fillna(0).sum())
        put_vol  = float(puts["volume"].fillna(0).sum())
        call_oi  = float(calls["openInterest"].fillna(0).sum())
        put_oi   = float(puts["openInterest"].fillna(0).sum())

        base["total_call_vol"] = int(call_vol)
        base["total_put_vol"]  = int(put_vol)
        base["total_call_oi"]  = int(call_oi)
        base["total_put_oi"]   = int(put_oi)

        if call_vol > 0:
            base["put_call_ratio"] = round(put_vol / call_vol, 3)

        call_iv_s = calls["impliedVolatility"].replace(0, np.nan).dropna()
        put_iv_s  = puts["impliedVolatility"].replace(0, np.nan).dropna()
        if not call_iv_s.empty:
            base["avg_call_iv"] = round(float(call_iv_s.mean()), 4)
        if not put_iv_s.empty:
            base["avg_put_iv"] = round(float(put_iv_s.mean()), 4)

        if base["avg_call_iv"] and base["avg_put_iv"] and base["avg_call_iv"] > 0:
            base["skew"] = round(base["avg_put_iv"] / base["avg_call_iv"] - 1, 4)

        # Unusual volume: today's flow vs open interest baseline
        if call_oi > 100:
            base["unusual_call_volume"] = (call_vol / call_oi) > 0.40
        if put_oi > 100:
            base["unusual_put_volume"] = (put_vol / put_oi) > 0.40

        base["iv_rank"] = get_iv_rank(ticker)
        return base

    except Exception as e:
        logger.warning(f"[Options] Chain analysis error {ticker}: {e}")
        return base


def get_options_signals(ticker: str) -> list[dict]:
    """Generate conviction signals from options chain data."""
    if ticker in _SKIP_TICKERS:
        return []

    data = analyze_options_chain(ticker)
    if not data.get("has_options"):
        return []

    signals = []

    def sig(signal_type, value, score, direction, extra=None):
        return {
            "ticker":      ticker,
            "signal_type": signal_type,
            "value":       round(float(value), 4),
            "score":       score,
            "direction":   direction,
            "source":      "options_flow",
            "raw_data":    {**(extra or {}), **{k: v for k, v in data.items() if v is not None}},
            "timestamp":   datetime.utcnow(),
        }

    pc = data.get("put_call_ratio")
    if pc is not None:
        if pc < 0.5:
            # Heavy call buying relative to puts — bullish
            signals.append(sig("pc_ratio_extreme_bullish", pc, 0.12, "bullish",
                               {"note": "heavy call buying"}))
        elif 1.5 <= pc <= 2.5:
            # Heavy put buying — bearish hedging
            signals.append(sig("pc_ratio_extreme_bearish", pc, -0.12, "bearish",
                               {"note": "heavy put hedging"}))
        elif pc > 2.5:
            # Extreme fear — contrarian bullish signal
            signals.append(sig("pc_ratio_extreme_bearish", pc, 0.08, "bullish",
                               {"note": "extreme fear — contrarian buy"}))

    if data.get("unusual_call_volume") and data.get("total_call_vol", 0) > 500:
        signals.append(sig("unusual_call_volume",
                           data["total_call_vol"], 0.14, "bullish",
                           {"call_vol": data["total_call_vol"],
                            "call_oi": data["total_call_oi"]}))

    if data.get("unusual_put_volume") and data.get("total_put_vol", 0) > 500:
        signals.append(sig("unusual_put_volume",
                           data["total_put_vol"], -0.14, "bearish",
                           {"put_vol": data["total_put_vol"],
                            "put_oi": data["total_put_oi"]}))

    iv_rank = data.get("iv_rank")
    if iv_rank is not None:
        if iv_rank > 75:
            signals.append(sig("iv_expansion", iv_rank, -0.05, "bearish",
                               {"iv_rank": iv_rank, "note": "elevated IV, fear rising"}))
        elif iv_rank < 20:
            signals.append(sig("iv_crush_setup", iv_rank, 0.05, "bullish",
                               {"iv_rank": iv_rank, "note": "compressed IV, calm market"}))

    skew = data.get("skew")
    if skew is not None:
        if skew > 0.15:
            signals.append(sig("skew_bearish", skew, -0.07, "bearish",
                               {"note": "put IV premium elevated"}))
        elif skew < -0.05:
            signals.append(sig("skew_bullish", skew, 0.07, "bullish",
                               {"note": "call IV elevated, bullish positioning"}))

    logger.info(f"[Options] {ticker}: {len(signals)} signals "
                f"(P/C={pc}, unusual_calls={data.get('unusual_call_volume')}, "
                f"IV_rank={iv_rank})")
    return signals


def run_options_scan(tickers: list) -> dict:
    """Scan options flow for all tickers. Returns {ticker: [signals]}."""
    results = {}
    for ticker in tickers:
        if ticker in _SKIP_TICKERS or ticker.startswith("^"):
            continue
        try:
            results[ticker] = get_options_signals(ticker)
        except Exception as e:
            logger.error(f"[Options] Error for {ticker}: {e}")
            results[ticker] = []
    total = sum(len(v) for v in results.values())
    logger.info(f"[Options] Scan complete: {total} signals across {len(results)} tickers")
    return results
