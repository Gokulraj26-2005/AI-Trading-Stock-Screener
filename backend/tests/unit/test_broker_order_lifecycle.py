import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.dhan_mcp_client import normalize_order_status


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        (" TRADED ", "FILLED"),
        ("filled", "FILLED"),
        ("TRANSIT", "TRANSIT"),
        ("accepted", "ACCEPTED"),
        ("part_traded", "PARTIAL"),
        ("rejected", "REJECTED"),
        ("not-a-status", "UNKNOWN"),
    ],
)
def test_normalize_order_status_is_safe_and_distinguishes_fill(raw_status, expected):
    assert normalize_order_status(raw_status) == expected


@pytest.mark.asyncio
async def test_accepted_order_does_not_register_watchdog_position():
    from routers import watchdog_router

    slot_mgr = MagicMock()
    slot_mgr.can_open_new_trade.return_value = True
    watchdog = MagicMock()
    shortlist = MagicMock()
    shortlist.get_trade.return_value = None
    shorts = MagicMock()
    shorts.get_trade.return_value = None
    mcp = MagicMock()
    mcp.place_mis_order = AsyncMock(
        return_value={
            "status": "SUCCESS",
            "order_id": "ORD-1",
            "order_status": "TRANSIT",
            "lifecycle_status": "TRANSIT",
        }
    )

    with (
        patch.object(watchdog_router, "get_capital_slot_manager", return_value=slot_mgr),
        patch.object(watchdog_router, "get_algo_live_watchdog", return_value=watchdog),
        patch.object(watchdog_router, "get_dhan_mcp_client", return_value=mcp),
        patch("services.ai_shortlist_service.get_ai_shortlist_service", return_value=shortlist),
        patch(
            "services.ai_shortlist_service_shorts.get_ai_shortlist_service_shorts",
            return_value=shorts,
        ),
    ):
        result = await watchdog_router._execute_trade_approval(
            "TCS", "11536", 3500.0, 17
        )

    assert result["status"] == "TRANSIT"
    assert result["order_id"] == "ORD-1"
    watchdog.register_position.assert_not_called()
    shortlist.update_trade_status.assert_not_called()
