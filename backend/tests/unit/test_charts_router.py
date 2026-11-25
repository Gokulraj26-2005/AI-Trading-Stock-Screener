"""
Unit tests for Charts API Router (/api/chart) and Intraday ML integration.
"""

import pytest
from fastapi.testclient import TestClient


def test_get_chart_data_normal(client: TestClient):
    """Verifies default request returns live chart telemetry without running ML."""
    response = client.get(
        "/api/chart/RELIANCE?interval=5m",
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "RELIANCE"
    assert data["interval"] == "5m"
    assert "candles" in data
    assert len(data["candles"]) > 0
    assert "indicators" in data
    assert "order_flow" in data
    assert "quant_metrics" in data
