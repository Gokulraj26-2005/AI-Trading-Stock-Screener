# backend/tests/unit/test_dhan_token_service.py
import os
import json
import pytest
from pathlib import Path
from services.dhan_token_service import (
    save_dhan_token,
    get_dhan_token,
    _is_valid_token_candidate,
)
import db_store


def test_is_valid_token_candidate():
    secret = "00000000-0000-4000-8000-000000000001"
    assert not _is_valid_token_candidate(None)
    assert not _is_valid_token_candidate("")
    assert not _is_valid_token_candidate("short")
    # Must reject if identical to the API secret
    assert not _is_valid_token_candidate(secret, api_secret=secret)
    # Must accept valid JWT
    valid_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
    assert _is_valid_token_candidate(valid_jwt, api_secret=secret)


def test_save_and_restore_token(tmp_path, monkeypatch):
    import services.dhan_token_service as dhan_token_service

    test_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_access_token_payload_for_persistence_test"
    secret = "00000000-0000-4000-8000-000000000001"
    token_file = tmp_path / ".dhan_token"
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(dhan_token_service, "_TOKEN_FILE", token_file)
    monkeypatch.setattr(dhan_token_service, "ENV_LOCATIONS", [env_file])
    monkeypatch.setenv("DHAN_API_SECRET", secret)

    # 1. Save token
    save_dhan_token(test_token, expires_at="2099-01-01T00:00:00", user_id="test_user")

    # 2. Simulate container restart (in-memory env cleared back to secret)
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", secret)

    # 3. get_dhan_token should recover the token from SQLite or .dhan_token file
    restored = get_dhan_token()
    assert restored == test_token

    # 4. Clean up
    db_store.delete_token("test_user")
    if token_file.exists():
        token_file.unlink()
