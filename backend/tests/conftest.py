"""Pytest config for backend unit tests.

Adds `backend/src/` to sys.path so tests can `from core.oauth import ...`
the same way the application code does (the app expects `src` to be on
PYTHONPATH; tests inherit that convention).

Also pins the env vars OAuth + the firmware mount need so the app
imports cleanly in a no-network, no-/firmware environment.
"""

import os
import sys
import tempfile

# 1. sys.path setup: backend/src/ → import root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 2. Firmware mount point — main.py mkdirs it at import time. Default
# `/firmware` is read-only on most dev boxes; redirect to a tmpdir.
os.environ.setdefault("STRA2US_FIRMWARE_DIR", tempfile.mkdtemp(prefix="stra2us_test_firmware_"))

# 3. OAuth env vars — needed by core.oauth's required-env helpers
# whenever a test exercises a code path that calls them. Set defaults
# here so individual tests don't have to repeat them; tests that need
# to exercise the unset/disabled path monkeypatch over the top.
os.environ.setdefault("STRA2US_GOOGLE_OAUTH_ENABLED", "1")
os.environ.setdefault("STRA2US_GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("STRA2US_GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("STRA2US_OAUTH_REDIRECT_URI", "http://testserver/oauth/google/callback")

# 4. Cookie escape hatch — TestClient runs over plain HTTP so Secure-
# flagged cookies would be silently dropped, breaking the round-trip
# assertions.
os.environ.setdefault("STRA2US_COOKIE_INSECURE", "1")
