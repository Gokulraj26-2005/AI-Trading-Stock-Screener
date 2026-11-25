# backend/tests/unit/test_auth_guards.py
"""
Unit tests for authentication guards, token verification, and symbol validation.
Uses auth_helpers and the session service directly — no monkeypatching of globals.
"""
import asyncio
import os
import sys

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

# conftest.py handles path/env bootstrap — nothing needed here.
from auth_helpers import (
    _extract_bearer_token,
    validate_symbol,
    verify_token,
)
from security import create_jwt
from state.session_state import SessionService


# ── helpers ────────────────────────────────────────────────────────────────────

def _build_request(headers_map: dict | None = None) -> Request:
    headers = []
    for key, value in (headers_map or {}).items():
        headers.append((key.lower().encode("utf-8"), value.encode("utf-8")))
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers}
    return Request(scope)


def _build_request_with_cookies(cookies: dict | None = None, headers_map: dict | None = None) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    combined_headers = dict(headers_map or {})
    if cookie_header:
        combined_headers["cookie"] = cookie_header
    return _build_request(combined_headers)


# ── Symbol validation ──────────────────────────────────────────────────────────

def test_validate_symbol_normalizes_exchange_and_suffix():
    assert validate_symbol("NSE:reliance.ns") == "RELIANCE"


def test_validate_symbol_rejects_invalid_characters():
    with pytest.raises(HTTPException) as exc:
        validate_symbol("INVALID$")
    assert exc.value.status_code == 422


def test_validate_symbol_rejects_empty():
    with pytest.raises(HTTPException) as exc:
        validate_symbol("")
    assert exc.value.status_code == 422


# ── Bearer token extraction ────────────────────────────────────────────────────

def test_extract_bearer_token_rejects_missing_header(monkeypatch):
    import auth_helpers
    monkeypatch.setattr(auth_helpers, "PRODUCTION_MODE", True)
    req = _build_request()
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token(req)
    assert exc.value.status_code == 401


def test_extract_bearer_token_rejects_wrong_scheme():
    req = _build_request({"Authorization": "Basic abc"})
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token(req)
    assert exc.value.status_code == 401


# ── verify_token ──────────────────────────────────────────────────────────────

def test_verify_token_accepts_valid_jwt():
    token = create_jwt({"sub": "dev_user", "scope": "app"}, expires_seconds=60)
    req = _build_request({"Authorization": f"Bearer {token}"})
    payload = verify_token(req)
    assert payload.get("sub") == "dev_user"


def test_verify_token_rejects_invalid_jwt():
    req = _build_request({"Authorization": "Bearer invalid"})
    with pytest.raises(HTTPException) as exc:
        verify_token(req)
    assert exc.value.status_code == 401


# ── SessionService ─────────────────────────────────────────────────────────────

def test_session_service_issues_and_consumes_bootstrap_key():
    svc = SessionService(bootstrap_ttl_seconds=60)
    key, ttl = asyncio.run(svc.issue_bootstrap_key())
    assert isinstance(key, str) and len(key) > 0
    assert ttl == 60
    result = asyncio.run(svc.consume_bootstrap_key(key))
    assert result is True


def test_session_service_rejects_wrong_bootstrap_key():
    svc = SessionService(bootstrap_ttl_seconds=60)
    asyncio.run(svc.issue_bootstrap_key())
    result = asyncio.run(svc.consume_bootstrap_key("wrong-key"))
    assert result is False


def test_session_service_key_is_one_shot():
    svc = SessionService(bootstrap_ttl_seconds=60)
    key, _ = asyncio.run(svc.issue_bootstrap_key())
    assert asyncio.run(svc.consume_bootstrap_key(key)) is True
    # Second consume must fail — key was cleared.
    assert asyncio.run(svc.consume_bootstrap_key(key)) is False


def test_session_service_stream_generation_increments():
    svc = SessionService()
    gen1 = svc.current_stream_generation()
    svc.rotate_stream_generation()
    assert svc.current_stream_generation() == gen1 + 1


# ── ScanStateService ───────────────────────────────────────────────────────────

def test_scan_state_acquire_slot_mutual_exclusion():
    from state.scan_state import ScanStateService
    svc = ScanStateService()
    first = asyncio.run(svc.acquire_scan_slot())
    second = asyncio.run(svc.acquire_scan_slot())
    assert first is True
    assert second is False
    asyncio.run(svc.release_scan_slot())
    third = asyncio.run(svc.acquire_scan_slot())
    assert third is True


def test_scan_state_update_and_get():
    from state.scan_state import ScanStateService
    svc = ScanStateService()
    svc.update_status(status="running", progress=5)
    snap = svc.get_status()
    assert snap["status"] == "running"
    assert snap["progress"] == 5
