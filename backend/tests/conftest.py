"""Pytest config for backend unit tests.

Adds `backend/src/` to sys.path so tests can `from core.oauth import ...`
the same way the application code does (the app expects `src` to be on
PYTHONPATH; tests inherit that convention).

Also pins the env vars OAuth needs so the app imports cleanly in a
no-network environment.
"""

import os
import sys

# 1. sys.path setup: backend/src/ → import root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 2. OAuth env vars — needed by core.oauth's required-env helpers
# whenever a test exercises a code path that calls them. Set defaults
# here so individual tests don't have to repeat them; tests that need
# to exercise the unset/disabled path monkeypatch over the top.
os.environ.setdefault("STRA2US_GOOGLE_OAUTH_ENABLED", "1")
os.environ.setdefault("STRA2US_GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("STRA2US_GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("STRA2US_OAUTH_REDIRECT_URI", "http://testserver/oauth/google/callback")

# 3. Cookie escape hatch — TestClient runs over plain HTTP so Secure-
# flagged cookies would be silently dropped, breaking the round-trip
# assertions.
os.environ.setdefault("STRA2US_COOKIE_INSECURE", "1")
