# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Structural tests for app.js's P4 touched-state wiring.

P4 wires P0's `forms/touched_state.js` module into the customer
device page's form. This file's tests are a sibling to
`test_touched_state_js.py` (which covers the module itself);
here we assert that `app.js` actually pulls the module in and
calls the right entrypoints.

Same caveat as `test_touched_state_js.py` — the repo has no JS
test runtime, so behavior is encoded as substring assertions.
The live-DOM verification path is the staging walkthrough
(P4 plan steps 1–8).
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_JS = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app", "app.js",
))
_DEVICE_HTML = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app", "device.html",
))
_LANDING_HTML = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app", "landing.html",
))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ----- module loading -----

def test_app_js_imports_touched_state():
    src = _read(_APP_JS)
    assert re.search(
        r"import\s*\{[^}]*\bserialize[^}]*\}\s*from\s*['\"]\./forms/touched_state\.js['\"]",
        src,
    ), "app.js must `import` from forms/touched_state.js"


def test_app_js_imports_init_and_attach_submit_handler():
    """The two entrypoints P4 needs from the P0 module."""
    src = _read(_APP_JS)
    assert re.search(r"\binit\b\s+as\s+\w+", src) or re.search(
        r"\binit\b", src,
    )
    assert "attachSubmitHandler" in src


def test_device_html_loads_app_js_as_module():
    """Without `type="module"`, the `import` would 500 in the
    browser console."""
    src = _read(_DEVICE_HTML)
    assert re.search(
        r'<script\s+type=["\']module["\']\s+src=["\']/app/_static/app\.js',
        src,
    )


def test_landing_html_loads_app_js_as_module():
    """Same bootstrap module covers landing — ensures both pages
    can use the module-loading idiom uniformly."""
    src = _read(_LANDING_HTML)
    assert re.search(
        r'<script\s+type=["\']module["\']\s+src=["\']/app/_static/app\.js',
        src,
    )


# ----- wiring into the form -----

def test_init_called_on_catalog_form():
    """initDevice must call `init(form)` on the
    `<form class="catalog-form">` that page_renderer emits."""
    src = _read(_APP_JS)
    assert ".catalog-form" in src, "must select the catalog-app form"
    # Some form of `initTouchedState(form)` / `init(form)` call.
    assert re.search(
        r"(initTouchedState|init)\s*\(\s*form\s*\)", src,
    )


def test_attach_submit_handler_wired():
    src = _read(_APP_JS)
    assert re.search(
        r"attachSubmitHandler\s*\(\s*form\s*,", src,
    )


def test_submit_handler_uses_fetch_post():
    """The form's submit gets intercepted; payload goes via fetch
    to `form.action`. Browser-native form submit would otherwise
    bypass the touched-state serialization."""
    src = _read(_APP_JS)
    assert re.search(r"fetch\s*\(\s*form\.action", src) or re.search(
        r"fetch\s*\(\s*\w+\.action", src,
    )
    # The body must be URLSearchParams (form-urlencoded), matching
    # what the server's `await request.form()` parses.
    assert "URLSearchParams" in src


def test_submit_handler_prevents_default():
    """preventDefault is what stops the browser's native submit
    from racing the fetch. Without it the form posts twice — once
    as fetch, once as native — and the fetch's per-field omission
    is silently overridden."""
    src = _read(_APP_JS)
    assert "preventDefault()" in src


def test_submit_handler_reloads_on_success():
    """After fetch resolves OK, reload to pick up the freshly
    server-rendered page (with new data-original values for the
    next interaction)."""
    src = _read(_APP_JS)
    assert "window.location.reload" in src


# ----- non-regression: existing behavior preserved -----

def test_reveal_button_handler_still_present():
    """P3's Reveal flow stays — encrypted non-write_only fields
    keep the existing decrypt-on-click path."""
    src = _read(_APP_JS)
    assert "reveal-btn" in src or "bindRevealButtons" in src


def test_telemetry_refresh_still_present():
    src = _read(_APP_JS)
    assert "refreshTelemetry" in src
    assert "renderStatusBadge" in src


def test_no_inline_event_handlers_in_device_html():
    """`<script type="module">` runs under stricter CSP. Belt-and-
    suspenders: no `on*=` attributes either, so a future CSP flip
    to enforcing doesn't break the page."""
    src = _read(_DEVICE_HTML)
    assert not re.search(r"\son[a-z]+\s*=", src)


# ----- defense against regressions of the trim -----

def test_no_jsyaml_cdn_script_tag_in_html():
    """P3's CSP win — the `cdn.jsdelivr.net` script-src violation
    is gone. Catch a future regression that re-adds it as an
    actual `<script src=...>`. (Doc comments in app.js mentioning
    the historical reference are fine.)"""
    for path in (_DEVICE_HTML, _LANDING_HTML):
        src = _read(path)
        assert not re.search(
            r"<script[^>]*src=[\"'][^\"']*jsdelivr", src,
        ), f"{path} re-added jsdelivr <script>"
        assert not re.search(
            r"<script[^>]*src=[\"'][^\"']*js-yaml", src,
        ), f"{path} re-added js-yaml <script>"


def test_innerHTML_only_in_telemetry_path():
    """app.js uses innerHTML only in `renderActivityList`'s template
    (telemetry tail) — every interpolated value goes through
    `escapeHtml`. The form path uses `dataset` / `value` /
    `getAttribute` instead. If a future change introduces
    innerHTML elsewhere, review whether the values are escaped."""
    src = _read(_APP_JS)
    lines = src.splitlines()
    inner_html_lines = [
        (i + 1, line) for i, line in enumerate(lines)
        if ".innerHTML" in line
    ]
    # All current uses must be in renderActivityList. Heuristic:
    # the function is defined around line ~360 and ends around
    # line ~410. Every innerHTML line should fall in that band.
    for lineno, _ in inner_html_lines:
        assert 300 < lineno < 420, (
            f"innerHTML at line {lineno} outside renderActivityList — "
            "verify the interpolated value is escaped via escapeHtml"
        )
