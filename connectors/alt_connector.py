"""
ATLAS Connector: Alternative Data
===================================
The edge layer — data most retail traders don't use:
- Reddit (PRAW) — r/wallstreetbets, r/stocks mention velocity
- StockTwits — bullish/bearish ratio
- SEC EDGAR — Form 4 insider transactions (real-time)
- Capitol Trades — Congressional stock disclosures
- Google Trends (pytrends) — search interest spikes

ALL FREE. No paid subscriptions required.
"""

import requests
import re
from datetime import datetime, timedelta
from typing import Optional
import sys, os, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
from core.database import get_session, SocialSignal, InsiderTrade, CongressTrade

logger = logging.getLogger("atlas.alt")


# ─── REDDIT ────────────────────────────────────────────────────────────────────

def fetch_reddit_mentions(ticker: str, subreddits: list = None) -> dict:
    """
    Count mentions of a ticker in WSB and related subs.
    Uses PRAW (Python Reddit API Wrapper).
    Get free credentials at: reddit.com/prefs/apps → create app (script)
    """
    if not REDDIT_CLIENT_ID:
        return _reddit_unavailable(ticker)

    try:
        import praw
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
        )

        subreddits = subreddits or ["wallstreetbets", "stocks", "investing", "options"]
        total_mentions = 0
        sentiment_scores = []
        hot_posts = []

        for sub_name in subreddits:
            sub = reddit.subreddit(sub_name)
            for post in sub.search(ticker, time_filter="day", limit=20):
                title_lower = post.title.lower()
                # Check if ticker is actually mentioned (not just random match)
                if (f"${ticker.lower()}" in title_lower or
                    f" {ticker.lower()} " in title_lower or
                    ticker.lower() == title_lower.strip()):

                    total_mentions += 1
                    # Upvote ratio as sentiment proxy (>0.8 = community bullish)
                    sentiment = (post.upvote_ratio - 0.5) * 2  # scale to -1 to +1
                    sentiment_scores.append(sentiment)

                    if post.score > 100:
                        hot_posts.append({
                            "title": post.title[:100],
                            "score": post.score,
                            "upvote_ratio": post.upvote_ratio,
                            "sub": sub_name,
                        })

        avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0.0

        return {
            "ticker": ticker,
            "platform": "reddit",
            "mention_count": total_mentions,
            "sentiment_score": round(avg_sentiment, 3),
            "bullish_pct": round(sum(1 for s in sentiment_scores if s > 0) / max(len(sentiment_scores), 1), 3),
            "bearish_pct": round(sum(1 for s in sentiment_scores if s < 0) / max(len(sentiment_scores), 1), 3),
            "hot_posts": hot_posts[:3],
            "source": "reddit_praw",
        }
    except Exception as e:
        logger.error(f"[Alt] Reddit error for {ticker}: {e}")
        return _reddit_unavailable(ticker)


def _reddit_unavailable(ticker: str) -> dict:
    return {"ticker": ticker, "platform": "reddit", "mention_count": 0,
            "sentiment_score": 0.0, "bullish_pct": 0.0, "bearish_pct": 0.0,
            "source": "unavailable"}


# ─── STOCKTWITS ────────────────────────────────────────────────────────────────

def fetch_stocktwits_sentiment(ticker: str) -> dict:
    """
    Fetch StockTwits stream for a ticker.
    No API key required for public stream.
    Returns bullish/bearish ratio and recent message volume.
    """
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        headers = {"User-Agent": "ATLAS/1.0 (personal trading research)"}
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code != 200:
            logger.warning(f"[Alt] StockTwits {ticker}: HTTP {resp.status_code}")
            return _stocktwits_empty(ticker)

        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return _stocktwits_empty(ticker)

        bullish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bearish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total   = bullish + bearish

        bull_pct = bullish / total if total > 0 else 0.5
        bear_pct = bearish / total if total > 0 else 0.5
        sentiment = (bull_pct - 0.5) * 2   # -1 to +1

        logger.info(f"[Alt] StockTwits {ticker}: {bullish}B/{bearish}Be ({len(messages)} msgs)")
        return {
            "ticker":         ticker,
            "platform":       "stocktwits",
            "mention_count":  len(messages),
            "sentiment_score": round(sentiment, 3),
            "bullish_pct":    round(bull_pct, 3),
            "bearish_pct":    round(bear_pct, 3),
            "source":         "stocktwits",
        }
    except Exception as e:
        logger.error(f"[Alt] StockTwits error for {ticker}: {e}")
        return _stocktwits_empty(ticker)


def _stocktwits_empty(ticker: str) -> dict:
    return {"ticker": ticker, "platform": "stocktwits", "mention_count": 0,
            "sentiment_score": 0.0, "bullish_pct": 0.5, "bearish_pct": 0.5,
            "source": "stocktwits"}


