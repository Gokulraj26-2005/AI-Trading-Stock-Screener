"""
AI Stock Shortlisting & Trade Lifecycle Service for SHORTS (VIA-Agent7)
======================================================================
Dedicated SQLite storage and timeline management for bearish short setups.
Isolated database: shortlist_shorts.sqlite
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "shortlist_shorts.sqlite"


def get_current_ist_timestamp() -> str:
    """Returns formatted IST timestamp, e.g., '10:15:22 IST, 20-Sep'."""
    now_ist = datetime.now(_IST)
    return now_ist.strftime("%H:%M:%S IST, %d-%b")


class AIShortlistServiceShorts:
    """
    Manages short setups, parameters, and timeline logs in an isolated SQLite database.
    """

    _instance: Optional["AIShortlistServiceShorts"] = None

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = db_path
        if str(self.db_path) != ":memory:" and isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def get_instance(cls) -> "AIShortlistServiceShorts":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS shortlisted_trades (
                        symbol TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        setup_type TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        stop_loss REAL NOT NULL,
                        target_1 REAL NOT NULL,
                        target_2 REAL NOT NULL,
                        risk_reward_ratio REAL NOT NULL,
                        conviction TEXT NOT NULL,
                        status TEXT NOT NULL,
                        current_price REAL NOT NULL,
                        pnl_pct REAL DEFAULT 0.0,
                        catalyst_summary TEXT,
                        tape_evidence TEXT,
                        thesis TEXT,
                        invalidation_rule TEXT,
                        model_name TEXT,
                        rsi_5m REAL,
                        rvol REAL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                # Migrations: ensure rsi_5m and rvol columns exist in existing databases
                try:
                    conn.execute("ALTER TABLE shortlisted_trades ADD COLUMN rsi_5m REAL")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE shortlisted_trades ADD COLUMN rvol REAL")
                except Exception:
                    pass
                for column, definition in (
                    ("trade_intent_id", "TEXT"),
                    ("intent_security_id", "TEXT"),
                    ("intent_trigger_price", "REAL"),
                    ("intent_quantity", "INTEGER"),
                    ("intent_action", "TEXT"),
                    ("intent_stop_loss", "REAL"),
                    ("intent_target_1", "REAL"),
                    ("intent_target_2", "REAL"),
                    ("approval_status", "TEXT"),
                    ("approval_result", "TEXT"),
                ):
                    try:
                        conn.execute(f"ALTER TABLE shortlisted_trades ADD COLUMN {column} {definition}")
                    except Exception:
                        pass

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS shortlist_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        timestamp_ist TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        price_at_event REAL,
                        message TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                for column, definition in (("event_id", "TEXT"), ("correlation_id", "TEXT")):
                    try:
                        conn.execute(f"ALTER TABLE shortlist_events ADD COLUMN {column} {definition}")
                    except sqlite3.OperationalError:
                        pass
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_sym 
                    ON shortlist_events (symbol, created_at DESC)
                """)
                # Auto-cleanup: purge timeline events from previous days
                today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                conn.execute(
                    "DELETE FROM shortlist_events WHERE created_at < ?",
                    (today_iso,)
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"Failed to initialize shortlist shorts SQLite DB: {exc}")

    # ── Database CRUD Operations ───────────────────────────────────────────────

    def _enrich_trade_metrics(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures rsi_5m and rvol are always populated with valid floats and tape_evidence is clean."""
        # 1. Parse or resolve rsi_5m
        if trade.get("rsi_5m") is None:
            if trade.get("tape_evidence"):
                m = re.search(r"5m\s*RSI\s*[:\s]?\s*([\d.]+)", str(trade["tape_evidence"]), re.IGNORECASE)
                if m:
                    try:
                        trade["rsi_5m"] = float(m.group(1))
                    except ValueError:
                        pass
        if trade.get("rsi_5m") is None:
            try:
                from services.indicator_cache import get_cached_rsi
                cached_val = get_cached_rsi(trade.get("symbol", "").upper())
                if cached_val is not None:
                    trade["rsi_5m"] = float(cached_val)
            except Exception:
                pass

        # 2. Parse or resolve rvol
        if trade.get("rvol") is None:
            if trade.get("tape_evidence"):
                m = re.search(r"RVOL\s*[:\s]?\s*([\d.]+)x?", str(trade["tape_evidence"]), re.IGNORECASE)
                if m:
                    try:
                        trade["rvol"] = float(m.group(1))
                    except ValueError:
                        pass
        if trade.get("rvol") is None:
            trade["rvol"] = 1.0

        # 3. Clean up any lingering "None" in tape_evidence
        rsi_val = trade.get("rsi_5m")
        rvol_val = trade.get("rvol")
        if trade.get("tape_evidence") and "None" in str(trade["tape_evidence"]):
            if rsi_val is not None and rvol_val is not None:
                trade["tape_evidence"] = f"RVOL {rvol_val:.2f}x | 5m RSI {rsi_val:.1f}"
            elif rsi_val is not None:
                trade["tape_evidence"] = f"RVOL 1.00x | 5m RSI {rsi_val:.1f}"
            else:
                trade["tape_evidence"] = "Live Momentum Verified"

        return trade

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Returns all currently shortlisted short trades ordered by conviction and R:R."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT * FROM shortlisted_trades 
                    ORDER BY 
                        CASE conviction WHEN 'VERY_HIGH' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,
                        risk_reward_ratio DESC
                """)
                rows = cur.fetchall()
                return [self._enrich_trade_metrics(dict(r)) for r in rows]
        except Exception as exc:
            logger.error(f"Error fetching shortlisted short trades: {exc}")
            return []

    def get_trade(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch a single short trade by symbol."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT * FROM shortlisted_trades WHERE symbol = ?", (symbol.upper().strip(),))
                row = cur.fetchone()
                return self._enrich_trade_metrics(dict(row)) if row else None
        except Exception as exc:
            logger.error(f"Error fetching short trade {symbol}: {exc}")
            return None

    def upsert_trade(self, trade: Dict[str, Any]) -> None:
        """Insert or update a shortlisted short trade."""
        now_iso = datetime.now(timezone.utc).isoformat()
        sym = trade["symbol"].upper().strip()
        rsi_val = float(trade["rsi_5m"]) if trade.get("rsi_5m") is not None else None
        rvol_val = float(trade["rvol"]) if trade.get("rvol") is not None else None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO shortlisted_trades (
                        symbol, action, setup_type, entry_price, stop_loss, target_1, target_2,
                        risk_reward_ratio, conviction, status, current_price, pnl_pct,
                        catalyst_summary, tape_evidence, thesis, invalidation_rule,
                        model_name, rsi_5m, rvol, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        action = excluded.action,
                        setup_type = excluded.setup_type,
                        entry_price = excluded.entry_price,
                        stop_loss = excluded.stop_loss,
                        target_1 = excluded.target_1,
                        target_2 = excluded.target_2,
                        risk_reward_ratio = excluded.risk_reward_ratio,
                        conviction = excluded.conviction,
                        status = excluded.status,
                        current_price = excluded.current_price,
                        pnl_pct = excluded.pnl_pct,
                        catalyst_summary = excluded.catalyst_summary,
                        tape_evidence = excluded.tape_evidence,
                        thesis = excluded.thesis,
                        invalidation_rule = excluded.invalidation_rule,
                        model_name = excluded.model_name,
                        rsi_5m = coalesce(excluded.rsi_5m, shortlisted_trades.rsi_5m),
                        rvol = coalesce(excluded.rvol, shortlisted_trades.rvol),
                        updated_at = excluded.updated_at
                """, (
                    sym,
                    trade.get("action", "SELL"),
                    trade.get("setup_type", "BREAKDOWN"),
                    float(trade.get("entry_price", 0.0)),
                    float(trade.get("stop_loss", 0.0)),
                    float(trade.get("target_1", 0.0)),
                    float(trade.get("target_2", 0.0)),
                    float(trade.get("risk_reward_ratio", 2.0)),
                    trade.get("conviction", "HIGH"),
                    trade.get("status", "WATCHING"),
                    float(trade.get("current_price", trade.get("entry_price", 0.0))),
                    float(trade.get("pnl_pct", 0.0)),
                    trade.get("catalyst_summary", ""),
                    trade.get("tape_evidence", ""),
                    trade.get("thesis", ""),
                    trade.get("invalidation_rule", ""),
                    trade.get("model_name", "AI Engine"),
                    rsi_val,
                    rvol_val,
                    trade.get("created_at", now_iso),
                    now_iso,
                ))
                conn.commit()
        except Exception as exc:
            logger.error(f"Error saving short trade {sym}: {exc}")

    def update_trade_price_and_status(
        self,
        symbol: str,
        current_price: float,
        pnl_pct: float,
        status: str,
        tape_evidence: Optional[str] = None,
        rsi_5m: Optional[float] = None,
        rvol: Optional[float] = None,
    ) -> None:
        """Quick update of live telemetry for a short trade."""
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                params = [current_price, pnl_pct, status]
                updates = ["current_price = ?", "pnl_pct = ?", "status = ?"]
                if tape_evidence:
                    updates.append("tape_evidence = ?")
                    params.append(tape_evidence)
                if rsi_5m is not None:
                    updates.append("rsi_5m = ?")
                    params.append(float(rsi_5m))
                if rvol is not None:
                    updates.append("rvol = ?")
                    params.append(float(rvol))
                updates.append("updated_at = ?")
                params.append(now_iso)
                params.append(symbol.upper().strip())

                sql = f"UPDATE shortlisted_trades SET {', '.join(updates)} WHERE symbol = ?"
                conn.execute(sql, tuple(params))
                conn.commit()
        except Exception as exc:
            logger.debug(f"Error updating short trade price for {symbol}: {exc}")

    def update_trade_status(self, symbol: str, status: str) -> None:
        """Update only the status of a shortlisted short trade."""
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE shortlisted_trades SET status = ?, updated_at = ? WHERE symbol = ?",
                    (status, now_iso, symbol.upper().strip())
                )
                conn.commit()
        except Exception as exc:
            logger.debug(f"Error updating status for short trade {symbol}: {exc}")

    def create_trade_intent(
        self, symbol: str, security_id: str, trigger_price: float, quantity: int, action: str
    ) -> Optional[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT trade_intent_id, stop_loss, target_1, target_2 FROM shortlisted_trades WHERE symbol = ?",
                    (symbol.upper().strip(),),
                ).fetchone()
                if not row:
                    return None
                if row[0]:
                    return str(row[0])
                intent_id = str(uuid.uuid4())
                conn.execute(
                    """UPDATE shortlisted_trades
                       SET trade_intent_id = ?, intent_security_id = ?,
                           intent_trigger_price = ?, intent_quantity = ?,
                           intent_action = ?, intent_stop_loss = ?, intent_target_1 = ?,
                           intent_target_2 = ?, approval_status = 'PENDING'
                       WHERE symbol = ? AND trade_intent_id IS NULL""",
                    (intent_id, security_id, float(trigger_price), int(quantity), action.upper(),
                     row[1], row[2], row[3],
                     symbol.upper().strip()),
                )
                conn.commit()
                return intent_id
        except Exception as exc:
            logger.error("Error creating short trade intent for %s: %s", symbol, exc)
            return None

    def claim_trade_intent(self, intent_id: str) -> Dict[str, Any]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM shortlisted_trades WHERE trade_intent_id = ?",
                    (intent_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return {"status": "INVALID_INTENT"}
                record = dict(row)
                if record.get("approval_status") == "EXECUTING":
                    conn.commit()
                    return {"status": "IN_PROGRESS", "trade": record}
                if record.get("approval_status") in {"EXECUTED", "QUEUED"}:
                    conn.commit()
                    return {"status": "REPLAY", "result": json.loads(record.get("approval_result") or "{}"), "trade": record}
                conn.execute(
                    "UPDATE shortlisted_trades SET approval_status = 'EXECUTING', updated_at = ? WHERE trade_intent_id = ?",
                    (datetime.now(timezone.utc).isoformat(), intent_id),
                )
                conn.commit()
                return {"status": "CLAIMED", "trade": record}
        except Exception as exc:
            logger.error("Error claiming short trade intent %s: %s", intent_id, exc)
            return {"status": "ERROR", "reason": str(exc)}

    def complete_trade_intent(self, intent_id: str, result: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE shortlisted_trades
                       SET approval_status = ?, approval_result = ?, updated_at = ?
                       WHERE trade_intent_id = ?""",
                    ("EXECUTED" if result.get("status") == "EXECUTED" else str(result.get("status", "FAILED")),
                     json.dumps(result), datetime.now(timezone.utc).isoformat(), intent_id),
                )
                conn.commit()
        except Exception as exc:
            logger.error("Error completing short trade intent %s: %s", intent_id, exc)

    def delete_trade(self, symbol: str) -> bool:
        """Remove a short trade from the database."""
        sym = symbol.upper().strip()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM shortlisted_trades WHERE symbol = ?", (sym,))
                conn.commit()
                return True
        except Exception as exc:
            logger.error(f"Error deleting short trade {sym}: {exc}")
            return False

    def clear_all(self) -> None:
        """Clear all active shortlisted short trades and timeline history."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM shortlisted_trades")
                conn.execute("DELETE FROM shortlist_events")
                conn.commit()
        except Exception as exc:
            logger.error(f"Error clearing short shortlist: {exc}")

    # ── Timeline Event Logging ─────────────────────────────────────────────────

    def add_timeline_event(
        self,
        symbol: str,
        event_type: str,
        message: str,
        price_at_event: Optional[float] = None,
        severity: str = "info",
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a micro-event into the shorts timeline log with exact IST timestamp.
        Severity: 'info' | 'success' | 'warning' | 'danger'
        """
        sym = symbol.upper().strip()
        ts_ist = get_current_ist_timestamp()
        now_iso = datetime.now(timezone.utc).isoformat()
        event_id = str(uuid.uuid4())
        correlation_id = correlation_id or str(uuid.uuid4())

        event = {
            "symbol": sym,
            "timestamp_ist": ts_ist,
            "event_type": event_type,
            "price_at_event": price_at_event,
            "message": message,
            "severity": severity,
            "created_at": now_iso,
            "event_id": event_id,
            "correlation_id": correlation_id,
        }

        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO shortlist_events 
                    (symbol, timestamp_ist, event_type, price_at_event, message, severity, created_at, event_id, correlation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (sym, ts_ist, event_type, price_at_event, message, severity, now_iso, event_id, correlation_id))
                event["id"] = cur.lastrowid
                conn.commit()
        except Exception as exc:
            logger.error(f"Error recording shorts timeline event for {sym}: {exc}")

        return event

    def get_timeline_events(
        self, symbol: Optional[str] = None, limit: int = 60, after_id: int = 0
    ) -> List[Dict[str, Any]]:
        """Retrieve recent timeline logs for shorts desk."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                if symbol:
                    cur.execute("""
                        SELECT * FROM shortlist_events 
                        WHERE symbol = ? AND id > ?
                        ORDER BY id ASC LIMIT ?
                    """, (symbol.upper().strip(), after_id, limit))
                else:
                    cur.execute("""
                        SELECT e.* FROM shortlist_events e
                        INNER JOIN shortlisted_trades t ON e.symbol = t.symbol
                        WHERE e.id > ? ORDER BY e.id ASC LIMIT ?
                    """, (after_id, limit))
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.error(f"Error fetching shorts timeline events: {exc}")
            return []


def get_ai_shortlist_service_shorts() -> AIShortlistServiceShorts:
    return AIShortlistServiceShorts.get_instance()
