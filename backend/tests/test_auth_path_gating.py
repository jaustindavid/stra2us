# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the auth middleware's path-gating predicate
(`_path_needs_admin_auth` in `main.py`).

The predicate is the source of truth for "what's public vs. what
needs an admin session." It's a pure function over the request
path, easy to test directly without spinning up the FastAPI app.

v1.7.1 Sprint 3 tightened the public exception list:
* `/app/` (bare landing form) — was public, now gated
* `/api/app/lookup_device` — was public, now gated

This closes the lookup_device enumeration concern that the
pre-v1.7.1 docstring had been mitigating by suggesting Cloudflare
Turnstile / CAPTCHA at the edge.
"""

from __future__ import annotations

import pytest

from main import _path_needs_admin_auth


# ----- v1.7.1 Sprint 3: tightened paths -----

class TestSprintThreeTightenedPaths:
    """The two paths v1.7.1 moved from public → gated."""

    def test_app_landing_form_requires_auth(self):
        """Bare `/app/` was public pre-v1.7.1; gated as of Sprint 3."""
        assert _path_needs_admin_auth("/app/") is True
        assert _path_needs_admin_auth("/app") is True

    def test_lookup_device_requires_auth(self):
        """The lookup endpoint was public pre-v1.7.1; gated as of
        Sprint 3 to prevent device-name enumeration by un-allowlisted
        attackers. OAuth + Google's allowlist now serves the same
        role CAPTCHA at the edge would have."""
        assert _path_needs_admin_auth("/api/app/lookup_device") is True


# ----- public exceptions preserved -----

class TestPublicExceptionsPreserved:
    """The Sprint 3 tightening should NOT have made the rest of the
    public-exception list auth-required. These paths must stay
    reachable without a session — they serve OAuth's own pages,
    static assets, per-app theme + asset bundles, and CSP reports."""

    @pytest.mark.parametrize("path", [
        "/oauth/google/login",
        "/oauth/google/callback",
        "/oauth/unauthorized",
    ])
    def test_oauth_paths_public(self, path):
        assert _path_needs_admin_auth(path) is False

    def test_admin_logout_public(self):
        """Logout has to work with a corrupted session cookie."""
        assert _path_needs_admin_auth("/admin/logout") is False

    @pytest.mark.parametrize("path", [
        "/app/_static/app.js",
        "/app/_static/forms/touched_state.js",
        "/app/_static/favicon.png",
    ])
    def test_app_static_public(self, path):
        assert _path_needs_admin_auth(path) is False

    def test_per_app_asset_bundle_public(self):
        """Catalog-published assets (logos, favicons) referenced by
        per-app theme stylesheets must be reachable without a
        session — they're embedded in <img> / <link> tags on the
        public landing-page render path."""
        assert _path_needs_admin_auth("/app/critterchron/_assets/logo.svg") is False

    def test_per_app_theme_css_public(self):
        assert _path_needs_admin_auth("/app/critterchron/_theme.css") is False

    def test_csp_report_endpoint_public(self):
        """Browsers POST CSP violations same-origin without a
        session; the endpoint must accept them anonymously."""
        assert _path_needs_admin_auth("/api/_csp_report") is False


# ----- standard auth-required paths -----

class TestAuthRequiredPaths:
    """Sanity: the canonical /admin* and /api/admin* paths still
    require auth. These were the original v1.5 admin paths; Sprint 3
    didn't touch them."""

    @pytest.mark.parametrize("path", [
        "/admin/",
        "/admin/dashboard",
        "/api/admin/keys",
        "/api/admin/peek/kv/some/path",
        "/api/admin/release",
    ])
    def test_admin_paths_gated(self, path):
        assert _path_needs_admin_auth(path) is True

    def test_per_device_customer_page_gated(self):
        """`/app/<app>/<device>` was always auth-required; ACL check
        inside the handler narrows further. Unchanged in Sprint 3."""
        assert _path_needs_admin_auth("/app/critterchron/tommy_tanuki") is True
        assert _path_needs_admin_auth("/app/critterchron/tommy_tanuki/") is True
