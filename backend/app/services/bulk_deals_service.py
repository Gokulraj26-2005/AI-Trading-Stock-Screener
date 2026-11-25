"""
Bulk & Block Deals Ingestion Service for VIA-Agent7
====================================================
Tracks high-value institutional and promoter transactions (>0.5% equity or single
orders > ₹10 Crore) published by NSE.

Feeds Factor 3 (Large Trade Activity) of the Universal Institutional Flow Engine.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "bulk_block_deals.sqlite"


class BulkDealsService:
    """Manages ingestion and queries of institutional bulk and block deals."""

    _instance: Optional["BulkDealsService"] = None

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def get_instance(cls) -> "BulkDealsService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS bulk_block_deals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        deal_date TEXT NOT NULL,
                        deal_type TEXT NOT NULL,       -- 'BULK' or 'BLOCK'
                        symbol TEXT NOT NULL,
                        client_name TEXT NOT NULL,
                        buy_sell TEXT NOT NULL,        -- 'BUY' or 'SELL'
                        quantity INTEGER NOT NULL,
                        trade_price REAL NOT NULL,
                        total_value_cr REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(deal_date, deal_type, symbol, client_name, buy_sell, quantity)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_deals_sym_date 
                    ON bulk_block_deals (symbol, deal_date)
                """)
                conn.commit()
        except Exception as exc:
            logger.warning(f"Could not initialize bulk_block_deals table: {exc}")

    def get_symbol_deal_summary(self, symbol: str, lookback_days: int = 5) -> Dict[str, Any]:
        """
        Calculates net institutional cash flow from bulk/block deals in the last N days.
        Returns net cash in ₹ Crores and list of recent deal records.
        """
        upper_sym = symbol.upper().strip()
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT deal_date, deal_type, client_name, buy_sell, quantity, trade_price, total_value_cr
                    FROM bulk_block_deals
                    WHERE symbol = ? AND deal_date >= ?
                    ORDER BY deal_date DESC
                """, (upper_sym, cutoff_date))
                rows = cur.fetchall()

                deals = []
                net_cr = 0.0
                for r in rows:
                    is_buy = r[3].upper() == "BUY"
                    val_cr = float(r[6])
                    net_cr += val_cr if is_buy else -val_cr
                    deals.append({
                        "date": r[0],
                        "type": r[1],
                        "client": r[2],
                        "action": r[3],
                        "qty": r[4],
                        "price": r[5],
                        "value_cr": val_cr,
                    })

                return {
                    "has_deals": len(deals) > 0,
                    "net_cash_cr": round(net_cr, 2),
                    "deal_count": len(deals),
                    "deals": deals[:5],
                }
        except Exception as exc:
            logger.debug(f"Error querying bulk deals for {symbol}: {exc}")
            return {"has_deals": False, "net_cash_cr": 0.0, "deal_count": 0, "deals": []}

    def record_deal(
        self,
        deal_date: str,
        deal_type: str,
        symbol: str,
        client_name: str,
        buy_sell: str,
        quantity: int,
        trade_price: float,
    ) -> None:
        """Manually or programmatically insert a deal."""
        total_cr = round((quantity * trade_price) / 10_000_000.0, 2)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO bulk_block_deals
                    (deal_date, deal_type, symbol, client_name, buy_sell, quantity, trade_price, total_value_cr, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (deal_date, deal_type.upper(), symbol.upper(), client_name, buy_sell.upper(), quantity, trade_price, total_cr, now_iso))
                conn.commit()
        except Exception as exc:
            logger.warning(f"Could not record deal: {exc}")


def get_bulk_deals_service() -> BulkDealsService:
    return BulkDealsService.get_instance()
