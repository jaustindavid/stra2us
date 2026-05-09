# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Regression: admin static HTML must not load any external CDN
resources (P5 followup #1a — `docs/csp_admin_audit.md`).

Vendoring js-yaml + Inter font in P5 sub-stage 1a was the
foundation for the eventual admin CSP enforcing flip (1d).
Without this guard, a future admin tweak that re-adds a CDN
script tag would silently regress us back into Report-Only
violation territory — caught here at test time instead of in
browser console after deploy.
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADMIN_DIR = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static",
))


def _admin_html_files():
    """Yield every `.html` directly under `backend/src/static/`
    (the admin UI). Skips the `app/` subdirectory — that's the
    customer-facing surface, separately audited and already CSP-
    clean from P3+P4."""
    for entry in os.listdir(_ADMIN_DIR):
        if entry.endswith(".html"):
            yield os.path.join(_ADMIN_DIR, entry)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_no_external_script_src():
    for path in _admin_html_files():
        src = _read(path)
        offenders = re.findall(
            r'<script[^>]*src=["\']https?://[^"\']+["\']', src,
        )
        assert not offenders, (
            f"{os.path.basename(path)} has external <script src>: {offenders}"
        )


def test_no_external_stylesheet_link():
    for path in _admin_html_files():
        src = _read(path)
        offenders = re.findall(
            r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']https?://[^"\']+["\']', src,
        )
        offenders += re.findall(
            r'<link[^>]*href=["\']https?://[^"\']+["\'][^>]*rel=["\']stylesheet["\']', src,
        )
        assert not offenders, (
            f"{os.path.basename(path)} has external <link rel=stylesheet>: {offenders}"
        )


def test_vendored_js_yaml_present():
    """Pin the vendored copy's existence — if a future cleanup
    deletes _vendor/ without also fixing the index.html reference,
    the admin UI breaks."""
    path = os.path.join(_ADMIN_DIR, "_vendor", "js-yaml-4.1.0.min.js")
    assert os.path.isfile(path), f"missing vendored js-yaml at {path}"
    # ~40 KB; a 0-byte file would mean a botched re-vendor.
    assert os.path.getsize(path) > 10_000, "vendored js-yaml looks truncated"


def test_vendored_inter_font_present():
    base = os.path.join(_ADMIN_DIR, "_vendor", "inter")
    for name in ("inter.css", "inter-latin.woff2", "OFL.txt"):
        path = os.path.join(base, name)
        assert os.path.isfile(path), f"missing vendored Inter file: {path}"


def test_inter_css_references_local_url_only():
    """The `@font-face src: url(...)` must point at the local
    `/admin/_vendor/...` path, not back at fonts.gstatic.com.
    Catches a vendor-by-copy-paste that forgot to rewrite the
    URLs. Comments mentioning the original hosts in prose are
    fine (the audit doc encourages context for future readers);
    we only want to flag *active* CSS references."""
    css = _read(os.path.join(_ADMIN_DIR, "_vendor", "inter", "inter.css"))
    # Strip /* … */ comments before scanning.
    no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # Active `url(...)` references must not point at the CDNs.
    url_refs = re.findall(r"url\s*\(([^)]+)\)", no_comments)
    for ref in url_refs:
        assert "fonts.gstatic.com" not in ref, f"active url() at gstatic: {ref}"
        assert "fonts.googleapis.com" not in ref, f"active url() at googleapis: {ref}"
    # Positive: at least one url() must point at the vendored woff2.
    assert any("/admin/_vendor/inter/inter-latin.woff2" in ref for ref in url_refs)


def test_index_html_references_vendored_paths():
    """The vendored files only matter if index.html actually
    points at them."""
    src = _read(os.path.join(_ADMIN_DIR, "index.html"))
    assert "_vendor/inter/inter.css" in src
    assert "_vendor/js-yaml-4.1.0.min.js" in src
