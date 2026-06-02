import sys, os, json, time
from http.server import BaseHTTPRequestHandler
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _run_scan():
    from config import WATCHLIST
    from core.database import init_db, get_session, MarketRegime, ConvictionScore
    from connectors.macro_connector import get_fear_greed_index, get_yield_curve, classify_regime
    from connectors.price_connector import get_price_signals
    from connectors.news_connector import run_news_scan
    from connectors.alt_connector import get_alt_signals
    from signals.scorer import score_all_tickers
    from risk.risk_engine import full_risk_check
    from execution.executor import AlpacaClient, execute_trade

    t0 = time.time()
    engine = init_db()
    session = get_session(engine)

    # 1. Macro regime
    fear_greed = get_fear_greed_index()
    yield_data = get_yield_curve()
    regime_info = classify_regime(fear_greed=fear_greed, yield_data=yield_data)
    regime = regime_info.get("regime", "neutral")

    db_regime = MarketRegime(
        date=datetime.utcnow(),
        regime=regime,
        vix_level=regime_info.get("vix"),
        spy_vs_200ma=regime_info.get("spy_vs_200ma"),
        yield_spread=regime_info.get("yield_spread"),
        fear_greed=fear_greed.get("value") if isinstance(fear_greed, dict) else fear_greed,
        regime_score=regime_info.get("regime_score"),
    )
    session.add(db_regime)
    session.commit()

    tickers = [t for t in WATCHLIST if not t.startswith("^")]

    # 2–4. Collect all signals
    all_signals = []
    for ticker in tickers:
        try:
            price_sigs = get_price_signals(ticker)
            all_signals.extend(price_sigs)
        except Exception:
            pass

    try:
        news_sigs = run_news_scan(tickers)
        all_signals.extend(news_sigs)
    except Exception:
        pass

    try:
        alt_sigs = get_alt_signals(tickers)
        all_signals.extend(alt_sigs)
    except Exception:
        pass

    # 5. Score
    conviction_scores = score_all_tickers(all_signals, regime, session)

    # 6. Execute approved trades
    fire_signals = [s for s in conviction_scores if s.get("action") == "FIRE"]
    trades_executed = 0
    try:
        alpaca = AlpacaClient()
        for sig in fire_signals:
            risk = full_risk_check(sig, session, alpaca)
            if risk.get("approved"):
                execute_trade(sig, risk, alpaca, session, dry_run=False)
                trades_executed += 1
    except Exception:
        pass

    session.close()

    watch_signals = [s for s in conviction_scores if s.get("action") == "WATCH"]
    return {
        "success": True,
        "summary": {
            "regime": regime,
            "tickers_scanned": len(tickers),
            "total_signals": len(all_signals),
            "fire_signals": len(fire_signals),
            "watch_signals": len(watch_signals),
            "trades_executed": trades_executed,
            "duration_seconds": round(time.time() - t0, 1),
            "timestamp": datetime.utcnow().isoformat(),
        },
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            result = _run_scan()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"success": False, "error": str(e)})

    def do_GET(self):
        # Allow cron jobs (which use GET) to trigger scans
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
