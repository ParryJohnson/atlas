"""
ATLAS Connector: News & Sentiment
==================================
Pulls news from Finnhub (free, includes pre-scored sentiment)
and NewsAPI (free dev tier).

Finnhub: free at finnhub.io — 60 req/min
NewsAPI: free at newsapi.org — 100 req/day dev tier

Sentiment scoring:
- Finnhub returns pre-scored sentiment (saves us running FinBERT locally)
- We normalize all scores to -1.0 (very bearish) to +1.0 (very bullish)
"""

import requests
import finnhub
from datetime import datetime, timedelta
from typing import Optional
import sys, os, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import FINNHUB_API_KEY, NEWSAPI_KEY
from core.database import get_session, NewsItem, Signal

logger = logging.getLogger("atlas.news")

# Simple keyword-based sentiment scoring for when we don't have Finnhub scores
BULLISH_KEYWORDS = [
    "beat", "beats", "exceeds", "record", "surge", "rally", "breakout",
    "upgrade", "outperform", "strong", "growth", "profit", "gains",
    "bullish", "buy", "partnership", "deal", "contract", "approval",
    "innovation", "expansion", "revenue", "earnings beat"
]
BEARISH_KEYWORDS = [
    "miss", "misses", "below", "disappoints", "decline", "falls", "crash",
    "downgrade", "underperform", "weak", "loss", "cuts", "layoffs",
    "bearish", "sell", "investigation", "lawsuit", "recall", "default",
    "bankruptcy", "warning", "shortfall", "guidance cut"
]


def keyword_sentiment(text: str) -> float:
    """
    Fast keyword-based sentiment. Returns -1.0 to +1.0.
    Used as fallback when API scores unavailable.
    """
    text_lower = text.lower()
    bull_hits = sum(1 for w in BULLISH_KEYWORDS if w in text_lower)
    bear_hits = sum(1 for w in BEARISH_KEYWORDS if w in text_lower)
    total = bull_hits + bear_hits
    if total == 0:
        return 0.0
    return round((bull_hits - bear_hits) / total, 3)


def fetch_finnhub_news(ticker: str, days_back: int = 3) -> list[dict]:
    """
    Fetch company news from Finnhub with sentiment scores.
    Returns list of normalized news items.
    """
    if not FINNHUB_API_KEY:
        logger.warning("[News] FINNHUB_API_KEY not set")
        return []
    try:
        client = finnhub.Client(api_key=FINNHUB_API_KEY)
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        news = client.company_news(ticker, _from=start_date, to=end_date)
        results = []
        for item in news[:20]:   # Cap at 20 per ticker
            headline = item.get("headline", "")
            summary  = item.get("summary", "")
            full_text = f"{headline} {summary}"

            # Finnhub doesn't include sentiment in free tier news endpoint
            # so we use keyword scoring
            sentiment = keyword_sentiment(full_text)

            results.append({
                "ticker":      ticker,
                "headline":    headline,
                "source":      item.get("source", ""),
                "url":         item.get("url", ""),
                "sentiment":   sentiment,
                "sentiment_label": "positive" if sentiment > 0.1 else
                                   "negative" if sentiment < -0.1 else "neutral",
                "published_at": datetime.fromtimestamp(item.get("datetime", 0)),
                "raw": item,
            })

        logger.info(f"[News] Finnhub: {len(results)} articles for {ticker}")
        return results

    except Exception as e:
        logger.error(f"[News] Finnhub error for {ticker}: {e}")
        return []


def fetch_newsapi_headlines(query: str, days_back: int = 2) -> list[dict]:
    """
    Fetch headlines from NewsAPI.
    query can be a ticker, company name, or topic.
    """
    if not NEWSAPI_KEY:
        logger.warning("[News] NEWSAPI_KEY not set")
        return []
    try:
        from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "from": from_date,
            "sortBy": "relevancy",
            "language": "en",
            "pageSize": 10,
            "apiKey": NEWSAPI_KEY,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for article in data.get("articles", []):
            headline = article.get("title", "")
            description = article.get("description", "") or ""
            full_text = f"{headline} {description}"
            sentiment = keyword_sentiment(full_text)
            pub = article.get("publishedAt", "")
            try:
                published_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                published_at = datetime.utcnow()

            results.append({
                "ticker":      None,
                "headline":    headline,
                "source":      article.get("source", {}).get("name", ""),
                "url":         article.get("url", ""),
                "sentiment":   sentiment,
                "sentiment_label": "positive" if sentiment > 0.1 else
                                   "negative" if sentiment < -0.1 else "neutral",
                "published_at": published_at,
                "raw": article,
            })
        logger.info(f"[News] NewsAPI: {len(results)} articles for '{query}'")
        return results
    except Exception as e:
        logger.error(f"[News] NewsAPI error: {e}")
        return []


