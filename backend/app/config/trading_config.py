"""
Institutional Trading & Capital Management Configuration.
==========================================================
Enforces strict capital sizing, risk boundaries, and execution rules:
- Master Execution Switch: ON / OFF execution before Telegram & MCP placement
- Total Capital: Customizable (Default: ₹1,80,000)
- Allocation Per Trade: Customizable (Default: ₹60,000)
- Max Concurrent Active Trades: Customizable (Default: 3 trades)
- Hard Stop Loss Floor: Customizable (Default: 1.0% maximum)
- Breakeven Trigger: Customizable (Default: +0.8% gain)
- Structural Support Trailing: Customizable (Default: +1.5% gain)
- Overbought Exhaustion: 5m RSI >= 80 (Evaluates CVD and 20-level market depth)
- Auto-Purge Invalidation: Customizable (Default: 0.20% slip below trigger)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


SETTINGS_FILE_PATH = Path(__file__).resolve().parent.parent / "data" / "watchdog_settings.json"


@dataclass
class TradingCapitalConfig:
    # Master Execution Switch (ON/OFF before reaching Telegram & MCP placement)
    execution_enabled: bool = True

    # Capital Allocation
    TOTAL_CAPITAL: float = 180_000.0        # Total capital allocated for live trading desk
    PER_TRADE_ALLOCATION: float = 60_000.0  # Fixed capital allocated per trade
    MAX_ACTIVE_TRADES: int = 3              # Max concurrent active positions (e.g. 3 x 60k = 180k)
    MIS_LEVERAGE: float = 5.0               # Dynamic Intraday (MIS) leverage multiplier (e.g. 5x on NSE EQ)
    MAX_RISK_PER_TRADE: float = 600.0       # Maximum planned loss per trade before fees/slippage
    SLIPPAGE_BUFFER_PER_SHARE: float = 0.05
    FALLBACK_PAPER_ON_BROKER_ERROR: bool = False  # Never turn a configured LIVE broker rejection into a simulated fill

    # Risk Controls
    MAX_HARD_STOP_LOSS_PCT: float = 0.010   # 1.0% Maximum Hard SL Floor (e.g. 0.010 = 1.0%)
    BREAKEVEN_TRIGGER_PCT: float = 0.008    # +0.8% Profit triggers Breakeven Lock
    FEE_BUFFER_PCT: float = 0.0010          # +0.10% buffer above entry for STT/brokerage coverage
    STRUCTURAL_TRAIL_PCT: float = 0.015     # +1.5% Profit activates 5m VWAP / 20-EMA structural trailing
    TIGHT_RUNNER_TRAIL_PCT: float = 0.0035  # 0.35% trailing gap behind price during aggressive runner

    # Qualification & Gatekeeping
    MIN_POTENTIAL_SCORE: int = 85           # Tier A+ setups only
    TRIGGER_SUSTAIN_TICKS: int = 40         # ~2 minutes duration persistence at 3s ticks
    MAX_SLIP_COUNT: int = 2                 # Up to 2 micro-pullbacks tolerated
    RETEST_TOLERANCE_PCT: float = 0.0015    # 0.15% retest buffer below trigger
    MAX_ENTRY_RSI_5M: float = 73.0          # Block entry if 5m RSI >= 73
    MIN_ENTRY_RVOL: float = 1.0             # Must have at least 1.0x average volume
    PURGE_SLIP_TOLERANCE_PCT: float = 0.0020 # Auto-evict from desk if slips > 0.20% below trigger before fill
    QUEUED_CANDIDATE_TTL_SECONDS: float = 300.0
    PROMOTION_CUTOFF_TIME_IST: str = "15:00" # No new promotions after 15:00 IST (prior to 15:10 Dhan MIS square-off)
    MAX_AGGREGATOR_QUOTE_AGE_MS: int = 15_000 # Max quote age for aggregator ticks during promotion
    MAX_REST_QUOTE_AGE_MS: int = 15_000       # Max quote age for REST quotes during promotion

    # Overbought Momentum Audit
    RSI_OVERBOUGHT_THRESHOLD: float = 80.0  # RSI 80+ audit with CVD & depth
    BID_ASK_EXHAUSTION_RATIO: float = 0.75  # Depth ratio indicating buyer exhaustion

    # Dhan MCP & Telegram Settings
    DHAN_MCP_ENDPOINT: str = field(default_factory=lambda: os.getenv("DHAN_MCP_ENDPOINT", "https://mcp.dhan.co/mcp"))
    DHAN_BASE_API_URL: str = field(default_factory=lambda: os.getenv("DHAN_BASE_API_URL", "https://api.dhan.co/v2"))
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    def to_dict(self) -> Dict[str, Any]:
        """Returns JSON-serializable dictionary representation of current settings."""
        return {
            "execution_enabled": self.execution_enabled,
            "total_capital": float(self.TOTAL_CAPITAL),
            "per_trade_allocation": float(self.PER_TRADE_ALLOCATION),
            "max_active_trades": int(self.MAX_ACTIVE_TRADES),
            "mis_leverage": float(self.MIS_LEVERAGE),
            "fallback_paper_on_broker_error": bool(self.FALLBACK_PAPER_ON_BROKER_ERROR),
            "max_hard_stop_loss_pct": float(self.MAX_HARD_STOP_LOSS_PCT),
            "max_hard_stop_loss_percent": round(self.MAX_HARD_STOP_LOSS_PCT * 100.0, 2),
            "breakeven_trigger_pct": float(self.BREAKEVEN_TRIGGER_PCT),
            "breakeven_trigger_percent": round(self.BREAKEVEN_TRIGGER_PCT * 100.0, 2),
            "fee_buffer_pct": float(self.FEE_BUFFER_PCT),
            "fee_buffer_percent": round(self.FEE_BUFFER_PCT * 100.0, 2),
            "structural_trail_pct": float(self.STRUCTURAL_TRAIL_PCT),
            "structural_trail_percent": round(self.STRUCTURAL_TRAIL_PCT * 100.0, 2),
            "purge_slip_tolerance_pct": float(self.PURGE_SLIP_TOLERANCE_PCT),
            "purge_slip_tolerance_percent": round(self.PURGE_SLIP_TOLERANCE_PCT * 100.0, 2),
            "min_potential_score": int(self.MIN_POTENTIAL_SCORE),
            "max_entry_rsi_5m": float(self.MAX_ENTRY_RSI_5M),
            "min_entry_rvol": float(self.MIN_ENTRY_RVOL),
            "telegram_configured": bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID),
        }

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dynamically updates parameters and persists them to disk.
        """
        if "execution_enabled" in updates:
            self.execution_enabled = bool(updates["execution_enabled"])

        if "total_capital" in updates and float(updates["total_capital"]) > 0:
            self.TOTAL_CAPITAL = float(updates["total_capital"])

        if "per_trade_allocation" in updates and float(updates["per_trade_allocation"]) > 0:
            self.PER_TRADE_ALLOCATION = float(updates["per_trade_allocation"])

        if "max_active_trades" in updates and int(updates["max_active_trades"]) > 0:
            self.MAX_ACTIVE_TRADES = int(updates["max_active_trades"])

        if "mis_leverage" in updates and float(updates["mis_leverage"]) > 0:
            self.MIS_LEVERAGE = float(updates["mis_leverage"])

        if "fallback_paper_on_broker_error" in updates:
            self.FALLBACK_PAPER_ON_BROKER_ERROR = bool(updates["fallback_paper_on_broker_error"])

        # Automatically align total_capital if not explicitly overridden
        if "per_trade_allocation" in updates and "max_active_trades" in updates and "total_capital" not in updates:
            self.TOTAL_CAPITAL = self.PER_TRADE_ALLOCATION * self.MAX_ACTIVE_TRADES

        # Stop loss percentage handling (accepts either 1.0 or 0.010)
        if "max_hard_stop_loss_percent" in updates:
            val = float(updates["max_hard_stop_loss_percent"])
            self.MAX_HARD_STOP_LOSS_PCT = round(val / 100.0, 4) if val > 0.05 else round(val, 4)
        elif "max_hard_stop_loss_pct" in updates:
            self.MAX_HARD_STOP_LOSS_PCT = float(updates["max_hard_stop_loss_pct"])

        # Breakeven percentage handling
        if "breakeven_trigger_percent" in updates:
            val = float(updates["breakeven_trigger_percent"])
            self.BREAKEVEN_TRIGGER_PCT = round(val / 100.0, 4) if val > 0.05 else round(val, 4)
        elif "breakeven_trigger_pct" in updates:
            self.BREAKEVEN_TRIGGER_PCT = float(updates["breakeven_trigger_pct"])

        # Structural trail percentage handling
        if "structural_trail_percent" in updates:
            val = float(updates["structural_trail_percent"])
            self.STRUCTURAL_TRAIL_PCT = round(val / 100.0, 4) if val > 0.05 else round(val, 4)
        elif "structural_trail_pct" in updates:
            self.STRUCTURAL_TRAIL_PCT = float(updates["structural_trail_pct"])

        # Auto-purge tolerance
        if "purge_slip_tolerance_percent" in updates:
            val = float(updates["purge_slip_tolerance_percent"])
            self.PURGE_SLIP_TOLERANCE_PCT = round(val / 100.0, 4) if val > 0.05 else round(val, 4)
        elif "purge_slip_tolerance_pct" in updates:
            self.PURGE_SLIP_TOLERANCE_PCT = float(updates["purge_slip_tolerance_pct"])

        self.save_to_disk()
        return self.to_dict()

    def toggle_execution(self, enabled: Optional[bool] = None) -> bool:
        """Toggles or sets the master execution switch and persists."""
        if enabled is not None:
            self.execution_enabled = bool(enabled)
        else:
            self.execution_enabled = not self.execution_enabled
        self.save_to_disk()
        return self.execution_enabled

    def save_to_disk(self) -> None:
        """Persists mutable settings to data/watchdog_settings.json."""
        try:
            SETTINGS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "execution_enabled": self.execution_enabled,
                "TOTAL_CAPITAL": self.TOTAL_CAPITAL,
                "PER_TRADE_ALLOCATION": self.PER_TRADE_ALLOCATION,
                "MAX_ACTIVE_TRADES": self.MAX_ACTIVE_TRADES,
                "MIS_LEVERAGE": self.MIS_LEVERAGE,
                "FALLBACK_PAPER_ON_BROKER_ERROR": self.FALLBACK_PAPER_ON_BROKER_ERROR,
                "MAX_HARD_STOP_LOSS_PCT": self.MAX_HARD_STOP_LOSS_PCT,
                "BREAKEVEN_TRIGGER_PCT": self.BREAKEVEN_TRIGGER_PCT,
                "FEE_BUFFER_PCT": self.FEE_BUFFER_PCT,
                "STRUCTURAL_TRAIL_PCT": self.STRUCTURAL_TRAIL_PCT,
                "PURGE_SLIP_TOLERANCE_PCT": self.PURGE_SLIP_TOLERANCE_PCT,
            }
            with open(SETTINGS_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            print(f"Warning: Could not save watchdog settings to disk: {exc}")

    def load_from_disk(self) -> None:
        """Restores persisted settings if file exists."""
        if not SETTINGS_FILE_PATH.exists():
            return
        try:
            with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "execution_enabled" in data:
                self.execution_enabled = bool(data["execution_enabled"])
            if "TOTAL_CAPITAL" in data:
                self.TOTAL_CAPITAL = float(data["TOTAL_CAPITAL"])
            if "PER_TRADE_ALLOCATION" in data:
                self.PER_TRADE_ALLOCATION = float(data["PER_TRADE_ALLOCATION"])
            if "MAX_ACTIVE_TRADES" in data:
                self.MAX_ACTIVE_TRADES = int(data["MAX_ACTIVE_TRADES"])
            if "MIS_LEVERAGE" in data:
                self.MIS_LEVERAGE = float(data["MIS_LEVERAGE"])
            if "FALLBACK_PAPER_ON_BROKER_ERROR" in data:
                self.FALLBACK_PAPER_ON_BROKER_ERROR = bool(data["FALLBACK_PAPER_ON_BROKER_ERROR"])
            if "MAX_HARD_STOP_LOSS_PCT" in data:
                self.MAX_HARD_STOP_LOSS_PCT = float(data["MAX_HARD_STOP_LOSS_PCT"])
            if "BREAKEVEN_TRIGGER_PCT" in data:
                self.BREAKEVEN_TRIGGER_PCT = float(data["BREAKEVEN_TRIGGER_PCT"])
            if "FEE_BUFFER_PCT" in data:
                self.FEE_BUFFER_PCT = float(data["FEE_BUFFER_PCT"])
            if "STRUCTURAL_TRAIL_PCT" in data:
                self.STRUCTURAL_TRAIL_PCT = float(data["STRUCTURAL_TRAIL_PCT"])
            if "PURGE_SLIP_TOLERANCE_PCT" in data:
                self.PURGE_SLIP_TOLERANCE_PCT = float(data["PURGE_SLIP_TOLERANCE_PCT"])
        except Exception as exc:
            print(f"Warning: Could not load watchdog settings from disk: {exc}")


CAPITAL_CONFIG = TradingCapitalConfig()
CAPITAL_CONFIG.load_from_disk()