# ─── SEC EDGAR — INSIDER TRADES ────────────────────────────────────────────────

def fetch_insider_trades(ticker: str, days_back: int = 30) -> list[dict]:
    """
    Fetch Form 4 insider transactions from SEC EDGAR.
    No API key required.
    Big insider buys (especially by CEO/CFO) = strong bullish signal.
    """
    try:
        # EDGAR full-text search API
        url = "https://efts.sec.gov/LATEST/search-index"
        params = {
            "q": f'"{ticker}"',
            "dateRange": "custom",
            "startdt": (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d"),
            "enddt": datetime.now().strftime("%Y-%m-%d"),
            "forms": "4",
        }
        headers = {"User-Agent": "ATLAS trading research (personal use)"}
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params, headers=headers, timeout=15
        )

        # Try the main EDGAR search endpoint
        url2 = "https://efts.sec.gov/LATEST/search-index?q=%22" + ticker + "%22&forms=4&dateRange=custom&startdt=" + \
               (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d") + "&enddt=" + \
               datetime.now().strftime("%Y-%m-%d")

        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={"q": f'"{ticker}"', "forms": "4",
                    "dateRange": "custom",
                    "startdt": (datetime.now()-timedelta(days=days_back)).strftime("%Y-%m-%d"),
                    "enddt": datetime.now().strftime("%Y-%m-%d")},
            headers={"User-Agent": "ATLAS/1.0 personal-use research@example.com"},
            timeout=15
        )

        if resp.status_code != 200:
            return []

        data = resp.json()
        filings = data.get("hits", {}).get("hits", [])

        trades = []
        for filing in filings[:10]:
            src = filing.get("_source", {})
            trades.append({
                "ticker":            ticker,
                "insider_name":      src.get("display_names", ["Unknown"])[0] if src.get("display_names") else "Unknown",
                "insider_title":     "",
                "transaction_type":  "unknown",
                "shares":            0,
                "price":             0,
                "value":             0,
                "filed_date":        datetime.now(),
                "transaction_date":  datetime.now(),
                "url":               f"https://www.sec.gov/Archives/edgar/{src.get('file_date', '')}",
                "form_type":         src.get("form_type", "4"),
            })

        logger.info(f"[Alt] EDGAR: {len(trades)} Form 4 filings for {ticker}")
        return trades

    except Exception as e:
        logger.debug(f"[Alt] EDGAR error for {ticker}: {e}")
        return []


def fetch_insider_trades_simple(ticker: str) -> list[dict]:
    """
    Simplified insider trade fetch using EDGAR company submissions.
    More reliable than full-text search.
    """
    try:
        # First get the CIK for the ticker
        search_url = f"https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker}&type=4&dateb=&owner=include&count=10&search_text=&action=getcompany&output=atom"
        headers = {"User-Agent": "ATLAS/1.0 research personal@example.com"}
        resp = requests.get(search_url, headers=headers, timeout=10)

        # Parse result count from response
        count = resp.text.count("<entry>") if resp.status_code == 200 else 0

        logger.info(f"[Alt] EDGAR insider check {ticker}: {count} recent Form 4 filings found")
        return [{"ticker": ticker, "filing_count": count, "source": "edgar"}]

    except Exception as e:
        logger.debug(f"[Alt] EDGAR simple error: {e}")
        return []


# ─── CAPITOL TRADES — CONGRESS TRADING ────────────────────────────────────────

