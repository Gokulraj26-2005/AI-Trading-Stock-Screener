import hashlib
import os
import subprocess
import sys


APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "app")
)


def _import_security(jwt_secret: str, allow_weak: bool | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["JWT_SECRET"] = jwt_secret
    if allow_weak is None:
        env.pop("ALLOW_WEAK_JWT_SECRET", None)
    else:
        env["ALLOW_WEAK_JWT_SECRET"] = "true" if allow_weak else "false"
    env["PYTHONPATH"] = APP_DIR + os.pathsep + env.get("PYTHONPATH", "")

    return subprocess.run(
        [sys.executable, "-c", "import security; print(security.JWT_SECRET)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_short_secret_rejected_when_weak_mode_disabled():
    result = _import_security("short-secret", allow_weak=False)

    assert result.returncode != 0
    combined = (result.stderr or "") + (result.stdout or "")
    assert "ALLOW_WEAK_JWT_SECRET is disabled" in combined


def test_short_secret_rejected_by_default_when_toggle_unset():
    result = _import_security("short-secret", allow_weak=None)

    assert result.returncode != 0
    combined = (result.stderr or "") + (result.stdout or "")
    assert "ALLOW_WEAK_JWT_SECRET is disabled" in combined


def test_short_secret_is_derived_when_weak_mode_enabled():
    result = _import_security("short-secret", allow_weak=True)

    assert result.returncode == 0
    expected = hashlib.sha256("short-secret".encode("utf-8")).hexdigest()
    assert expected in (result.stdout or "")


def test_long_secret_is_used_directly():
    long_secret = "test-jwt-secret-that-is-at-least-32-bytes-long"
    result = _import_security(long_secret, allow_weak=False)

    assert result.returncode == 0
    assert long_secret in (result.stdout or "")
