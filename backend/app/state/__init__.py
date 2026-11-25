# backend/app/state/__init__.py
"""
State service layer.

All process-level mutable state lives here as typed service objects.
They are instantiated once at startup and attached to ``app.state``.
Router modules access them via thin FastAPI ``Depends`` wrappers defined
in each service module.
"""
from .scan_state import ScanStateService, MacroRegimeService
from .session_state import SessionService

__all__ = ["ScanStateService", "MacroRegimeService", "SessionService"]
