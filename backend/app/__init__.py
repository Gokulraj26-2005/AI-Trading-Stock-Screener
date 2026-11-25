# AI Portfolio Backend Package
import sys
from pathlib import Path

# Ensure the package directory is available on sys.path so local top-level imports work
# when the package is executed via uvicorn or as a module from outside the app directory.
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from .app import app, _security_baseline_check, _security_sensitive_endpoint_check
except ImportError:
    pass
