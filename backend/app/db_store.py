# backend/app/db_store.py
import sqlite3
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Iterator
from logger import logger
from exceptions import DatabaseError

import os

BASE = Path(__file__).resolve().parent
_DB_OVERRIDE = os.getenv("PORTFOLIO_DB_PATH")
DB = Path(_DB_OVERRIDE) if _DB_OVERRIDE else (BASE / "portfolio.db")


def get_connection():
    """Get database connection with row factory and WAL mode"""
    try:
        conn = sqlite3.connect(DB, timeout=20.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}")
        raise DatabaseError(f"Failed to connect to database: {str(e)}")


@contextmanager
def managed_connection() -> Iterator[sqlite3.Connection]:
    """Context manager that guarantees sqlite connection cleanup."""
    conn = None
    try:
        conn = get_connection()
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                # Best-effort cleanup only; avoid masking primary exceptions.
                pass

def init_db():
    """Initialize database with all required tables"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
        
            # Users table
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE,
                    name TEXT,
                    broker_user_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            try:
                c.execute('ALTER TABLE users ADD COLUMN broker_user_id TEXT')
            except sqlite3.OperationalError:
                pass
        
            # Broker tokens table
            c.execute('''
                CREATE TABLE IF NOT EXISTS broker_tokens (
                    user_id TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
        
            # Sector Cache table
            c.execute('''
                CREATE TABLE IF NOT EXISTS sector_cache (
                    symbol TEXT PRIMARY KEY,
                    sector TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')

            # News Cache table
            c.execute('''
                CREATE TABLE IF NOT EXISTS news_cache (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    sentiment TEXT,
                    summary TEXT,
                    created_at TEXT NOT NULL
                )
            ''')

            # Stop Loss Cache table
            c.execute('''
                CREATE TABLE IF NOT EXISTS stop_loss_cache (
                    symbol TEXT PRIMARY KEY,
                    stop_loss_price REAL NOT NULL,
                    rationale TEXT,
                    risk_profile TEXT DEFAULT 'balanced',
                    updated_at TEXT NOT NULL
                )
            ''')

            # Pinned Stocks table
            c.execute('''
                CREATE TABLE IF NOT EXISTS pinned_stocks (
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, symbol),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')

            # Macro Regime Cache table
            c.execute('''
                CREATE TABLE IF NOT EXISTS macro_regime_cache (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    regime_data TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_update_at TEXT NOT NULL
                )
            ''')

            # Durable trading lifecycle state.  These records are intentionally
            # separate from shortlist caches so a restart cannot recreate a
            # position from an ephemeral signal.
            c.execute('''
                CREATE TABLE IF NOT EXISTS lifecycle_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    security_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    requested_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    filled_price REAL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS lifecycle_positions (
                    symbol TEXT PRIMARY KEY,
                    position_data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS lifecycle_queue (
                    symbol TEXT PRIMARY KEY,
                    candidate_data TEXT NOT NULL,
                    enqueued_at REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            try:
                c.execute("SELECT filled_quantity FROM lifecycle_orders LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(
                    "ALTER TABLE lifecycle_orders ADD COLUMN filled_quantity INTEGER NOT NULL DEFAULT 0"
                )

            # Create indexes
            c.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_news_symbol ON news_cache(symbol)')
        
            # Migration: Add risk_profile column to stop_loss_cache if it doesn't exist
            try:
                c.execute("SELECT risk_profile FROM stop_loss_cache LIMIT 1")
            except sqlite3.OperationalError:
                # Column doesn't exist, add it
                logger.info("Adding risk_profile column to stop_loss_cache table")
                c.execute("ALTER TABLE stop_loss_cache ADD COLUMN risk_profile TEXT DEFAULT 'balanced'")
                logger.info("risk_profile column added successfully")
            
            conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise DatabaseError(f"Failed to initialize database: {str(e)}")

# News Operations
def save_news(news_items: List[Dict[str, Any]]):
    """Save news items to cache"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
        
            for item in news_items:
                # Use link as unique ID part to avoid duplicates
                news_id = str(uuid.uuid5(uuid.NAMESPACE_URL, item['link']))
            
                c.execute('''
                    INSERT INTO news_cache
                    (id, symbol, title, link, published_at, sentiment, summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                    sentiment=excluded.sentiment,
                    summary=excluded.summary
                ''', (
                    news_id,
                    item['symbol'],
                    item['title'],
                    item['link'],
                    item['published_at'],
                    item.get('sentiment', 'Neutral'),
                    item.get('summary', ''),
                    now
                ))
        
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving news: {e}")

