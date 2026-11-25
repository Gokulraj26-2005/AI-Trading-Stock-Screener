"""
Unit tests for adaptive news fetching and FinBERT re-evaluation:
- Inside trading hours (09:00 - 16:00 IST) delta lookback, append, and 24h pruning
- Outside trading hours full 24h refresh
- Headline deduplication and SQLite cache persistence
"""

import pytest
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from services.multi_tf_analysis import (
    _is_trading_hours_ist,
    _fetch_stock_news_24h_sync,
    _normalize_title_for_dedup,
    HistoricalCandleCache,
    _NEWS_CACHE,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_is_trading_hours_ist_logic():
    # Monday 11:30 AM IST -> True
    mon_trading = datetime(2026, 10, 19, 11, 30, tzinfo=_IST)
    assert _is_trading_hours_ist(mon_trading) is True

    # Monday 08:30 AM IST (Pre-market) -> False
    mon_premarket = datetime(2026, 10, 19, 8, 30, tzinfo=_IST)
    assert _is_trading_hours_ist(mon_premarket) is False

    # Monday 16:30 PM IST (Post-market) -> False
    mon_postmarket = datetime(2026, 10, 19, 16, 30, tzinfo=_IST)
    assert _is_trading_hours_ist(mon_postmarket) is False

    # Saturday 12:00 PM IST (Weekend) -> False
    sat_noon = datetime(2026, 10, 24, 12, 0, tzinfo=_IST)
    assert _is_trading_hours_ist(sat_noon) is False

    # Sunday 15:00 PM IST (Weekend) -> False
    sun_afternoon = datetime(2026, 10, 25, 15, 0, tzinfo=_IST)
    assert _is_trading_hours_ist(sun_afternoon) is False


def test_outside_trading_hours_pulls_full_24h():
    """Outside trading hours, it queries when:24h and completely refreshes articles."""
    _NEWS_CACHE.clear()
    sym = "TESTSYM1"
    now_ts = time.time()

    # Pre-populate cache with an old article
    _NEWS_CACHE[sym] = (
        now_ts - 7200,
        "Old Headline",
        [{"title": "Old Headline", "source": "News", "pub_ts": now_ts - 7200, "hours_ago": 2}]
    )

    mock_new_articles = [
        {"title": "Fresh 24h Breaking News", "source": "LiveMint", "pub_ts": now_ts - 1800, "hours_ago": 1}
    ]

    # Mock datetime to a Sunday outside trading hours
    sunday_dt = datetime(2026, 10, 25, 10, 0, tzinfo=_IST)
    with patch("services.multi_tf_analysis.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: sunday_dt if tz == _IST else datetime.fromtimestamp(now_ts, tz=timezone.utc)
        with patch("services.multi_tf_analysis._fetch_stock_news_from_google") as mock_fetch:
            mock_fetch.return_value = mock_new_articles

            summary, articles = _fetch_stock_news_24h_sync(sym, force_refresh=True)

            # Verified: requested when:24h outside trading hours
            mock_fetch.assert_called_once_with(sym, "24h")
            assert len(articles) == 1
            assert articles[0]["title"] == "Fresh 24h Breaking News"


def test_inside_trading_hours_appends_and_prunes_24h():
    """Inside trading hours, it queries elapsed window (e.g. 2h), appends, and prunes >24h."""
    _NEWS_CACHE.clear()
    sym = "TESTSYM2"
    now_ts = time.time()

    # 1 article from 3 hours ago, 1 article from 26 hours ago (stale > 24h)
    prev_cached = [
        {"title": "Morning Catalyst 3h ago", "source": "ET", "pub_ts": now_ts - (3 * 3600), "hours_ago": 3},
        {"title": "Yesterday News 26h ago", "source": "Reuters", "pub_ts": now_ts - (26 * 3600), "hours_ago": 26},
    ]

    # Last pulled 2 hours ago (7200s)
    last_pulled = now_ts - 7200
    _NEWS_CACHE[sym] = (last_pulled, "Morning Catalyst 3h ago", prev_cached)

    new_articles = [
        {"title": "Breaking News Just Now", "source": "Bloomberg", "pub_ts": now_ts - 900, "hours_ago": 1},
        # Duplicate of previous article
        {"title": "Morning Catalyst 3h ago", "source": "ET", "pub_ts": now_ts - (3 * 3600), "hours_ago": 3},
    ]

    # Mock datetime to Monday 11:30 AM IST (inside trading hours)
    monday_dt = datetime(2026, 10, 19, 11, 30, tzinfo=_IST)
    with patch("services.multi_tf_analysis.time.time", return_value=now_ts):
        with patch("services.multi_tf_analysis.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: monday_dt if tz == _IST else datetime.fromtimestamp(now_ts, tz=timezone.utc)
            with patch("services.multi_tf_analysis._fetch_stock_news_from_google") as mock_fetch:
                mock_fetch.return_value = new_articles

                summary, articles = _fetch_stock_news_24h_sync(sym, force_refresh=True)

            # Verified: window requested was 2h (since 7200s elapsed)
            mock_fetch.assert_called_once_with(sym, "2h")

            # Verified: 26h old article pruned!
            titles = [a["title"] for a in articles]
            assert "Yesterday News 26h ago" not in titles

            # Verified: Breaking news appended!
            assert "Breaking News Just Now" in titles

            # Verified: Duplicate deduplicated!
            assert titles.count("Morning Catalyst 3h ago") == 1
            assert len(articles) == 2


def test_sqlite_news_cache_persistence(tmp_path):
    """Verify news cache is persisted and retrieved across restarts from SQLite."""
    db_file = tmp_path / "test_candles.sqlite"
    cache = HistoricalCandleCache(db_path=db_file)

    now_ts = time.time()
    articles = [
        {"title": "Acquisition announced", "source": "CNBC", "pub_ts": now_ts, "hours_ago": 1, "is_regulatory": True}
    ]
    cache.save_news_cache("RELIANCE", now_ts, "Summary string", articles)

    # Read back
    result = cache.get_news_cache("RELIANCE")
    assert result is not None
    ts, summary, read_articles = result
    assert ts == pytest.approx(now_ts, rel=1e-3)
    assert summary == "Summary string"
    assert len(read_articles) == 1
    assert read_articles[0]["title"] == "Acquisition announced"
