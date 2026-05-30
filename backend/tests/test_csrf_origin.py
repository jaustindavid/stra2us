# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the CSRF Origin guard (v1.8.x security finding #2).

The admin session is a cookie; SameSite=lax is the primary CSRF
control, and `admin_auth_middleware` adds a defense-in-depth Origin
check that rejects state-changing requests to the admin/app surface
whose `Origin` header names an unrecognized host.

Design under test:
* Only state-changing methods (POST/PUT/PATCH/DELETE) are guarded.
* Only the cookie-authed admin/app surface (`_path_needs_admin_auth`)
  is guarded.
* A request with NO Origin passes the guard (non-browser clients like
  curl / the stra2us CLI carry no ambient cookie to abuse).
* A foreign Origin → 403 before auth runs.
* Same-origin (Origin host == browser-facing host or BROWSER_HOST) →
  passes the guard (then hits normal auth: 401 without creds).
* STRA2US_ALLOWED_ORIGINS adds extra allowed hosts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from main import _csrf_origin_ok, _allowed_origin_hosts


# ----- unit: the predicate ------------------------------------------

class _FakeReq:
    """Minimal stand-in for a Starlette Request: just headers + url host."""
    def __init__(self, headers=None, xfh=None):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        if xfh is not None:
            h["x-forwarded-host"] = xfh
        self.headers = h

        class _URL:
            hostname = "testserver"
        self.url = _URL()


def test_no_origin_passes():
    """curl / CLI clients send no Origin — must pass (they carry no cookie)."""
    assert _csrf_origin_ok(_FakeReq()) is True


def test_foreign_origin_rejected():
    req = _FakeReq({"origin": "https://evil.example.com"})
    assert _csrf_origin_ok(req) is False


def test_same_origin_via_browser_host_passes():
    # BROWSER_HOST is in the allowed set regardless of X-Forwarded-Host.
    req = _FakeReq({"origin": f"https://{main.BROWSER_HOST}"})
    assert _csrf_origin_ok(req) is True


def test_same_origin_via_forwarded_host_passes():
    req = _FakeReq({"origin": "https://my.host.example"}, xfh="my.host.example")
    assert _csrf_origin_ok(req) is True


def test_allowed_origins_env_extends(monkeypatch):
    monkeypatch.setenv("STRA2US_ALLOWED_ORIGINS", "extra.example.com, another.example")
    req = _FakeReq({"origin": "https://extra.example.com"})
    assert _csrf_origin_ok(req) is True
    assert "another.example" in _allowed_origin_hosts(req)


def test_port_in_origin_is_ignored():
    """urlparse().hostname strips the port — an Origin with a port still
    matches the bare host."""
    req = _FakeReq({"origin": f"https://{main.BROWSER_HOST}:8443"})
    assert _csrf_origin_ok(req) is True


# ----- integration: the middleware via TestClient -------------------

@pytest.fixture
def client():
    return TestClient(main.app)


def _evil():
    return {"Origin": "https://evil.example.com"}


def test_post_admin_foreign_origin_gets_403(client):
    """A forged cross-site POST to a mutating admin endpoint is rejected
    with 403 BEFORE auth runs (so it's a clean CSRF rejection, not a
    401 auth challenge)."""
    r = client.post("/api/admin/keys", json={"client_id": "x"}, headers=_evil())
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"]


def test_post_admin_no_origin_passes_guard(client):
    """No Origin → guard passes; the request then fails normal auth
    (401), proving it was NOT blocked by CSRF."""
    r = client.post("/api/admin/keys", json={"client_id": "x"})
    assert r.status_code == 401


def test_post_admin_same_origin_passes_guard(client):
    """Same-origin POST passes the guard and reaches auth (401 w/o creds)."""
    r = client.post(
        "/api/admin/keys",
        json={"client_id": "x"},
        headers={"Origin": f"https://{main.BROWSER_HOST}"},
    )
    assert r.status_code == 401


def test_get_admin_foreign_origin_not_csrf_blocked(client):
    """GET is not state-changing — the CSRF guard ignores it. (It still
    needs auth, so 401, but NOT a 403 cross-origin rejection.)"""
    r = client.get("/api/admin/keys", headers=_evil())
    assert r.status_code == 401


def test_device_endpoints_outside_csrf_surface():
    """Device endpoints are HMAC-authed (no ambient cookie to abuse), so
    they're outside `_path_needs_admin_auth` and thus not CSRF-guarded.
    Asserted via the predicate (Redis-free) — the guard condition is
    `state-changing method AND _path_needs_admin_auth(path)`, so a False
    here means a forged Origin can never produce the 403 on these paths."""
    from main import _path_needs_admin_auth
    assert _path_needs_admin_auth("/kv/some/key") is False
    assert _path_needs_admin_auth("/q/some/topic") is False
