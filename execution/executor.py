"""
ATLAS Execution: Alpaca Paper Trading
=======================================
Handles all trade execution via Alpaca's paper trading API.
Paper trading = real market data, fake money. Zero risk while validating.

Get free API keys at: https://alpaca.markets
Set ALPACA_PAPER=true in your .env (default)

Docs: https://docs.alpaca.markets/reference/
"""

import requests
from datetime import datetime
from typing import Optional
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from core.database import get_session, Trade, Position

logger = logging.getLogger("atlas.execution")

ALPACA_BASE = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"


class AlpacaClient:
    """Thin wrapper around Alpaca REST API."""

    def __init__(self):
        if not ALPACA_API_KEY:
            logger.warning("[Execution] ALPACA_API_KEY not set — running in dry-run mode")
        self.headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type":        "application/json",
        }
        self.mode = "PAPER" if ALPACA_PAPER else "LIVE"

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        try:
            resp = requests.get(
                f"{ALPACA_BASE}{endpoint}",
                headers=self.headers, params=params, timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"[Execution] GET {endpoint}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"[Execution] GET {endpoint} error: {e}")
        return None

    def _post(self, endpoint: str, body: dict) -> Optional[dict]:
        try:
            resp = requests.post(
                f"{ALPACA_BASE}{endpoint}",
                headers=self.headers, json=body, timeout=10
            )
            if resp.status_code in (200, 201):
                return resp.json()
            logger.error(f"[Execution] POST {endpoint}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"[Execution] POST {endpoint} error: {e}")
        return None

    def _delete(self, endpoint: str) -> bool:
        try:
            resp = requests.delete(
                f"{ALPACA_BASE}{endpoint}",
                headers=self.headers, timeout=10
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"[Execution] DELETE {endpoint} error: {e}")
            return False

    def get_account(self) -> Optional[dict]:
        """Get account info: cash, portfolio value, buying power."""
        return self._get("/v2/account")

    def get_positions(self) -> list:
        """Get all open positions."""
        return self._get("/v2/positions") or []

    def get_position(self, ticker: str) -> Optional[dict]:
        """Get a specific position."""
        return self._get(f"/v2/positions/{ticker}")

    def get_orders(self, status: str = "open") -> list:
        """Get orders by status: open, closed, all."""
        return self._get("/v2/orders", params={"status": status, "limit": 50}) or []

    def submit_market_order(self, ticker: str, shares: int,
                             side: str, time_in_force: str = "day") -> Optional[dict]:
        """
        Submit a market order.
        side: "buy" or "sell"
        """
        if not ALPACA_API_KEY:
            logger.info(f"[Execution] DRY RUN: {side.upper()} {shares} {ticker} @ market")
            return {"id": "dry-run", "status": "accepted", "symbol": ticker,
                    "qty": shares, "side": side, "type": "market"}

        body = {
            "symbol":        ticker,
            "qty":           str(shares),
            "side":          side,
            "type":          "market",
            "time_in_force": time_in_force,
        }
        order = self._post("/v2/orders", body)
        if order:
            logger.info(f"[Execution] {self.mode} ORDER: {side.upper()} {shares} {ticker} "
                       f"— order_id={order.get('id', 'N/A')}")
        return order

    def submit_bracket_order(self, ticker: str, shares: int,
                              side: str, stop_loss: float,
                              take_profit: float) -> Optional[dict]:
        """
        Bracket order: entry + stop loss + take profit in one order.
        This is the preferred order type for ATLAS — all exits are automatic.
        """
        if not ALPACA_API_KEY:
            logger.info(f"[Execution] DRY RUN: BRACKET {side.upper()} {shares} {ticker} "
                       f"SL=${stop_loss:.2f} TP=${take_profit:.2f}")
            return {"id": "dry-run", "status": "accepted", "type": "bracket"}

        body = {
            "symbol":        ticker,
            "qty":           str(shares),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",    # Good till cancelled
            "order_class":   "bracket",
            "stop_loss":     {"stop_price": str(round(stop_loss, 2))},
            "take_profit":   {"limit_price": str(round(take_profit, 2))},
        }
        order = self._post("/v2/orders", body)
        if order:
            logger.info(f"[Execution] {self.mode} BRACKET: {side.upper()} {shares} {ticker} "
                       f"SL=${stop_loss:.2f} TP=${take_profit:.2f} — id={order.get('id','N/A')}")
        return order

    def close_position(self, ticker: str) -> Optional[dict]:
        """Close an entire position at market."""
        if not ALPACA_API_KEY:
            logger.info(f"[Execution] DRY RUN: CLOSE {ticker}")
            return {"status": "accepted"}
        result = self._delete(f"/v2/positions/{ticker}")
        if result:
            logger.info(f"[Execution] {self.mode} CLOSED: {ticker}")
        return {"status": "closed" if result else "error"}

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        return self._delete("/v2/orders") or False

    def get_latest_quote(self, ticker: str) -> Optional[float]:
        """Get latest price for a ticker."""
        try:
            resp = requests.get(
                f"{ALPACA_DATA}/v2/stocks/{ticker}/quotes/latest",
                headers=self.headers, timeout=5
            )
            if resp.status_code == 200:
                quote = resp.json().get("quote", {})
                return (quote.get("ap", 0) + quote.get("bp", 0)) / 2   # mid price
        except Exception:
            pass
        return None


def execute_trade(conviction: dict, risk_result: dict,
                   client: AlpacaClient = None) -> Optional[dict]:
    """
    Execute a trade based on conviction and risk assessment.
    Logs everything to the database.
    """
    if not risk_result.get("approved"):
        logger.warning(f"[Execution] Trade blocked: {risk_result.get('summary')}")
        return None

    if client is None:
        client = AlpacaClient()

    ticker    = conviction["ticker"]
    direction = conviction["direction"]
    side      = "buy" if direction == "long" else "sell"
    sizing    = risk_result["sizing"]
    stops     = risk_result["stops"]
    shares    = sizing["shares"]

    # Submit bracket order (preferred: auto stop + target)
    order = client.submit_bracket_order(
        ticker=ticker,
        shares=shares,
        side=side,
        stop_loss=stops["stop_loss"],
        take_profit=stops["take_profit"],
    )

    if not order:
        logger.error(f"[Execution] Order submission failed for {ticker}")
        return None

    # Persist to database
    session = get_session()
    entry_price = stops["entry"]

    trade = Trade(
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        shares=shares,
        position_value=sizing["position_value"],
        stop_loss_price=stops["stop_loss"],
        take_profit_price=stops["take_profit"],
        conviction_score=conviction["score"],
        regime_at_entry=conviction.get("regime", "unknown"),
        signals_at_entry=conviction.get("contributing", []),
        entry_time=datetime.utcnow(),
        is_paper=ALPACA_PAPER,
        alpaca_order_id=order.get("id", ""),
    )
    session.add(trade)

    # Also add to positions
    position = Position(
        ticker=ticker,
        direction=direction,
        shares=shares,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss_price=stops["stop_loss"],
        high_water_mark=entry_price,
        unrealized_pnl=0.0,
        unrealized_pct=0.0,
        entry_time=datetime.utcnow(),
    )
    session.add(position)
    session.commit()
    session.close()

    logger.info(f"[Execution] ✅ TRADE EXECUTED: {side.upper()} {shares} {ticker} "
               f"@ ${entry_price:.2f} | SL=${stops['stop_loss']:.2f} | "
               f"TP=${stops['take_profit']:.2f} | "
               f"Risk=${sizing['risk_amount']:.2f} ({sizing['risk_pct']:.1%})")

    return {
        "ticker":       ticker,
        "side":         side,
        "shares":       shares,
        "entry_price":  entry_price,
        "stop_loss":    stops["stop_loss"],
        "take_profit":  stops["take_profit"],
        "order_id":     order.get("id"),
        "status":       order.get("status"),
        "paper":        ALPACA_PAPER,
    }


def sync_positions(client: AlpacaClient = None):
    """
    Sync Alpaca positions with our local database.
    Run this periodically to keep P&L current.
    """
    if client is None:
        client = AlpacaClient()

    alpaca_positions = client.get_positions()
    session = get_session()

    for ap in alpaca_positions:
        ticker = ap.get("symbol")
        if not ticker:
            continue

        pos = session.query(Position).filter_by(ticker=ticker).first()
        if pos:
            current_price   = float(ap.get("current_price", pos.entry_price))
            unrealized_pnl  = float(ap.get("unrealized_pl", 0))
            unrealized_pct  = float(ap.get("unrealized_plpc", 0))

            pos.current_price  = current_price
            pos.unrealized_pnl = unrealized_pnl
            pos.unrealized_pct = unrealized_pct
            pos.last_updated   = datetime.utcnow()

    session.commit()
    session.close()
    logger.info(f"[Execution] Synced {len(alpaca_positions)} positions")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Execution Layer Test ===\n")

    client = AlpacaClient()

    if ALPACA_API_KEY:
        # Live test with real Alpaca API
        account = client.get_account()
        if account:
            print(f"Account: ${float(account.get('portfolio_value', 0)):,.2f} portfolio")
            print(f"Cash: ${float(account.get('cash', 0)):,.2f}")
            print(f"Buying power: ${float(account.get('buying_power', 0)):,.2f}")
            print(f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}")
        positions = client.get_positions()
        print(f"Open positions: {len(positions)}")
    else:
        print("No Alpaca keys set — dry-run mode active")
        print("Get free paper trading keys at: https://alpaca.markets")

        # Simulate a trade execution
        mock_conviction = {
            "ticker": "NVDA", "direction": "long", "score": 0.72,
            "regime": "bull", "contributing": []
        }
        mock_risk = {
            "approved": True,
            "sizing": {"shares": 22, "position_value": 9900, "risk_amount": 330, "risk_pct": 0.0033},
            "stops": {"entry": 450.0, "stop_loss": 433.0, "take_profit": 492.5},
            "summary": "TRADE APPROVED"
        }
        result = execute_trade(mock_conviction, mock_risk, client)
        print(f"\nDry-run trade result:")
        if result:
            for k, v in result.items():
                print(f"  {k}: {v}")
