"""
ATLAS Real-Time & Intraday Connector
======================================
Multi-timeframe data: 5m, 15m, 1h bars via yfinance
Pre-market gap detection
Real-time quote snapshot via Alpaca
Intraday technical signals

Timeframe → Trade type mapping:
  5m / 15m  → intraday  (minutes to hours)
  1h / 4h   → swing     (days to weeks)
  Daily      → position  (weeks to months)
"""

import yfinance as yf
import pandas as pd
import pandas_ta as ta
from datetime import datetime
import sys, os, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER

logger = logging.getLogger("atlas.realtime")

TIMEFRAMES = {
    "5m":  {"period": "5d",  "interval": "5m",  "trade_type": "intraday", "lookback": 40},
    "15m": {"period": "5d",  "interval": "15m", "trade_type": "intraday", "lookback": 30},
    "1h":  {"period": "60d", "interval": "1h",  "trade_type": "swing",    "lookback": 30},
    "4h":  {"period": "60d", "interval": "60m", "trade_type": "swing",    "lookback": 20},
}


def get_realtime_quote(ticker: str) -> dict:
    """Latest bid/ask snapshot via Alpaca. Returns {} if unavailable."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = client.get_stock_latest_quote(req)[ticker]
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        return {
            "bid": bid,
            "ask": ask,
            "spread": round(ask - bid, 4),
            "bid_size": int(quote.bid_size),
            "ask_size": int(quote.ask_size),
            "order_imbalance": round(
                (quote.ask_size - quote.bid_size) / (quote.ask_size + quote.bid_size + 1), 4
            ),
        }
    except Exception as e:
        logger.debug(f"[RealTime] Quote unavailable for {ticker}: {e}")
        return {}


def get_premarket_gap(ticker: str) -> dict:
    """
    Detect pre-market gap vs prior close.
    Returns gap_pct (positive = gap up, negative = gap down).
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="5d", interval="5m", prepost=True)
        if df.empty or len(df) < 20:
            return {}
        df.columns = [c.lower() for c in df.columns]

        # Prior regular-session close (hour < 16, i.e. before 4pm)
        reg_session = df[df.index.hour < 16]
        if reg_session.empty:
            return {}
        prior_close = float(reg_session["close"].iloc[-1])

        # Pre-market bars: 4am–9:29am today
        now = datetime.now()
        premarket = df[
            (df.index.date == now.date()) &
            ((df.index.hour >= 4) & (df.index.hour < 9) |
             ((df.index.hour == 9) & (df.index.minute < 30)))
        ]
        if premarket.empty:
            return {}

        premarket_price = float(premarket["close"].iloc[-1])
        gap_pct = (premarket_price - prior_close) / prior_close * 100

        return {
            "prior_close": round(prior_close, 2),
            "premarket_price": round(premarket_price, 2),
            "gap_pct": round(gap_pct, 3),
        }
    except Exception as e:
        logger.debug(f"[RealTime] Pre-market gap error {ticker}: {e}")
        return {}


def get_intraday_bars(ticker: str, timeframe: str = "15m") -> pd.DataFrame:
    """Fetch intraday OHLCV at the given timeframe."""
    cfg = TIMEFRAMES.get(timeframe, TIMEFRAMES["15m"])
    try:
        df = yf.Ticker(ticker).history(period=cfg["period"], interval=cfg["interval"])
        if df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        logger.warning(f"[RealTime] Bar fetch error {ticker}/{timeframe}: {e}")
        return pd.DataFrame()


def compute_intraday_technicals(df: pd.DataFrame) -> pd.DataFrame:
    """Add technical indicators to an intraday bar DataFrame."""
    if df.empty or len(df) < 15:
        return df

    df["ema_9"]  = ta.ema(df["close"], length=9)
    df["ema_21"] = ta.ema(df["close"], length=21)
    df["rsi"]    = ta.rsi(df["close"], length=14)
    df["atr"]    = ta.atr(df["high"], df["low"], df["close"], length=14)

    macd = ta.macd(df["close"])
    if macd is not None and not macd.empty:
        df["macd"]        = macd.iloc[:, 0]
        df["macd_signal"] = macd.iloc[:, 1]

    bb = ta.bbands(df["close"], length=20)
    if bb is not None and not bb.empty:
        df["bb_upper"] = bb.iloc[:, 2]
        df["bb_lower"] = bb.iloc[:, 0]

    df["vol_sma"]   = ta.sma(df["volume"], length=20)
    df["vol_ratio"] = df["volume"] / df["vol_sma"].replace(0, float("nan"))

    try:
        df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        pass

    return df