def fetch_finnhub_market_sentiment(ticker: str) -> Optional[dict]:
    """
    Fetch Finnhub's social sentiment for a ticker.
    Returns reddit + twitter mention counts and sentiment.
    """
    if not FINNHUB_API_KEY:
        return None
    try:
        client = finnhub.Client(api_key=FINNHUB_API_KEY)
        # Social sentiment endpoint (free tier)
        data = client.stock_social_sentiment(ticker)
        if not data:
            return None

        reddit  = data.get("reddit", [])
        twitter = data.get("twitter", [])

        # Aggregate most recent entries
        def aggregate(items, n=5):
            if not items:
                return {"mentions": 0, "sentiment": 0.0, "positive": 0, "negative": 0}
            recent = sorted(items, key=lambda x: x.get("atTime", ""), reverse=True)[:n]
            mentions   = sum(i.get("mention", 0) for i in recent)
            pos_score  = sum(i.get("positiveScore", 0) for i in recent) / len(recent)
            neg_score  = sum(i.get("negativeScore", 0) for i in recent) / len(recent)
            return {
                "mentions":  mentions,
                "sentiment": round(pos_score - neg_score, 3),
                "positive":  round(pos_score, 3),
                "negative":  round(neg_score, 3),
            }

        return {
            "ticker":  ticker,
            "reddit":  aggregate(reddit),
            "twitter": aggregate(twitter),
            "combined_sentiment": round(
                (aggregate(reddit)["sentiment"] + aggregate(twitter)["sentiment"]) / 2, 3
            ),
        }
    except Exception as e:
        logger.debug(f"[News] Social sentiment error for {ticker}: {e}")
        return None


def aggregate_news_sentiment(ticker: str, articles: list[dict]) -> dict:
    """
    Aggregate multiple articles into a single sentiment signal.
    Weights recent articles more heavily.
    """
    if not articles:
        return {"ticker": ticker, "avg_sentiment": 0.0, "article_count": 0,
                "signal": "neutral", "bullish_count": 0, "bearish_count": 0}

    now = datetime.utcnow()
    weighted_sum = 0.0
    weight_total = 0.0
    bullish = 0
    bearish = 0

    for article in articles:
        pub = article.get("published_at", now)
        if isinstance(pub, datetime):
            hours_ago = max((now - pub.replace(tzinfo=None)).total_seconds() / 3600, 0.1)
        else:
            hours_ago = 24

        # Recency weight: articles from last 2 hours = 2x weight
        recency_weight = max(2.0 - (hours_ago / 24), 0.2)
        sentiment = article.get("sentiment", 0.0)

        weighted_sum  += sentiment * recency_weight
        weight_total  += recency_weight

        if sentiment > 0.1:
            bullish += 1
        elif sentiment < -0.1:
            bearish += 1

    avg_sentiment = weighted_sum / weight_total if weight_total > 0 else 0.0

    if avg_sentiment > 0.3:
        signal = "strong_bullish"
    elif avg_sentiment > 0.1:
        signal = "mild_bullish"
    elif avg_sentiment < -0.3:
        signal = "strong_bearish"
    elif avg_sentiment < -0.1:
        signal = "mild_bearish"
    else:
        signal = "neutral"

    return {
        "ticker":         ticker,
        "avg_sentiment":  round(avg_sentiment, 3),
        "article_count":  len(articles),
        "bullish_count":  bullish,
        "bearish_count":  bearish,
        "signal":         signal,
    }


def store_news(articles: list[dict], session):
    """Persist news items to database."""
    for article in articles:
        record = NewsItem(
            ticker=article.get("ticker"),
            headline=article.get("headline", "")[:500],
            source=article.get("source", "")[:60],
            url=article.get("url", "")[:500],
            sentiment=article.get("sentiment", 0.0),
            sentiment_label=article.get("sentiment_label", "neutral"),
            published_at=article.get("published_at", datetime.utcnow()),
        )
        session.add(record)
    session.commit()


def run_news_scan(tickers: list) -> dict:
    """
    Full news scan for a list of tickers.
    Returns dict: {ticker: sentiment_summary}
    """
    results = {}
    session = get_session()

    for ticker in tickers:
        articles = fetch_finnhub_news(ticker, days_back=2)
        if articles:
            store_news(articles, session)
        sentiment = aggregate_news_sentiment(ticker, articles)
        results[ticker] = sentiment
        time.sleep(0.5)   # Rate limit respect

    session.close()
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\n=== News Connector Test (keyword scorer) ===\n")

    # Test keyword sentiment scorer (no API key needed)
    headlines = [
        ("NVDA beats earnings by 15%, record revenue", "NVDA"),
        ("Tesla misses delivery targets, stock falls", "TSLA"),
        ("Apple announces massive buyback and dividend increase", "AAPL"),
        ("Fed warns of continued rate hikes amid inflation", "SPY"),
        ("Meta faces antitrust investigation from DOJ", "META"),
        ("Microsoft Azure growth accelerates, upgrades guidance", "MSFT"),
        ("Ordinary day, market moves sideways", "SPY"),
    ]

    print(f"{'Headline':<55} {'Ticker':<8} {'Score':>7} {'Label'}")
    print("-" * 85)
    for headline, ticker in headlines:
        score = keyword_sentiment(headline)
        label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"
        print(f"{headline[:54]:<55} {ticker:<8} {score:>7.3f} {label}")

    # Test aggregation
    print("\n--- Aggregation test ---")
    mock_articles = [
        {"sentiment": 0.6, "published_at": datetime.utcnow() - timedelta(hours=1)},
        {"sentiment": 0.4, "published_at": datetime.utcnow() - timedelta(hours=3)},
        {"sentiment": -0.2, "published_at": datetime.utcnow() - timedelta(hours=12)},
        {"sentiment": 0.5, "published_at": datetime.utcnow() - timedelta(hours=0.5)},
    ]
    summary = aggregate_news_sentiment("AAPL", mock_articles)
    print(f"AAPL aggregated: avg={summary['avg_sentiment']:.3f}, signal={summary['signal']}")
    print(f"  bullish={summary['bullish_count']}, bearish={summary['bearish_count']}, total={summary['article_count']}")
