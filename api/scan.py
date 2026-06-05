import sys, os, json, time, math
from http.server import BaseHTTPRequestHandler
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _clean(obj):
    """Recursively replace NaN/Inf with None so json.dumps never chokes."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _run_scan():
    from config import WATCHLIST, INITIAL_CAPITAL
    from core.database import init_db, get_session, MarketRegime
    from connectors.macro_connector import get_fear_greed_index, get_yield_curve, classify_regime
    from connectors.price_connector import run_price_scan, get_vix_level
    from connectors.realtime_connector import run_realtime_scan, get_intraday_bars, compute_intraday_technicals
    from connectors.options_connector import run_options_scan
    from connectors.news_connector import run_news_scan
    from connectors.alt_connector import get_alt_signals
    from signals.scorer import score_all_tickers
    from risk.risk_engine import full_risk_check
    from execution.executor import AlpacaClient, execute_trade
    from learning.performance_tracker import check_and_close_trades

    t0 = time.time()
    init_db()
    session = get_session()
    client  = AlpacaClient()

    tickers = [t for t in WATCHLIST if not t.startswith("^")]
    errors  = []

    # ── Step 0: Learning loop — close any positions Alpaca already exited ──────
    try:
        closed = check_and_close_trades(session, client)
    except Exception as e:
        errors.append(f"learning: {e}")

    # ── Step 1: Macro regime ───────────────────────────────────────────────────
    try:
        import yfinance as yf
        vix        = get_vix_level()
        fear_greed = get_fear_greed_index()
        yield_data = get_yield_curve()

        spy_hist     = yf.Ticker("SPY").history(period="2y")
        spy_close    = float(spy_hist["Close"].iloc[-1])
        spy_200ma    = float(spy_hist["Close"].rolling(200).mean().dropna().iloc[-1])
        spy_vs_200ma = (spy_close - spy_200ma) / spy_200ma * 100

        regime_data = classify_regime(
            vix=vix,
            spy_vs_200ma=spy_vs_200ma,
            yield_spread=yield_data.get("spread", 0.3),
            fear_greed=fear_greed.get("score", 50),
        )

        db_regime = MarketRegime(
            date=datetime.utcnow(),
            regime=regime_data.get("regime", "neutral"),
            vix_level=vix,
            spy_vs_200ma=spy_vs_200ma,
            yield_spread=yield_data.get("spread"),
            fear_greed=fear_greed.get("score"),
            regime_score=regime_data.get("regime_score"),
        )
        session.add(db_regime)
        session.commit()
    except Exception as e:
        errors.append(f"macro: {e}")
        regime_data = {"regime": "neutral", "multiplier": 1.0}

    # ── Step 2: Daily price + technicals ──────────────────────────────────────
    all_signals: dict = {}
    try:
        daily_signals = run_price_scan(tickers)
        for ticker, sigs in daily_signals.items():
            all_signals.setdefault(ticker, []).extend(sigs)
    except Exception as e:
        errors.append(f"price: {e}")

    # ── Step 3: Intraday signals (15m + 1h) ───────────────────────────────────
    try:
        for ticker in tickers:
            rt_sigs = run_realtime_scan(ticker, timeframes=["15m", "1h"])
            if rt_sigs:
                all_signals.setdefault(ticker, []).extend(rt_sigs)
    except Exception as e:
        errors.append(f"realtime: {e}")

    # ── Step 4: News + alternative data ───────────────────────────────────────
    try:
        news_sigs = run_news_scan(tickers[:12])
        for ticker, sigs in news_sigs.items():
            all_signals.setdefault(ticker, []).extend(sigs)
    except Exception as e:
        errors.append(f"news: {e}")

    try:
        for ticker in tickers[:8]:
            alt_sigs = get_alt_signals(ticker)
            if alt_sigs:
                all_signals.setdefault(ticker, []).extend(alt_sigs)
    except Exception as e:
        errors.append(f"alt_data: {e}")

    # ── Step 5: Options flow ───────────────────────────────────────────────────
    try:
        options_sigs = run_options_scan(tickers)
        for ticker, sigs in options_sigs.items():
            if sigs:
                all_signals.setdefault(ticker, []).extend(sigs)
    except Exception as e:
        errors.append(f"options: {e}")

    # ── Step 6: Score ──────────────────────────────────────────────────────────
    scored = score_all_tickers(all_signals, regime_data, session=session)

    # ── Step 7: Risk check + execute FIRE signals ──────────────────────────────
    fires = [s for s in scored if s["action"] == "FIRE"]
    trades_executed = 0

    try:
        account = client.get_account()
        portfolio_value = float(account.get("portfolio_value", INITIAL_CAPITAL)) if account else INITIAL_CAPITAL
    except Exception:
        portfolio_value = INITIAL_CAPITAL

    fired_details = []
    for conviction in fires:
        ticker = conviction["ticker"]
        try:
            atr_df = get_intraday_bars(ticker, "15m")
            if not atr_df.empty:
                atr_df      = compute_intraday_technicals(atr_df)
                atr         = float(atr_df["atr"].dropna().iloc[-1]) if "atr" in atr_df.columns else 0
                entry_price = float(atr_df["close"].iloc[-1])
            else:
                atr, entry_price = 0, 0
        except Exception:
            atr, entry_price = 0, 0

        if entry_price <= 0:
            continue

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

        fired_details.append({
            "ticker":    ticker,
            "score":     conviction["score"],
            "direction": conviction["direction"],
            "approved":  risk["approved"],
            "summary":   risk.get("summary", ""),
        })

        if risk["approved"]:
            try:
                execute_trade(conviction, risk, client)
                trades_executed += 1
            except Exception as e:
                errors.append(f"execute {ticker}: {e}")

    session.close()

    watches = [s for s in scored if s["action"] == "WATCH"]
    return {
        "success": True,
        "summary": {
            "regime":           regime_data.get("regime", "neutral"),
            "regime_multiplier": regime_data.get("multiplier", 1.0),
            "tickers_scanned":  len(scored),
            "total_signals":    sum(len(v) for v in all_signals.values()),
            "fire_signals":     len(fires),
            "watch_signals":    len(watches),
            "trades_executed":  trades_executed,
            "duration_seconds": round(time.time() - t0, 1),
            "timestamp":        datetime.utcnow().isoformat(),
            "errors":           errors,
        },
        "fired": fired_details,
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            result = _run_scan()
            self._respond(200, _clean(result))
        except Exception as e:
            self._respond(500, {"success": False, "error": str(e)})

    def do_GET(self):
        self.do_POST()

    def do_OPTIONS(self):
        self._cors()
        self.end_headers()

    def _respond(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *args):
        pass
