"""
ATLAS Connector: yfinance
=========================
Fetches OHLCV price data, options chains, and computes
technical indicators via pandas-ta.
No API key required.
"""

import yfinance as yf
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import WATCHLIST
from core.database import get_session, RawMarketData
import logging

logger = logging.getLogger("atlas.price")


def fetch_ohlcv(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """
    Fetch OHLCV data for a ticker.
    period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
    """
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period=period, interval=interval)
        if df.empty:
            logger.warning(f"[Price] No data for {ticker}")
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        df.columns = [c.lower() for c in df.columns]
        logger.info(f"[Price] {ticker}: {len(df)} bars ({interval})")
        return df
    except Exception as e:
        logger.error(f"[Price] Error fetching {ticker}: {e}")
        return pd.DataFrame()


def compute_technicals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all technical indicators to an OHLCV DataFrame.
    Returns enriched DataFrame.
    """
    if df.empty or len(df) < 20:
        return df

    # ── Trend ──────────────────────────────────────────────────────────────────
    df["ema_9"]   = ta.ema(df["close"], length=9)
    df["ema_21"]  = ta.ema(df["close"], length=21)
    df["sma_50"]  = ta.sma(df["close"], length=50)
    df["sma_200"] = ta.sma(df["close"], length=200)

    # ── Momentum ───────────────────────────────────────────────────────────────
    df["rsi"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"])
    if macd is not None:
        df["macd"]        = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"]   = macd["MACDh_12_26_9"]

    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is not None:
        df["stoch_k"] = stoch["STOCHk_14_3_3"]
        df["stoch_d"] = stoch["STOCHd_14_3_3"]

    # ── Volatility ─────────────────────────────────────────────────────────────
    bb = ta.bbands(df["close"], length=20)
    if bb is not None:
        df["bb_upper"] = bb["BBU_20_2.0"]
        df["bb_mid"]   = bb["BBM_20_2.0"]
        df["bb_lower"] = bb["BBL_20_2.0"]
        df["bb_width"] = bb["BBB_20_2.0"]

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ── Volume ─────────────────────────────────────────────────────────────────
    df["obv"]        = ta.obv(df["close"], df["volume"])
    df["vol_sma_20"] = ta.sma(df["volume"], length=20)
    df["vol_ratio"]  = df["volume"] / df["vol_sma_20"]   # > 2 = volume spike

    # ── VWAP (only meaningful intraday, useful as S/R on daily) ───────────────
    try:
        df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        pass

    return df


def get_signals_from_technicals(df: pd.DataFrame, ticker: str) -> list[dict]:
    """
    Read the latest bar and extract signal hits.
    Returns a list of signal dicts for the scoring engine.
    """
    if df.empty or len(df) < 30:
        return []

    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row
    signals = []

    def sig(signal_type, value, score_contribution, direction, extra=None):
        return {
            "ticker": ticker,
            "signal_type": signal_type,
            "value": round(float(value), 4),
            "score": score_contribution,
            "direction": direction,
            "source": "yfinance_technicals",
            "raw_data": extra or {},
        }

    # RSI
    if pd.notna(row.get("rsi")):
        if row["rsi"] < 30:
            signals.append(sig("rsi_oversold", row["rsi"], 0.15, "bullish"))
        elif row["rsi"] > 70:
            signals.append(sig("rsi_overbought", row["rsi"], -0.15, "bearish"))

    # MACD crossover
    if pd.notna(row.get("macd")) and pd.notna(prev.get("macd")):
        if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            signals.append(sig("macd_bullish_cross", row["macd"], 0.12, "bullish"))
        elif prev["macd"] > prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            signals.append(sig("macd_bearish_cross", row["macd"], -0.12, "bearish"))

    # Bollinger Band touches
    if pd.notna(row.get("bb_lower")) and pd.notna(row.get("bb_upper")):
        if row["close"] <= row["bb_lower"]:
            signals.append(sig("bb_lower_touch", row["close"], 0.10, "bullish",
                               {"bb_lower": row["bb_lower"]}))
        elif row["close"] >= row["bb_upper"]:
            signals.append(sig("bb_upper_touch", row["close"], -0.10, "bearish",
                               {"bb_upper": row["bb_upper"]}))

    # Golden / Death cross (50 vs 200 SMA)
    if pd.notna(row.get("sma_50")) and pd.notna(row.get("sma_200")):
        if pd.notna(prev.get("sma_50")) and pd.notna(prev.get("sma_200")):
            if prev["sma_50"] < prev["sma_200"] and row["sma_50"] > row["sma_200"]:
                signals.append(sig("golden_cross", row["sma_50"], 0.14, "bullish"))
            elif prev["sma_50"] > prev["sma_200"] and row["sma_50"] < row["sma_200"]:
                signals.append(sig("death_cross", row["sma_50"], -0.14, "bearish"))

    # Volume spike
    if pd.notna(row.get("vol_ratio")) and row["vol_ratio"] > 2.0:
        signals.append(sig("volume_spike", row["vol_ratio"], 0.08, "bullish",
                           {"vol_ratio": row["vol_ratio"]}))

    return signals


def get_vix_level() -> float:
    """Fetch current VIX level."""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="2d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"[Price] VIX fetch error: {e}")
    return 20.0   # default to neutral if unavailable


def get_options_data(ticker: str) -> dict:
    """
    Fetch options chain summary: put/call ratio, IV, nearest expiry.
    """
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return {}

        # Use nearest expiry
        chain = t.option_chain(expirations[0])
        calls_vol = chain.calls["volume"].sum()
        puts_vol  = chain.puts["volume"].sum()
        pc_ratio  = puts_vol / calls_vol if calls_vol > 0 else 1.0

        avg_call_iv = chain.calls["impliedVolatility"].mean()
        avg_put_iv  = chain.puts["impliedVolatility"].mean()

        return {
            "put_call_ratio": round(pc_ratio, 3),
            "avg_call_iv": round(avg_call_iv, 3),
            "avg_put_iv": round(avg_put_iv, 3),
            "nearest_expiry": expirations[0],
            "calls_volume": int(calls_vol),
            "puts_volume": int(puts_vol),
        }
    except Exception as e:
        logger.warning(f"[Price] Options fetch error for {ticker}: {e}")
        return {}


def store_ohlcv(df: pd.DataFrame, ticker: str, session):
    """Persist latest OHLCV bar to database."""
    if df.empty:
        return
    row = df.iloc[-1]
    record = RawMarketData(
        ticker=ticker,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        date=datetime.utcnow(),
    )
    session.add(record)
    session.commit()


def run_price_scan(tickers: list = None) -> dict:
    """
    Full price scan: fetch data, compute technicals, extract signals.
    Returns dict: {ticker: [signal, ...]}
    """
    tickers = tickers or [t for t in WATCHLIST if not t.startswith("^")]
    all_signals = {}
    session = get_session()

    for ticker in tickers:
        df = fetch_ohlcv(ticker, period="1y", interval="1d")
        if df.empty:
            continue
        df = compute_technicals(df)
        signals = get_signals_from_technicals(df, ticker)
        all_signals[ticker] = signals
        store_ohlcv(df, ticker, session)

    session.close()
    total = sum(len(v) for v in all_signals.values())
    logger.info(f"[Price] Scan complete — {total} signals across {len(all_signals)} tickers")
    return all_signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Price Connector Test ===")
    df = fetch_ohlcv("AAPL", period="3mo")
    df = compute_technicals(df)
    print(f"AAPL shape: {df.shape}")
    print(f"Latest row:\n{df.iloc[-1][['close','rsi','macd','bb_lower','bb_upper','atr','vol_ratio']].to_string()}")

    signals = get_signals_from_technicals(df, "AAPL")
    print(f"\nSignals detected: {len(signals)}")
    for s in signals:
        print(f"  [{s['direction'].upper():8}] {s['signal_type']:25} value={s['value']:.3f} score={s['score']}")

    vix = get_vix_level()
    print(f"\nVIX: {vix:.2f}")
