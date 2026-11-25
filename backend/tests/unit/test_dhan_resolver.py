"""
Dhan Instrument Resolver Tests
===============================
Tests that symbol → security_id resolution uses correct exchange segments
(IDX_I for indices, NSE_EQ for equities) and handles unresolved symbols.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))

# Sample CSV content mimicking Dhan scrip master format
MOCK_CSV = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_ISIN,SEM_INSTRUMENT_NAME
NSE,E,2885,RELIANCE,Reliance Industries,INE002A01018,EQUITY
NSE,C,1333,HDFCBANK,HDFC Bank Limited,INE040A01034,EQUITY
NSE,EQ,11536,TCS-EQ,TCS,INE467B01029,EQUITY
NSE,I,13,NIFTY,Nifty 50,INE000X01000,INDEX
NSE,I,26017,INDIA VIX,India VIX,INE000Y01000,VOLATILITY INDEX
NSE,E,759551,PIRAMALFIN,Piramal Finance Limited,INE140A01024,EQUITY
BSE,E,99999,BSESOMETHING,,INE000Z01000,EQUITY
"""


@pytest.fixture
def resolver():
    """Create a fresh resolver and load mock CSV."""
    from app.services.dhan_instrument_resolver import DhanInstrumentResolver
    r = DhanInstrumentResolver()
    r._parse_csv(MOCK_CSV)
    r._loaded = True
    return r


class TestExchangeSegment:
    def test_idx_i_constant(self):
        from app.services.dhan_instrument_resolver import EXCHANGE_IDX
        assert EXCHANGE_IDX == "IDX_I", f"Expected 'IDX_I', got '{EXCHANGE_IDX}'"

    def test_nse_eq_constant(self):
        from app.services.dhan_instrument_resolver import EXCHANGE_NSE_EQ
        assert EXCHANGE_NSE_EQ == "NSE_EQ"


class TestEquityResolution:
    def test_resolve_reliance(self, resolver):
        sec_id = resolver.get_security_id("RELIANCE")
        assert sec_id == "2885"

    def test_resolve_hdfcbank(self, resolver):
        sec_id = resolver.get_security_id("HDFCBANK")
        assert sec_id == "1333"

    def test_resolve_case_insensitive(self, resolver):
        sec_id = resolver.get_security_id("reliance")
        assert sec_id == "2885"

    def test_reverse_lookup(self, resolver):
        symbol = resolver.get_symbol("2885")
        assert symbol == "RELIANCE"


class TestIndexResolution:
    def test_resolve_nifty50(self, resolver):
        sec_id = resolver.get_security_id("NIFTY 50")
        assert sec_id == "13"

    def test_resolve_india_vix(self, resolver):
        sec_id = resolver.get_security_id("INDIA VIX")
        assert sec_id == "26017"


class TestSubscriptionList:
    def test_equity_segment(self, resolver):
        instruments = resolver.get_subscription_list(["RELIANCE"])
        assert len(instruments) == 1
        assert instruments[0]["ExchangeSegment"] == "NSE_EQ"
        assert instruments[0]["SecurityId"] == "2885"

    def test_index_uses_idx_i(self, resolver):
        instruments = resolver.get_subscription_list(["NIFTY 50"])
        assert len(instruments) == 1
        assert instruments[0]["ExchangeSegment"] == "IDX_I"
        assert instruments[0]["SecurityId"] == "13"

    def test_mixed_symbols(self, resolver):
        instruments = resolver.get_subscription_list(["RELIANCE", "NIFTY 50", "TCS"])
        assert len(instruments) == 3
        segments = {i["ExchangeSegment"] for i in instruments}
        assert "NSE_EQ" in segments
        assert "IDX_I" in segments


class TestUnresolvedSymbols:
    def test_unknown_symbol_returns_none(self, resolver):
        sec_id = resolver.get_security_id("NOSUCHSYMBOL")
        assert sec_id is None

    def test_resolve_symbols_logs_unresolved(self, resolver):
        resolved = resolver.resolve_symbols(["RELIANCE", "NOSUCHSYMBOL"])
        assert "2885" in resolved
        assert len(resolved) == 1  # NOSUCHSYMBOL should not be in results

    def test_subscription_list_skips_unresolved(self, resolver):
        instruments = resolver.get_subscription_list(["RELIANCE", "DOESNOTEXIST"])
        assert len(instruments) == 1  # Only RELIANCE

    def test_alias_resolution_pel(self, resolver):
        sec_id = resolver.get_security_id("PEL")
        assert sec_id == "759551"
        res = resolver.resolve_symbols(["PEL"])
        assert "759551" in res
        assert res["759551"] == "PEL"


class TestTokenCollisionIsolation:
    def test_equity_and_index_share_token_without_collision(self):
        from app.services.dhan_instrument_resolver import DhanInstrumentResolver, EXCHANGE_IDX, EXCHANGE_NSE_EQ
        collision_csv = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_ISIN,SEM_INSTRUMENT_NAME
NSE,E,13,ABB,ABB India,INE117A01022,EQUITY
NSE,I,13,NIFTY,Nifty 50,INE000X01000,INDEX
"""
        r = DhanInstrumentResolver()
        r._parse_csv(collision_csv)
        r._loaded = True

        # Check get_security_id
        assert r.get_security_id("ABB") == "13"
        assert r.get_security_id("NIFTY 50") == "13"

        # Check segment-aware get_symbol
        assert r.get_symbol("13", exchange_seg=EXCHANGE_NSE_EQ) == "ABB"
        assert r.get_symbol("13", exchange_seg=1) == "ABB"
        assert r.get_symbol("13", exchange_seg=EXCHANGE_IDX) == "NIFTY 50"
        assert r.get_symbol("13", exchange_seg=0) == "NIFTY 50"

        # Check resolve_symbols prioritizes equity for trading universe
        eq_map = r.resolve_symbols(["ABB"])
        assert eq_map.get("13") == "ABB"

        idx_map = r.resolve_index_symbols(["NIFTY 50"])
        assert idx_map.get("13") == "NIFTY 50"


class TestNseSearch:
    def test_search_exact_symbol(self, resolver):
        results = resolver.search_nse_symbols("RELIANCE")
        assert len(results) >= 1
        assert results[0]["symbol"] == "RELIANCE"
        assert results[0]["security_id"] == "2885"

    def test_search_partial_symbol(self, resolver):
        results = resolver.search_nse_symbols("REL")
        symbols = [r["symbol"] for r in results]
        assert "RELIANCE" in symbols

    def test_search_company_name(self, resolver):
        results = resolver.search_nse_symbols("HDFC Bank")
        symbols = [r["symbol"] for r in results]
        assert "HDFCBANK" in symbols

    def test_search_alias_zomato(self, resolver):
        results = resolver.search_nse_symbols("ZOMATO")
        # In mock CSV, ETERNAL isn't present, but should not error
        assert isinstance(results, list)

    def test_search_alias_tatamotors(self, resolver):
        results = resolver.search_nse_symbols("TATAMOTORS")
        assert isinstance(results, list)

    def test_search_empty_query(self, resolver):
        assert resolver.search_nse_symbols("") == []
        assert resolver.search_nse_symbols("   ") == []



