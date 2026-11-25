"""
Institutional Capital Slot & Priority Queue Manager
===================================================
Enforces strict portfolio sizing constraints:
- Total Capital: ₹1,80,000 (1.8 Lakhs)
- Fixed Allocation: ₹60,000 per trade (Always exactly ₹60k sizing)
- Max Active Concurrent Slots: 3 Trades
- Priority Waiting Queue: When 3 slots are full, newly approved setups wait in FIFO queue
"""

from __future__ import annotations

import time
from threading import RLock
from dataclasses import asdict, dataclass, field
from math import floor, isfinite
from typing import Any, Dict, List, Optional
from config.trading_config import CAPITAL_CONFIG
from logger import logger


@dataclass
class SlotPosition:
    symbol: str
    security_id: str
    entry_price: float
    quantity: int
    order_id: str
    sl_order_id: str = ""
    allocated_capital: float = field(default_factory=lambda: CAPITAL_CONFIG.PER_TRADE_ALLOCATION)
    entry_time: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CapitalSlotManager:
    def __init__(self, restore_persisted: bool = False):
        self.active_slots: Dict[str, SlotPosition] = {}
        self._reserved_slots: Dict[str, str] = {}
        self._lock = RLock()
        self.priority_queue: List[Dict[str, Any]] = []
        if restore_persisted:
            self.restore_persisted_state()

    @staticmethod
    def _persist_queue(candidates: List[Dict[str, Any]]) -> None:
        from db_store import save_lifecycle_queue
        save_lifecycle_queue(candidates)

    def restore_persisted_state(self) -> None:
        from db_store import get_lifecycle_positions, get_lifecycle_queue
        for data in get_lifecycle_positions():
            if data.get("symbol") in self.active_slots:
                continue
            self.active_slots[data["symbol"]] = SlotPosition(
                symbol=data["symbol"],
                security_id=str(data.get("security_id", "")),
                entry_price=float(data["entry_price"]),
                quantity=int(data["quantity"]),
                order_id=str(data["order_id"]),
                sl_order_id=str(data.get("sl_order_id", "")),
                allocated_capital=float(data.get("allocated_capital", CAPITAL_CONFIG.PER_TRADE_ALLOCATION)),
                entry_time=float(data.get("entry_time", time.time())),
            )
        self.priority_queue = get_lifecycle_queue()
        now = time.time()
        for candidate in self.priority_queue:
            candidate.setdefault(
                "expires_at",
                float(candidate.get("enqueued_at", now))
                + float(getattr(CAPITAL_CONFIG, "QUEUED_CANDIDATE_TTL_SECONDS", 300.0)),
            )

    def can_open_new_trade(self) -> bool:
        """Returns True if at least 1 of the 3 capital slots is free."""
        with self._lock:
            return len(self.active_slots) + len(self._reserved_slots) < CAPITAL_CONFIG.MAX_ACTIVE_TRADES

    def reserve_slot(self, symbol: str, reservation_id: str) -> bool:
        """Atomically reserve capacity before broker submission.

        Repeating the same reservation is idempotent; a different reservation for
        an active or pending symbol is rejected.
        """
        symbol = str(symbol).upper().strip()
        reservation_id = str(reservation_id).strip()
        if not symbol or not reservation_id:
            return False
        with self._lock:
            if symbol in self.active_slots:
                return False
            existing = self._reserved_slots.get(symbol)
            if existing:
                return existing == reservation_id
            if len(self.active_slots) + len(self._reserved_slots) >= CAPITAL_CONFIG.MAX_ACTIVE_TRADES:
                return False
            self._reserved_slots[symbol] = reservation_id
            return True

    def release_reservation(self, symbol: str, reservation_id: Optional[str] = None) -> bool:
        symbol = str(symbol).upper().strip()
        with self._lock:
            existing = self._reserved_slots.get(symbol)
            if not existing or (reservation_id is not None and existing != str(reservation_id)):
                return False
            del self._reserved_slots[symbol]
            return True

    def reservation_for(self, symbol: str) -> Optional[str]:
        with self._lock:
            return self._reserved_slots.get(str(symbol).upper().strip())

    def calculate_quantity(self, trigger_price: float, leverage: Optional[float] = None) -> int:
        """
        Calculates exact share count for MIS intraday trade using dynamic leverage.
        Buying Power = PER_TRADE_ALLOCATION * leverage
        Qty = floor(Buying Power / trigger_price)
        """
        if trigger_price <= 0:
            return 0
        effective_leverage = float(leverage) if leverage and float(leverage) > 0 else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        effective_buying_power = CAPITAL_CONFIG.PER_TRADE_ALLOCATION * effective_leverage
        return int(effective_buying_power // trigger_price)

    def calculate_risk_based_quantity(
        self,
        entry_price: float,
        stop_price: float,
        risk_budget: float,
        slippage_per_share: float = 0.0,
        fees_per_share: float = 0.0,
        leverage: Optional[float] = None,
        margin_allocation: Optional[float] = None,
    ) -> int:
        """Return the smaller of risk-sized quantity and margin-sized quantity.

        ``risk_budget`` and all price/cost values are expressed in rupees. Slippage
        and fees are per share and are added to the stop distance before sizing.
        Margin allocation is the cash reserved for the position; leverage only
        increases the notional buying-power cap and never increases the risk
        budget.
        """
        numeric_values = {
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_budget": risk_budget,
            "slippage_per_share": slippage_per_share,
            "fees_per_share": fees_per_share,
        }
        if any(not isfinite(float(value)) for value in numeric_values.values()):
            raise ValueError("Risk sizing inputs must be finite numbers.")
        if slippage_per_share < 0 or fees_per_share < 0:
            raise ValueError("Slippage and fees cannot be negative.")
        if leverage is not None and (
            not isfinite(float(leverage)) or float(leverage) <= 0
        ):
            raise ValueError("Leverage must be a positive finite number.")
        if margin_allocation is not None and (
            not isfinite(float(margin_allocation)) or float(margin_allocation) <= 0
        ):
            raise ValueError("Margin allocation must be a positive finite number.")

        if entry_price <= 0 or stop_price <= 0 or risk_budget <= 0:
            return 0

        effective_leverage = (
            float(leverage)
            if leverage is not None
            else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        )
        effective_margin = (
            float(margin_allocation)
            if margin_allocation is not None
            else float(CAPITAL_CONFIG.PER_TRADE_ALLOCATION)
        )
        risk_per_share = (
            abs(entry_price - stop_price)
            + float(slippage_per_share)
            + float(fees_per_share)
        )
        if risk_per_share <= 0:
            return 0

        risk_quantity = floor(float(risk_budget) / risk_per_share)
        margin_quantity = floor(
            effective_margin * effective_leverage / float(entry_price)
        )
        return max(0, min(risk_quantity, margin_quantity))

    def calculate_execution_quantity(
        self,
        entry_price: float,
        stop_price: float,
        leverage: Optional[float] = None,
        risk_budget: Optional[float] = None,
        slippage_per_share: Optional[float] = None,
        fees_per_share: float = 0.0,
    ) -> int:
        """Size an executable order from its stop distance, never from leverage alone."""
        budget = float(
            risk_budget
            if risk_budget is not None
            else getattr(CAPITAL_CONFIG, "MAX_RISK_PER_TRADE", 0.0)
        )
        slippage = float(
            slippage_per_share
            if slippage_per_share is not None
            else getattr(CAPITAL_CONFIG, "SLIPPAGE_BUFFER_PER_SHARE", 0.0)
        )
        return self.calculate_risk_based_quantity(
            entry_price=entry_price,
            stop_price=stop_price,
            risk_budget=budget,
            slippage_per_share=slippage,
            fees_per_share=fees_per_share,
            leverage=leverage,
        )

    def occupy_slot(
        self,
        symbol: str,
        security_id: str,
        entry_price: float,
        quantity: int,
        order_id: str,
        sl_order_id: str = "",
    ) -> SlotPosition:
        """
        Occupies one of the 3 available slots with ₹60,000 capital.
        """
        with self._lock:
            if symbol in self.active_slots:
                return self.active_slots[symbol]
            if not self.can_open_new_trade() and symbol not in self._reserved_slots:
                raise RuntimeError(
                    f"Cannot open trade on {symbol}: All {CAPITAL_CONFIG.MAX_ACTIVE_TRADES} capital slots are occupied."
                )
            slot = SlotPosition(
                symbol=symbol,
                security_id=security_id,
                entry_price=entry_price,
                quantity=quantity,
                order_id=order_id,
                sl_order_id=sl_order_id,
            )
            self._reserved_slots.pop(symbol, None)
            self.active_slots[symbol] = slot
            self._persist_queue(self.priority_queue)
        logger.info(
            "Capital Slot Occupied: %s (Qty: %d @ ₹%.2f, Slots Active: %d/%d, Total Capital: ₹%.2f)",
            symbol, quantity, entry_price, len(self.active_slots),
            CAPITAL_CONFIG.MAX_ACTIVE_TRADES, CAPITAL_CONFIG.TOTAL_CAPITAL
        )
        return slot

    def release_slot(self, symbol: str, validator: Any = None) -> Optional[Dict[str, Any]]:
        """
        Releases ₹60,000 capital from active slot.
        If any trade was waiting in the Priority Queue, pops and returns it for immediate promotion.
        """
        with self._lock:
            removed = self.active_slots.pop(symbol, None)
            self._reserved_slots.pop(symbol, None)
        if removed:
            logger.info(
                "Capital Slot Released: %s (Freed ₹%.2f. Available Slots: %d/%d)",
                symbol, CAPITAL_CONFIG.PER_TRADE_ALLOCATION,
                CAPITAL_CONFIG.MAX_ACTIVE_TRADES - len(self.active_slots),
                CAPITAL_CONFIG.MAX_ACTIVE_TRADES
            )

        # FIFO Queue promotion
        validator = validator or getattr(self, "_queue_validator", None)
        now = time.time()
        while self.priority_queue:
            next_trade = self.priority_queue.pop(0)
            expires_at = float(next_trade.get("expires_at", 0.0) or 0.0)
            if expires_at and expires_at <= now:
                continue
            if validator is not None and not validator(next_trade):
                continue
            self._persist_queue(self.priority_queue)
            logger.info("Popping candidate %s from Priority Queue for immediate slot allocation", next_trade.get("symbol"))
            return next_trade
        self._persist_queue(self.priority_queue)
        return None

    def set_queue_validator(self, validator: Any) -> None:
        self._queue_validator = validator

    def enqueue_approved_trade(self, candidate_payload: Dict[str, Any]):
        """
        Enqueues an approved trade when all 3 slots are occupied.
        """
        # Avoid duplicate enqueuing
        existing_syms = {c.get("symbol") for c in self.priority_queue}
        if candidate_payload.get("symbol") not in existing_syms and candidate_payload.get("symbol") not in self.active_slots:
            candidate_payload["enqueued_at"] = time.time()
            candidate_payload.setdefault(
                "expires_at",
                candidate_payload["enqueued_at"] + float(
                    getattr(CAPITAL_CONFIG, "QUEUED_CANDIDATE_TTL_SECONDS", 300.0)
                ),
            )
            self.priority_queue.append(candidate_payload)
            self._persist_queue(self.priority_queue)
            logger.info(
                "Trade %s enqueued in Priority Queue (Queue position: #%d)",
                candidate_payload.get("symbol"), len(self.priority_queue)
            )

    def remove_from_queue(self, symbol: str) -> bool:
        initial_len = len(self.priority_queue)
        self.priority_queue = [c for c in self.priority_queue if c.get("symbol") != symbol]
        if len(self.priority_queue) < initial_len:
            self._persist_queue(self.priority_queue)
        return len(self.priority_queue) < initial_len

    def get_portfolio_summary(self) -> Dict[str, Any]:
        active_count = len(self.active_slots)
        deployed_capital = active_count * CAPITAL_CONFIG.PER_TRADE_ALLOCATION
        available_capital = CAPITAL_CONFIG.TOTAL_CAPITAL - deployed_capital

        return {
            "total_capital": CAPITAL_CONFIG.TOTAL_CAPITAL,
            "per_trade_allocation": CAPITAL_CONFIG.PER_TRADE_ALLOCATION,
            "max_slots": CAPITAL_CONFIG.MAX_ACTIVE_TRADES,
            "active_slots_count": active_count,
            "available_slots_count": CAPITAL_CONFIG.MAX_ACTIVE_TRADES - active_count,
            "deployed_capital": deployed_capital,
            "available_capital": available_capital,
            "reserved_slots_count": len(self._reserved_slots),
            "active_positions": [slot.to_dict() for slot in self.active_slots.values()],
            "priority_queue": self.priority_queue,
        }


_slot_manager_singleton: Optional[CapitalSlotManager] = None

def get_capital_slot_manager() -> CapitalSlotManager:
    global _slot_manager_singleton
    if _slot_manager_singleton is None:
        _slot_manager_singleton = CapitalSlotManager(restore_persisted=True)
    return _slot_manager_singleton
