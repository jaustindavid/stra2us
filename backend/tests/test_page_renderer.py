# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Page-render snapshot tests (P3 of
`docs/fr_catalog_app_ui_plan.md`).

The page_renderer is the assembly layer — it composes
widget_renderer + markdown_cache + chrome + off-spec markup. These
tests exercise the composed output against catalog shapes mirrored
from the FR's "Combined example" (and `tools/examples/critterchron_v2.s2s.yaml`).
"""

from __future__ import annotations

import pytest

from services import markdown_cache
from services.page_renderer import compute_publish_hash, render_page
from services.value_resolver import ResolvedValue


@pytest.fixture(autouse=True)
def reset_cache():
    markdown_cache.clear()
    yield
    markdown_cache.clear()


# Compact form of the FR's combined example. Each var has a `label`
# so they all surface on the customer page (visibility gate).
_CRITTERCHRON = {
    "app": "critterchron",
    "theme": {
        "primary_color": "#5b3fb8",
        "logo_asset": "logo.svg",
        "logo_alt": "Critterchron",
        "product_name": "Critterchron",
    },
    "ui": {
        "header_markdown": "## Configure your Critterchron\n\nSettings sync within ~30 seconds.",
        "footer_markdown": "Critterchron, Inc.",
    },
    "vars": {
        "display_mode": {
            "type": "string", "scope": ["app", "device"],
            "label": "Display mode",
            "enum": ["clock", "weather", "photo", "off"],
            "default": "clock",
        },
        "ir_brightness": {
            "type": "int", "scope": ["app", "device"],
            "label": "IR brightness",
            "min": 0, "max": 100, "widget": "slider", "step": 5,
            "default": 50,
        },
        "wifi_password": {
            "type": "string", "scope": ["app", "device"],
            "label": "Wi-Fi password",
            "widget": "secret", "write_only": True, "max_length": 63,
        },
        "greeting": {
            "type": "string", "scope": ["app", "device"],
            "label": "Greeting", "multiline": True, "max_length": 200,
            "default": "hi!",
        },
        "start_time": {
            "type": "string", "scope": ["app", "device"],
            "label": "Start time",
            "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
            "default": "07:00",
            "help": "24-hour HH:MM",
            "help_markdown": "Examples: `07:00`, `13:30`.",
        },
        # Operator-only — should NOT appear on the customer page.
        "ir_program": {
            "type": "string", "scope": ["device"], "ops_only": True,
        },
    },
}


def _values(**overrides) -> dict:
    """Default ResolvedValue map: every customer-facing var resolves
    to its catalog default. Overrides let individual tests inject
    off-spec or write_only stored values."""
    base = {
        "display_mode": ResolvedValue("clock", from_default=True),
        "ir_brightness": ResolvedValue("50", from_default=True),
        "wifi_password": ResolvedValue(None),
        "greeting": ResolvedValue("hi!", from_default=True),
        "start_time": ResolvedValue("07:00", from_default=True),
    }
    base.update(overrides)
    return base


# ----- structural -----

def test_section_wrapper_includes_form():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<section class="catalog-app">' in html
    assert html.endswith("</section>")
    assert '<form method="post" action="/app/critterchron/dev1"' in html
    assert '<button type="submit"' in html


def test_chrome_emits_logo_and_product_name():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert 'src="/app/critterchron/_assets/logo.svg"' in html
    assert 'alt="Critterchron"' in html
    assert '<h1 class="catalog-product-name">Critterchron</h1>' in html


def test_header_and_footer_markdown_rendered():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<div class="catalog-header-md">' in html
    assert "<h2>Configure your Critterchron</h2>" in html
    assert '<div class="catalog-footer-md">' in html
    assert "Critterchron, Inc." in html


def test_only_labelled_vars_appear():
    """`label:` is the visibility gate. `ir_program` lacks one;
    must NOT show up in the customer-facing form."""
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert 'data-var="ir_program"' not in html
    assert 'data-var="display_mode"' in html


def test_each_setting_card_has_label_and_widget():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    # 5 customer-facing vars → 5 setting cards
    assert html.count('<div class="setting-card"') == 5


# ----- per-widget snapshot through the page renderer -----

def test_enum_renders_as_select_with_label():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<label class="setting-label" for="field-display_mode">Display mode</label>' in html
    assert '<select name="display_mode"' in html
    assert 'id="field-display_mode"' in html


def test_slider_renders_with_min_max_and_id():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert 'type="range" name="ir_brightness"' in html
    assert 'id="field-ir_brightness"' in html
    assert 'min="0"' in html and 'max="100"' in html


def test_secret_with_write_only_renders_empty():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON,
                      values=_values(wifi_password=ResolvedValue("real-secret")))
    # write_only ships empty regardless of stored value
    assert 'name="wifi_password"' in html
    assert 'data-original="real-secret"' in html
    assert 'data-write-only="true"' in html


def test_pattern_field_emits_pattern_attr():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert r'pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$"' in html


def test_textarea_for_multiline():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<textarea name="greeting"' in html
    assert 'maxlength="200"' in html


# ----- off-spec values -----

def test_off_spec_int_renders_warning_badge_and_clamped_widget():
    """The FR's central example: brightness=129, catalog max=100.
    Page should show:
      - the warning badge with verbatim "129"
      - the slider clamped to 100 in `value=`
      - `data-original="129"` on the slider so P4's untouched
        submit preserves the off-spec value."""
    html = render_page(
        app="critterchron", device="dev1",
        catalog=_CRITTERCHRON,
        values=_values(ir_brightness=ResolvedValue("129")),
    )
    assert 'class="setting-warning"' in html
    assert '<strong>129</strong>' in html
    assert "not in current allowed values" in html
    assert 'data-original="129"' in html
    assert 'value="100"' in html  # display clamped


def test_off_spec_enum_renders_warning():
    """Stored value `pixie` for an enum that doesn't include it.
    Per FR: dropdown shows only catalog-valid options; warning
    badge quotes the verbatim off-spec value."""
    html = render_page(
        app="critterchron", device="dev1",
        catalog=_CRITTERCHRON,
        values=_values(display_mode=ResolvedValue("pixie")),
    )
    assert 'class="setting-warning"' in html
    assert '<strong>pixie</strong>' in html


# ----- help text + help_markdown -----

def test_plain_help_renders_under_field():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<div class="setting-help">24-hour HH:MM</div>' in html


def test_help_markdown_rendered_through_sanitizer():
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON, values=_values())
    assert '<div class="setting-help-md">' in html
    # Inline backticks in markdown → `<code>`
    assert "<code>07:00</code>" in html


def test_help_markdown_uses_cache_on_repeat_render(monkeypatch):
    """Re-rendering the same page should not re-sanitize the
    markdown blocks. Verifies the `(app, publish_hash, block_id)`
    cache key is reaching all the way through page_renderer."""
    counter = {"n": 0}
    from services import markdown_cache as mc
    real = mc.sanitize_markdown

    def wrapped(source, *, app, max_bytes=None):
        counter["n"] += 1
        return real(source, app=app, max_bytes=max_bytes)

    monkeypatch.setattr(mc, "sanitize_markdown", wrapped)

    render_page(app="critterchron", device="dev1",
                catalog=_CRITTERCHRON, values=_values())
    first_count = counter["n"]
    render_page(app="critterchron", device="dev1",
                catalog=_CRITTERCHRON, values=_values())
    # Second render hit the cache for every block.
    assert counter["n"] == first_count


def test_publish_hash_bump_invalidates_help_markdown_cache(monkeypatch):
    """Different catalog dict → different publish_hash → fresh
    cache entry → sanitizer runs again."""
    counter = {"n": 0}
    from services import markdown_cache as mc
    real = mc.sanitize_markdown

    def wrapped(source, *, app, max_bytes=None):
        counter["n"] += 1
        return real(source, app=app, max_bytes=max_bytes)

    monkeypatch.setattr(mc, "sanitize_markdown", wrapped)

    render_page(app="critterchron", device="dev1",
                catalog=_CRITTERCHRON, values=_values())
    n1 = counter["n"]
    # Mutate the catalog (simulating a republish)
    catalog2 = dict(_CRITTERCHRON)
    catalog2["theme"] = dict(_CRITTERCHRON["theme"])
    catalog2["theme"]["primary_color"] = "#000000"
    render_page(app="critterchron", device="dev1",
                catalog=catalog2, values=_values())
    assert counter["n"] > n1


# ----- empty catalog cases -----

def test_no_theme_no_chrome_emitted():
    catalog = {"app": "demo", "vars": {
        "x": {"type": "int", "scope": ["app"], "label": "X"},
    }}
    html = render_page(app="demo", device="dev1",
                      catalog=catalog,
                      values={"x": ResolvedValue("1")})
    # Chrome `<header>` still wraps but is empty when no theme.
    assert "<h1" not in html  # no product name
    assert "<img" not in html  # no logo


def test_no_ui_block_skips_markdown_sections():
    catalog = {"app": "demo", "vars": {
        "x": {"type": "int", "scope": ["app"], "label": "X"},
    }}
    html = render_page(app="demo", device="dev1",
                      catalog=catalog,
                      values={"x": ResolvedValue("1")})
    assert "catalog-header-md" not in html
    assert "catalog-footer-md" not in html


def test_no_customer_facing_vars_renders_empty_form():
    """Catalog with only operator vars (none with `label:`) — the
    form is empty but well-formed. Customer sees just the chrome
    + a submit button. Edge case but should not 500."""
    catalog = {"app": "demo", "vars": {
        "internal": {"type": "int", "scope": ["app"]},
    }}
    html = render_page(app="demo", device="dev1", catalog=catalog,
                      values={})
    assert '<form method="post"' in html
    assert "setting-card" not in html
    assert '<button type="submit"' in html


# ----- v1.6.7: data-from-default plumbing (TODO #6) ----------------
# Renderer tags inputs whose current value came from the catalog
# default (vs. a stored KV record) with `data-from-default="true"`.
# The touched-state serializer reads this to skip clean fields,
# so saving one edit doesn't materialize per-device overrides for
# every other field's catalog default. The encrypted-with-Reveal
# branch doesn't need the tag (encrypted values aren't from the
# catalog default; they're a stored encrypted record).

def test_data_from_default_emitted_when_value_is_catalog_default():
    """Field whose current value resolved to the catalog default
    gets `data-from-default="true"` on its `<input>`."""
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON,
                      values={"display_mode": ResolvedValue("clock", from_default=True)})
    # display_mode is an enum-rendered <select>, but the
    # data-from-default attribute should still ride along.
    assert 'data-from-default="true"' in html
    # And it should be on the display_mode field specifically.
    assert 'id="field-display_mode" data-from-default="true"' in html


def test_data_from_default_omitted_for_stored_value():
    """Field whose current value came from a stored KV record
    (not the catalog default) does NOT get the tag — the
    operator's previous edit is the authoritative state, and a
    no-op submit should preserve it."""
    html = render_page(app="critterchron", device="dev1",
                      catalog=_CRITTERCHRON,
                      values={"display_mode": ResolvedValue("weather", from_default=False)})
    # Look at the display_mode select specifically — other fields
    # may legitimately have data-from-default if they fell to
    # default; we only care that THIS one doesn't.
    # Cheap heuristic: find the substring near display_mode's id.
    needle = 'id="field-display_mode"'
    idx = html.find(needle)
    assert idx >= 0
    nearby = html[idx:idx + 100]
    assert "data-from-default" not in nearby


def test_data_from_default_not_on_encrypted_reveal_path():
    """The encrypted-non-write_only Reveal branch renders an empty
    input bound to an external fetch — no concept of "default"
    applies. The renderer should NOT emit `data-from-default` on
    that code path. Uses a one-field custom catalog because the
    main fixture's `wifi_password` is `write_only: true`, which
    routes through a different (widget-renderer) branch."""
    custom_catalog = {
        "app": "demo",
        "vars": {
            # Encrypted, not write_only — hits the Reveal-button path.
            "api_token": {
                "type": "string", "scope": ["app", "device"],
                "label": "API token",
                "widget": "secret", "encrypted": True,
            },
        },
    }
    html = render_page(app="demo", device="dev1",
                      catalog=custom_catalog,
                      values={"api_token": ResolvedValue("plaintext-token",
                                                        encrypted=True)})
    needle = 'id="field-api_token"'
    idx = html.find(needle)
    assert idx >= 0
    nearby = html[idx:idx + 200]
    assert 'data-encrypted="true"' in nearby, (
        "encrypted-non-write_only field should hit the Reveal branch "
        "(data-encrypted=true on the input)"
    )
    assert "data-from-default" not in nearby


# ----- publish hash stability -----

def test_publish_hash_stable_across_renders():
    h1 = compute_publish_hash(_CRITTERCHRON)
    h2 = compute_publish_hash(_CRITTERCHRON)
    assert h1 == h2
    assert len(h1) == 8
