# backend/app/state/session_state.py
"""
SessionService
--------------
Typed replacement for the former module-level globals in main.py:

    _session_bootstrap_hash        -> SessionService
    _session_bootstrap_expires_at  -> SessionService
    _session_bootstrap_lock        -> internal asyncio.Lock (hidden)
    _stream_token_generation       -> SessionService
    _stream_token_generation_lock  -> internal threading.Lock (hidden)

Stored on ``app.state.session`` at startup; injected via
``get_session_service`` dependency.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request


class SessionService:
    """Owns session-bootstrap key lifecycle and stream-token generation."""

    def __init__(self, bootstrap_ttl_seconds: int = 300) -> None:
        self._bootstrap_ttl_seconds = bootstrap_ttl_seconds

        # Bootstrap key state (protected by async lock)
        self._bootstrap_lock: asyncio.Lock = asyncio.Lock()
        self._bootstrap_hash: Optional[str] = None
        self._bootstrap_expires_at: Optional[datetime] = None

        # Stream token generation counter (protected by threading lock for
        # safe access from sync + async contexts)
        self._generation_lock: threading.Lock = threading.Lock()
        self._stream_generation: int = 1

    # ── bootstrap key helpers ─────────────────────────────────────────────

    @staticmethod
    def _hash_secret(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def issue_bootstrap_key(self) -> tuple[str, int]:
        """
        Generate and store a one-time session bootstrap key.

        Returns ``(plain_key, ttl_seconds)``.  The plain key is returned to
        the caller (token exchange endpoint) for transmission to the frontend.
        Only the SHA-256 hash is stored in memory.
        """
        key = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(seconds=self._bootstrap_ttl_seconds)

        async with self._bootstrap_lock:
            self._bootstrap_hash = self._hash_secret(key)
            self._bootstrap_expires_at = expires_at

        return key, self._bootstrap_ttl_seconds

    async def consume_bootstrap_key(self, candidate: str) -> bool:
        """
        Validate and consume the bootstrap key (one-shot).

        Returns ``True`` on success, ``False`` on invalid/expired/missing.
        """
        cleaned = str(candidate or "").strip()
        if not cleaned:
            return False

        candidate_hash = self._hash_secret(cleaned)
        now = datetime.utcnow()

        async with self._bootstrap_lock:
            expected_hash = self._bootstrap_hash
            expires_at = self._bootstrap_expires_at

            if not expected_hash or not expires_at:
                return False

            if now >= expires_at:
                self._clear_bootstrap_key_locked()
                return False

            if not hmac.compare_digest(expected_hash, candidate_hash):
                return False

            self._clear_bootstrap_key_locked()
            return True

    def _clear_bootstrap_key_locked(self) -> None:
        """Must be called with _bootstrap_lock held."""
        self._bootstrap_hash = None
        self._bootstrap_expires_at = None

    def clear_bootstrap_key(self) -> None:
        """Synchronous clear — used by daily-reset and logout paths."""
        # We deliberately do NOT hold the async lock here because callers
        # are in sync context (scheduler callbacks).  Race window is tiny
        # and the worst outcome is a stale key that expires naturally.
        self._bootstrap_hash = None
        self._bootstrap_expires_at = None

    # ── stream-token generation counter ──────────────────────────────────

    def current_stream_generation(self) -> int:
        with self._generation_lock:
            return int(self._stream_generation)

    def rotate_stream_generation(self) -> int:
        """Bump generation counter — invalidates all issued stream tokens."""
        with self._generation_lock:
            self._stream_generation += 1
            return int(self._stream_generation)


# ── FastAPI dependency wrapper ────────────────────────────────────────────────

def get_session_service(request: Request) -> SessionService:
    """FastAPI dependency: inject SessionService from app.state."""
    svc = getattr(request.app.state, "session", None)
    if svc is None:
        svc = SessionService()
        request.app.state.session = svc
    return svc
