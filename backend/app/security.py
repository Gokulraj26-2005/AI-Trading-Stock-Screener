# backend/app/security.py
import os
import hashlib
import logging
import jwt
from datetime import datetime, timedelta, timezone


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _load_jwt_secret() -> str:
    secret = (os.getenv("JWT_SECRET") or "").strip()
    
    # For local/personal use, generate a deterministic secret if not provided
    if not secret:
        local_dev_mode = not _env_bool("PRODUCTION_MODE", False)
        if local_dev_mode:
            logger.info("JWT_SECRET not set - using deterministic local development key")
            # Generate a deterministic 32+ char secret based on machine name
            import platform
            machine_id = platform.node() or "localhost"
            default_secret = f"local-dev-secret-{machine_id}-padding-to-32-chars-minimum-length"
            return hashlib.sha256(default_secret.encode("utf-8")).hexdigest()
        raise RuntimeError("JWT_SECRET environment variable is required in production mode")

    # Legacy/dev environments may still use short placeholders.
    # Derive a deterministic 32-byte-plus HMAC key from the configured value.
    if len(secret) < 32:
        production_mode = _env_bool("PRODUCTION_MODE", False)
        allow_weak_secret = _env_bool("ALLOW_WEAK_JWT_SECRET", False)
        if not allow_weak_secret:
            raise RuntimeError(
                "JWT_SECRET must be at least 32 characters when "
                "ALLOW_WEAK_JWT_SECRET is disabled"
            )

        logger.warning(
            "JWT_SECRET is shorter than recommended length (32). "
            "Using SHA-256 derived key material for token signing. "
            "Set ALLOW_WEAK_JWT_SECRET=false to enforce strict policy."
        )
        if production_mode:
            logger.warning(
                "PRODUCTION_MODE is enabled while weak JWT secret mode is active. "
                "Set ALLOW_WEAK_JWT_SECRET=false after rotating to a 32+ character JWT_SECRET."
            )
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    return secret


# Check if JWT auth should be disabled for local development
DISABLE_JWT_AUTH = _env_bool("DISABLE_JWT_AUTH", False)

JWT_SECRET = _load_jwt_secret()

def create_jwt(payload: dict, expires_seconds: int = 86400) -> str:
    to_encode = payload.copy()
    now = datetime.now(timezone.utc)
    to_encode.update({"iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=expires_seconds)).timestamp())})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")

def decode_jwt(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
