# backend/app/main.py
"""
Entry-point shim.

All route logic, middleware, and state has moved to:
  - app/app.py          (assembler + lifespan)
  - app/routers/        (five focused APIRouter modules)
  - app/state/          (typed state services)
  - app/app_config.py   (config constants)
  - app/auth_helpers.py (shared token/symbol utilities)
  - app/pipeline_helpers.py (orchestrator lifecycle)

This file is retained so that:
  1. uvicorn can be started with ``app.main:app`` (unchanged entry-point).
  2. Legacy monkeypatch-based tests can still import ``app.main`` and find
     backwards-compatible attribute aliases during the migration period.
"""
from __future__ import annotations

import os
import time

try:
    from dotenv import load_dotenv
    # Try to load .env from the root of the project (one level above backend)
    _env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    if os.path.exists(_env_path):
        load_dotenv(dotenv_path=_env_path, override=False)
    else:
        # Fallback to backend directory .env if it exists
        load_dotenv(override=False)
except ImportError:
    pass

# Enforce IST for NSE market hours (Asia/Kolkata)
os.environ['TZ'] = 'Asia/Kolkata'
if hasattr(time, 'tzset'):
    time.tzset()

import asyncio
import sys
from typing import Optional

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


try:
    # Package import mode (e.g., import app.main)
    from app.app import app  # type: ignore  # noqa: F401

    from app.app_config import (  # type: ignore  # noqa: F401
        DEFAULT_DEV_USER_ID,
        SESSION_COOKIE_NAME,
        STREAM_COOKIE_NAME,
        ALLOWED_ORIGINS,
        PRODUCTION_MODE,
        FAIL_CLOSED_ON_LIVE_ERROR,
        ALLOW_STREAM_TOKEN_QUERY,
        STREAM_TOKEN_TTL_SECONDS,
    )

    from app.auth_helpers import (  # type: ignore  # noqa: F401
        _extract_bearer_token,
        _extract_app_session_token,
        _decode_app_token,
        verify_token,
        validate_symbol as _validate_symbol,
        _normalize_symbol,
    )


    from app import db_store  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    # Direct module mode retained for older direct-path test setups.
    from app import app  # type: ignore  # noqa: F401

    from app_config import (  # noqa: F401
        DEFAULT_DEV_USER_ID,
        SESSION_COOKIE_NAME,
        STREAM_COOKIE_NAME,
        ALLOWED_ORIGINS,
        PRODUCTION_MODE,
        FAIL_CLOSED_ON_LIVE_ERROR,
        ALLOW_STREAM_TOKEN_QUERY,
        STREAM_TOKEN_TTL_SECONDS,
    )

    from auth_helpers import (  # noqa: F401
        _extract_bearer_token,
        _extract_app_session_token,
        _decode_app_token,
        verify_token,
        validate_symbol as _validate_symbol,
        _normalize_symbol,
    )

    import db_store  # noqa: F401

try:
    from app.models import AuthResponse  # type: ignore
except ModuleNotFoundError:
    from models import AuthResponse  # type: ignore

try:
    from app.security import create_jwt  # type: ignore
except ModuleNotFoundError:
    from security import create_jwt  # type: ignore

# ── Backwards-compatibility aliases ──────────────────────────────────────────
# These allow existing test code that does ``import main; main.DEFAULT_DEV_USER_ID``
# to keep working without immediate rewrite.  They will be cleaned up once all
# tests have been migrated to the new import paths (see backend/tests/).

# Session-service shims for legacy tests that mutate the session globals:
#   main._consume_session_bootstrap_key(...)
#   main._current_stream_token_generation()

def _consume_session_bootstrap_key(candidate: str):
    """Delegate to app.state.session.consume_bootstrap_key (async)."""
    return _ensure_compat_session_service().consume_bootstrap_key(candidate)


def _current_stream_token_generation() -> int:
    return _ensure_compat_session_service().current_stream_generation()


def _ensure_compat_session_service():
    svc = getattr(app.state, "session", None)
    if svc is not None:
        return svc

    try:
        from app.state.session_state import SessionService  # type: ignore
    except ModuleNotFoundError:
        from state.session_state import SessionService  # type: ignore

    svc = SessionService()
    app.state.session = svc
    return svc


def _clean_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().strip('"').strip("'")
    return cleaned or None


async def api_create_session_token(req, response):
    """Legacy shim retained for unit tests."""
    user_id = DEFAULT_DEV_USER_ID
    session_ttl_seconds = 12 * 3600
    session_token = create_jwt({"sub": user_id, "scope": "app"}, expires_seconds=session_ttl_seconds)
    return AuthResponse(
        access_token=session_token,
        user_id=user_id,
        expires_in=session_ttl_seconds,
    )



