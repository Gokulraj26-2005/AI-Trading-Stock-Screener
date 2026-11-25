# backend/tests/integration/test_api.py
"""
Integration tests — full HTTP round-trips against the assembled FastAPI app.
Uses fixtures from conftest.py (app, client, session_headers).
"""
import pytest
import app_config


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    assert "service" in data
    assert "version" in data


def test_security_headers_present(client):
    response = client.get("/health")
    headers = response.headers
    assert "x-content-type-options" in headers
    assert "x-frame-options" in headers
    assert "strict-transport-security" in headers
    assert "content-security-policy" in headers


def test_chart_search_requires_auth(client):
    client.cookies.clear()
    response = client.get("/api/chart/search?q=RELIANCE")
    assert response.status_code == 401


def test_chart_search_rejects_invalid_auth_scheme(client):
    client.cookies.clear()
    response = client.get("/api/chart/search?q=RELIANCE", headers={"Authorization": "Basic invalid"})
    assert response.status_code == 401


def test_chart_search_rejects_invalid_bearer_token(client):
    client.cookies.clear()
    response = client.get("/api/chart/search?q=RELIANCE", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401


def test_chart_search_accepts_valid_session_token(client, session_headers):
    response = client.get("/api/chart/search?q=RELIANCE", headers=session_headers)
    assert response.status_code == 200
    data = response.json()
    assert "matches" in data


def test_session_token_requires_active_dhan_token_in_production(client, monkeypatch):
    import app_config
    import routers.auth as auth_router
    import services.dhan_token_service as token_svc
    monkeypatch.setattr(app_config, "PRODUCTION_MODE", True)
    monkeypatch.setattr(app_config, "USE_DHAN_MOCK", False)
    monkeypatch.setattr(auth_router, "PRODUCTION_MODE", True)
    monkeypatch.setattr(auth_router, "USE_DHAN_MOCK", False)
    monkeypatch.setattr(token_svc, "get_dhan_token", lambda: None)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_API_SECRET", raising=False)
    client.cookies.clear()
    response = client.post("/api/auth/session-token")
    assert response.status_code == 401


def test_session_token_mints_token(session_headers):
    assert "Authorization" in session_headers
    assert session_headers["Authorization"].startswith("Bearer ")


def test_stream_token_requires_auth(client):
    client.cookies.clear()
    response = client.post("/api/auth/stream-token")
    assert response.status_code == 401


def test_stream_token_mints_with_valid_session(client, session_headers):
    response = client.post("/api/auth/stream-token", headers=session_headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data.get("access_token"), str)
    assert data.get("expires_in", 0) > 0
    assert app_config.STREAM_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_broker_status_is_public(client):
    response = client.get("/api/broker/status")
    assert response.status_code == 200
    assert response.json().get("broker") == "dhan"


def test_chart_rejects_invalid_symbol(client, session_headers):
    response = client.get("/api/chart/INVALID$", headers=session_headers)
    assert response.status_code == 422


def test_analysis_symbols_endpoint(client, session_headers):
    response = client.get("/api/analysis/symbols?universe=nifty50", headers=session_headers)
    assert response.status_code == 200
    data = response.json()
    assert data.get("count", 0) > 0
    assert "symbols" in data


def test_analysis_rejects_invalid_symbol(client, session_headers):
    response = client.get("/api/analysis/INVALID$", headers=session_headers)
    assert response.status_code == 422
