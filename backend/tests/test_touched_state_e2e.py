# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""End-to-end Playwright tests for the touched-state JS module
(`backend/src/static/app/forms/touched_state.js`, P0 + P4).

P5 followup #3 in `docs/fr_catalog_app_ui_progress.md`. Pre-#3 the
JS module had structural-only tests (substring assertions on the
file source) — useful but limited; they missed two real issues
that walkthroughs caught (P4's cache-control regression, the
CF Insights script-src violation).

These tests run the harness page
(`backend/src/static/app/forms/_test_harness.html`) in a real
Chromium browser via Playwright, mutate the DOM, and read the
behaviors back via DOM queries + the harness's "Show payload"
JSON output. Same harness the manual P0 walkthrough uses; if a
behavior renders correctly here it'll render correctly when a
human drives the page.

**Running locally:**
    pip install playwright pytest-playwright
    playwright install chromium
    pytest tests/test_touched_state_e2e.py

**CI:** if Playwright + browsers aren't installed, every test in
this module skips (clean signal — the suite is opt-in
infrastructure rather than a hard CI gate).

The static-server fixture serves
`backend/src/static/app/` over `http://127.0.0.1:<random>/` so
the harness's `<script type="module">` import path
(`./forms/touched_state.js`) resolves through HTTP — ES modules
loaded from `file://` hit cross-origin restrictions in modern
Chromium.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Skip the entire module if Playwright isn't installed. Imports
# must be guarded — `from playwright.sync_api import …` raises
# ImportError otherwise, which collection-failures the file
# rather than skipping it cleanly.
playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed — install via "
           "`pip install playwright pytest-playwright && "
           "playwright install chromium` to run E2E tests",
)
sync_playwright = playwright.sync_playwright


_HARNESS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "static", "app",
))


# ----- static server fixture -----

class _SilentHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with logging suppressed — pytest's
    captured-stdout would otherwise fill with one access-log line
    per request, drowning out actual test output."""

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    """Serve `backend/src/static/app/` on `http://127.0.0.1:<port>/`
    for the duration of the test module. The harness page is at
    `/forms/_test_harness.html` relative to that root — same path
    the production server exposes via the `/app/_static/` mount,
    so the test corpus is byte-identical to what runs live.

    Module-scoped so the cost of starting + stopping the server
    is paid once per test file instead of per test."""
    cwd = os.getcwd()
    os.chdir(_HARNESS_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SilentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        os.chdir(cwd)


# ----- browser + page fixtures -----

@pytest.fixture(scope="module")
def browser():
    """Single Chromium instance per module. New page per test
    (function-scoped fixture below) so each test starts fresh."""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium launch failed — run "
                        f"`playwright install chromium` ({e})")
        yield b
        b.close()


@pytest.fixture
def page(browser, static_server):
    """Function-scoped page navigated to the harness. Each test
    gets a clean DOM."""
    ctx = browser.new_context()
    p = ctx.new_page()
    p.goto(f"{static_server}/forms/_test_harness.html")
    # Wait for the module to load + initTouchedState to bind. The
    # harness imports touched_state.js as ES module + calls
    # init(form) on DOMContentLoaded; by the time `load` event
    # fires, the bind is done. `domcontentloaded` would fire too
    # early — module imports race past DOMContentLoaded.
    p.wait_for_load_state("load")
    yield p
    ctx.close()


# ----- helpers -----

def _payload(page) -> dict:
    """Click "Show payload" and parse the JSON the harness
    renders into the `<pre id=out>`."""
    page.click("#show")
    text = page.locator("#out").inner_text()
    return json.loads(text)


# ----- behavioral tests -----

def test_initial_data_original_captured(page):
    """Every form field should carry the data-original attribute
    after init() — set either by the renderer (server-side) or by
    `_ensureOriginal` falling back to the field's current value."""
    assert page.get_attribute('input[name="greeting"]', "data-original") == "hi!"
    assert page.get_attribute('input[name="start_time"]', "data-original") == "07:00"
    assert page.get_attribute('input[name="ir_brightness"]', "data-original") == "129"
    assert page.get_attribute('input[name="wifi_password"]', "data-original") == "secret-from-server"


def test_initial_dirty_flag_false(page):
    """Untouched fields read `data-dirty="false"` after init."""
    for sel in ('input[name="greeting"]',
                'input[name="start_time"]',
                'input[name="ir_brightness"]',
                'input[name="wifi_password"]'):
        assert page.get_attribute(sel, "data-dirty") == "false"


def test_typing_flips_dirty(page):
    """The first input event flips data-dirty to true."""
    page.fill('input[name="greeting"]', "hello world")
    assert page.get_attribute('input[name="greeting"]', "data-dirty") == "true"


