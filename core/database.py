"""
ATLAS Database Schema
=====================
Single SQLite database (upgrades to PostgreSQL at scale).
All signals, trades, portfolio state, and learning data live here.
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Boolean, Text, JSON, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import DB_URL, DB_PATH

Base = declarative_base()


class Signal(Base):
    """Every signal detected — fired or not."""
    __tablename__ = "signals"

    id            = Column(Integer, primary_key=True)
    ticker        = Column(String(20), nullable=False, index=True)
    signal_type   = Column(String(60), nullable=False)   # e.g. "rsi_oversold"
    value         = Column(Float)                         # raw value (RSI=28.3)
    score         = Column(Float)                         # weighted score contribution
    direction     = Column(String(10))                    # "bullish" / "bearish"
    source        = Column(String(40))                    # "finnhub", "reddit", "edgar"
    raw_data      = Column(JSON)                          # full payload for debugging
    timestamp     = Column(DateTime, default=datetime.utcnow, index=True)
    is_fired      = Column(Boolean, default=False)        # did this trigger a trade?


class ConvictionScore(Base):
    """Aggregated conviction score per ticker per scan."""
    __tablename__ = "conviction_scores"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(20), nullable=False, index=True)
    score           = Column(Float, nullable=False)
    direction       = Column(String(10))                  # "long" / "short"
    regime          = Column(String(20))
    signals_fired   = Column(JSON)                        # list of contributing signals
    above_threshold = Column(Boolean, default=False)
    timestamp       = Column(DateTime, default=datetime.utcnow, index=True)


class Trade(Base):
    """Every paper (and eventually live) trade."""
    __tablename__ = "trades"

    id                = Column(Integer, primary_key=True)
    ticker            = Column(String(20), nullable=False, index=True)
    direction         = Column(String(10))                # "long" / "short"
    entry_price       = Column(Float)
    exit_price        = Column(Float)
    shares            = Column(Float)
    position_value    = Column(Float)
    stop_loss_price   = Column(Float)
    take_profit_price = Column(Float)
    conviction_score  = Column(Float)
    regime_at_entry   = Column(String(20))
    signals_at_entry  = Column(JSON)
    entry_time        = Column(DateTime, index=True)
    exit_time         = Column(DateTime)
    exit_reason       = Column(String(40))                # "stop_loss", "take_profit", "signal_exit", "manual"
    pnl               = Column(Float)
    pnl_pct           = Column(Float)
    is_paper          = Column(Boolean, default=True)
    alpaca_order_id   = Column(String(60))
    notes             = Column(Text)


class Position(Base):
    """Currently open positions."""
    __tablename__ = "positions"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(20), nullable=False, unique=True, index=True)
    direction       = Column(String(10))
    shares          = Column(Float)
    entry_price     = Column(Float)
    current_price   = Column(Float)
    stop_loss_price = Column(Float)
    high_water_mark = Column(Float)                       # for trailing stop
    unrealized_pnl  = Column(Float)
    unrealized_pct  = Column(Float)
    entry_time      = Column(DateTime)
    last_updated    = Column(DateTime, default=datetime.utcnow)


class MarketRegime(Base):
    """Daily market regime snapshot."""
    __tablename__ = "market_regime"

    id              = Column(Integer, primary_key=True)
    date            = Column(DateTime, index=True)
    regime          = Column(String(20))                  # bull/bear/neutral/high_vol/crash
    vix_level       = Column(Float)
    spy_vs_200ma    = Column(Float)                       # % above/below 200MA
    yield_spread    = Column(Float)                       # 10Y - 2Y
    fear_greed      = Column(Integer)
    regime_score    = Column(Float)                       # 0-1 confidence


class RawMarketData(Base):
    """OHLCV snapshots for key tickers."""
    __tablename__ = "raw_market_data"

    id         = Column(Integer, primary_key=True)
    ticker     = Column(String(20), nullable=False, index=True)
    open       = Column(Float)
    high       = Column(Float)
    low        = Column(Float)
    close      = Column(Float)
    volume     = Column(Float)
    date       = Column(DateTime, index=True)

    __table_args__ = (Index("ix_ticker_date", "ticker", "date"),)


class NewsItem(Base):
    """News articles with sentiment scores."""
    __tablename__ = "news"

    id             = Column(Integer, primary_key=True)
    ticker         = Column(String(20), index=True)       # nullable = market-wide news
    headline       = Column(Text)
    source         = Column(String(60))
    url            = Column(Text)
    sentiment      = Column(Float)                        # -1.0 to +1.0
    sentiment_label = Column(String(20))                  # positive/negative/neutral
    published_at   = Column(DateTime, index=True)
    fetched_at     = Column(DateTime, default=datetime.utcnow)


class SocialSignal(Base):
    """Reddit and StockTwits social data."""
    __tablename__ = "social_signals"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(20), nullable=False, index=True)
    platform        = Column(String(20))                  # "reddit" / "stocktwits"
    mention_count   = Column(Integer)
    sentiment_score = Column(Float)
    bullish_pct     = Column(Float)
    bearish_pct     = Column(Float)
    volume_vs_avg   = Column(Float)                       # current / 7-day avg
    timestamp       = Column(DateTime, default=datetime.utcnow, index=True)


class InsiderTrade(Base):
    """SEC Form 4 insider transactions."""
    __tablename__ = "insider_trades"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(20), nullable=False, index=True)
    insider_name    = Column(String(100))
    insider_title   = Column(String(100))
    transaction_type = Column(String(20))                 # "buy" / "sell"
    shares          = Column(Float)
    price           = Column(Float)
    value           = Column(Float)
    filed_date      = Column(DateTime, index=True)
    transaction_date = Column(DateTime)


class CongressTrade(Base):
    """Congressional stock trades (STOCK Act disclosures)."""
    __tablename__ = "congress_trades"

    id               = Column(Integer, primary_key=True)
    ticker           = Column(String(20), nullable=False, index=True)
    politician_name  = Column(String(100))
    party            = Column(String(10))
    chamber          = Column(String(20))
    transaction_type = Column(String(20))
    amount_range     = Column(String(40))                 # e.g. "$15,001 - $50,000"
    transaction_date = Column(DateTime, index=True)
    disclosure_date  = Column(DateTime)
    days_to_disclose = Column(Integer)


class PortfolioSnapshot(Base):
    """Daily portfolio state for performance tracking."""
    __tablename__ = "portfolio_snapshots"

    id               = Column(Integer, primary_key=True)
    date             = Column(DateTime, index=True)
    total_value      = Column(Float)
    cash             = Column(Float)
    invested         = Column(Float)
    daily_pnl        = Column(Float)
    daily_pnl_pct    = Column(Float)
    total_pnl        = Column(Float)
    total_pnl_pct    = Column(Float)
    sharpe_ratio     = Column(Float)
    sortino_ratio    = Column(Float)
    max_drawdown     = Column(Float)
    open_positions   = Column(Integer)
    spy_daily_return = Column(Float)                      # benchmark comparison


class SignalPerformance(Base):
    """Learning loop: how each signal type has performed over time."""
    __tablename__ = "signal_performance"

    id              = Column(Integer, primary_key=True)
    signal_type     = Column(String(60), unique=True)
    total_fires     = Column(Integer, default=0)
    win_count       = Column(Integer, default=0)
    loss_count      = Column(Integer, default=0)
    win_rate        = Column(Float)
    avg_return      = Column(Float)
    current_weight  = Column(Float)
    weight_adjusted = Column(Boolean, default=False)
    last_updated    = Column(DateTime, default=datetime.utcnow)


def _make_engine(url=None):
    from sqlalchemy.pool import NullPool
    target = url or DB_URL
    if target.startswith("sqlite"):
        return create_engine(target, echo=False)
    return create_engine(target, echo=False, poolclass=NullPool)


def init_db():
    """Initialize the database and create all tables."""
    if DB_URL.startswith("sqlite"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = _make_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session(engine=None):
    """Get a database session."""
    if engine is None:
        engine = _make_engine()
    Session = sessionmaker(bind=engine)
    return Session()


if __name__ == "__main__":
    engine = init_db()
    print("[DB] All tables created successfully.")
    print("[DB] Tables:", list(Base.metadata.tables.keys()))
