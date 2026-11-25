# backend/app/services/dhan_token_service.py
"""
Dhan Token Persistence & Lifecycle Service
============================================
Ensures that once a Dhan 24-hour access token is acquired via consent login or .env,
it is persisted across container restarts, SQLite database restarts, and reboots.

Dhan access tokens have a 24-hour validity window. Users should NEVER have to
re-login on container restarts while their token remains valid.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Persistent file location inside the container (/app/app/.dhan_token maps to ./backend/app/.dhan_token on host)
_TOKEN_FILE_DIR = Path(__file__).resolve().parent.parent
_TOKEN_FILE = _TOKEN_FILE_DIR / ".dhan_token"

ENV_LOCATIONS = [
    Path("/app/.env"),
    Path("/workspace/.env"),
    Path(".env"),
    Path(__file__).resolve().parent.parent.parent / ".env",
]


def _is_valid_token_candidate(token: Optional[str], api_secret: Optional[str] = None) -> bool:
    """Check if a string looks like an authentic Dhan access token (JWT), not an API secret or expired."""
    if not token:
        return False
    t = token.strip().strip('"').strip("'")
    if not t:
        return False
    # If it matches the static 36-char UUID API secret, it is NOT an access token
    if api_secret and t == api_secret.strip():
        return False
    # Dhan API secrets are 36-character UUIDs (e.g. 00000000-0000-4000-8000-000000000000)
    # Dhan access tokens are JWT strings (typically starting with eyJ and >100 characters)
    # or at least not the known secret key
    if len(t) < 30:
        return False
    try:
        import jwt
        unverified = jwt.decode(t, options={"verify_signature": False})
        exp = unverified.get("exp")
        if exp and datetime.utcnow().timestamp() > exp:
            return False
    except Exception:
        pass
    return True


def get_dhan_token() -> Optional[str]:
    """
    Retrieve the active Dhan access token from:
      1. os.environ["DHAN_ACCESS_TOKEN"] (if it's a real token, not the API secret)
      2. SQLite database (portfolio.db -> broker_tokens table)
      3. Persistent JSON file (.dhan_token)
      4. Disk .env files

    If found in persistent storage, updates os.environ and memory automatically.
    """
    api_secret = os.getenv("DHAN_API_SECRET", "").strip()

    # 1. Check SQLite database (persists freshly logged-in user tokens)
    try:
        import db_store
        latest = db_store.load_latest_token()
        if latest and latest.get("access_token"):
            db_token = str(latest["access_token"]).strip()
            if _is_valid_token_candidate(db_token, api_secret):
                expires_at = latest.get("expires_at")
                is_expired = False
                if expires_at:
                    try:
                        exp_dt = datetime.fromisoformat(expires_at)
                        if datetime.utcnow() > exp_dt:
                            logger.info("Persisted SQLite Dhan token expired at %s", expires_at)
                            is_expired = True
                    except Exception:
                        pass
                if not is_expired and db_token:
                    os.environ["DHAN_ACCESS_TOKEN"] = db_token
                    return db_token
    except Exception as exc:
        logger.debug("Error checking SQLite token: %s", exc)

    # 2. Check in-memory env
    env_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if _is_valid_token_candidate(env_token, api_secret):
        return env_token

    # 3. Check persistent .dhan_token JSON file
    try:
        if _TOKEN_FILE.exists():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            file_token = str(data.get("access_token") or "").strip()
            if _is_valid_token_candidate(file_token, api_secret):
                expires_at = data.get("expires_at")
                if expires_at:
                    try:
                        exp_dt = datetime.fromisoformat(expires_at)
                        if datetime.utcnow() > exp_dt:
                            logger.info("Persisted file Dhan token expired at %s", expires_at)
                            file_token = ""
                    except Exception:
                        pass
                if file_token:
                    logger.info("Restored active Dhan access token from .dhan_token file")
                    os.environ["DHAN_ACCESS_TOKEN"] = file_token
                    return file_token
    except Exception as exc:
        logger.debug("Error reading .dhan_token file: %s", exc)

    # 4. Check disk .env files
    for p in ENV_LOCATIONS:
        try:
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    line = line.strip()
                    if line.startswith("DHAN_ACCESS_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if _is_valid_token_candidate(val, api_secret):
                            logger.info("Restored active Dhan access token from %s", p)
                            os.environ["DHAN_ACCESS_TOKEN"] = val
                            return val
        except Exception:
            continue

    return None


def save_dhan_token(
    access_token: str,
    expires_at: Optional[str] = None,
    user_id: str = "default",
) -> None:
    """
    Persist an active Dhan access token to:
      1. os.environ["DHAN_ACCESS_TOKEN"]
      2. SQLite database (portfolio.db)
      3. Persistent file (.dhan_token)
      4. Disk .env files (host-mounted)
    """
    cleaned_token = access_token.strip().strip('"').strip("'")
    if not cleaned_token:
        return

    os.environ["DHAN_ACCESS_TOKEN"] = cleaned_token

    # 1. SQLite Database
    try:
        import db_store
        db_store.save_token(
            user_id=user_id,
            access_token=cleaned_token,
            expires_at=expires_at,
        )
        logger.info("Dhan access token saved to SQLite database for user %s", user_id)
    except Exception as exc:
        logger.warning("Could not persist Dhan token to SQLite db: %s", exc)

    # 2. Persistent .dhan_token file on mounted volume
    try:
        payload = {
            "access_token": cleaned_token,
            "expires_at": expires_at,
            "saved_at": datetime.utcnow().isoformat(),
            "user_id": user_id,
        }
        _TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Dhan access token written to %s", _TOKEN_FILE)
    except Exception as exc:
        logger.warning("Could not persist Dhan token to %s: %s", _TOKEN_FILE, exc)

    # 3. Disk .env files
    for env_path in ENV_LOCATIONS:
        try:
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
                new_lines = []
                found = False
                for line in lines:
                    if line.startswith("DHAN_ACCESS_TOKEN="):
                        new_lines.append(f"DHAN_ACCESS_TOKEN={cleaned_token}")
                        found = True
                    else:
                        new_lines.append(line)
                if not found:
                    new_lines.append(f"DHAN_ACCESS_TOKEN={cleaned_token}")
                env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                logger.info("Dhan access token updated in %s", env_path)
                break
        except Exception as exc:
            logger.debug("Could not write token to %s: %s", env_path, exc)


async def verify_dhan_token(
    token: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Verify the Dhan access token against Dhan's /fundlimit endpoint."""
    t = token or get_dhan_token()
    cid = (
        client_id
        or os.getenv("DHAN_USER_ID")
        or os.getenv("DHAN_CLIENT_ID")
        or os.getenv("DHAN_API_KEY")
    )
    if not t or not cid:
        return False, None

    base_url = str(os.getenv("DHAN_BASE_URL") or "https://api.dhan.co/v2").rstrip("/")
    try:
        import httpx
        headers = {
            "access-token": t,
            "client-id": str(cid).strip(),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{base_url}/fundlimit", headers=headers)
            if resp.status_code == 200:
                return True, resp.json()
            logger.warning("Dhan token verification failed with status %d: %s", resp.status_code, resp.text[:200])
            return False, None
    except Exception as exc:
        logger.warning("Error verifying Dhan token against %s/fundlimit: %s", base_url, exc)
        return False, None
