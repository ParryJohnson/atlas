# ATLAS — Adaptive Trading & Learning Aggregation System

> Multi-signal quant trading system. Free data feeds. Paper trading first.

---

## Quick Start (5 minutes)

### 1. Clone and install
```bash
git clone <your-repo>
cd atlas
pip install -r requirements.txt
```

### 2. Get your free API keys
| Service | What it does | Sign up |
|---|---|---|
| **Alpaca** | Paper trading + execution | [alpaca.markets](https://alpaca.markets) |
| **Finnhub** | News, earnings, sentiment | [finnhub.io](https://finnhub.io) |
| **FRED** | Macro economic data | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/fred/) |
| **NewsAPI** | Headlines from 80k+ sources | [newsapi.org](https://newsapi.org) |
| **Reddit** | WSB social sentiment | [reddit.com/prefs/apps](https://reddit.com/prefs/apps) |

### 3. Configure
```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

### 4. Run
```bash
# One scan cycle (dry run, no trades)
python atlas.py scan

# Full paper trading mode (live market data, fake money)
python atlas.py paper

# Check portfolio status
python atlas.py status
```

---

## Architecture

```
Raw Data Feeds (free APIs)
        │
        ▼
Signal Processing Layer
  ├── Technical (RSI, MACD, BB, ATR, Golden Cross...)
  ├── Sentiment (Finnhub, NewsAPI, Reddit, StockTwits)
  ├── Alternative (SEC EDGAR, Congress trades, Google Trends)
  └── Macro (FRED, Fear & Greed, VIX, Yield Curve)
        │
        ▼
Confluence Scorer (weighted aggregation + regime multiplier)
        │
        ▼
Risk Engine (position sizing, stops, drawdown breaker, blackouts)
        │
        ▼
Alpaca Execution (paper → live when ready)
        │
        ▼
Database + Learning Loop (SQLite, signal attribution, weekly review)
```

---

## Signal Universe

### Technical Signals
- RSI oversold/overbought (< 30 / > 70)
- MACD bullish/bearish crossover
- Bollinger Band touches (lower = potential bounce)
- Golden Cross / Death Cross (50 vs 200 MA)
- Volume spike (> 2x 20-day average)

### Sentiment Signals
- News headline sentiment (keyword + Finnhub scores)
- Reddit WSB/stocks mention velocity
- StockTwits bullish/bearish ratio
- CNN Fear & Greed Index (contrarian signal at extremes)

### Alternative/Edge Signals
- SEC Form 4 insider buy/sell transactions
- Congressional stock disclosures (STOCK Act)
- Google Trends search interest spikes
- CoinGecko crypto market regime

### Macro Signals
- FRED yield curve (2Y-10Y spread)
- Federal funds rate direction
- CPI trend, unemployment
- VIX level and term structure

---

## Risk Management

Every trade passes these checks before execution:

1. **Position sizing** — ATR-based, max 2% portfolio risk per trade
2. **Drawdown circuit breaker** — halts all trading if down 10% in a month
3. **Sector concentration** — max 30% in any single sector
4. **Correlation guard** — no more than 10 positions at once
5. **Earnings blackout** — no new entries within 2 days of earnings
6. **Trailing stops** — activates after +5% gain, trails 3% below high

---

## Conviction Scoring

```
Score = Σ(signal_weight × recency_factor × regime_multiplier)

FIRE   (|score| > 0.55) → Execute trade
WATCH  (|score| > 0.35) → Add to watchlist
IGNORE (|score| < 0.35) → Log only

Regime multipliers:
  bull:     1.2x  (push longs harder)
  neutral:  1.0x  (normal)
  bear:     0.5x  (be conservative)
  high_vol: 0.7x  (reduce size)
  crash:    0.2x  (almost nothing)
```

---

## Roadmap

- [x] Phase 1: Data plumbing (all connectors)
- [x] Phase 2: Signal engine + confluence scorer
- [x] Phase 3: Risk management layer
- [x] Phase 4: Alpaca paper trading execution
- [ ] Phase 5: FinBERT local NLP (deeper sentiment)
- [ ] Phase 6: Backtesting on 2021–2024 historical data
- [ ] Phase 7: Learning loop (signal weight auto-adjustment)
- [ ] Phase 8: Discord alerts + performance dashboard
- [ ] Phase 9: Live trading with real capital (after 60+ days paper)

---

## File Structure

```
atlas/
├── atlas.py              ← Main orchestrator (run this)
├── config.py             ← All settings and signal weights
├── .env.example          ← API key template
├── core/
│   └── database.py       ← SQLite schema, all tables
├── connectors/
│   ├── price_connector.py    ← yfinance + pandas-ta
│   ├── macro_connector.py    ← FRED + Fear&Greed + regime
│   ├── news_connector.py     ← Finnhub + NewsAPI + sentiment
│   └── alt_connector.py      ← Reddit + StockTwits + EDGAR + Congress
├── signals/
│   └── scorer.py         ← Confluence scoring engine
├── risk/
│   └── risk_engine.py    ← All risk checks + position sizing
├── execution/
│   └── executor.py       ← Alpaca bracket orders
├── learning/             ← (Phase 7) signal attribution loop
├── backtest/             ← (Phase 6) historical validation
├── data/
│   └── atlas.db          ← SQLite database
└── logs/                 ← Daily log files
```

---

## Important Warnings

- **Paper trade for at least 60 days** before using real money
- **ALPACA_PAPER=true** is locked on by default — change carefully
- No trading system guarantees profit — past performance ≠ future results
- The circuit breaker exists for a reason — don't disable it
- This is a tool, not a financial advisor
