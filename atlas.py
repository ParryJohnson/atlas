"""
ATLAS Main Orchestrator
========================
The conductor. Runs the full scan cycle:

1. Fetch macro regime
2. Scan price + technicals for all tickers
3. Scan news + sentiment
4. Scan alternative data (social, insider, congress)
5. Score all signals via confluence engine
6. Run risk checks on FIRE-level signals
7. Execute approved trades via Alpaca
8. Log everything

Run modes:
  python atlas.py scan      — one full scan cycle
  python atlas.py paper     — continuous paper trading (scheduled)
  python atlas.py status    — show portfolio status
  python atlas.py report    — generate performance report
"""

import sys
import os
import time
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    WATCHLIST, INITIAL_CAPITAL, LOG_DIR, LOG_LEVEL,
    PRE_MARKET_SCAN_TIME, MARKET_OPEN_SCAN_TIME,
    MIDDAY_SCAN_TIME, CLOSE_SCAN_TIME, POST_MARKET_TIME
)
from core.database import init_db, get_session, PortfolioSnapshot
from connectors.macro_connector import get_fear_greed_index, get_yield_curve, classify_regime
from connectors.news_connector import run_news_scan
from connectors.alt_connector import get_alt_signals
from connectors.price_connector import run_price_scan, get_vix_level
from connectors.realtime_connector import run_realtime_scan
from connectors.options_connector import run_options_scan
from signals.scorer import score_all_tickers, format_conviction_report
from risk.risk_engine import full_risk_check
from execution.executor import AlpacaClient, execute_trade, sync_positions
from learning.performance_tracker import check_and_close_trades

# ── Logging setup ──────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(name)-18s] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / f"atlas_{datetime.now().strftime('%Y%m%d')}.log"),
    ]
)
logger = logging.getLogger("atlas.main")


def get_portfolio_value(client: AlpacaClient) -> float:
    """Get current portfolio value from Alpaca or return initial capital."""
    if client and client.headers.get("APCA-API-KEY-ID"):
        account = client.get_account()
        if account:
            return float(account.get("portfolio_value", INITIAL_CAPITAL))
    return INITIAL_CAPITAL


