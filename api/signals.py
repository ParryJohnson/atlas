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
    from core.database import get_session, ConvictionScore

    session = get_session()
    try:
        rows = (
            session.query(ConvictionScore)
            .order_by(ConvictionScore.timestamp.desc())
            .limit(60)
            .all()
        )
        signals = [
            {
                "id": r.id,
                "ticker": r.ticker,
                "score": round(r.score, 4),
                "direction": r.direction,
                "regime": r.regime,
                "above_threshold": r.above_threshold,
                "signals_fired": r.signals_fired or [],
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in rows
        ]
    finally:
        session.close()

    return {"signals": signals}


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