def get_news_for_symbols(symbols: List[str], limit: int = 50) -> List[Dict[str, Any]]:
    """Get news for specific symbols"""
    try:
        if not symbols:
            return []
            
        with managed_connection() as conn:
            c = conn.cursor()
        
            # Filter by date (last 7 days)
            cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        
            placeholders = ','.join(['?'] * len(symbols))
            query = f'''
                SELECT * FROM news_cache
                WHERE symbol IN ({placeholders})
                AND published_at > ?
                ORDER BY published_at DESC
                LIMIT ?
            '''
        
            c.execute(query, symbols + [cutoff, limit])
            rows = c.fetchall()
        
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting news: {e}")
        return []

def clear_old_news(days: int = 7):
    """Delete news older than X days"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        
            c.execute("DELETE FROM news_cache WHERE published_at < ? OR published_at NOT LIKE '20%'", (cutoff,))
            conn.commit()
    except Exception as e:
        logger.error(f"Error clearing old news: {e}")

# Sector Cache operations
def save_cached_sector(symbol: str, sector: str):
    """Save sector to cache"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
        
            c.execute('''
                REPLACE INTO sector_cache (symbol, sector, updated_at)
                VALUES (?, ?, ?)
            ''', (symbol, sector, now))
        
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving sector cache: {e}")
        # Don't raise error for cache operations, just log

def get_cached_sector(symbol: str) -> Optional[str]:
    """Get sector from cache"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT sector FROM sector_cache WHERE symbol = ?', (symbol,))
            row = c.fetchone()
        
        return row['sector'] if row else None
    except Exception as e:
        logger.error(f"Error getting sector cache: {e}")
        return None

def get_all_cached_sectors() -> Dict[str, str]:
    """Get all cached sectors"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT symbol, sector FROM sector_cache')
            rows = c.fetchall()
        
        return {row['symbol']: row['sector'] for row in rows}
    except Exception as e:
        logger.error(f"Error getting all cached sectors: {e}")
        return {}


