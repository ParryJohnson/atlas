"""
ATLAS Risk Management Engine
==============================
Every trade MUST pass all risk checks before execution.
This is what keeps you alive when the market turns.

Key modules:
1. Position sizer — ATR-based Kelly variant
2. Portfolio risk checker — concentration, correlation
3. Drawdown circuit breaker — kills trading if monthly drawdown hits limit
4. Stop-loss calculator — dynamic ATR-based stops
5. Earnings blackout enforcer — no new positions near earnings
"""

from datetime import datetime, timedelta
from typing import Optional
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    RISK_PER_TRADE_PCT, MAX_POSITION_SIZE_PCT, MAX_PORTFOLIO_POSITIONS,
    MAX_SECTOR_EXPOSURE_PCT, MAX_MONTHLY_DRAWDOWN,
    TRAILING_STOP_TRIGGER, TRAILING_STOP_DISTANCE
)
from core.database import get_session, Position, Trade, PortfolioSnapshot

logger = logging.getLogger("atlas.risk")

# Sector mapping (simplified — expand as universe grows)
SECTOR_MAP = {
    "AAPL": "tech",  "MSFT": "tech",  "NVDA": "tech",
    "GOOGL": "tech", "META": "tech",  "AMZN": "tech",
    "TSLA": "auto",  "JPM": "finance","BAC": "finance",
    "GS": "finance", "XOM": "energy", "CVX": "energy",
    "JNJ": "health", "UNH": "health", "SPY": "etf",
    "QQQ": "etf",    "IWM": "etf",    "TLT": "bonds",
    "GLD": "commodities",
}


class RiskResult:
    """Result of a risk check."""
    def __init__(self, approved: bool, reason: str, details: dict = None):
        self.approved = approved
        self.reason   = reason
        self.details  = details or {}

    def __bool__(self):
        return self.approved

    def __repr__(self):
        status = "✅ APPROVED" if self.approved else "❌ REJECTED"
        return f"RiskResult({status}: {self.reason})"