def get_intraday_signals(df: pd.DataFrame, ticker: str, timeframe: str) -> list[dict]:
    """Extract trading signals from an intraday bar DataFrame."""
    if df.empty or len(df) < 20:
        return []

    cfg        = TIMEFRAMES.get(timeframe, {})
    trade_type = cfg.get("trade_type", "intraday")
    source     = f"realtime_{timeframe}"
    signals    = []

    row  = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row

    def sig(signal_type, value, score, direction, extra=None):
        return {
            "ticker":     ticker,
            "signal_type": signal_type,
            "value":      round(float(value), 4),
            "score":      score,
            "direction":  direction,
            "source":     source,
            "timeframe":  timeframe,
            "trade_type": trade_type,
            "raw_data":   {**(extra or {}), "timeframe": timeframe},
            "timestamp":  datetime.utcnow(),
        }

    # ── VWAP ────────────────────────────────────────────────────────────────────
    if pd.notna(row.get("vwap")) and pd.notna(prev.get("vwap")):
        if prev["close"] < prev["vwap"] and row["close"] > row["vwap"]:
            signals.append(sig("intraday_vwap_reclaim", row["close"], 0.11, "bullish",
                               {"vwap": row["vwap"]}))
        elif prev["close"] > prev["vwap"] and row["close"] < row["vwap"]:
            signals.append(sig("intraday_vwap_rejection", row["close"], -0.11, "bearish",
                               {"vwap": row["vwap"]}))

    # ── EMA trend cross ──────────────────────────────────────────────────────────
    if pd.notna(row.get("ema_9")) and pd.notna(row.get("ema_21")) \
            and pd.notna(prev.get("ema_9")) and pd.notna(prev.get("ema_21")):
        if prev["ema_9"] < prev["ema_21"] and row["ema_9"] > row["ema_21"]:
            signals.append(sig("intraday_trend_up", row["ema_9"], 0.09, "bullish"))
        elif prev["ema_9"] > prev["ema_21"] and row["ema_9"] < row["ema_21"]:
            signals.append(sig("intraday_trend_down", row["ema_9"], -0.09, "bearish"))

    # ── RSI momentum ────────────────────────────────────────────────────────────
    if pd.notna(row.get("rsi")) and pd.notna(prev.get("rsi")):
        if prev["rsi"] < 50 <= row["rsi"]:
            signals.append(sig("momentum_burst", row["rsi"], 0.08, "bullish"))
        elif prev["rsi"] > 50 >= row["rsi"]:
            signals.append(sig("momentum_fade", row["rsi"], -0.08, "bearish"))
        if row["rsi"] < 25:
            signals.append(sig("rsi_oversold", row["rsi"], 0.13, "bullish"))
        elif row["rsi"] > 75:
            signals.append(sig("rsi_overbought", row["rsi"], -0.13, "bearish"))

    # ── MACD cross ───────────────────────────────────────────────────────────────
    if pd.notna(row.get("macd")) and pd.notna(row.get("macd_signal")) \
            and pd.notna(prev.get("macd")) and pd.notna(prev.get("macd_signal")):
        if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            signals.append(sig("macd_bullish_cross", row["macd"], 0.08, "bullish"))
        elif prev["macd"] > prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            signals.append(sig("macd_bearish_cross", row["macd"], -0.08, "bearish"))

    # ── Accumulation / Distribution ──────────────────────────────────────────────
    if pd.notna(row.get("vol_ratio")) and row["vol_ratio"] > 1.5:
        if row["close"] > prev["close"]:
            signals.append(sig("accumulation", row["vol_ratio"], 0.09, "bullish",
                               {"vol_ratio": row["vol_ratio"]}))
        elif row["close"] < prev["close"]:
            signals.append(sig("distribution", row["vol_ratio"], -0.09, "bearish",
                               {"vol_ratio": row["vol_ratio"]}))

    # ── Session high/low breakout ────────────────────────────────────────────────
    lookback   = min(cfg.get("lookback", 20), len(df) - 1)
    prior_high = df["high"].iloc[-lookback:-1].max()
    prior_low  = df["low"].iloc[-lookback:-1].min()
    if pd.notna(prior_high) and row["close"] > prior_high * 1.002:
        signals.append(sig("high_of_day_breakout", row["close"], 0.10, "bullish",
                           {"breakout_level": round(prior_high, 2)}))
    elif pd.notna(prior_low) and row["close"] < prior_low * 0.998:
        signals.append(sig("low_of_day_breakdown", row["close"], -0.10, "bearish",
                           {"breakdown_level": round(prior_low, 2)}))

    return signals


def get_premarket_signals(ticker: str) -> list[dict]:
    """Convert pre-market gap data into trading signals."""
    gap = get_premarket_gap(ticker)
    if not gap or "gap_pct" not in gap:
        return []

    pct = gap["gap_pct"]
    if pct >= 2.0:
        score = min(0.10 + (pct - 2.0) * 0.015, 0.20)
        return [{"ticker": ticker, "signal_type": "premarket_gap_up",
                 "value": round(pct, 3), "score": score, "direction": "bullish",
                 "source": "premarket", "timeframe": "premarket",
                 "trade_type": "intraday", "raw_data": gap,
                 "timestamp": datetime.utcnow()}]
    elif pct <= -2.0:
        score = max(-0.10 + (pct + 2.0) * 0.015, -0.20)
        return [{"ticker": ticker, "signal_type": "premarket_gap_down",
                 "value": round(pct, 3), "score": score, "direction": "bearish",
                 "source": "premarket", "timeframe": "premarket",
                 "trade_type": "intraday", "raw_data": gap,
                 "timestamp": datetime.utcnow()}]
    return []


def get_current_atr(ticker: str, timeframe: str = "15m") -> float:
    """Return the latest ATR value for a ticker at a given timeframe."""
    df = get_intraday_bars(ticker, timeframe)
    if df.empty:
        return 0.0
    df = compute_intraday_technicals(df)
    atr = df["atr"].dropna()
    return float(atr.iloc[-1]) if not atr.empty else 0.0


def run_realtime_scan(ticker: str, timeframes: list = None) -> list[dict]:
    """
    Full real-time scan for one ticker across multiple timeframes.
    Returns all signals tagged with their timeframe and trade_type.
    """
    timeframes = timeframes or ["15m", "1h"]
    all_signals = []

    # Pre-market gap (only relevant before 10am ET)
    hour = datetime.now().hour
    if 4 <= hour < 10:
        all_signals.extend(get_premarket_signals(ticker))

    for tf in timeframes:
        df = get_intraday_bars(ticker, tf)
        if df.empty:
            continue
        df = compute_intraday_technicals(df)
        sigs = get_intraday_signals(df, ticker, tf)
        all_signals.extend(sigs)
        logger.debug(f"[RealTime] {ticker}/{tf}: {len(sigs)} signals")

    return all_signals
