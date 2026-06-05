import sys, os, json, math
from http.server import BaseHTTPRequestHandler
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def _clean(obj):
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj

def _get_data():
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
    from core.database import get_session, ConvictionScore, Trade, Signal

    account = {}
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        acct = client.get_account()
        account = {
            "portfolio_value": float(acct.portfolio_value or 0),
            "cash": float(acct.cash or 0),
            "buying_power": float(acct.buying_power or 0),
            "equity": float(acct.equity or 0),
            "daytrade_count": int(acct.daytrade_count or 0),
        }
    except Exception as e:
        account = {"error": str(e)}

    session = get_session()
    try:
        total_signals = session.query(Signal).count()
        total_trades = session.query(Trade).count()
        last_score = (
            session.query(ConvictionScore)
            .order_by(ConvictionScore.timestamp.desc())
            .first()
        )
        last_scan = last_score.timestamp.isoformat() if last_score else None
        open_positions = (
            session.query(Trade)
            .filter(Trade.exit_time == None)
            .count()
        )
    finally:
        session.close()

    return {
        "account": account,
        "stats": {
            "open_positions": open_positions,
            "total_trades": total_trades,
            "total_signals": total_signals,
            "last_scan": last_scan,
        },
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            data = _get_data()
            self._respond(200, data)
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_OPTIONS(self):
        self._cors()
        self.end_headers()

    def _respond(self, code, data):
        body = json.dumps(_clean(data), default=str).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *args):
        pass
