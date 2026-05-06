"""Layer 1 unit tests for the Google OAuth flow (Phase 1 of v1.5).

Mocks Google's token endpoint + ID-token validation so the tests run
hermetically (no network, no JWKS fetches). Exercises the callback
handler's branching logic in isolation per the test plan in
`docs/fr_v15_auth.md > OAuth test plan > Layer 1`.

Layers 2 (integration with a fake Google) and 3 (manual smoke on
staging) are intentionally not in this file — Phase 1 ships with
Layer 1 only; Layer 2 lands when the middleware integration goes in
during Phase 2.
"""

from __future__ import annotations

import os
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Minimal async-compatible redis stand-in. Backed by a dict so
    tests can preload `admin_acls:<email>` keys (or omit them to
    simulate the unauthorized case).

    Only the calls that routes_oauth.py and the activity-log middleware
    make are implemented — `get` and `xadd`. Adding more is fine when a
    test needs them; failing loudly on missing methods is preferable to
    silently no-oping."""

    def __init__(self, store: Optional[dict] = None):
        self.store: dict = dict(store or {})
        self.xadd_calls: list = []

    async def get(self, key):
        # routes_oauth uses bytes-mode redis (decode_responses=False),
        # so values come back as bytes. Mirror that to catch any code
        # that accidentally treats them as str.
        v = self.store.get(key)
        if v is None:
            return None
        return v.encode("utf-8") if isinstance(v, str) else v

    async def xadd(self, *args, **kwargs):
        self.xadd_calls.append((args, kwargs))
        return b"0-0"


@pytest.fixture
def fake_redis():
    return _FakeRedis()


@pytest.fixture
def client(fake_redis):
    """TestClient with the redis singleton swapped for `fake_redis`.

    Patches `get_redis_client` at every module that did `from
    core.redis_client import get_redis_client` (the name is bound at
    import time, so patching only `core.redis_client.get_redis_client`
    leaves stale references in callers). Importing `main` here (rather
    than at module load) keeps the conftest env-var setup from racing
    with the import."""
    # Force `main` import first so all the routers are registered and
    # their `from ... import get_redis_client` bindings exist.
    from main import app  # noqa: F401
    targets = [
        "core.redis_client.get_redis_client",
        "api.routes_oauth.get_redis_client",
        "api.routes_admin.get_redis_client",
        "api.routes_device.get_redis_client",
        "api.routes_app.get_redis_client",
    ]
    patches = []
    for t in targets:
        try:
            p = patch(t, return_value=fake_redis)
            p.start()
            patches.append(p)
        except (AttributeError, ModuleNotFoundError):
            # Module didn't import that name — skip; not all routers
            # necessarily call redis the same way.
            pass
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_STATE = "abc123-state-token"


def _set_state_cookie(client: TestClient, state: str = VALID_STATE):
    """Plant the oauth_state cookie on the test client so the callback
    can verify it. Mirrors what the /oauth/google/login route would
    have set on a real round-trip."""
    from core import oauth as oauth_mod
    client.cookies.set(oauth_mod.COOKIE_OAUTH_STATE, state)


def _claims(email: str = "alice@example.com", verified: bool = True) -> dict:
    """A synthetic ID-token claims dict. Matches the shape google-auth's
    `verify_oauth2_token` returns for an `openid email` scope token."""
    return {
        "iss": "https://accounts.google.com",
        "aud": os.environ["STRA2US_GOOGLE_CLIENT_ID"],
        "email": email,
        "email_verified": verified,
        "sub": "1234567890",
    }


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------

def test_login_returns_503_when_oauth_disabled(client):
    with patch("core.oauth.is_enabled", return_value=False):
        r = client.get("/oauth/google/login", follow_redirects=False)
    assert r.status_code == 503


def test_callback_returns_503_when_oauth_disabled(client):
    with patch("core.oauth.is_enabled", return_value=False):
        r = client.get("/oauth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /oauth/google/login
# ---------------------------------------------------------------------------

def test_login_redirects_to_google_with_state_cookie(client):
    r = client.get("/oauth/google/login", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in location
    assert "client_id=test-client-id.apps.googleusercontent.com" in location
    assert "scope=openid+email" in location
    assert "prompt=select_account" in location
    # The state in the URL must match the cookie the response sets.
    from core import oauth as oauth_mod
    cookie_state = r.cookies.get(oauth_mod.COOKIE_OAUTH_STATE)
    assert cookie_state and f"state={cookie_state}" in location


# ---------------------------------------------------------------------------
# /oauth/google/callback — CSRF / state validation
# ---------------------------------------------------------------------------

def test_callback_state_missing_returns_400_and_skips_token_exchange(client):
    # No state cookie planted → mismatch.
    with patch("core.oauth.exchange_code_for_id_token") as exch:
        r = client.get(
            "/oauth/google/callback?code=goodcode&state=anything",
            follow_redirects=False,
        )
    assert r.status_code == 400
    exch.assert_not_called()  # never reached the token-exchange step


def test_callback_state_mismatch_returns_400(client):
    _set_state_cookie(client, "cookie-state")
    with patch("core.oauth.exchange_code_for_id_token") as exch:
        r = client.get(
            "/oauth/google/callback?code=goodcode&state=different-state",
            follow_redirects=False,
        )
    assert r.status_code == 400
    exch.assert_not_called()


def test_callback_missing_code_returns_400(client):
    _set_state_cookie(client)
    r = client.get(
        f"/oauth/google/callback?code=&state={VALID_STATE}",
        follow_redirects=False,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /oauth/google/callback — token validation failures
# ---------------------------------------------------------------------------

def test_callback_token_exchange_failure_returns_502(client):
    from core import oauth as oauth_mod
    _set_state_cookie(client)
    with patch(
        "core.oauth.exchange_code_for_id_token",
        side_effect=oauth_mod.OAuthError("network down"),
    ):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 502


def test_callback_id_token_validation_failure_returns_401(client):
    from core import oauth as oauth_mod
    _set_state_cookie(client)
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt.token"), \
         patch(
             "core.oauth.validate_id_token",
             side_effect=oauth_mod.OAuthError("bad signature"),
         ):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 401
    # No session cookie should be issued on validation failure.
    assert "admin_session" not in r.cookies


# ---------------------------------------------------------------------------
# /oauth/google/callback — authorization branching
# ---------------------------------------------------------------------------

def test_callback_unauthorized_email_renders_unauthorized_page(client, fake_redis):
    # admin_acls:<email> deliberately NOT preloaded → unauthorized branch.
    _set_state_cookie(client)
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt"), \
         patch("core.oauth.validate_id_token", return_value=_claims("nobody@example.com")):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 403
    assert "isn't authorized" in r.text
    assert "nobody@example.com" in r.text
    # Critically: NO admin_session cookie set on the unauthorized branch.
    assert "admin_session" not in r.cookies


def test_callback_authorized_email_issues_session_and_redirects(client, fake_redis):
    fake_redis.store["admin_acls:alice@example.com"] = '{"acls":["*:rw"]}'
    _set_state_cookie(client)
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt"), \
         patch("core.oauth.validate_id_token", return_value=_claims("alice@example.com")):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/"
    # Session cookie issued.
    assert "admin_session" in r.cookies
    # And it's actually a valid token for that user.
    from core.admin_auth import verify_session_token
    assert verify_session_token(r.cookies["admin_session"]) == "alice@example.com"


def test_callback_authorized_preserves_redirect_to_cookie(client, fake_redis):
    from core import oauth as oauth_mod
    fake_redis.store["admin_acls:alice@example.com"] = '{"acls":["*:rw"]}'
    _set_state_cookie(client)
    client.cookies.set(oauth_mod.COOKIE_OAUTH_REDIRECT_TO, "/app/critterchron/timmy_tanuki")
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt"), \
         patch("core.oauth.validate_id_token", return_value=_claims("alice@example.com")):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/app/critterchron/timmy_tanuki"


def test_callback_rejects_offsite_redirect_to(client, fake_redis):
    """Open-redirect defense — only same-origin paths (`/...`) honored."""
    from core import oauth as oauth_mod
    fake_redis.store["admin_acls:alice@example.com"] = '{"acls":["*:rw"]}'
    _set_state_cookie(client)
    client.cookies.set(oauth_mod.COOKIE_OAUTH_REDIRECT_TO, "https://evil.example.com/steal")
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt"), \
         patch("core.oauth.validate_id_token", return_value=_claims("alice@example.com")):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/"  # fell back to default


# ---------------------------------------------------------------------------
# Cookie attributes — easy to silently get wrong, easy to assert here.
# ---------------------------------------------------------------------------

def test_session_cookie_path_is_root(client, fake_redis):
    """Cookie Path must be `/` so the session is sent on /admin/, /app/,
    /api/admin/, etc. uniformly. Defaulting to the request path (the
    callback's `/oauth/google/callback`) would silently break every
    other route — checked here so it can never regress unnoticed."""
    fake_redis.store["admin_acls:alice@example.com"] = '{"acls":["*:rw"]}'
    _set_state_cookie(client)
    with patch("core.oauth.exchange_code_for_id_token", return_value="raw.jwt"), \
         patch("core.oauth.validate_id_token", return_value=_claims("alice@example.com")):
        r = client.get(
            f"/oauth/google/callback?code=c&state={VALID_STATE}",
            follow_redirects=False,
        )
    set_cookie_headers = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else [r.headers.get("set-cookie", "")]
    session_header = next((h for h in set_cookie_headers if h.startswith("admin_session=")), None)
    assert session_header is not None
    assert "Path=/" in session_header
    assert "HttpOnly" in session_header


# ---------------------------------------------------------------------------
# /oauth/unauthorized — direct navigation
# ---------------------------------------------------------------------------

def test_unauthorized_page_renders_with_email_param(client):
    r = client.get("/oauth/unauthorized?email=foo@bar.com")
    assert r.status_code == 403
    assert "foo@bar.com" in r.text


def test_unauthorized_page_escapes_email_html(client):
    r = client.get("/oauth/unauthorized?email=<script>alert(1)</script>@x.com")
    assert r.status_code == 403
    # Raw script tag must not survive into the page.
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


# ---------------------------------------------------------------------------
# core.oauth pure helpers (no FastAPI involved)
# ---------------------------------------------------------------------------

def test_state_token_is_unique_and_url_safe():
    from core.oauth import generate_state_token
    a = generate_state_token()
    b = generate_state_token()
    assert a != b
    assert len(a) >= 32
    # URL-safe alphabet only.
    import string
    assert set(a).issubset(set(string.ascii_letters + string.digits + "-_"))


def test_state_token_verify_constant_time_compare():
    from core.oauth import verify_state_token
    assert verify_state_token("abc", "abc") is True
    assert verify_state_token("abc", "abd") is False
    assert verify_state_token(None, "abc") is False
    assert verify_state_token("abc", None) is False
    assert verify_state_token("", "") is False  # both falsy → reject