def calculate_position_size(
    portfolio_value: float,
    entry_price: float,
    stop_loss_price: float,
    conviction_score: float = 0.55,
    atr: float = None,
) -> dict:
    """
    Calculate position size using ATR-based risk management.

    Logic:
    - Never risk more than RISK_PER_TRADE_PCT of portfolio on one trade
    - Risk = entry_price - stop_loss_price (per share)
    - Shares = (portfolio * risk_pct) / risk_per_share
    - Scale position size by conviction score (higher confidence = larger)
    - Hard cap at MAX_POSITION_SIZE_PCT of portfolio

    Returns:
        dict with shares, position_value, risk_amount, risk_pct
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        return {"approved": False, "reason": "Invalid prices"}

    risk_per_share = abs(entry_price - stop_loss_price)
    if risk_per_share < 0.01:
        return {"approved": False, "reason": "Stop too tight"}

    # Scale risk by conviction (0.55 threshold = 80% of max risk, 1.0 = 100%)
    conviction_factor = min(conviction_score / SIGNAL_FIRE_THRESHOLD, 1.0) if conviction_score else 0.8
    adjusted_risk_pct = RISK_PER_TRADE_PCT * (0.7 + 0.3 * conviction_factor)

    dollar_risk = portfolio_value * adjusted_risk_pct
    raw_shares  = dollar_risk / risk_per_share

    # Apply max position cap
    max_shares_by_pct = (portfolio_value * MAX_POSITION_SIZE_PCT) / entry_price
    shares = min(raw_shares, max_shares_by_pct)
    shares = max(1, int(shares))  # At least 1 share, round down

    position_value = shares * entry_price
    actual_risk    = shares * risk_per_share
    actual_risk_pct = actual_risk / portfolio_value

    return {
        "approved":        True,
        "shares":          shares,
        "position_value":  round(position_value, 2),
        "position_pct":    round(position_value / portfolio_value, 4),
        "risk_amount":     round(actual_risk, 2),
        "risk_pct":        round(actual_risk_pct, 4),
        "risk_per_share":  round(risk_per_share, 2),
        "conviction_factor": round(conviction_factor, 3),
    }


def calculate_stop_loss(entry_price: float, atr: float,
                         direction: str = "long",
                         atr_multiplier: float = 2.0) -> dict:
    """
    Calculate ATR-based stop loss and initial take profit.

    Stop = entry - (ATR * multiplier) for longs
    Take profit = entry + (ATR * multiplier * 2) for 2:1 R/R
    """
    if atr is None or atr <= 0:
        atr = entry_price * 0.02   # Default: 2% of price

    stop_distance = atr * atr_multiplier

    if direction == "long":
        stop_loss    = entry_price - stop_distance
        take_profit  = entry_price + (stop_distance * 2.5)  # 2.5:1 R/R
    else:
        stop_loss    = entry_price + stop_distance
        take_profit  = entry_price - (stop_distance * 2.5)

    return {
        "entry":        round(entry_price, 2),
        "stop_loss":    round(stop_loss, 2),
        "take_profit":  round(take_profit, 2),
        "stop_distance": round(stop_distance, 2),
        "atr_used":     round(atr, 2),
        "risk_reward":  2.5,
    }


def update_trailing_stop(position: dict, current_price: float) -> dict:
    """
    Update trailing stop if price has moved in our favor.
    Activates after TRAILING_STOP_TRIGGER % gain.
    Trails at TRAILING_STOP_DISTANCE below high water mark.
    """
    entry_price     = position["entry_price"]
    high_water_mark = position.get("high_water_mark", entry_price)
    current_stop    = position["stop_loss_price"]
    direction       = position.get("direction", "long")

    if direction == "long":
        gain_pct = (current_price - entry_price) / entry_price
        if gain_pct >= TRAILING_STOP_TRIGGER:
            # Update high water mark
            new_hwm = max(high_water_mark, current_price)
            new_trail_stop = new_hwm * (1 - TRAILING_STOP_DISTANCE)
            new_stop = max(current_stop, new_trail_stop)

            return {
                "stop_loss_price": round(new_stop, 2),
                "high_water_mark": round(new_hwm, 2),
                "trailing_active": True,
                "gain_pct": round(gain_pct, 4),
            }

    return {
        "stop_loss_price": current_stop,
        "high_water_mark": high_water_mark,
        "trailing_active": False,
    }


def check_portfolio_risk(ticker: str, position_value: float,
                          portfolio_value: float, session) -> RiskResult:
    """
    Check portfolio-level risk: max positions, sector concentration.
    """
    open_positions = session.query(Position).all()

    # Max positions check
    if len(open_positions) >= MAX_PORTFOLIO_POSITIONS:
        return RiskResult(False, f"Max positions reached ({MAX_PORTFOLIO_POSITIONS})",
                         {"open_count": len(open_positions)})

    # Check if already in this ticker
    existing = session.query(Position).filter_by(ticker=ticker).first()
    if existing:
        return RiskResult(False, f"Already have open position in {ticker}")

    # Sector concentration check
    new_sector = SECTOR_MAP.get(ticker, "other")
    sector_value = sum(
        p.shares * (p.current_price or p.entry_price)
        for p in open_positions
        if SECTOR_MAP.get(p.ticker, "other") == new_sector
    )
    sector_total  = sector_value + position_value
    sector_pct    = sector_total / portfolio_value

    if sector_pct > MAX_SECTOR_EXPOSURE_PCT:
        return RiskResult(False,
            f"Sector concentration too high: {new_sector} would be {sector_pct:.1%} "
            f"(max {MAX_SECTOR_EXPOSURE_PCT:.0%})",
            {"sector": new_sector, "sector_pct": sector_pct})

    return RiskResult(True, "Portfolio risk check passed",
                     {"open_positions": len(open_positions),
                      "sector": new_sector,
                      "sector_pct": round(sector_pct, 4)})


def check_drawdown_circuit_breaker(portfolio_value: float,
                                    session) -> RiskResult:
    """
    Check if monthly drawdown circuit breaker is triggered.
    If down more than MAX_MONTHLY_DRAWDOWN this month, stop all trading.
    """
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)

    # Get portfolio value at start of month
    first_snapshot = session.query(PortfolioSnapshot)\
        .filter(PortfolioSnapshot.date >= month_start)\
        .order_by(PortfolioSnapshot.date.asc())\
        .first()

    if not first_snapshot:
        return RiskResult(True, "No monthly history yet — circuit breaker inactive")

    month_start_value = first_snapshot.total_value
    drawdown = (portfolio_value - month_start_value) / month_start_value

    if drawdown <= -MAX_MONTHLY_DRAWDOWN:
        return RiskResult(False,
            f"⚡ CIRCUIT BREAKER TRIGGERED: Monthly drawdown {drawdown:.1%} "
            f"exceeds limit of -{MAX_MONTHLY_DRAWDOWN:.0%}. "
            f"Trading HALTED. Manual review required.",
            {"drawdown_pct": drawdown,
             "month_start_value": month_start_value,
             "current_value": portfolio_value})

    return RiskResult(True,
        f"Drawdown within limits: {drawdown:+.1%} this month",
        {"drawdown_pct": drawdown, "limit": -MAX_MONTHLY_DRAWDOWN})


def check_earnings_blackout(ticker: str, earnings_dates: dict = None) -> RiskResult:
    """
    Block new positions within 2 days of earnings.
    earnings_dates: {ticker: datetime} — populated by Finnhub earnings calendar
    """
    if not earnings_dates or ticker not in earnings_dates:
        return RiskResult(True, "No earnings date found — proceed")

    earnings_date = earnings_dates.get(ticker)
    if not isinstance(earnings_date, datetime):
        return RiskResult(True, "Earnings date unavailable")

    days_to_earnings = (earnings_date - datetime.utcnow()).days

    if -1 <= days_to_earnings <= 2:
        return RiskResult(False,
            f"EARNINGS BLACKOUT: {ticker} reports in {days_to_earnings} day(s) "
            f"({earnings_date.strftime('%Y-%m-%d')}). "
            f"No new positions within 2 days of earnings.",
            {"days_to_earnings": days_to_earnings,
             "earnings_date": earnings_date.isoformat()})

    return RiskResult(True,
        f"Earnings {days_to_earnings} days away — safe to trade",
        {"days_to_earnings": days_to_earnings})


def full_risk_check(
    ticker: str,
    direction: str,
    entry_price: float,
    atr: float,
    portfolio_value: float,
    conviction_score: float,
    earnings_dates: dict = None,
    session = None,
) -> dict:
    """
    Run ALL risk checks. Returns full risk assessment with position sizing.
    This is the gate — all checks must pass before a trade fires.
    """
    if session is None:
        session = get_session()

    results = {}
    all_passed = True

    # 1. Circuit breaker
    cb = check_drawdown_circuit_breaker(portfolio_value, session)
    results["circuit_breaker"] = cb
    if not cb:
        all_passed = False

    # 2. Earnings blackout
    if earnings_dates:
        eb = check_earnings_blackout(ticker, earnings_dates)
        results["earnings_blackout"] = eb
        if not eb:
            all_passed = False

    # 3. Stop loss calculation
    stops = calculate_stop_loss(entry_price, atr, direction)
    results["stops"] = stops

    # 4. Position sizing
    sizing = calculate_position_size(
        portfolio_value, entry_price,
        stops["stop_loss"], conviction_score, atr
    )
    results["sizing"] = sizing
    if not sizing.get("approved"):
        all_passed = False
        results["sizing_check"] = RiskResult(False, sizing.get("reason", "Sizing failed"))

    # 5. Portfolio risk (only if sizing approved)
    if sizing.get("approved"):
        pr = check_portfolio_risk(ticker, sizing["position_value"], portfolio_value, session)
        results["portfolio_risk"] = pr
        if not pr:
            all_passed = False

    results["approved"]  = all_passed
    results["ticker"]    = ticker
    results["direction"] = direction
    results["summary"]   = (
        f"{'✅ TRADE APPROVED' if all_passed else '❌ TRADE BLOCKED'}: "
        f"{ticker} {direction.upper()} — "
        + (f"{sizing.get('shares',0)} shares @ ${entry_price:.2f}, "
           f"stop=${stops.get('stop_loss',0):.2f}, "
           f"target=${stops.get('take_profit',0):.2f}, "
           f"risk=${sizing.get('risk_amount',0):.2f} ({sizing.get('risk_pct',0):.1%})"
           if all_passed and sizing.get("approved")
           else "See rejection reasons above")
    )

    return results


# Import here to avoid circular
try:
    from config import SIGNAL_FIRE_THRESHOLD
except ImportError:
    SIGNAL_FIRE_THRESHOLD = 0.55


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Risk Engine Test ===\n")

    portfolio = 100_000

    # Test position sizing
    print("--- Position Sizing ---")
    scenarios = [
        {"entry": 450.00, "stop": 435.00, "conviction": 0.72, "label": "NVDA high conviction"},
        {"entry": 185.00, "stop": 179.00, "conviction": 0.57, "label": "AAPL threshold"},
        {"entry":  22.50, "stop":  21.00, "conviction": 0.90, "label": "Small cap, max conviction"},
        {"entry": 580.00, "stop": 560.00, "conviction": 0.55, "label": "SPY minimum threshold"},
    ]
    print(f"{'Scenario':<35} {'Shares':>7} {'Value':>10} {'Risk$':>8} {'Risk%':>7}")
    print("-" * 75)
    for s in scenarios:
        stops  = calculate_stop_loss(s["entry"], None, "long")
        sizing = calculate_position_size(portfolio, s["entry"], s["stop"], s["conviction"])
        if sizing["approved"]:
            print(f"{s['label']:<35} {sizing['shares']:>7} "
                  f"${sizing['position_value']:>9,.2f} "
                  f"${sizing['risk_amount']:>7,.2f} "
                  f"{sizing['risk_pct']:>6.1%}")

    # Test stop loss calc
    print("\n--- Stop Loss Calculations ---")
    for entry, atr, label in [(450, 8.5, "NVDA"), (185, 3.2, "AAPL"), (22.5, 0.8, "Small cap")]:
        sl = calculate_stop_loss(entry, atr, "long")
        print(f"  {label:10}: entry=${sl['entry']}, stop=${sl['stop_loss']}, "
              f"target=${sl['take_profit']}, distance=${sl['stop_distance']:.2f}")

    # Test trailing stop
    print("\n--- Trailing Stop ---")
    position = {"entry_price": 400.0, "stop_loss_price": 385.0,
                "high_water_mark": 400.0, "direction": "long"}
    for price in [400, 410, 420, 425, 418]:
        result = update_trailing_stop(position, price)
        position["stop_loss_price"]  = result["stop_loss_price"]
        position["high_water_mark"]  = result["high_water_mark"]
        print(f"  Price=${price}: stop=${result['stop_loss_price']:.2f}, "
              f"HWM=${result['high_water_mark']:.2f}, "
              f"trailing={'YES' if result['trailing_active'] else 'NO'}")

    # Regime check
    print("\n--- Drawdown Circuit Breaker ---")
    cb = check_drawdown_circuit_breaker(92_000, get_session())  # Down 8%
    print(f"  Portfolio $92k (started at $100k): {cb}")
    cb2 = check_drawdown_circuit_breaker(89_000, get_session())  # Down 11%
    print(f"  Portfolio $89k (started at $100k): {cb2}")
