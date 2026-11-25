# backend/app/routers/broker.py
"""
Broker router — Dhan integration status and compatibility shims.

Routes
------
GET   /api/broker/status
POST  /api/broker/mock-mode
POST  /api/broker/logout
"""
import hashlib
import hmac
import os
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Dict, Any, Optional

import app_config
from app_config import DHAN_ENABLED, USE_DHAN_MOCK, PRODUCTION_MODE
from state.session_state import SessionService, get_session_service

router = APIRouter(tags=["broker"])


@router.post("/api/broker/webhook")
async def dhan_order_webhook(request: Request) -> Dict[str, Any]:
    """Apply a Dhan order update; malformed or unauthenticated updates fail closed."""
    body = await request.body()
    secret = os.getenv("DHAN_WEBHOOK_SECRET", "").strip()
    if secret:
        supplied = request.headers.get("x-dhan-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Invalid broker webhook signature.")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Broker webhook must contain JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Broker webhook payload must be an object.")
    order_id = str(payload.get("orderId") or payload.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="Broker webhook is missing orderId.")
    from services.broker_reconciliation import reconcile_order
    from services.dhan_mcp_client import get_dhan_mcp_client
    from services.live_algo_watchdog import get_algo_live_watchdog
    result = await reconcile_order(
        order_id,
        client=get_dhan_mcp_client(),
        long_watchdog=get_algo_live_watchdog(),
        broker_event={
            "status": "SUCCESS",
            "order_id": order_id,
            "order_status": payload.get("orderStatus") or payload.get("status"),
            "lifecycle_status": payload.get("lifecycle_status"),
            "filled_quantity": payload.get("filledQty", payload.get("filledQuantity", payload.get("tradedQuantity", 0))),
            "fill_price": payload.get("averageTradedPrice", payload.get("avgTradedPrice")),
            "data": payload,
        },
    )
    return result

class BrokerStatusResponse(BaseModel):
    enabled: bool
    mock_mode: bool
    configured: bool
    has_access_token: bool = False
    broker: str = "dhan"
    details: Dict[str, Any]

class MockModeRequest(BaseModel):
    enable: bool = True


async def _dhan_auth_request(path: str, *, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"https://auth.dhan.co{path}", headers=headers or {})
    if response.status_code != 200:
        try:
            error_payload = response.json()
            error_message = str(error_payload.get("errorMessage") or error_payload.get("message") or "").strip()
        except ValueError:
            error_message = ""
        detail = "Dhan rejected the consent request. Verify that DHAN_USER_ID is your numeric Dhan client ID and that the API key/secret belong to the same app."
        if error_message:
            detail = f"Dhan authentication failed: {error_message}"
        raise HTTPException(status_code=502, detail=detail)
    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Dhan authentication returned an invalid response.")
    return payload


@router.get("/api/dhan/login-url")
async def get_dhan_login_url():
    """Create a Dhan consent session and return its browser login URL."""
    client_id = (os.getenv("DHAN_USER_ID") or os.getenv("DHAN_CLIENT_ID") or "").strip()
    api_key = (os.getenv("DHAN_API_KEY") or "").strip()
    api_secret = (os.getenv("DHAN_API_SECRET") or "").strip()
    if not client_id or not api_key or not api_secret:
        raise HTTPException(
            status_code=503,
            detail="Dhan login requires DHAN_USER_ID (your Dhan client ID), DHAN_API_KEY, and DHAN_API_SECRET.",
        )

    payload = await _dhan_auth_request(
        f"/app/generate-consent?client_id={client_id}",
        headers={"app_id": api_key, "app_secret": api_secret},
    )
    consent_id = str(payload.get("consentAppId") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=502, detail="Dhan did not return a consent session.")
    return {
        "login_url": f"https://auth.dhan.co/login/consentApp-login?consentAppId={consent_id}",
        "consent_app_id": consent_id,
    }


