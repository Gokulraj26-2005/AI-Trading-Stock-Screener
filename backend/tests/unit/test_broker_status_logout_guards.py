# backend/tests/unit/test_broker_status_logout_guards.py
"""
Unit tests for Dhan broker status and logout endpoint guards.
Uses TestClient against the assembled app (via conftest client fixture).
"""
import pytest
from fastapi.testclient import TestClient
from security import create_jwt


def _auth_headers() -> dict[str, str]:
    app_token = create_jwt({"sub": "dev_user", "scope": "app"}, expires_seconds=120)
    return {"Authorization": f"Bearer {app_token}"}


def test_broker_status_endpoint(client):
    response = client.get("/api/broker/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("broker") == "dhan"
    assert "enabled" in payload
    assert "configured" in payload
    assert "details" in payload


def test_broker_mock_mode_toggle(client):
    response = client.post("/api/broker/mock-mode", json={"enable": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("mock_mode") is True


def test_broker_logout_endpoint(client):
    response = client.post("/api/broker/logout")
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("logged_out") is True

