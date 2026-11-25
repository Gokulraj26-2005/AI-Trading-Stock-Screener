import pytest
from services.short_screener_engine import ShortScreenerEngine
from services.finbert_sentiment import FinBERTSentimentEngine


def test_real_finbert_news_analysis_structure():
    """Verify FinBERT news analysis produces required schema with headlines, scores, and details."""
    engine = FinBERTSentimentEngine.get_instance()
    sample_articles = [
        {
            "title": "Reliance Q3 Net Profit Surges 12% YoY on Robust Retail & Jio Growth",
            "source": "Economic Times",
            "hours_ago": 1,
            "is_regulatory": False,
        },
        {
            "title": "Reliance Receives Regulatory Clearance for New Green Hydrogen Facility",
            "source": "BSE Announcement",
            "hours_ago": 3,
            "is_regulatory": True,
        },
    ]

    res = engine._fallback_rule_analysis("RELIANCE", sample_articles, 0)
    assert res["sentiment_score"] > 0
    assert res["sentiment_label"] == "BULLISH"
    assert len(res["headlines"]) == 2
    for h in res["headlines"]:
        assert "title" in h
        assert "hours_ago" in h
        assert "score" in h
        assert "source" in h
        assert "sentiment" in h


def test_news_endpoint_integration(client, session_headers, monkeypatch):
    """Test REST API endpoint /api/short-screener/news/{symbol}."""
    sample_articles = [
        {
            "title": "Reliance Q3 Net Profit Surges 12% YoY on Robust Retail & Refining",
            "source": "Economic Times",
            "hours_ago": 1,
            "is_regulatory": False,
        }
    ]

    import services.multi_tf_analysis as mta
    monkeypatch.setattr(mta, "_fetch_stock_news_24h_sync", lambda sym: ("Reliance Industries", sample_articles))

    res = client.get("/api/short-screener/news/RELIANCE", headers=session_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["symbol"] == "RELIANCE"
    assert "sentiment_score" in data
    assert "sentiment_label" in data
    assert "probabilities" in data
    assert "article_count" in data
    assert data["article_count"] >= 1
    assert "headlines" in data
    assert len(data["headlines"]) >= 1
    assert "summary" in data
    assert data["is_evaluated"] is True
    assert data.get("status") == "EVALUATED"


def test_news_endpoint_no_news_handling(client, session_headers, monkeypatch):
    """Test REST API endpoint returns NO_NEWS status when search finishes with 0 articles."""
    import services.finbert_sentiment as fbs
    fbs._SENTIMENT_CACHE.clear()

    import services.multi_tf_analysis as mta
    # Mock returning 0 articles for a quiet stock
    monkeypatch.setattr(mta, "_fetch_stock_news_24h_sync", lambda sym: ("Bharat Petroleum", []))

    res = client.get("/api/short-screener/news/BPCL", headers=session_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["symbol"] == "BPCL"
    assert data["status"] == "NO_NEWS"
    assert data["sentiment_label"] == "NO_NEWS"
    assert data["sentiment_score"] == 0
    assert data["article_count"] == 0
    assert data["is_evaluated"] is True
    assert "No news articles published for BPCL" in data["summary"]





def test_conglomerate_entity_disambiguation_rules():
    """Verify strict filtering prevents sister-group news bleed (e.g. Jio on RELIANCE)."""
    from services.multi_tf_analysis import (
        is_article_strictly_relevant,
        get_company_name_for_symbol,
    )

    # 1. RELIANCE: Must reject Jio standalone, Jio IPO, Jio Cinema, RPower
    assert not is_article_strictly_relevant(
        "RELIANCE", "Reliance Industries", "Jio Financial Services to launch new credit cards"
    )
    assert not is_article_strictly_relevant(
        "RELIANCE", "Reliance Industries", "Jio IPO expected in 2026: Valuation estimated at $100B"
    )
    assert not is_article_strictly_relevant(
        "RELIANCE", "Reliance Industries", "Reliance Power hits 5% upper circuit on debt-free status"
    )
    # Must accept genuine Reliance Industries news
    assert is_article_strictly_relevant(
        "RELIANCE", "Reliance Industries", "Reliance Industries Q3 profit rises 10% on oil-to-chemicals rebound"
    )
    assert is_article_strictly_relevant(
        "RELIANCE", "Reliance Industries", "RIL board approves new investment in green hydrogen hub"
    )

    # 2. TATACHEM: Must reject Tata Motors, Tata Steel, Tata Power
    assert not is_article_strictly_relevant(
        "TATACHEM", "Tata Chemicals", "Tata Motors reports 15% increase in EV sales this quarter"
    )
    assert not is_article_strictly_relevant(
        "TATACHEM", "Tata Chemicals", "Tata Steel cuts UK plant jobs amidst restructuring"
    )
    assert is_article_strictly_relevant(
        "TATACHEM", "Tata Chemicals", "Tata Chemicals commissions expanded soda ash plant in Gujarat"
    )

    # 3. HDFCBANK: Must reject HDFC Life, HDFC AMC
    assert not is_article_strictly_relevant(
        "HDFCBANK", "HDFC Bank", "HDFC Life insurance premium collection rises 18% in Q2"
    )
    assert not is_article_strictly_relevant(
        "HDFCBANK", "HDFC Bank", "HDFC AMC expands mutual fund offerings"
    )
    assert is_article_strictly_relevant(
        "HDFCBANK", "HDFC Bank", "HDFC Bank net interest income surges as retail deposits grow"
    )