# ----- live pattern feedback (FR's red/green per keystroke) -----

def test_pattern_field_neutral_until_first_input(page):
    """P5 followup #7 — the FR says feedback fires "as the user
    types," so on first paint the field must be neutral (no
    data-valid attribute set). Pre-#7 the module set data-valid
    at bind time, painting every valid pattern field green
    immediately on page load."""
    valid = page.get_attribute('input[name="start_time"]', "data-valid")
    assert valid is None, f"expected data-valid unset on first paint, got {valid!r}"


def test_pattern_invalid_input_sets_data_valid_false(page):
    """Type something that doesn't match the HH:MM pattern →
    data-valid="false"."""
    page.fill('input[name="start_time"]', "7am")
    assert page.get_attribute('input[name="start_time"]', "data-valid") == "false"


def test_pattern_valid_input_sets_data_valid_true(page):
    page.fill('input[name="start_time"]', "07:30")
    assert page.get_attribute('input[name="start_time"]', "data-valid") == "true"


def test_pattern_toggles_back_to_false(page):
    """Once typing started, the field is no longer neutral —
    every keystroke updates data-valid based on validity. Goes
    valid → invalid → valid as the customer types."""
    page.fill('input[name="start_time"]', "07:30")
    assert page.get_attribute('input[name="start_time"]', "data-valid") == "true"
    page.fill('input[name="start_time"]', "07:99")  # not a valid minute
    assert page.get_attribute('input[name="start_time"]', "data-valid") == "false"


# ----- serialize: dirty/clean branches -----

def test_serialize_emits_data_original_when_clean(page):
    """Clean field (untouched) → serialize emits data-original
    verbatim. The off-spec preservation case: ir_brightness
    stored 129, slider clamped to 100; untouched submit must
    write 129 back (not 100)."""
    payload = _payload(page)
    assert payload["greeting"] == "hi!"
    assert payload["ir_brightness"] == "129"  # data-original, not the
                                              # slider's display value 100
    assert payload["start_time"] == "07:00"


def test_serialize_emits_live_value_when_dirty(page):
    """Dirty field → serialize emits the input's live `.value`."""
    page.fill('input[name="greeting"]', "hello world")
    payload = _payload(page)
    assert payload["greeting"] == "hello world"


def test_serialize_omits_clean_write_only(page):
    """**The marquee P4 fix.** wifi_password is write_only;
    untouched submit must omit it entirely so the server's
    iterate-present-fields handler doesn't write empty over the
    stored secret."""
    payload = _payload(page)
    assert "wifi_password" not in payload, (
        f"clean write_only field should be omitted; got payload: {payload}"
    )


def test_serialize_includes_dirty_write_only(page):
    """The inverse — when the customer DOES type a new value into
    the password field, the new value goes through."""
    page.fill('input[name="wifi_password"]', "newsecret")
    payload = _payload(page)
    assert payload["wifi_password"] == "newsecret"


def test_serialize_off_spec_slider_preserves_data_original(page):
    """The full off-spec scenario from FR Part 1: ir_brightness
    stored 129, slider visually pinned at 100, customer doesn't
    touch the slider. Submit serializes 129 (data-original), not
    100 (the clamped display value)."""
    # Sanity: visual value is clamped on render
    visible_value = page.input_value('input[name="ir_brightness"]')
    assert visible_value == "100"
    # data-original is the un-clamped raw
    assert page.get_attribute('input[name="ir_brightness"]', "data-original") == "129"
    # Submit (no interaction) → emits data-original
    payload = _payload(page)
    assert payload["ir_brightness"] == "129"


def test_serialize_dirty_slider_emits_live_value(page):
    """When the customer DOES interact, snap-on-edit applies and
    the new (in-range) value gets written. Use evaluate() to set
    the value + dispatch the input event since dragging a real
    range input is finicky in headless."""
    page.evaluate("""
        const el = document.querySelector('input[name="ir_brightness"]');
        el.value = '50';
        el.dispatchEvent(new Event('input', { bubbles: true }));
    """)
    payload = _payload(page)
    assert payload["ir_brightness"] == "50"


# ----- radio group serialization -----

def test_radio_serialize_picks_checked_when_clean(page):
    """Radio group: untouched submit emits the data-original of
    whichever radio is `checked` on initial render. The harness
    pre-checks "clock"."""
    payload = _payload(page)
    assert payload["display_mode"] == "clock"


def test_radio_serialize_picks_checked_when_dirty(page):
    """Click a different radio → its value goes through."""
    page.check('input[type="radio"][value="weather"]')
    payload = _payload(page)
    assert payload["display_mode"] == "weather"