@router.post("/api/dhan/consume-token")
async def consume_dhan_token(body: Dict[str, str], req: Request):
    """Exchange the tokenId returned by Dhan's browser login for an access token."""
    token_id = str(body.get("token_id") or "").strip()
    api_key = (os.getenv("DHAN_API_KEY") or "").strip()
    api_secret = (os.getenv("DHAN_API_SECRET") or "").strip()
    if not token_id or not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="A Dhan tokenId and configured API key/secret are required.")

    payload = await _dhan_auth_request(
        f"/app/consumeApp-consent?tokenId={token_id}",
        headers={"app_id": api_key, "app_secret": api_secret},
    )
    access_token = str(payload.get("accessToken") or "").strip()
    if not access_token:
        raise HTTPException(status_code=502, detail="Dhan did not return an access token.")
    app_config.USE_DHAN_MOCK = False
    os.environ["USE_DHAN_MOCK"] = "false"

    # Persist token across SQLite, .dhan_token file, and host .env
    from services.dhan_token_service import save_dhan_token
    save_dhan_token(
        access_token=access_token,
        expires_at=payload.get("expiryTime"),
        user_id=api_key or "default",
    )

    from pipeline_helpers import restart_pipeline_background
    await restart_pipeline_background(req.app)
    return {"status": "authenticated", "expires_at": payload.get("expiryTime")}

@router.get("/api/broker/status", response_model=BrokerStatusResponse)
async def get_broker_status():
    """Get the current broker integration status."""
    from services.dhan_token_service import get_dhan_token
    client_id = os.getenv("DHAN_USER_ID") or os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_API_KEY")
    token = get_dhan_token()
    api_secret = os.getenv("DHAN_API_SECRET")
    if not token:
        token = api_secret
    use_mock = app_config.USE_DHAN_MOCK or os.getenv("USE_DHAN_MOCK", "false").lower() == "true"
    env_name = getattr(app_config, "DHAN_ENV", "sandbox")
    base_url = getattr(app_config, "DHAN_BASE_URL", "https://sandbox.dhan.co/v2")
    
    configured = bool(client_id and token)
    has_access_token = bool(token) and (use_mock or env_name == "sandbox")
    
    details: Dict[str, Any] = {
        "client_id_prefix": client_id[:4] + "..." if client_id and len(client_id) > 4 else None,
        "has_token": bool(token),
        "mock_mode": use_mock,
        "environment": env_name,
        "base_url": base_url,
    }

    if configured and not use_mock:
        try:
            import httpx
            headers = {
                "access-token": token,
                "client-id": client_id,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{base_url}/fundlimit", headers=headers)
                if res.status_code == 200:
                    fund_data = res.json()
                    details["verified"] = True
                    details["available_balance"] = fund_data.get("availabelBalance", 0.0)
                    has_access_token = True
                else:
                    details["verified"] = False
                    details["api_status_code"] = res.status_code
        except Exception as exc:
            details["verified"] = False
            details["api_error"] = str(exc)

    return BrokerStatusResponse(
        enabled=app_config.DHAN_ENABLED,
        mock_mode=use_mock,
        configured=configured,
        has_access_token=has_access_token,
        broker="dhan",
        details=details,
    )

@router.post("/api/broker/mock-mode")
async def toggle_mock_mode(body: MockModeRequest, req: Request):
    """Enable or disable mock data mode dynamically for previewing."""
    from pipeline_helpers import restart_pipeline_background, is_pipeline_task_running
    
    app_config.USE_DHAN_MOCK = body.enable
    os.environ["USE_DHAN_MOCK"] = "true" if body.enable else "false"
    
    if body.enable:
        app_config.PRODUCTION_MODE = False
        os.environ["PRODUCTION_MODE"] = "false"
        app_config.FAIL_CLOSED_ON_LIVE_ERROR = False
        os.environ["FAIL_CLOSED_ON_LIVE_ERROR"] = "false"
        
    # Restart pipeline in the chosen mode (mock or live)
    try:
        await restart_pipeline_background(req.app)
    except Exception as exc:
        pass
    
    return {
        "status": "ok",
        "mock_mode": body.enable,
        "pipeline_running": is_pipeline_task_running(req.app),
    }


@router.post("/api/broker/logout")
async def broker_logout(
    response: Response,
    session_svc: SessionService = Depends(get_session_service),
):
    """Clear the local Dhan app and stream sessions without deleting credentials."""
    from app_config import SESSION_COOKIE_NAME, STREAM_COOKIE_NAME
    from routers.auth import _clear_auth_cookie
    session_svc.rotate_stream_generation()

    _clear_auth_cookie(response, SESSION_COOKIE_NAME)
    _clear_auth_cookie(response, STREAM_COOKIE_NAME)
    return {"status": "logged_out", "broker": "dhan", "logged_out": True}
