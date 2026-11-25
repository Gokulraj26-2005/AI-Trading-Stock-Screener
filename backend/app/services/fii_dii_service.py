"""
FII & DII Cash Market Activity Service for VIA-Agent7
======================================================
Tracks consolidated daily Net Foreign Institutional Investor (FII) and
Domestic Institutional Investor (DII) cash equity figures (in ₹ Crores).

Informs the Macro Regime Engine of institutional capital flows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "portfolio.db"


class FIIDIIService:
    """Manages ingestion and caching of daily FII / DII net cash flows."""

    _instance: Optional["FIIDIIService"] = None

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._cached_data: Optional[Dict[str, Any]] = None
        self._last_fetch_ts: float = 0.0
        self._init_db()

    @classmethod
    def get_instance(cls) -> "FIIDIIService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fii_dii_activity (
                        trade_date TEXT PRIMARY KEY,
                        fii_net_cr REAL NOT NULL,
                        dii_net_cr REAL NOT NULL,
                        total_net_cr REAL NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.commit()
        except Exception as exc:
            logger.warning(f"Could not initialize fii_dii_activity table: {exc}")

    def fetch_latest_fii_dii(self) -> Dict[str, Any]:
        """
        Fetches today's FII/DII figures from official exchange or fallback source,
        persists to SQLite, and updates cache.
        """
        now = time.time()
        # Rate limit updates to at most once per 30 minutes
        if self._cached_data and (now - self._last_fetch_ts < 1800):
            return self._cached_data

        data = self._fetch_from_exchange()
        if not data:
            data = self._load_from_db()

        if not data:
            # Baseline neutral fallback if exchange feeds are offline
            data = {
                "trade_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "fii_net_cr": 0.0,
                "dii_net_cr": 0.0,
                "total_net_cr": 0.0,
                "bias": "NEUTRAL",
                "source": "fallback_baseline",
            }

        self._cached_data = data
        self._last_fetch_ts = now
        return data

    def _fetch_from_exchange(self) -> Optional[Dict[str, Any]]:
        """Attempt to fetch official daily FII/DII cash flow figures."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Source 1: NSE official endpoint
        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode("utf-8"))
                    # Parse NSE structure: list of [{"category": "FII/FPI *", "netValue": "-1234.50"}, ...]
                    fii_net = 0.0
                    dii_net = 0.0
                    t_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    for row in raw:
                        cat = str(row.get("category", "")).upper()
                        val = float(str(row.get("netValue", "0")).replace(",", ""))
                        if "FII" in cat or "FPI" in cat:
                            fii_net = val
                            t_date = row.get("date", t_date)
                        elif "DII" in cat:
                            dii_net = val

                    return self._save_and_format(t_date, fii_net, dii_net, source="nse_direct")
        except Exception as exc:
            logger.debug(f"Direct NSE FII/DII fetch skipped ({exc}), checking cached DB...")

        return None

    def _save_and_format(self, trade_date: str, fii_net: float, dii_net: float, source: str = "exchange") -> Dict[str, Any]:
        total_net = round(fii_net + dii_net, 2)
        bias = "BULLISH" if fii_net > 1000.0 else ("BEARISH" if fii_net < -1000.0 else "NEUTRAL")
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO fii_dii_activity 
                    (trade_date, fii_net_cr, dii_net_cr, total_net_cr, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (trade_date, fii_net, dii_net, total_net, now_iso))
                conn.commit()
        except Exception as exc:
            logger.warning(f"Could not persist FII/DII data: {exc}")

        return {
            "trade_date": trade_date,
            "fii_net_cr": fii_net,
            "dii_net_cr": dii_net,
            "total_net_cr": total_net,
            "bias": bias,
            "source": source,
        }

    def _load_from_db(self) -> Optional[Dict[str, Any]]:
        """Load the most recent recorded session from SQLite."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT trade_date, fii_net_cr, dii_net_cr, total_net_cr 
                    FROM fii_dii_activity 
                    ORDER BY trade_date DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    fii_net = float(row[1])
                    dii_net = float(row[2])
                    total_net = float(row[3])
                    bias = "BULLISH" if fii_net > 1000.0 else ("BEARISH" if fii_net < -1000.0 else "NEUTRAL")
                    return {
                        "trade_date": row[0],
                        "fii_net_cr": fii_net,
                        "dii_net_cr": dii_net,
                        "total_net_cr": total_net,
                        "bias": bias,
                        "source": "sqlite_cache",
                    }
        except Exception as exc:
            logger.debug(f"DB load error for FII/DII: {exc}")
        return None


def get_fii_dii_service() -> FIIDIIService:
    return FIIDIIService.get_instance()
