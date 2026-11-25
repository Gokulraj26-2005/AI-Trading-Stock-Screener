# backend/app/routers/auth.py
"""
Auth router — session-token and stream-token issuance.

Routes
------
POST /api/auth/session-token
POST /api/auth/stream-token
"""
from __future__ import annotations

import os
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

import db_store
from app_config import PRODUCTION_MODE, USE_DHAN_MOCK
from logger import logger
from models import AuthResponse, StreamTokenResponse
from security import create_jwt, DISABLE_JWT_AUTH
from state.scan_state import get_scan_state, ScanStateService
from state.session_state import get_session_service, SessionService

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── internal helpers (imported by app.py as well) ────────────────────────────

def _clean_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().strip('"').strip("'")
    return cleaned or None


def _is_secure_cookie_scheme(scheme: str) -> bool:
    forced = os.getenv("AUTH_COOKIE_SECURE")
    if forced is not None:
        return str(forced).strip().lower() in {"1", "true", "yes", "on"}
    return scheme.lower() in {"https", "wss"}


def _is_request_secure(req: Request) -> bool:
    forwarded_proto = str(req.headers.get("x-forwarded-proto") or "").strip().lower()
    if forwarded_proto:
        return _is_secure_cookie_scheme(forwarded_proto.split(",", 1)[0].strip())
    return _is_secure_cookie_scheme(str(req.url.scheme or ""))


def _set_auth_cookie(
    response: Response,
    key: str,
    value: str,
    max_age_seconds: int,
    secure: bool,
    samesite: str = "lax",
) -> None:
    response.set_cookie(
        key=key,
        value=value,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=int(max_age_seconds),
        path="/",
    )


def _clear_auth_cookie(response: Response, key: str) -> None:
    response.delete_cookie(key=key, path="/")


# ── shared token dependency (also used by other routers) ─────────────────────

def _extract_app_session_token(req: Request, session_cookie_name: str) -> str:
    header = (req.headers.get("Authorization") or "").strip()
    if header:
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="Authorization header must use Bearer token",
            )
        token = header[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Bearer token is empty")
        return token

    cookie_token = str(req.cookies.get(session_cookie_name) or "").strip()
    if cookie_token:
        return cookie_token

    raise HTTPException(status_code=401, detail="Missing session token")


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("/session-token", response_model=AuthResponse)
async def api_create_session_token(
    req: Request,
    response: Response,
    session_svc: Annotated[SessionService, Depends(get_session_service)],
):
    from app_config import (
        SESSION_COOKIE_NAME,
        STREAM_COOKIE_NAME,
        AUTH_COOKIE_SAMESITE,
        DEFAULT_DEV_USER_ID,
        DHAN_ENABLED,
        USE_DHAN_MOCK,
        PRODUCTION_MODE,
    )
    from auth_helpers import verify_token

    has_valid_app_session = False
    existing_claims = None

    try:
        existing_claims = verify_token(req)
        has_valid_app_session = True
    except HTTPException:
        has_valid_app_session = False

    subject = str((existing_claims or {}).get("sub") or "").strip()
    user_id = subject or _resolve_active_user_id(DEFAULT_DEV_USER_ID)
    _remember_active_user_id(user_id)

    from services.dhan_token_service import get_dhan_token
    token = get_dhan_token()
    if not token and (USE_DHAN_MOCK or not PRODUCTION_MODE):
        token = "mock_dhan_access_token"
        os.environ["DHAN_ACCESS_TOKEN"] = token

    if not token and PRODUCTION_MODE:
        raise HTTPException(
            status_code=401,
            detail="No active Dhan access token. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env or login with Dhan.",
        )
    if token and not USE_DHAN_MOCK:
        client_id = (
            _clean_env(os.getenv("DHAN_USER_ID"))
            or _clean_env(os.getenv("DHAN_CLIENT_ID"))
            or _clean_env(os.getenv("DHAN_API_KEY"))
        )
        base_url = str(os.getenv("DHAN_BASE_URL") or "https://api.dhan.co/v2").rstrip("/")
        if not client_id:
            raise HTTPException(status_code=401, detail="Dhan client ID is not configured.")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                dhan_response = await client.get(
                    f"{base_url}/fundlimit",
                    headers={
                        "access-token": token,
                        "client-id": client_id,
                        "Content-Type": "application/json",
                    },
                )
            if dhan_response.status_code != 200:
                raise HTTPException(
                    status_code=401,
                    detail="Dhan authentication failed. Generate a fresh Dhan access token.",
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Dhan authentication check failed: %s", exc)
            raise HTTPException(status_code=503, detail="Unable to reach Dhan for authentication.") from exc

    try:
        if not db_store.get_user(user_id):
            db_store.create_user(
                user_id=user_id,
                email=f"{user_id}@example.com",
                name=user_id,
            )
    except Exception as exc:
        logger.warning("Unable to upsert local user for session token minting: %s", exc)

    session_ttl_seconds = 12 * 3600
    session_token = create_jwt({"sub": user_id, "scope": "app"}, expires_seconds=session_ttl_seconds)
    _set_auth_cookie(
        response=response,
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age_seconds=session_ttl_seconds,
        secure=_is_request_secure(req),
        samesite=AUTH_COOKIE_SAMESITE,
    )
    return AuthResponse(
        access_token=session_token,
        user_id=user_id,
        expires_in=session_ttl_seconds,
    )


@router.post("/stream-token", response_model=StreamTokenResponse)
async def api_create_stream_token(
    req: Request,
    response: Response,
    session_svc: Annotated[SessionService, Depends(get_session_service)],
):
    from app_config import STREAM_COOKIE_NAME, AUTH_COOKIE_SAMESITE, STREAM_TOKEN_TTL_SECONDS
    from auth_helpers import verify_token

    claims = verify_token(req)
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise HTTPException(status_code=401, detail="Token subject is missing")

    stream_generation = session_svc.current_stream_generation()
    stream_token = create_jwt(
        {"sub": subject, "scope": "stream", "sg": stream_generation},
        expires_seconds=STREAM_TOKEN_TTL_SECONDS,
    )
    _set_auth_cookie(
        response=response,
        key=STREAM_COOKIE_NAME,
        value=stream_token,
        max_age_seconds=STREAM_TOKEN_TTL_SECONDS,
        secure=_is_request_secure(req),
        samesite=AUTH_COOKIE_SAMESITE,
    )
    return StreamTokenResponse(
        access_token=stream_token,
        expires_in=STREAM_TOKEN_TTL_SECONDS,
    )


# ── helper stubs ─────────────────────────────────────────────────────────────

def _resolve_active_user_id(fallback: str = "local_user") -> str:
    from app_config import DEFAULT_DEV_USER_ID
    return _clean_env(os.getenv("DHAN_CLIENT_ID")) or _clean_env(os.getenv("ACTIVE_USER_ID")) or fallback or DEFAULT_DEV_USER_ID


def _remember_active_user_id(user_id: str) -> None:
    cleaned = _clean_env(user_id)
    if cleaned:
        os.environ["ACTIVE_USER_ID"] = cleaned