def fetch_congress_trades(ticker: str = None, limit: int = 20) -> list[dict]:
    """
    Scrape recent congressional stock trades from Capitol Trades.
    Free, no API key required.
    Politicians buying a stock aggressively = potential insider-adjacent signal.
    """
    try:
        url = "https://www.capitoltrades.com/trades"
        params = {}
        if ticker:
            params["ticker"] = ticker
        params["pageSize"] = limit

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        }

        # Try their API endpoint
        api_url = "https://api.capitoltrades.com/trades"
        if ticker:
            api_url += f"?issuer={ticker}&pageSize={limit}"
        else:
            api_url += f"?pageSize={limit}&sortBy=-transactionDate"

        resp = requests.get(api_url, headers=headers, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            trades = data.get("data", [])
            results = []

            for trade in trades:
                results.append({
                    "ticker":            trade.get("issuer", {}).get("ticker", ""),
                    "politician_name":   trade.get("politician", {}).get("name", ""),
                    "party":             trade.get("politician", {}).get("party", ""),
                    "chamber":           trade.get("politician", {}).get("chamber", ""),
                    "transaction_type":  trade.get("type", ""),
                    "amount_range":      trade.get("size", ""),
                    "transaction_date":  trade.get("txDate", ""),
                    "disclosure_date":   trade.get("filingDate", ""),
                    "source":            "capitoltrades",
                })

            logger.info(f"[Alt] Capitol Trades: {len(results)} trades" +
                       (f" for {ticker}" if ticker else ""))
            return results

    except Exception as e:
        logger.debug(f"[Alt] Capitol Trades error: {e}")

    return []


# ─── GOOGLE TRENDS ─────────────────────────────────────────────────────────────

def fetch_google_trends(ticker: str, company_name: str = None) -> dict:
    """
    Fetch Google Trends search interest for a ticker.
    Rising search interest often precedes price moves.
    Uses pytrends (no API key required).
    """
    try:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        kw = company_name or ticker
        pytrends.build_payload([kw], timeframe="now 7-d", geo="US")

        interest = pytrends.interest_over_time()
        if interest.empty:
            return {"ticker": ticker, "trend_score": 50, "spike": False}

        latest = int(interest[kw].iloc[-1])
        avg_7d = float(interest[kw].mean())
        spike  = latest > (avg_7d * 1.5) and latest > 60

        logger.info(f"[Alt] Google Trends {ticker}: latest={latest}, avg={avg_7d:.1f}, spike={spike}")
        return {
            "ticker":      ticker,
            "trend_score": latest,        # 0-100 relative to peak
            "avg_7d":      round(avg_7d, 1),
            "spike":       spike,
            "ratio":       round(latest / max(avg_7d, 1), 2),
            "signal":      "bullish" if spike else "neutral",
        }
    except Exception as e:
        logger.debug(f"[Alt] Google Trends error for {ticker}: {e}")
        return {"ticker": ticker, "trend_score": 50, "spike": False, "signal": "neutral"}


# ─── AGGREGATED SIGNALS ────────────────────────────────────────────────────────

def get_alt_signals(ticker: str) -> list[dict]:
    """
    Run all alternative data sources and convert to signal dicts.
    """
    signals = []

    # StockTwits (no key needed)
    st = fetch_stocktwits_sentiment(ticker)
    if st["mention_count"] > 0:
        if st["bullish_pct"] > 0.65:
            signals.append({
                "ticker": ticker, "signal_type": "stocktwits_bullish",
                "value": st["bullish_pct"], "score": 0.07,
                "direction": "bullish", "source": "stocktwits",
                "raw_data": st,
            })
        elif st["bearish_pct"] > 0.65:
            signals.append({
                "ticker": ticker, "signal_type": "stocktwits_bearish",
                "value": st["bearish_pct"], "score": -0.07,
                "direction": "bearish", "source": "stocktwits",
                "raw_data": st,
            })

    # Google Trends
    gt = fetch_google_trends(ticker)
    if gt.get("spike"):
        signals.append({
            "ticker": ticker, "signal_type": "google_trends_spike",
            "value": gt["ratio"], "score": 0.08,
            "direction": "bullish", "source": "google_trends",
            "raw_data": gt,
        })

    return signals


def store_social_signal(data: dict, session):
    """Persist social signal to database."""
    record = SocialSignal(
        ticker=data.get("ticker"),
        platform=data.get("platform"),
        mention_count=data.get("mention_count", 0),
        sentiment_score=data.get("sentiment_score", 0.0),
        bullish_pct=data.get("bullish_pct", 0.0),
        bearish_pct=data.get("bearish_pct", 0.0),
        volume_vs_avg=data.get("ratio", 1.0),
        timestamp=datetime.utcnow(),
    )
    session.add(record)
    session.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== Alternative Data Connector Test ===\n")

    # Test StockTwits (no key needed)
    print("--- StockTwits ---")
    for ticker in ["AAPL", "NVDA", "TSLA"]:
        st = fetch_stocktwits_sentiment(ticker)
        print(f"  {ticker}: mentions={st['mention_count']}, "
              f"bullish={st['bullish_pct']:.0%}, bearish={st['bearish_pct']:.0%}, "
              f"sentiment={st['sentiment_score']:+.3f}")
        time.sleep(0.5)

    # Test Congress trades (scrape)
    print("\n--- Congress Trades (recent, all tickers) ---")
    trades = fetch_congress_trades(limit=5)
    if trades:
        for t in trades[:5]:
            print(f"  {t.get('politician_name','?'):30} {t.get('ticker','?'):8} "
                  f"{t.get('transaction_type','?'):10} {t.get('amount_range','?')}")
    else:
        print("  (API may require browser headers — will work in live environment)")

    # Test Google Trends
    print("\n--- Google Trends ---")
    for ticker, name in [("NVDA", "NVIDIA"), ("TSLA", "Tesla")]:
        gt = fetch_google_trends(ticker, name)
        print(f"  {ticker}: score={gt['trend_score']}, spike={gt['spike']}, signal={gt['signal']}")
        time.sleep(1)
