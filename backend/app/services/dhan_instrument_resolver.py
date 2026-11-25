"""
Dhan Instrument Resolver
========================
Downloads and caches Dhan's daily instrument CSV, providing fast
symbol → security_id lookups for WebSocket subscriptions.

Supports auto-resolution for NIFTY 100+ universe and beyond.
"""
import csv
import io
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Dhan publishes a daily CSV of all tradeable instruments
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Import canonical index symbols from config
from config.symbols import INDEX_SYMBOLS

# Cache settings
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = CACHE_DIR / "dhan_instruments.csv"
CACHE_MAX_AGE_HOURS = 18  # Refresh if older than 18 hours (covers overnight)

# Exchange segment codes used by Dhan
EXCHANGE_NSE_EQ = "NSE_EQ"
EXCHANGE_NSE_FNO = "NSE_FNO"
EXCHANGE_BSE_EQ = "BSE_EQ"
EXCHANGE_IDX = "IDX_I"

# NIFTY 100 constituent symbols (expanded from NIFTY 50)
# This is the baseline universe — the resolver will also accept any
# additional symbols passed at runtime.
NIFTY_100_SYMBOLS: List[str] = [
    # NIFTY 50
    "RELIANCE", "HDFCBANK", "BHARTIARTL", "SBIN", "ICICIBANK", "TCS", "INFY",
    "BAJFINANCE", "HINDUNILVR", "LT", "SUNPHARMA", "MARUTI", "HCLTECH", "ITC",
    "AXISBANK", "ADANIPORTS", "NTPC", "M&M", "KOTAKBANK", "TITAN", "BEL",
    "ONGC", "JSWSTEEL", "ADANIENT", "COALINDIA", "NESTLEIND", "SHRIRAMFIN",
    "WIPRO", "ETERNAL", "JIOFIN", "HYUNDAI", "TECHM", "APOLLOHOSP", "MAXHEALTH",
    "TRENT", "INDIGO", "POWERGRID", "HINDALCO", "TATASTEEL", "ASIANPAINT",
    "TATACONSUM", "CIPLA", "DRREDDY", "SBILIFE", "EICHERMOT", "BAJAJFINSV",
    "ULTRACEMCO", "HDFCLIFE", "TVSMOTOR", "GRASIM",
    # NIFTY NEXT 50 (additional ~50)
    "ABB", "AMBUJACEM", "BAJAJ-AUTO", "BANKBARODA", "BERGEPAINT",
    "BOSCHLTD", "BRITANNIA", "CANBK", "CHOLAFIN", "COLPAL",
    "DABUR", "DELHIVERY", "DIVISLAB", "DLF", "GAIL",
    "GODREJCP", "HAVELLS", "HEROMOTOCO", "HINDPETRO", "ICICIPRULI",
    "IDEA", "IDFCFIRSTB", "INDUSTOWER", "IOC", "IRCTC",
    "JINDALSTEL", "JSWENERGY", "LICI", "LODHA", "LUPIN",
    "MARICO", "MOTHERSON", "NHPC", "NAUKRI", "OBEROIRLTY",
    "OFSS", "PAGEIND", "PAYTM", "PEL", "PERSISTENT",
    "PIDILITIND", "PNB", "POLYCAB", "SRF", "TATAELXSI",
    "TATAPOWER", "TORNTPHARM", "TTML", "UNITDSPR", "VBL",
    "VEDL", "ZYDUSLIFE",
]

# Index symbols tracked for regime detection
# Corporate symbol aliases for historical/renamed NSE constituents
SYMBOL_ALIASES: Dict[str, str] = {
    "PEL": "PIRAMALFIN",
    "ZOMATO": "ETERNAL",
    "TATAMOTORS": "TMCV",
    "M&M": "MM",
    "L&T": "LT",
    "GALLANT": "GALLANTT",
    "GALLANTTISPAT": "GALLANTT",
    "ICICIPRULI": "ICICILIFE",
    "ICICIPRU": "ICICILIFE",
    "HEG": "HEGAM",
}