def run_full_scan(dry_run: bool = True) -> dict:
    """
    Execute one complete ATLAS scan cycle.
    Returns summary of what fired, what was watched, what was executed.
    """
    start_time = datetime.utcnow()
    logger.info("=" * 60)
    logger.info(f"ATLAS SCAN CYCLE STARTED — {start_time.strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info("=" * 60)

    client   = AlpacaClient()
    session  = get_session()
    results  = {
        "scan_time":   start_time.isoformat(),
        "regime":      {},
        "scores":      [],
        "fired":       [],
        "executed":    [],
        "errors":      [],
    }

    # ── Step 0: Check for closed positions → learning loop ────────────────────
    try:
        closed = check_and_close_trades(session, client)
        if closed:
            logger.info(f"[0/6] Learning loop: {len(closed)} position(s) closed → weights updated")
    except Exception as e:
        logger.warning(f"[0/6] Learning loop error: {e}")

    # ── Step 1: Macro regime ───────────────────────────────────────────────────
    logger.info("[1/6] Detecting market regime...")
    try:
        vix        = get_vix_level()
        fear_greed = get_fear_greed_index()
        yield_data = get_yield_curve()

        import yfinance as yf
        spy_hist     = yf.Ticker("SPY").history(period="1y")
        spy_close    = float(spy_hist["Close"].iloc[-1])
        spy_200ma    = float(spy_hist["Close"].rolling(200).mean().iloc[-1])
        spy_vs_200ma = (spy_close - spy_200ma) / spy_200ma * 100

        regime_data = classify_regime(
            vix=vix,
            spy_vs_200ma=spy_vs_200ma,
            yield_spread=yield_data.get("spread", 0.3),
            fear_greed=fear_greed.get("score", 50),
        )
        results["regime"] = regime_data
        logger.info(f"  Regime: {regime_data['regime'].upper()} | VIX={vix:.1f} | "
                    f"SPY vs 200MA={spy_vs_200ma:+.1f}% | F&G={fear_greed.get('score', '?')}")
    except Exception as e:
        logger.error(f"[1/6] Macro error: {e}")
        regime_data = {"regime": "neutral", "multiplier": 1.0}
        results["errors"].append(f"macro: {e}")

    # ── Step 2: Daily price + technicals ──────────────────────────────────────
    logger.info("[2/6] Running daily price & technical scan...")
    all_signals: dict = {}
    tickers = [t for t in WATCHLIST if not t.startswith("^")]
    try:
        daily_signals = run_price_scan(tickers)
        for ticker, sigs in daily_signals.items():
            all_signals.setdefault(ticker, []).extend(sigs)
        logger.info(f"  Daily signals: {sum(len(v) for v in daily_signals.values())} across {len(daily_signals)} tickers")
    except Exception as e:
        logger.error(f"[2/6] Price scan error: {e}")
        results["errors"].append(f"price: {e}")

    # ── Step 3: Intraday + real-time signals ───────────────────────────────────
    logger.info("[3/6] Running intraday & real-time scan...")
    try:
        for ticker in tickers:
            rt_sigs = run_realtime_scan(ticker, timeframes=["15m", "1h"])
            if rt_sigs:
                all_signals.setdefault(ticker, []).extend(rt_sigs)
            time.sleep(0.1)
        rt_total = sum(len(v) for v in all_signals.values()) - sum(
            len(daily_signals.get(t, [])) for t in tickers
        )
        logger.info(f"  Intraday signals added: {rt_total}")
    except Exception as e:
        logger.error(f"[3/6] Realtime scan error: {e}")
        results["errors"].append(f"realtime: {e}")

    # ── Step 4: News, sentiment, alternative data ─────────────────────────────
    logger.info("[4/6] Scanning news, sentiment & alternative data...")
    try:
        news_data = run_news_scan(tickers[:12])
        news_count = 0
        for ticker, sentiment in news_data.items():
            sig = sentiment.get("signal", "neutral")
            avg = sentiment.get("avg_sentiment", 0.0)
            if sig in ("strong_bullish", "mild_bullish") and avg > 0.1:
                all_signals.setdefault(ticker, []).append({
                    "signal_type": "news_sentiment_bullish",
                    "value": avg, "score": min(abs(avg) * 0.10, 0.10),
                    "direction": "bullish", "source": "news",
                    "timestamp": datetime.utcnow(),
                })
                news_count += 1
            elif sig in ("strong_bearish", "mild_bearish") and avg < -0.1:
                all_signals.setdefault(ticker, []).append({
                    "signal_type": "news_sentiment_bearish",
                    "value": avg, "score": min(abs(avg) * 0.10, 0.10),
                    "direction": "bearish", "source": "news",
                    "timestamp": datetime.utcnow(),
                })
                news_count += 1
        logger.info(f"  News signals: {news_count}")
    except Exception as e:
        logger.warning(f"[4/6] News error: {e}")
        results["errors"].append(f"news: {e}")

    try:
        for ticker in tickers[:8]:
            alt_sigs = get_alt_signals(ticker)
            if alt_sigs:
                all_signals.setdefault(ticker, []).extend(alt_sigs)
            time.sleep(0.2)
    except Exception as e:
        logger.warning(f"[4/6] Alt data error: {e}")
        results["errors"].append(f"alt_data: {e}")

    # ── Step 5: Options flow ───────────────────────────────────────────────────
    logger.info("[5/6] Scanning options flow...")
    try:
        options_sigs = run_options_scan(tickers)
        for ticker, sigs in options_sigs.items():
            if sigs:
                all_signals.setdefault(ticker, []).extend(sigs)
        opt_total = sum(len(v) for v in options_sigs.values())
        logger.info(f"  Options signals: {opt_total}")
    except Exception as e:
        logger.warning(f"[5/6] Options error: {e}")
        results["errors"].append(f"options: {e}")

    # ── Step 6: Score all tickers ─────────────────────────────────────────────
    logger.info("[6/6] Running confluence scoring...")
    scored = score_all_tickers(all_signals, regime_data, session=session)
    results["scores"] = scored

    # Print the report
    print("\n" + format_conviction_report(scored))

    # ── Step 6: Risk check and execute FIRE signals ───────────────────────────
    fires = [s for s in scored if s["action"] == "FIRE"]
    logger.info(f"\n{'─'*40}")
    logger.info(f"Processing {len(fires)} FIRE signal(s)...")

    portfolio_value = get_portfolio_value(client)

    for conviction in fires:
        ticker = conviction["ticker"]
        logger.info(f"\n  Evaluating {ticker} [{conviction['direction'].upper()}] "
                   f"score={conviction['score']:+.4f}")

        # Get real-time ATR and entry price
        try:
            from connectors.realtime_connector import get_intraday_bars, compute_intraday_technicals
            atr_df = get_intraday_bars(ticker, "15m")
            if not atr_df.empty:
                atr_df = compute_intraday_technicals(atr_df)
                atr = float(atr_df["atr"].dropna().iloc[-1]) if "atr" in atr_df else 0
                entry_price = float(atr_df["close"].iloc[-1])
            else:
                atr, entry_price = 0, _get_mock_price(ticker)
        except Exception:
            atr, entry_price = 0, _get_mock_price(ticker)

        trade_type = conviction.get("trade_type", "swing")

        risk = full_risk_check(
            ticker=ticker,
            direction=conviction["direction"],
            entry_price=entry_price,
            atr=atr,
            portfolio_value=portfolio_value,
            conviction_score=abs(conviction["score"]),
            session=session,
            trade_type=trade_type,
        )
        logger.info(f"  Risk check: {risk['summary']}")

        results["fired"].append({
            "ticker":    ticker,
            "score":     conviction["score"],
            "direction": conviction["direction"],
            "approved":  risk["approved"],
        })

        if risk["approved"]:
            if dry_run:
                logger.info(f"  DRY RUN — skipping execution. Set dry_run=False to trade.")
            else:
                trade_result = execute_trade(conviction, risk, client)
                if trade_result:
                    results["executed"].append(trade_result)

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed = (datetime.utcnow() - start_time).total_seconds()
    logger.info(f"\n{'='*60}")
    logger.info(f"SCAN COMPLETE in {elapsed:.1f}s")
    logger.info(f"  Tickers scanned:  {len(scored)}")
    logger.info(f"  Total signals:    {sum(len(v) for v in all_signals.values())}")
    logger.info(f"  FIRE signals:     {len(fires)}")
    logger.info(f"  Trades approved:  {sum(1 for f in results['fired'] if f['approved'])}")
    logger.info(f"  Trades executed:  {len(results['executed'])}")
    logger.info(f"  Errors:           {len(results['errors'])}")
    logger.info(f"  Regime:           {regime_data['regime'].upper()}")
    logger.info(f"{'='*60}\n")

    session.close()
    return results


def show_status():
    """Display current portfolio status."""
    client  = AlpacaClient()
    session = get_session()

    print("\n" + "=" * 55)
    print("  ATLAS PORTFOLIO STATUS")
    print("=" * 55)

    if client.headers.get("APCA-API-KEY-ID"):
        account = client.get_account()
        if account:
            pv = float(account.get("portfolio_value", 0))
            cash = float(account.get("cash", 0))
            print(f"  Portfolio value:  ${pv:>12,.2f}")
            print(f"  Cash:             ${cash:>12,.2f}")
            print(f"  Invested:         ${pv - cash:>12,.2f}")

        positions = client.get_positions()
        if positions:
            print(f"\n  Open Positions ({len(positions)}):")
            print(f"  {'Ticker':<8} {'Shares':>7} {'Entry':>9} {'Current':>9} {'P&L':>10} {'P&L%':>7}")
            print("  " + "-" * 55)
            for p in positions:
                pnl = float(p.get("unrealized_pl", 0))
                pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
                print(f"  {p['symbol']:<8} {float(p['qty']):>7.0f} "
                      f"${float(p['avg_entry_price']):>8.2f} "
                      f"${float(p['current_price']):>8.2f} "
                      f"${pnl:>9.2f} {pnl_pct:>6.1f}%")
        else:
            print("\n  No open positions")
    else:
        print("  No Alpaca keys configured")
        print("  Add ALPACA_API_KEY and ALPACA_SECRET_KEY to your .env")

    # DB stats
    from core.database import Signal, Trade
    total_signals = session.query(Signal).count()
    total_trades  = session.query(Trade).count()
    print(f"\n  Database:")
    print(f"    Signals recorded: {total_signals:,}")
    print(f"    Trades logged:    {total_trades:,}")
    print("=" * 55 + "\n")
    session.close()


def _generate_demo_signals(tickers: list, regime: str) -> dict:
    """
    Generate realistic demo signals for testing without live data feeds.
    In production, this is replaced by real connector output.
    """
    import random
    random.seed(42)  # Deterministic for testing

    demo = {}
    signal_templates = {
        "bullish": [
            ("rsi_oversold", 0.15), ("macd_bullish_cross", 0.12),
            ("bb_lower_touch", 0.10), ("volume_spike", 0.08),
            ("news_sentiment_bullish", 0.10), ("insider_buy", 0.18),
            ("congress_buy", 0.12), ("google_trends_spike", 0.08),
        ],
        "bearish": [
            ("rsi_overbought", -0.15), ("macd_bearish_cross", -0.12),
            ("bb_upper_touch", -0.10), ("news_sentiment_bearish", -0.10),
            ("insider_sell", -0.10), ("death_cross", -0.14),
        ],
    }

    high_conviction = ["NVDA", "META"]    # Will fire
    medium_signals  = ["AAPL", "MSFT"]   # Will watch
    mixed           = ["TSLA", "JPM"]    # Contradictory signals
    low_signal      = [t for t in tickers if t not in high_conviction + medium_signals + mixed]

    for ticker in tickers:
        signals = []
        if ticker in high_conviction:
            # 4-5 aligned bullish signals = FIRE
            for name, weight in random.sample(signal_templates["bullish"], 4):
                signals.append({"signal_type": name, "value": round(random.uniform(0.5, 1.0), 2),
                                "score": weight, "direction": "bullish",
                                "source": "demo", "timestamp": datetime.utcnow()})
        elif ticker in medium_signals:
            # 2-3 bullish signals = WATCH
            for name, weight in random.sample(signal_templates["bullish"], 2):
                signals.append({"signal_type": name, "value": round(random.uniform(0.3, 0.7), 2),
                                "score": weight, "direction": "bullish",
                                "source": "demo", "timestamp": datetime.utcnow()})
        elif ticker in mixed:
            # Mixed signals = low score
            for name, weight in random.sample(signal_templates["bullish"], 2):
                signals.append({"signal_type": name, "value": 0.5, "score": weight,
                                "direction": "bullish", "source": "demo",
                                "timestamp": datetime.utcnow()})
            for name, weight in random.sample(signal_templates["bearish"], 2):
                signals.append({"signal_type": name, "value": 0.5, "score": weight,
                                "direction": "bearish", "source": "demo",
                                "timestamp": datetime.utcnow()})
        elif random.random() > 0.6:
            # Occasional weak signal
            name, weight = random.choice(signal_templates["bullish"])
            signals.append({"signal_type": name, "value": 0.3, "score": weight * 0.5,
                            "direction": "bullish", "source": "demo",
                            "timestamp": datetime.utcnow()})

        if signals:
            demo[ticker] = signals

    return demo


def _get_mock_price(ticker: str) -> float:
    """Mock price lookup for testing."""
    prices = {
        "AAPL": 185.0, "MSFT": 420.0, "NVDA": 875.0, "GOOGL": 178.0,
        "META": 520.0, "AMZN": 195.0, "TSLA": 175.0, "JPM": 205.0,
        "BAC": 42.0,   "GS": 490.0,   "XOM": 120.0,  "CVX": 165.0,
        "JNJ": 148.0,  "UNH": 530.0,  "SPY": 535.0,  "QQQ": 462.0,
    }
    return prices.get(ticker, 100.0)


if __name__ == "__main__":
    # Initialize database
    init_db()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        run_full_scan(dry_run=True)

    elif cmd == "paper":
        print("Starting continuous paper trading mode...")
        print("Press Ctrl+C to stop\n")
        try:
            while True:
                run_full_scan(dry_run=False)
                logger.info("Next scan in 60 minutes...")
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("ATLAS stopped.")

    elif cmd == "status":
        show_status()

    elif cmd == "test":
        print("Running system integration test...\n")
        run_full_scan(dry_run=True)
        show_status()

    else:
        print("Usage: python atlas.py [scan|paper|status|test]")
