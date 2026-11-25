# backend/app/state/scan_state.py
"""
ScanStateService + MacroRegimeService
--------------------------------------
Typed replacements for the former module-level globals in main.py:

    _scan_status_cache  -> ScanStateService
    _scan_in_progress   -> ScanStateService.acquire/release_scan_slot
    _scan_state_lock    -> internal asyncio.Lock (hidden)
    _macro_regime_cache -> MacroRegimeService

Both services are stored on ``app.state`` at startup and injected into
routes via the ``get_scan_state`` / ``get_macro_regime`` dependency
functions at the bottom of this module.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Request

from services.regime_engine import DEFAULT_WEIGHTS


# ── ScanStateService ──────────────────────────────────────────────────────────

class ScanStateService:
    """Owns all scan-pipeline state: status dict + exclusive scan slot."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None  # Lazy — created inside running event loop
        self._in_progress: bool = False
        self._status: Dict[str, Any] = {
            "status": "idle",
            "progress": 0,
            "total": 0,
            "last_scan_time": None,
            "ai_analyzed_results": [],
            "data_source": "none",
            "blocked_reason": None,
        }

    # ── read/write helpers ────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return a shallow copy of the current status dict."""
        return dict(self._status)

    def update_status(self, **kwargs: Any) -> None:
        """Merge *kwargs* into the status dict."""
        self._status.update(kwargs)

    def set_field(self, key: str, value: Any) -> None:
        self._status[key] = value

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    # ── scan slot (mutex) ─────────────────────────────────────────────────

    async def acquire_scan_slot(self) -> bool:
        """
        Attempt to acquire the exclusive scan slot.

        Returns ``True`` on success, ``False`` if a scan is already running.
        Thread-safe via the internal asyncio.Lock.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._in_progress:
                return False
            self._in_progress = True
            return True

    async def release_scan_slot(self) -> None:
        """Release the exclusive scan slot."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._in_progress = False

    # ── convenience snapshot (for telemetry / external callers) ──────────

    def snapshot(self) -> Dict[str, Any]:
        return self.get_status()


# ── MacroRegimeService ────────────────────────────────────────────────────────

class MacroRegimeService:
    """Owns the macro-regime classification result."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {
            "regime": "Calm Uptrend",
            "weights": dict(DEFAULT_WEIGHTS),
            "probabilities": {
                "Calm Uptrend": 1.0,
                "Calm Downtrend": 0.0,
                "High-Vol Trend": 0.0,
                "High-Vol Chop": 0.0,
            },
            "india_vix": None,
            "updated_at": None,
            "data_source": "bootstrap",
        }

    def update(self, **kwargs: Any) -> None:
        self._data.update(kwargs)

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def regime(self) -> str:
        return str(self._data.get("regime", "Calm Uptrend"))

    @property
    def india_vix(self) -> Optional[float]:
        return self._data.get("india_vix")

    @property
    def data_source(self) -> str:
        return str(self._data.get("data_source", "bootstrap"))


# ── Module-level global accessors (for non-FastAPI contexts e.g. AIObserver, Orchestrator) ──

_global_scan_state_svc: Optional[ScanStateService] = None
_global_macro_regime_svc: Optional[MacroRegimeService] = None


def set_global_scan_state(svc: ScanStateService) -> None:
    """Called once at lifespan startup to expose the scan state service globally."""
    global _global_scan_state_svc
    _global_scan_state_svc = svc


def get_global_scan_state() -> Optional[ScanStateService]:
    """Non-FastAPI accessor for the scan state service."""
    return _global_scan_state_svc


def set_global_macro_regime(svc: MacroRegimeService) -> None:
    """Called once at lifespan startup to expose the regime service globally."""
    global _global_macro_regime_svc
    _global_macro_regime_svc = svc


def get_global_macro_regime() -> Optional[MacroRegimeService]:
    """Non-FastAPI accessor for the macro regime service (e.g. AIObserverService)."""
    return _global_macro_regime_svc


# ── FastAPI dependency wrappers ───────────────────────────────────────────────

def get_scan_state(request: Request) -> ScanStateService:
    """FastAPI dependency: inject ScanStateService from app.state."""
    svc = getattr(request.app.state, "scan_state", None)
    if svc is None:
        if _global_scan_state_svc is not None:
            return _global_scan_state_svc
        svc = ScanStateService()
        request.app.state.scan_state = svc
    return svc


def get_macro_regime(request: Request) -> MacroRegimeService:
    """FastAPI dependency: inject MacroRegimeService from app.state."""
    svc = getattr(request.app.state, "macro_regime", None)
    if svc is None:
        if _global_macro_regime_svc is not None:
            return _global_macro_regime_svc
        svc = MacroRegimeService()
        request.app.state.macro_regime = svc
    return svc
