import sys, os, json, math
from http.server import BaseHTTPRequestHandler

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
    from core.database import get_session, Trade

    session = get_session()
    try:
        rows = (
            session.query(Trade)
            .order_by(Trade.entry_time.desc())
            .limit(100)
            .all()
        )
        trades = [
            {
                "id": r.id,
                "ticker": r.ticker,
                "direction": r.direction,
                "entry_price": r.entry_price,
                "exit_price": r.exit_price,
                "shares": r.shares,
                "position_value": r.position_value,
                "stop_loss_price": r.stop_loss_price,
                "take_profit_price": r.take_profit_price,
                "conviction_score": r.conviction_score,
                "regime_at_entry": r.regime_at_entry,
                "entry_time": (r.entry_time.isoformat() + "Z") if r.entry_time else None,
                "exit_time": (r.exit_time.isoformat() + "Z") if r.exit_time else None,
                "exit_reason": r.exit_reason,
                "pnl": r.pnl,
                "pnl_pct": r.pnl_pct,
                "is_paper": r.is_paper,
                "signals_at_entry": r.signals_at_entry or [],
            }
            for r in rows
        ]
    finally:
        session.close()

    win_trades = [t for t in trades if t["pnl"] is not None and t["pnl"] > 0]
    closed_trades = [t for t in trades if t["pnl"] is not None]
    win_rate = (len(win_trades) / len(closed_trades) * 100) if closed_trades else None
    total_pnl = sum(t["pnl"] for t in closed_trades) if closed_trades else 0

    return {
        "trades": trades,
        "summary": {
            "total": len(trades),
            "closed": len(closed_trades),
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "total_pnl": round(total_pnl, 2),
        },
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self._respond(200, _get_data())
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
