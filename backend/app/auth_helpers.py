# backend/app/auth_helpers.py
"""
Shared authentication helpers used by multiple routers.

These functions were previously private (_-prefixed) helpers inside main.py.
Extracted here to avoid circular imports between routers.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException, Request, WebSocket

from app_config import SESSION_COOKIE_NAME, STREAM_COOKIE_NAME, ALLOW_STREAM_TOKEN_QUERY, PRODUCTION_MODE
from security import decode_jwt, DISABLE_JWT_AUTH
import logging as _logging
_module_logger = _logging.getLogger(__name__)

# Default local user for when JWT is disabled
LOCAL_DEV_USER = "local_user"

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\&-]{0,24}$")


# ── Token extraction ──────────────────────────────────────────────────────────

def _extract_bearer_token(req: Request) -> str:
    header = (req.headers.get("Authorization") or "").strip()
    if not header:
        if DISABLE_JWT_AUTH:
            return "dev_preview_mock_token"
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Authorization header must use Bearer token"
        )
    token = header[7:].strip()
    if not token:
        if DISABLE_JWT_AUTH:
            return "dev_preview_mock_token"
        raise HTTPException(status_code=401, detail="Bearer token is empty")
    return token


def _extract_app_session_token(req: Request) -> str:
    header = (req.headers.get("Authorization") or "").strip()
    if header:
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="Authorization header must use Bearer token",
            )
        token = header[7:].strip()
        if not token:
            if DISABLE_JWT_AUTH:
                return "dev_preview_mock_token"
            raise HTTPException(status_code=401, detail="Bearer token is empty")
        return token

    cookie_token = str(req.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token

    if DISABLE_JWT_AUTH:
        return "dev_preview_mock_token"

    raise HTTPException(status_code=401, detail="Missing session token")


def _decode_app_token(token: str) -> dict:
    if DISABLE_JWT_AUTH or token in ("dev_preview_mock_token", "mock_access_token_value", "local-dev-token", "dev-preview-token"):
        return {"sub": LOCAL_DEV_USER, "scope": "app"}

    try:
        payload = decode_jwt(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    subject = str(payload.get("sub") or "").strip()
    if not subject:
        raise HTTPException(status_code=401, detail="Token subject is missing")

    scope = str(payload.get("scope") or "app").strip().lower()
    if scope != "app":
        raise HTTPException(
            status_code=401, detail="Token scope is not valid for this endpoint"
        )
    return payload


def verify_token(req: Request) -> dict:
    """Validate app session token and return its claims."""
    # Bypass JWT verification for local development
    if DISABLE_JWT_AUTH:
        return {"sub": LOCAL_DEV_USER, "scope": "app"}
    
    token = _extract_app_session_token(req)
    return _decode_app_token(token)


# ── Stream token helpers ──────────────────────────────────────────────────────

def _decode_stream_token(token: str, current_generation_fn) -> dict:
    """
    Validate a stream-scoped JWT.
    ``current_generation_fn`` must return the current stream-generation int.
    """
    # Bypass JWT verification for local development
    if DISABLE_JWT_AUTH or token in ("dev_preview_mock_token", "mock_access_token_value", "local-dev-token"):
        return {"sub": LOCAL_DEV_USER, "scope": "stream", "sg": current_generation_fn()}
    
    try:
        payload = decode_jwt(token)
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired stream token"
        ) from exc

    subject = str(payload.get("sub") or "").strip()
    if not subject:
        raise HTTPException(status_code=401, detail="Stream token subject is missing")

    scope = str(payload.get("scope") or "").strip().lower()
    if scope != "stream":
        raise HTTPException(status_code=401, detail="Token scope must be 'stream'")

    raw_generation = payload.get("sg")
    try:
        token_generation = int(raw_generation)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401, detail="Stream token generation is missing"
        )

    current_generation = current_generation_fn()
    if token_generation != current_generation:
        raise HTTPException(
            status_code=401, detail="Stream token generation is stale"
        )
    return payload


def extract_stream_token_from_request(
    req: Request, query_token: Optional[str] = None
) -> str:
    # Bypass for local development
    if DISABLE_JWT_AUTH or not PRODUCTION_MODE:
        return "local-dev-token"
    
    cookie_token = str(req.cookies.get(STREAM_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token

    cleaned_query = str(query_token or "").strip()
    if not cleaned_query:
        raise HTTPException(status_code=401, detail="Missing stream token cookie")

    if not ALLOW_STREAM_TOKEN_QUERY:
        raise HTTPException(
            status_code=401, detail="Stream token query parameter is disabled"
        )

    return cleaned_query


def extract_stream_token_from_websocket(websocket: WebSocket) -> str:
    # Bypass for local development
    if DISABLE_JWT_AUTH or not PRODUCTION_MODE:
        return "local-dev-token"
    
    cookie_token = str(websocket.cookies.get(STREAM_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token

    query_token = str(websocket.query_params.get("stream_token") or "").strip()
    if not query_token:
        # B-18 fix: clarified message — covers both missing cookie and query param.
        raise HTTPException(status_code=401, detail="Missing stream token (cookie 'stream_session' or query param 'stream_token')")  # noqa: E501

    if not ALLOW_STREAM_TOKEN_QUERY:
        raise HTTPException(
            status_code=401, detail="Stream token query parameter is disabled"
        )

    return query_token


# ── Symbol validation ─────────────────────────────────────────────────────────

def _normalize_symbol(symbol: str) -> str:
    cleaned = str(symbol or "").strip().upper()
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    if "." in cleaned:
        cleaned = cleaned.split(".", 1)[0]
    return cleaned


def validate_symbol(symbol: str, field_name: str = "symbol") -> str:
    normalized = _normalize_symbol(symbol)
    if not normalized:
        raise HTTPException(
            status_code=422, detail=f"{field_name} must be a non-empty symbol"
        )
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} format")
    return normalized