# User operations
def create_user(user_id: str, email: Optional[str] = None, name: Optional[str] = None, broker_user_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a new user"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
        
            c.execute('''
                INSERT INTO users (user_id, email, name, broker_user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, email, name, broker_user_id, now, now))
        
            conn.commit()
        
        logger.info(f"User created: {user_id}")
        return {"user_id": user_id, "email": email, "name": name, "created_at": now}
    except sqlite3.IntegrityError:
        raise DatabaseError(f"User {user_id} already exists")
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise DatabaseError(f"Failed to create user: {str(e)}")

def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get user by ID"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            row = c.fetchone()
        
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Error getting user: {e}")
        raise DatabaseError(f"Failed to get user: {str(e)}")

def get_all_users() -> List[Dict[str, Any]]:
    """Get all users"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM users')
            rows = c.fetchall()
        
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

def update_user(user_id: str, **kwargs) -> bool:
    """Update user fields"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
        
            # Build update query dynamically
            fields = []
            values = []
            for key, value in kwargs.items():
                if key in ['email', 'name', 'broker_user_id', 'is_active']:
                    fields.append(f"{key} = ?")
                    values.append(value)
        
            if not fields:
                return False
        
            fields.append("updated_at = ?")
            values.append(datetime.utcnow().isoformat())
            values.append(user_id)
        
            query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"
            c.execute(query, values)
        
            conn.commit()
            affected = c.rowcount
        
        logger.info(f"User updated: {user_id}")
        return affected > 0
    except Exception as e:
        logger.error(f"Error updating user: {e}")
        raise DatabaseError(f"Failed to update user: {str(e)}")

# Token operations
def save_token(user_id: str, access_token: str, refresh_token: Optional[str] = None, expires_at: Optional[str] = None):
    """Save broker token"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
        
            c.execute('''
                REPLACE INTO broker_tokens (user_id, access_token, refresh_token, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, access_token, refresh_token, expires_at, now))
        
            conn.commit()
        logger.info(f"Token saved for user: {user_id}")
    except Exception as e:
        logger.error(f"Error saving token: {e}")
        raise DatabaseError(f"Failed to save token: {str(e)}")

def load_token(user_id: str) -> Optional[Dict[str, Any]]:
    """Load broker token"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM broker_tokens WHERE user_id = ?', (user_id,))
            row = c.fetchone()
        
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Error loading token: {e}")
        raise DatabaseError(f"Failed to load token: {str(e)}")

def load_latest_token() -> Optional[Dict[str, Any]]:
    """Load the most recently saved broker token across users."""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM broker_tokens ORDER BY created_at DESC LIMIT 1')
            row = c.fetchone()

        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Error loading latest token: {e}")
        raise DatabaseError(f"Failed to load latest token: {str(e)}")

def delete_token(user_id: str) -> bool:
    """Delete broker token"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM broker_tokens WHERE user_id = ?', (user_id,))
            conn.commit()
            affected = c.rowcount
        
        logger.info(f"Token deleted for user: {user_id}")
        return affected > 0
    except Exception as e:
        logger.error(f"Error deleting token: {e}")
        raise DatabaseError(f"Failed to delete token: {str(e)}")

# Pinned Stocks Operations
def pin_stock(user_id: str, symbol: str) -> bool:
    """Pin a stock for the user"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
            c.execute('INSERT OR REPLACE INTO pinned_stocks (user_id, symbol, added_at) VALUES (?, ?, ?)', (user_id, symbol, now))
            conn.commit()
        logger.info(f"Stock {symbol} pinned for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error pinning stock: {e}")
        return False

def unpin_stock(user_id: str, symbol: str) -> bool:
    """Unpin a stock for the user"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM pinned_stocks WHERE user_id = ? AND symbol = ?', (user_id, symbol))
            conn.commit()
            affected = c.rowcount
        logger.info(f"Stock {symbol} unpinned for user {user_id}")
        return affected > 0
    except Exception as e:
        logger.error(f"Error unpinning stock: {e}")
        return False

def get_pinned_stocks(user_id: str) -> List[str]:
    """Get all pinned stocks for the user"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT symbol FROM pinned_stocks WHERE user_id = ? ORDER BY added_at ASC', (user_id,))
            rows = c.fetchall()
        return [row['symbol'] for row in rows]
    except Exception as e:
        logger.error(f"Error getting pinned stocks: {e}")
        return []

# Macro Regime Operations
def save_macro_regime(regime_data: Dict[str, Any], next_update_at: datetime):
    """Save macro regime results"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            now = datetime.utcnow().isoformat()
            next_update = next_update_at.isoformat()
            c.execute('''
                INSERT INTO macro_regime_cache (id, regime_data, updated_at, next_update_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    regime_data = excluded.regime_data,
                    updated_at = excluded.updated_at,
                    next_update_at = excluded.next_update_at
            ''', (json.dumps(regime_data), now, next_update))
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving macro regime: {e}")

def get_macro_regime() -> Optional[Dict[str, Any]]:
    """Get current macro regime results"""
    try:
        with managed_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM macro_regime_cache WHERE id = 1')
            row = c.fetchone()
        if row:
            data = dict(row)
            data['regime_data'] = json.loads(data['regime_data'])
            return data
        return None
    except Exception as e:
        logger.error(f"Error getting macro regime: {e}")
        return None


def save_lifecycle_order(
    order_id: str,
    symbol: str,
    security_id: str,
    action: str,
    quantity: int,
    requested_price: float,
    status: str,
    filled_price: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
    filled_quantity: Optional[int] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    with managed_connection() as conn:
        conn.execute(
            '''
            INSERT INTO lifecycle_orders
            (order_id, symbol, security_id, action, quantity, filled_quantity,
             requested_price, status, filled_price, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
              filled_quantity=excluded.filled_quantity, status=excluded.status,
              filled_price=excluded.filled_price,
              payload=excluded.payload, updated_at=excluded.updated_at
            ''',
            (order_id, symbol, security_id, action, int(quantity),
             int(filled_quantity if filled_quantity is not None else (
                 quantity if status == "FILLED" else 0
             )), float(requested_price), status, filled_price,
             json.dumps(payload or {}), now, now),
        )
        conn.commit()


def get_lifecycle_order(order_id: str) -> Optional[Dict[str, Any]]:
    with managed_connection() as conn:
        row = conn.execute(
            "SELECT * FROM lifecycle_orders WHERE order_id = ?", (str(order_id),)
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result.get("payload") or "{}")
    return result


def get_lifecycle_orders(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM lifecycle_orders"
    params: List[Any] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query += f" WHERE status IN ({placeholders})"
        params.extend(statuses)
    query += " ORDER BY created_at"
    with managed_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.get("payload") or "{}")
        result.append(item)
    return result


def update_lifecycle_position_status(symbol: str, status: str) -> None:
    with managed_connection() as conn:
        conn.execute(
            "UPDATE lifecycle_positions SET status = ?, updated_at = ? WHERE symbol = ?",
            (status, datetime.utcnow().isoformat(), str(symbol)),
        )
        conn.commit()


def save_lifecycle_position(position: Dict[str, Any], status: str = "ACTIVE") -> None:
    now = datetime.utcnow().isoformat()
    symbol = str(position["symbol"])
    with managed_connection() as conn:
        conn.execute(
            '''
            INSERT INTO lifecycle_positions
            (symbol, position_data, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              position_data=excluded.position_data, status=excluded.status,
              updated_at=excluded.updated_at
            ''',
            (symbol, json.dumps(position), status, now, now),
        )
        conn.commit()


def delete_lifecycle_position(symbol: str) -> None:
    with managed_connection() as conn:
        conn.execute("DELETE FROM lifecycle_positions WHERE symbol = ?", (symbol,))
        conn.commit()


def get_lifecycle_positions() -> List[Dict[str, Any]]:
    with managed_connection() as conn:
        rows = conn.execute(
            "SELECT position_data FROM lifecycle_positions WHERE status IN ('ACTIVE', 'PROTECTION_DEGRADED', 'EXIT_PENDING', 'EXIT_FAILED') ORDER BY created_at"
        ).fetchall()
    return [json.loads(row["position_data"]) for row in rows]


def save_lifecycle_queue(candidates: List[Dict[str, Any]]) -> None:
    now = datetime.utcnow().isoformat()
    with managed_connection() as conn:
        conn.execute("DELETE FROM lifecycle_queue")
        conn.executemany(
            "INSERT INTO lifecycle_queue (symbol, candidate_data, enqueued_at, updated_at) VALUES (?, ?, ?, ?)",
            [
                (str(candidate["symbol"]), json.dumps(candidate),
                 float(candidate.get("enqueued_at", 0.0)), now)
                for candidate in candidates
            ],
        )
        conn.commit()


def get_lifecycle_queue() -> List[Dict[str, Any]]:
    with managed_connection() as conn:
        rows = conn.execute(
            "SELECT candidate_data FROM lifecycle_queue ORDER BY enqueued_at, rowid"
        ).fetchall()
    return [json.loads(row["candidate_data"]) for row in rows]

# Initialize database on module import
init_db()
