"""
ATLAS Learning Loop
====================
Tracks each signal type's historical win rate and nudges weights
automatically after every closed trade.

Flow:
  1. check_and_close_trades() runs at the start of every scan
  2. Compares DB open positions against live Alpaca positions
  3. Any position that closed in Alpaca → mark closed in DB, compute P&L
  4. update_signal_performance() credits/debits each contributing signal
  5. _adjust_weight() nudges the signal's stored weight ±5% per result
  6. get_live_weights() returns the updated weights to the scorer

Weight guardrails:
  - Adjustments are ±5% of the current weight per closed trade
  - Total deviation from original config weight capped at ±40%
  - Minimum 5 closed trades before a signal's weight is touched
  - Neutral zone (45–60% win rate): no adjustment, wait for clearer signal
"""

import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SIGNAL_WEIGHTS

logger = logging.getLogger("atlas.learning")

MIN_SAMPLE_SIZE     = 5      # trades before adjusting weight
ADJUSTMENT_STEP     = 0.05   # ±5% per trade result
MAX_WEIGHT_DEVIATION = 0.40  # cap at ±40% from original


def update_signal_performance(session, trade) -> None:
    """
    Update win/loss counts and adjust weights for every signal
    that contributed to this trade's entry.
    """
    from core.database import SignalPerformance

    if not trade.signals_at_entry:
        return

    is_win = (trade.pnl or 0) > 0
    pnl_pct = trade.pnl_pct or 0.0

    for entry in trade.signals_at_entry:
        # signals_at_entry is stored as a list of dicts by scorer.py
        signal_type = entry.get("type") or entry.get("signal_type")
        if not signal_type:
            continue

        perf = session.query(SignalPerformance).filter_by(signal_type=signal_type).first()
        if not perf:
            perf = SignalPerformance(
                signal_type=signal_type,
                total_fires=0, win_count=0, loss_count=0,
                win_rate=0.5, avg_return=0.0,
                current_weight=SIGNAL_WEIGHTS.get(signal_type, 0.05),
                weight_adjusted=False,
            )
            session.add(perf)

        perf.total_fires = (perf.total_fires or 0) + 1
        if is_win:
            perf.win_count = (perf.win_count or 0) + 1
        else:
            perf.loss_count = (perf.loss_count or 0) + 1

        total = (perf.win_count or 0) + (perf.loss_count or 0)
        perf.win_rate = round(perf.win_count / total, 4) if total > 0 else 0.5

        # Exponential moving average of returns
        perf.avg_return = round(
            (perf.avg_return or 0.0) * 0.8 + pnl_pct * 0.2, 4
        )

        if total >= MIN_SAMPLE_SIZE:
            _adjust_weight(perf, signal_type)

        perf.last_updated = datetime.utcnow()

    session.commit()
    logger.info(
        f"[Learning] {'WIN' if is_win else 'LOSS'} {pnl_pct:+.2f}% — "
        f"updated {len(trade.signals_at_entry)} signal(s)"
    )


def _adjust_weight(perf, signal_type: str) -> None:
    """Nudge a signal's weight based on its win rate."""
    original = SIGNAL_WEIGHTS.get(signal_type, 0.05)
    if original == 0:
        return

    win_rate = perf.win_rate or 0.5
    current  = perf.current_weight or original

    if win_rate > 0.60:
        new_weight = current * (1 + ADJUSTMENT_STEP)
    elif win_rate < 0.45:
        new_weight = current * (1 - ADJUSTMENT_STEP)
    else:
        return  # neutral zone — no change

    sign  = 1 if original >= 0 else -1
    abs_o = abs(original)
    new_weight = sign * max(abs_o * (1 - MAX_WEIGHT_DEVIATION),
                            min(abs_o * (1 + MAX_WEIGHT_DEVIATION),
                                abs(new_weight)))

    old = perf.current_weight
    perf.current_weight  = round(new_weight, 5)
    perf.weight_adjusted = True
    logger.info(
        f"[Learning] {signal_type}: {old:.4f} → {new_weight:.4f} "
        f"(win_rate={win_rate:.0%}, n={perf.total_fires})"
    )


def get_live_weights(session) -> dict:
    """
    Return signal weights merged with learning-adjusted values.
    Falls back to config defaults for any signal not yet tracked.
    """
    from core.database import SignalPerformance

    weights = dict(SIGNAL_WEIGHTS)
    adjusted = (
        session.query(SignalPerformance)
        .filter(SignalPerformance.weight_adjusted == True)  # noqa: E712
        .all()
    )
    count = 0
    for p in adjusted:
        if (p.total_fires or 0) >= MIN_SAMPLE_SIZE and p.current_weight is not None:
            weights[p.signal_type] = p.current_weight
            count += 1

    if count:
        logger.info(f"[Learning] Applying {count} adjusted signal weight(s)")
    return weights


def check_and_close_trades(session, alpaca_client=None) -> list:
    """
    Detect positions that Alpaca closed (stop hit or take profit).
    Marks them closed in DB and triggers performance updates.
    Returns list of tickers that were closed.
    """
    from core.database import Trade, Position

    closed_tickers = []
    try:
        db_positions = session.query(Position).all()
        if not db_positions:
            return []

        alpaca_tickers: set = set()
        if alpaca_client:
            try:
                for p in alpaca_client.get_positions():
                    alpaca_tickers.add(p.get("symbol") or p.symbol)
            except Exception:
                pass

        for pos in db_positions:
            if pos.ticker in alpaca_tickers:
                continue  # still open

            # Position gone from Alpaca → it was closed
            trade = (
                session.query(Trade)
                .filter(Trade.ticker == pos.ticker, Trade.exit_time == None)
                .order_by(Trade.entry_time.desc())
                .first()
            )
            if trade:
                exit_px = pos.current_price or pos.entry_price
                trade.exit_time  = datetime.utcnow()
                trade.exit_price = exit_px
                direction_mult   = 1 if (trade.direction or "long") == "long" else -1
                trade.pnl        = round((exit_px - trade.entry_price) * (trade.shares or 1) * direction_mult, 2)
                trade.pnl_pct    = round((exit_px - trade.entry_price) / trade.entry_price * 100 * direction_mult, 4)
                trade.exit_reason = "closed_by_alpaca"
                update_signal_performance(session, trade)
                closed_tickers.append(pos.ticker)

            session.delete(pos)

        if closed_tickers:
            session.commit()
            logger.info(f"[Learning] Closed {len(closed_tickers)} position(s): {closed_tickers}")

    except Exception as e:
        logger.error(f"[Learning] check_and_close_trades error: {e}")

    return closed_tickers


def get_performance_summary(session) -> dict:
    """Summary of learning loop state for dashboard / logging."""
    from core.database import SignalPerformance

    all_perfs = session.query(SignalPerformance).order_by(
        SignalPerformance.win_rate.desc()
    ).all()
    mature = [p for p in all_perfs if (p.total_fires or 0) >= MIN_SAMPLE_SIZE]

    return {
        "total_tracked":   len(all_perfs),
        "adjusted_count":  sum(1 for p in all_perfs if p.weight_adjusted),
        "mature_signals":  len(mature),
        "top_performers":  [
            {"signal": p.signal_type, "win_rate": p.win_rate, "fires": p.total_fires}
            for p in mature[:5]
        ],
        "bottom_performers": [
            {"signal": p.signal_type, "win_rate": p.win_rate, "fires": p.total_fires}
            for p in mature[-5:]
        ],
    }