def resolve_canonical_symbol(symbol: str) -> str:
    """Returns the canonical trading symbol if an alias or legacy ticker is used."""
    upper = symbol.strip().upper()
    if upper in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[upper]
    if _resolver and _resolver._loaded:
        if upper in _resolver._nse_eq_map:
            return upper
        candidates = [
            k for k in _resolver._nse_eq_map.keys()
            if k.startswith(upper) and " " not in k and not k.endswith("-EQ")
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            candidates.sort(key=lambda c: len(c))
            if len(candidates[0]) <= len(upper) + 2:
                return candidates[0]
    return upper


class DhanInstrumentResolver:
    """
    Resolves trading symbols to Dhan security IDs using the scrip master CSV.

    Usage:
        resolver = DhanInstrumentResolver()
    Downloads and caches Dhan's scrip master CSV to provide fast in-memory
    resolution from trading symbol (e.g. 'RELIANCE') to Dhan security ID
    (e.g. '2885') and exchange segment.
    """

    def __init__(self) -> None:
        self._nse_eq_map: Dict[str, Tuple[str, str, str]] = {}  # symbol -> (security_id, segment, isin)
        self._id_to_symbol: Dict[str, str] = {}  # equity security_id -> symbol (e.g. "13" -> "ABB")
        self._id_to_name: Dict[str, str] = {}  # equity security_id -> company name (e.g. "Tata Motors")
        self._index_map: Dict[str, str] = {}  # index_name -> security_id (e.g. "NIFTY 50" -> "13")
        self._index_id_to_symbol: Dict[str, str] = {}  # index security_id -> index_name (e.g. "13" -> "NIFTY 50")
        self._loaded = False
        self._load_time: float = 0.0

    def _is_cache_fresh(self) -> bool:
        """Check if the cached CSV exists and is less than CACHE_MAX_AGE_HOURS old."""
        if not CACHE_FILE.exists():
            return False
        mtime = CACHE_FILE.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600.0
        return age_hours < CACHE_MAX_AGE_HOURS

    def _download_csv(self) -> str:
        """Download the latest scrip master CSV from Dhan."""
        logger.info("Downloading Dhan scrip master CSV from %s", DHAN_SCRIP_MASTER_URL)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(DHAN_SCRIP_MASTER_URL, timeout=30) as response:
                content = response.read().decode("utf-8")
            CACHE_FILE.write_text(content, encoding="utf-8")
            logger.info("Cached Dhan instrument CSV (%d bytes) to %s", len(content), CACHE_FILE)
            return content
        except Exception as exc:
            logger.error("Failed to download Dhan scrip master: %s", exc)
            if CACHE_FILE.exists():
                logger.warning("Falling back to stale cached CSV from %s", CACHE_FILE)
                return CACHE_FILE.read_text(encoding="utf-8")
            raise RuntimeError(
                f"Cannot download Dhan instrument CSV and no cache exists: {exc}"
            ) from exc

    def load(self, force_refresh: bool = False) -> None:
        """Load the instrument CSV (from cache or download)."""
        if self._loaded and not force_refresh:
            return

        if force_refresh or not self._is_cache_fresh():
            csv_text = self._download_csv()
        else:
            csv_text = CACHE_FILE.read_text(encoding="utf-8")
            logger.info("Using cached Dhan instrument CSV from %s", CACHE_FILE)

        self._parse_csv(csv_text)
        self._loaded = True
        self._load_time = time.time()

    def _parse_csv(self, csv_text: str) -> None:
        """Parse the scrip master CSV and build lookup maps."""
        self._nse_eq_map.clear()
        self._id_to_symbol.clear()
        self._id_to_name.clear()
        self._index_map.clear()

        reader = csv.DictReader(io.StringIO(csv_text))
        nse_eq_count = 0
        idx_count = 0

        for row in reader:
            try:
                exchange_segment = (row.get("SEM_EXM_EXCH_ID", "") or "").strip()
                segment = (row.get("SEM_SEGMENT", "") or "").strip()
                security_id = (row.get("SEM_SMST_SECURITY_ID", "") or "").strip()
                trading_symbol = (row.get("SEM_TRADING_SYMBOL", "") or "").strip()
                custom_symbol = (row.get("SEM_CUSTOM_SYMBOL", "") or "").strip()
                isin = (row.get("SEM_ISIN", "") or "").strip()
                instrument_type = (row.get("SEM_INSTRUMENT_NAME", "") or "").strip()

                if not security_id:
                    continue

                # NSE Equity: segments "E" (Equity), "EQ", or "C" (if instrument is EQUITY, excluding OPTCUR/FUTCUR)
                if exchange_segment == "NSE" and (segment in ("E", "EQ") or (segment == "C" and instrument_type == "EQUITY")):
                    clean_symbol = trading_symbol
                    if clean_symbol.endswith("-EQ"):
                        clean_symbol = clean_symbol[:-3]
                    clean_symbol = clean_symbol.upper()

                    # Index both clean symbol and trading symbol
                    self._nse_eq_map[clean_symbol] = (
                        security_id,
                        EXCHANGE_NSE_EQ,
                        isin,
                    )
                    self._nse_eq_map[trading_symbol.upper()] = (
                        security_id,
                        EXCHANGE_NSE_EQ,
                        isin,
                    )
                    # If custom_symbol is provided and doesn't conflict, also map it
                    if custom_symbol:
                        cs_clean = custom_symbol[:-3] if custom_symbol.endswith("-EQ") else custom_symbol
                        self._nse_eq_map[cs_clean.upper()] = (
                            security_id,
                            EXCHANGE_NSE_EQ,
                            isin,
                        )

                    self._id_to_symbol[security_id] = clean_symbol
                    company_name = custom_symbol or row.get("SM_SYMBOL_NAME", "") or clean_symbol
                    if security_id not in self._id_to_name or len(company_name) > len(self._id_to_name.get(security_id, "")):
                        self._id_to_name[security_id] = company_name
                    nse_eq_count += 1

                # Index instruments (NIFTY 50, INDIA VIX)
                if instrument_type in ("INDEX", "VOLATILITY INDEX") or segment == "I":
                    ts_u = trading_symbol.upper()
                    cs_u = custom_symbol.upper()
                    # Exact matches take precedence (exclude 500, equal weight, etc.)
                    if (ts_u == "NIFTY" or cs_u == "NIFTY 50" or ts_u == "NIFTY 50") and "500" not in ts_u and "500" not in cs_u:
                        self._index_map["NIFTY 50"] = security_id
                        self._index_map["NIFTY"] = security_id
                        self._index_id_to_symbol[security_id] = "NIFTY 50"
                        idx_count += 1
                    elif ts_u == "INDIA VIX" or cs_u == "INDIA VIX":
                        self._index_map["INDIA VIX"] = security_id
                        self._index_id_to_symbol[security_id] = "INDIA VIX"
                        idx_count += 1

            except Exception as exc:
                # Skip malformed rows silently
                continue

        # Ensure canonical index security IDs are present in index map
        if "NIFTY 50" not in self._index_map:
            self._index_map["NIFTY 50"] = "13"
            self._index_map["NIFTY"] = "13"
        self._index_id_to_symbol["13"] = "NIFTY 50"

        if "INDIA VIX" not in self._index_map:
            self._index_map["INDIA VIX"] = "21"
        self._index_id_to_symbol["21"] = "INDIA VIX"
        self._index_id_to_symbol["26017"] = "INDIA VIX"

        logger.info(
            "Parsed Dhan instrument CSV: %d NSE EQ instruments, %d index instruments",
            nse_eq_count, idx_count,
        )

    def get_security_id(self, symbol: str) -> Optional[str]:
        """Get the Dhan security_id for a given trading symbol."""
        if not self._loaded:
            self.load()
        sym_upper = symbol.strip().upper()
        entry = self._nse_eq_map.get(sym_upper)
        if entry:
            return entry[0]
        # Try index map
        if sym_upper in self._index_map:
            return self._index_map[sym_upper]
        # Try alias
        alias = SYMBOL_ALIASES.get(sym_upper)
        if alias:
            alias_entry = self._nse_eq_map.get(alias)
            if alias_entry:
                return alias_entry[0]
            if alias in self._index_map:
                return self._index_map[alias]
        # Try candidates matching trading symbol (excluding custom symbols with spaces or suffixes)
        candidates = [
            k for k in self._nse_eq_map.keys()
            if k.startswith(sym_upper) and " " not in k and not k.endswith("-EQ")
        ]
        if len(candidates) == 1:
            return self._nse_eq_map[candidates[0]][0]
        if len(candidates) > 1:
            candidates.sort(key=lambda c: len(c))
            if len(candidates[0]) <= len(sym_upper) + 2:
                return self._nse_eq_map[candidates[0]][0]
        return None

    def get_symbol(self, security_id: str, exchange_seg: Optional[Any] = None) -> Optional[str]:
        """Reverse-lookup: security_id → trading symbol, segment-aware."""
        if not self._loaded:
            self.load()
        sid = str(security_id)
        if exchange_seg in (0, "0", EXCHANGE_IDX, "IDX"):
            return self._index_id_to_symbol.get(sid, "NIFTY 50" if sid == "13" else ("INDIA VIX" if sid in ("21", "26017") else None))
        return self._id_to_symbol.get(sid) or self._index_id_to_symbol.get(sid)

    def get_symbol_map(self) -> Dict[str, str]:
        """Get copy of equity security_id -> symbol mapping."""
        if not self._loaded:
            self.load()
        return dict(self._id_to_symbol)

    def get_index_symbol_map(self) -> Dict[str, str]:
        """Get copy of index security_id -> symbol mapping."""
        if not self._loaded:
            self.load()
        return dict(self._index_id_to_symbol)

    def resolve_symbols(
        self,
        symbols: List[str],
        exchange_segment: str = EXCHANGE_NSE_EQ,
    ) -> Dict[str, str]:
        """
        Resolve a list of trading symbols to Dhan security IDs.

        Returns:
            Dict mapping security_id → symbol for successfully resolved symbols.
            Unresolved symbols are logged as warnings.
        """
        if not self._loaded:
            self.load()

        resolved: Dict[str, str] = {}
        unresolved: List[str] = []

        for symbol in symbols:
            sym_upper = symbol.upper()

            # Check NSE EQ map first so equities take precedence for trading universe
            entry = self._nse_eq_map.get(sym_upper)
            if entry:
                resolved[entry[0]] = sym_upper
                continue

            # Check index map if not an equity
            if sym_upper in self._index_map:
                resolved[self._index_map[sym_upper]] = sym_upper
                continue

            # Check alias
            alias = SYMBOL_ALIASES.get(sym_upper)
            if alias:
                alias_entry = self._nse_eq_map.get(alias)
                if alias_entry:
                    resolved[alias_entry[0]] = sym_upper
                    continue
                if alias in self._index_map:
                    resolved[self._index_map[alias]] = sym_upper
                    continue

            unresolved.append(symbol)

        if unresolved:
            logger.warning(
                "Could not resolve %d/%d symbols to Dhan security IDs: %s",
                len(unresolved), len(symbols), unresolved[:20],
            )

        logger.info(
            "Resolved %d/%d symbols to Dhan security IDs",
            len(resolved), len(symbols),
        )
        return resolved

    def resolve_index_symbols(self, index_symbols: List[str]) -> Dict[str, str]:
        """Resolve index names to security_id -> index_name."""
        if not self._loaded:
            self.load()
        resolved: Dict[str, str] = {}
        for sym in index_symbols:
            su = sym.upper()
            if su in self._index_map:
                resolved[self._index_map[su]] = su
            else:
                for k, v in self._index_id_to_symbol.items():
                    if v == su:
                        resolved[k] = su
        return resolved

    def search_nse_symbols(self, query: str, limit: int = 15) -> List[Dict[str, str]]:
        """
        Search all NSE EQ symbols by partial match on trading symbol or company name.
        Returns up to `limit` results sorted by relevance:
          1. Exact symbol / alias match
          2. Symbol prefix match
          3. Symbol contains match
          4. Company name prefix / contains match
        This searches the FULL Dhan instrument CSV (~2000+ NSE equities).
        """
        if not self._loaded:
            self.load()

        q = query.strip().upper()
        if not q:
            return []

        seen_ids: set = set()
        exact_matches: List[Dict[str, str]] = []
        prefix_matches: List[Dict[str, str]] = []
        contains_matches: List[Dict[str, str]] = []
        name_matches: List[Dict[str, str]] = []

        # 1. Check aliases first (e.g. ZOMATO -> ETERNAL, TATAMOTORS -> TMCV)
        for alias, target in SYMBOL_ALIASES.items():
            if alias == q or alias.startswith(q):
                target_sec_id = self.get_security_id(target)
                if target_sec_id and target_sec_id not in seen_ids:
                    seen_ids.add(target_sec_id)
                    comp_name = self._id_to_name.get(target_sec_id, target)
                    exact_matches.append({
                        "symbol": target,
                        "security_id": target_sec_id,
                        "name": f"{comp_name} ({alias})",
                        "segment": EXCHANGE_NSE_EQ,
                    })

        # 2. Iterate primary equity instruments
        for sec_id, clean_sym in self._id_to_symbol.items():
            if sec_id in seen_ids:
                continue

            comp_name = self._id_to_name.get(sec_id, clean_sym)
            comp_name_upper = comp_name.upper()

            item = {
                "symbol": clean_sym,
                "security_id": sec_id,
                "name": comp_name,
                "segment": EXCHANGE_NSE_EQ,
            }

            if clean_sym == q:
                seen_ids.add(sec_id)
                exact_matches.append(item)
            elif clean_sym.startswith(q):
                seen_ids.add(sec_id)
                prefix_matches.append(item)
            elif q in clean_sym:
                seen_ids.add(sec_id)
                contains_matches.append(item)
            elif q in comp_name_upper:
                seen_ids.add(sec_id)
                name_matches.append(item)

        prefix_matches.sort(key=lambda x: x["symbol"])
        contains_matches.sort(key=lambda x: x["symbol"])
        name_matches.sort(key=lambda x: x["symbol"])

        combined = exact_matches + prefix_matches + contains_matches + name_matches
        return combined[:limit]

    def get_subscription_list(
        self,
        symbols: List[str],
        exchange_segment: str = EXCHANGE_NSE_EQ,
    ) -> List[Dict[str, str]]:
        """
        Build the Dhan WebSocket InstrumentList for subscription requests.

        Returns:
            List of {"ExchangeSegment": ..., "SecurityId": ...} dicts
        """
        if not self._loaded:
            self.load()

        instruments = []
        for symbol in symbols:
            sym_upper = symbol.upper()

            # Index symbols use IDX segment
            if sym_upper in self._index_map:
                instruments.append({
                    "ExchangeSegment": EXCHANGE_IDX,
                    "SecurityId": self._index_map[sym_upper],
                })
                continue

            entry = self._nse_eq_map.get(sym_upper)
            if entry:
                instruments.append({
                    "ExchangeSegment": exchange_segment,
                    "SecurityId": entry[0],
                })
                continue

            # Check alias
            alias = SYMBOL_ALIASES.get(sym_upper)
            if alias:
                if alias in self._index_map:
                    instruments.append({
                        "ExchangeSegment": EXCHANGE_IDX,
                        "SecurityId": self._index_map[alias],
                    })
                    continue
                alias_entry = self._nse_eq_map.get(alias)
                if alias_entry:
                    instruments.append({
                        "ExchangeSegment": exchange_segment,
                        "SecurityId": alias_entry[0],
                    })
                    continue

        return instruments


# ── Global singleton ──────────────────────────────────────────────────────────

_resolver: Optional[DhanInstrumentResolver] = None


def get_instrument_resolver() -> DhanInstrumentResolver:
    """Get the global DhanInstrumentResolver singleton."""
    global _resolver
    if _resolver is None:
        _resolver = DhanInstrumentResolver()
    return _resolver


def resolve_dhan_instruments(
    symbols: List[str],
) -> Dict[str, str]:
    """
    Convenience function: resolve symbols to Dhan security IDs.

    Returns:
        Dict mapping security_id → symbol
    """
    resolver = get_instrument_resolver()
    resolver.load()
    return resolver.resolve_symbols(symbols)
