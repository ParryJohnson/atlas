"""
ATLAS Configuration
===================
All API keys are loaded from environment variables.
Copy .env.example to .env and fill in your keys.

FREE APIs used:
- yfinance         : no key needed
- Finnhub          : free at finnhub.io
- FRED             : free at fred.stlouisfed.org/docs/api/fred
- Reddit PRAW      : free at reddit.com/prefs/apps
- NewsAPI          : free at newsapi.org
- Alpaca           : free paper trading at alpaca.markets
- StockTwits       : no key needed for public feed
- Capitol Trades   : scraped (no key)
- SEC EDGAR        : no key needed
- CoinGecko        : no key needed for basic tier
- Google Trends    : no key needed (pytrends)
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── API KEYS (set these in your environment or .env file) ──────────────────────
FINNHUB_API_KEY       = os.getenv("FINNHUB_API_KEY", "")
FRED_API_KEY          = os.getenv("FRED_API_KEY", "")
NEWSAPI_KEY           = os.getenv("NEWSAPI_KEY", "")
REDDIT_CLIENT_ID      = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET  = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT     = os.getenv("REDDIT_USER_AGENT", "ATLAS/1.0")
ALPACA_API_KEY        = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY     = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER          = os.getenv("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL   = os.getenv("DISCORD_WEBHOOK_URL", "")

# ── DATABASE ───────────────────────────────────────────────────────────────────
DB_PATH = BASE_DIR / "data" / "atlas.db"
_default_db = f"sqlite:///{DB_PATH}"
DB_URL  = os.getenv("DATABASE_URL", _default_db)
# Neon/Heroku provide postgres:// but SQLAlchemy needs postgresql://
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# ── UNIVERSE — tickers ATLAS watches ──────────────────────────────────────────
# Start focused. Add more as the system proves itself.
WATCHLIST = [
    # Mega-cap tech (high liquidity, lots of signal data)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    # Financials
    "JPM", "BAC", "GS",
    # Energy
    "XOM", "CVX",
    # Healthcare
    "JNJ", "UNH",
    # ETFs for macro plays
    "SPY", "QQQ", "IWM", "TLT", "GLD",
    # Volatility
    "^VIX",
]

CRYPTO_WATCHLIST = ["bitcoin", "ethereum", "solana"]

# ── SIGNAL WEIGHTS (v1 — tuned by backtesting, updated by learning loop) ──────
SIGNAL_WEIGHTS = {
    # Technical
    "rsi_oversold":           0.15,   # RSI < 30
    "rsi_overbought":        -0.15,   # RSI > 70 (bearish signal)
    "macd_bullish_cross":     0.12,
    "macd_bearish_cross":    -0.12,
    "bb_lower_touch":         0.10,   # Price touched lower Bollinger Band
    "bb_upper_touch":        -0.10,
    "golden_cross":           0.14,   # 50MA crosses above 200MA
    "death_cross":           -0.14,
    "volume_spike":           0.08,   # Volume > 2x 20-day avg
    "vwap_reclaim":           0.09,   # Price reclaims VWAP intraday

    # Sentiment
    "news_sentiment_bullish": 0.10,   # Finnhub/FinBERT score > 0.5
    "news_sentiment_bearish": -0.10,
    "reddit_mention_spike":   0.08,   # Mentions up >200% vs 7-day avg
    "stocktwits_bullish":     0.07,
    "fear_greed_extreme_fear": 0.12,  # Score < 20 = contrarian buy
    "fear_greed_extreme_greed": -0.12,

    # Alternative / edge data
    "insider_buy":            0.18,   # Form 4 insider purchase
    "insider_sell":          -0.10,
    "congress_buy":           0.12,
    "earnings_beat":          0.15,   # Beat by > 5%
    "earnings_miss":         -0.15,
    "google_trends_spike":    0.08,   # Search interest +100% week-over-week

    # Macro
    "yield_curve_normal":     0.05,
    "yield_curve_inverted":  -0.08,
    "vix_spike":              0.10,   # VIX > 25 = contrarian long opportunity
    "vix_low":               -0.05,   # VIX < 12 = complacency, reduce longs
}

# ── REGIME MULTIPLIERS ─────────────────────────────────────────────────────────
# Scales all long signal weights based on detected market regime
REGIME_MULTIPLIERS = {
    "bull":       1.2,
    "neutral":    1.0,
    "bear":       0.5,   # Be much more cautious in bear market
    "high_vol":   0.7,   # Volatile markets = reduce position sizing
    "crash":      0.2,   # Near-crash conditions = mostly stay out
}

# ── TRADING THRESHOLDS ─────────────────────────────────────────────────────────
SIGNAL_FIRE_THRESHOLD   = 0.55   # Score must exceed this to execute a trade
SIGNAL_WATCH_THRESHOLD  = 0.35   # Score above this goes on watchlist
MAX_PORTFOLIO_POSITIONS = 10     # Never hold more than 10 positions
MAX_POSITION_SIZE_PCT   = 0.10   # Max 10% of portfolio in one position
MAX_SECTOR_EXPOSURE_PCT = 0.30   # Max 30% in one sector
RISK_PER_TRADE_PCT      = 0.02   # Risk 2% of portfolio per trade (stop-loss)
TRAILING_STOP_TRIGGER   = 0.05   # Activate trailing stop after +5% gain
TRAILING_STOP_DISTANCE  = 0.03   # Trail 3% below high water mark
MAX_MONTHLY_DRAWDOWN    = 0.10   # Circuit breaker at -10% monthly

# ── SCHEDULING ─────────────────────────────────────────────────────────────────
PRE_MARKET_SCAN_TIME  = "08:00"  # ET — collect overnight signals
MARKET_OPEN_SCAN_TIME = "09:45"  # ET — first intraday scan (let open settle)
MIDDAY_SCAN_TIME      = "12:00"  # ET
CLOSE_SCAN_TIME       = "15:30"  # ET — end of day positioning
POST_MARKET_TIME      = "16:30"  # ET — earnings, filings, news digest

# ── BACKTEST SETTINGS ──────────────────────────────────────────────────────────
BACKTEST_START = "2021-01-01"
BACKTEST_END   = "2024-12-31"
INITIAL_CAPITAL = 100_000       # Simulated capital for backtesting/paper

# ── LOGGING ────────────────────────────────────────────────────────────────────
LOG_DIR   = BASE_DIR / "logs"
LOG_LEVEL = "INFO"
