# backend/tests/conftest.py
"""
Shared pytest fixtures for the entire backend test suite.

This file is auto-loaded by pytest for both ``tests/unit/`` and
``tests/integration/`` sub-directories.
"""
import sys
import os

if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


import tempfile

_TEST_DB = tempfile.NamedTemporaryFile(suffix="_test_portfolio.db", delete=False)
_TEST_DB.close()
os.environ["PORTFOLIO_DB_PATH"] = _TEST_DB.name

# Bootstrap environment before any app imports.
# These are safe, non-production defaults that allow the app to import
# without real credentials.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-that-is-at-least-32-bytes-long")
os.environ.setdefault("DHAN_ENABLED", "true")
os.environ.setdefault("USE_DHAN_MOCK", "true")
os.environ.setdefault("PRODUCTION_MODE", "false")
os.environ.setdefault("FAIL_CLOSED_ON_LIVE_ERROR", "false")
os.environ.setdefault("ALLOW_WEAK_JWT_SECRET", "false")
os.environ["DISABLE_JWT_AUTH"] = "false"

# Ensure the app package directory is importable.
# We add backend/app/ directly so that ``import app_config``, ``import auth_helpers``,
# etc. all resolve without needing the ``app.`` prefix.
_BACKEND_APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")
)
if _BACKEND_APP_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_APP_DIR)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def init_test_db():
    """Ensure the isolated test portfolio database is initialized with all tables."""
    from db_store import init_db
    init_db()
    yield
    try:
        if os.path.exists(_TEST_DB.name):
            os.remove(_TEST_DB.name)
    except Exception:
        pass


@pytest.fixture(scope="session")
def app():
    """Return the assembled FastAPI application (session-scoped for speed)."""
    import main as main_module  # noqa: PLC0415
    _app = main_module.app
    return _app


@pytest.fixture
def client(app):
    """TestClient wrapping the assembled app within its lifespan context."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_user_store(monkeypatch):
    """In-memory user store — replaces db_store calls in tests."""
    users: dict = {}

    def _get_user(user_id: str):
        return users.get(user_id)

    def _create_user(user_id, email=None, name=None, broker_user_id=None):
        user_data = {"user_id": user_id, "email": email, "name": name, "broker_user_id": broker_user_id}
        users[user_id] = user_data
        return user_data

    import db_store
    monkeypatch.setattr(db_store, "get_user", _get_user)
    monkeypatch.setattr(db_store, "create_user", _create_user)
    return users


@pytest.fixture
def session_headers(client, monkeypatch, fake_user_store):
    """
    Mint a valid app session JWT and return it as an Authorization header dict.
    """
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "dhan-test-access-token")

    response = client.post("/api/auth/session-token")
    assert response.status_code == 200
    token = response.json().get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}
