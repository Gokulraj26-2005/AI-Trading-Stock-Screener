# backend/tests/unit/test_security_baseline.py
"""
Unit tests for the startup security baseline check in app.py.
Tests both dev-warn and production hard-fail behaviour.
"""
import os
import pytest


def _run_baseline(
    *,
    jwt_secret: str,
    production_mode: str = "false",
    allow_weak: str = "false",
    allowed_origins: str = "",
    expect_error: bool = False,
    expect_warning: bool = False,
) -> None:
    """
    Run _security_baseline_check() with the given env overrides and assert
    the expected outcome (error / warning / clean).
    """
    import importlib
    import app_config
    import app as app_module

    env_patch = {
        "JWT_SECRET": jwt_secret,
        "PRODUCTION_MODE": production_mode,
        "ALLOW_WEAK_JWT_SECRET": allow_weak,
        "ALLOWED_ORIGINS": allowed_origins,
    }

    saved = {k: os.environ.get(k) for k in env_patch}
    try:
        os.environ.update(env_patch)
        # Reload config so constants pick up the patched env
        importlib.reload(app_config)
        importlib.reload(app_module)

        if expect_error:
            with pytest.raises(RuntimeError) as exc_info:
                app_module._security_baseline_check()
            assert "Security baseline check failed" in str(exc_info.value)
        else:
            # Should not raise — may log warnings
            app_module._security_baseline_check()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Reload to restore original state
        importlib.reload(app_config)
        importlib.reload(app_module)


def test_baseline_passes_with_strong_secret():
    _run_baseline(
        jwt_secret="a-very-long-secure-jwt-secret-32chars!",
        production_mode="false",
        expect_error=False,
    )


def test_baseline_warns_in_dev_with_short_secret():
    """Dev mode should not raise even with a short secret."""
    _run_baseline(
        jwt_secret="short",
        production_mode="false",
        allow_weak="true",
        expect_error=False,
    )


def test_baseline_hard_fails_in_production_with_short_secret():
    """Production mode with a short, weak secret must hard-fail."""
    _run_baseline(
        jwt_secret="short",
        production_mode="true",
        allow_weak="true",
        expect_error=True,
    )


def test_baseline_hard_fails_in_production_with_allow_weak_enabled():
    """ALLOW_WEAK_JWT_SECRET=true in production must hard-fail."""
    _run_baseline(
        jwt_secret="a-very-long-secure-jwt-secret-32chars!",
        production_mode="true",
        allow_weak="true",
        allowed_origins="http://myapp.com",
        expect_error=True,
    )


def test_sensitive_endpoint_guard_check_has_no_findings_on_current_routes():
    import app as app_module

    findings = app_module._security_sensitive_endpoint_check(app_module.app)
    assert findings == []


def test_baseline_hard_fails_in_production_on_sensitive_guard_findings(monkeypatch):
    import importlib
    import app_config
    import app as app_module

    env_patch = {
        "JWT_SECRET": "a-very-long-secure-jwt-secret-32chars!",
        "PRODUCTION_MODE": "true",
        "ALLOW_WEAK_JWT_SECRET": "false",
        "ALLOWED_ORIGINS": "http://myapp.com",
    }
    saved = {k: os.environ.get(k) for k in env_patch}

    try:
        os.environ.update(env_patch)
        importlib.reload(app_config)
        importlib.reload(app_module)

        monkeypatch.setattr(
            app_module,
            "_security_sensitive_endpoint_check",
            lambda _application: ["forced-sensitive-guard-regression"],
        )

        with pytest.raises(RuntimeError) as exc_info:
            app_module._security_baseline_check(app_module.app)

        assert "forced-sensitive-guard-regression" in str(exc_info.value)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(app_config)
        importlib.reload(app_module)
