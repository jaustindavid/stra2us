"""Pytest config for backend unit tests.

Adds `backend/src/` to sys.path so tests can `from core.oauth import ...`
the same way the application code does (the app expects `src` to be on
PYTHONPATH; tests inherit that convention).

After P5 followup #2 (build-context consolidation), the backend
also imports from `stra2us_cli` (the catalog model + lint module +
sanitizers). The image installs the package via `pip install
/tools` at build time, so production runtime resolution Just
Works. For local pytest runs, we add `tools/` to sys.path here so
the import resolves without requiring a local `pip install -e
../tools`. Either way, same module.

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

# 2. tools/ → stra2us_cli import root (post-#2). Mirrors what
# `pip install /tools` does inside the image.
_TOOLS = os.path.normpath(os.path.join(_HERE, "..", "..", "tools"))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

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
