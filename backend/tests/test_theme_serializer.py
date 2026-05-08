# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the theme stylesheet serializer (P2 of
`docs/fr_catalog_app_ui_plan.md`).

Two halves:

1. **Happy path** — clean theme dicts → expected CSS. Order is
   stable so cache headers + ETags work; missing keys are
   silently skipped (the customer page falls back to defaults).
2. **Adversarial corpus** — values that *somehow* slipped past
   `catalog_lint`. The serializer re-validates each value's
   shape; nothing that wouldn't pass the lint regex / allowlist
   should escape into the CSS body. This is the FR's "Theme CSS
   serialization is data-not-string" promise — the parameterized
   helper holds even when lint fails open.
"""

from __future__ import annotations

import pytest

from services.theme_serializer import serialize_theme_css, theme_hash


# ----- happy path -----

def test_full_theme_emits_every_declaration():
    css = serialize_theme_css("critterchron", {
        "primary_color": "#5b3fb8",
        "accent_color": "#ffb86c",
        "bg_color": "#f7f3eb",
        "text_color": "#2a2a2a",
        "font_family": "system-ui",
    })
    assert '[data-app="critterchron"]' in css
    assert "--app-primary: #5b3fb8" in css
    assert "--app-accent: #ffb86c" in css
    assert "--app-bg: #f7f3eb" in css
    assert "--app-text: #2a2a2a" in css
    assert "--app-font: system-ui" in css


def test_partial_theme_emits_only_set_keys():
    """Missing keys are skipped — no `--app-x: undefined` slop. The
    base stylesheet's `var(--app-x, <fallback>)` covers the gap."""
    css = serialize_theme_css("critterchron", {"primary_color": "#fff"})
    assert "--app-primary: #fff;" in css
    for absent in ("--app-accent", "--app-bg", "--app-text", "--app-font"):
        assert absent not in css


def test_empty_theme_returns_empty_rule_body():
    """No theme block at all — emit a syntactically-valid empty
    rule. Lets the caller confirm the route exists + the catalog
    is reachable, without forcing a 404 on theme-less catalogs."""
    css = serialize_theme_css("critterchron", None)
    assert '[data-app="critterchron"]' in css
    assert "{\n}" in css


def test_declaration_order_stable():
    """Output order is fixed by `_DECLARATIONS`. Keeps the bytes
    hash-stable so ETags + `?v=` query params work across
    requests."""
    a = serialize_theme_css("demo", {
        "primary_color": "#fff",
        "bg_color": "#000",
    })
    b = serialize_theme_css("demo", {
        "bg_color": "#000",
        "primary_color": "#fff",
    })
    assert a == b


# ----- color formats -----

@pytest.mark.parametrize("good", [
    "#fff", "#FFF", "#5b3fb8", "#5B3FB8", "#fff8", "#5b3fb8aa",
])
def test_short_long_alpha_hex_accepted(good):
    css = serialize_theme_css("demo", {"primary_color": good})
    assert f"--app-primary: {good};" in css


# ----- adversarial corpus -----

# Each entry is a value the lint should have rejected. The
# serializer should produce *no* declaration for it (the
# lint-bypass attack surface). Test asserts the declaration is
# absent; the rule body should not gain a second declaration nor
# a `}` followed by a new rule.
@pytest.mark.parametrize("attack", [
    # Classic CSS-injection attempt: terminate the rule, open another.
    "#fff; } body { background: red",
    # Function-call value smuggled past a too-loose hex check.
    "#5b3fb8) expression(alert(1))",
    # Comment-out the closing brace.
    "#fff /* */ } body { color: red",
    # Newline / quote escapes — none of which match the hex regex.
    "#fff\n",
    '#fff"',
    "#fff'",
    # Bare `red` keyword (no `#`).
    "red",
    # `var()` recursion / external URL.
    "var(--app-primary)",
    "url(//evil.example.com/x.css)",
    # Empty-ish.
    "",
    "#",
    "#zzzzzz",  # not hex digits
    "##fff",
])
def test_adversarial_color_dropped(attack):
    css = serialize_theme_css("demo", {"primary_color": attack})
    # The declaration must not appear.
    assert f"--app-primary: {attack}" not in css
    # No second rule emitted.
    assert css.count("[data-app=") == 1
    # Body stays empty (only the harmless rule wrapper).
    assert "body {" not in css
    assert "color: red" not in css.replace("--app-text", "")  # the attack's payload didn't escape


@pytest.mark.parametrize("font", [
    "Comic Sans MS",  # not in allowlist
    "system-ui, sans-serif",  # comma-separated chain rejected
    "url(//attacker/font.woff)",
    "Helvetica",  # web-style font
    "",
])
def test_adversarial_font_dropped(font):
    css = serialize_theme_css("demo", {"font_family": font})
    assert f"--app-font: {font}" not in css


def test_adversarial_app_slug_returns_empty():
    """App slug shape is part of the trust boundary too. A slug
    that violates `^[a-z][a-z0-9_]*$` returns the empty string —
    the route handler 404s upstream, so this is defense in depth."""
    for bad in (
        '" }] * { all: unset } [data-app="demo',
        "demo; SELECT 1",
        "demo with spaces",
        "DEMO",
        "_underscore",
    ):
        assert serialize_theme_css(bad, {"primary_color": "#fff"}) == ""


def test_non_string_color_rejected():
    """JSON / pydantic could in principle hand us non-string
    values for a color slot if a future change loosens the schema.
    The serializer treats anything non-str as None."""
    for bad in (123, None, True, ["#fff"], {"value": "#fff"}):
        css = serialize_theme_css("demo", {"primary_color": bad})
        assert "--app-primary" not in css


# ----- hash stability -----

def test_hash_stable_across_key_order():
    """JSON-with-sort_keys means semantically-identical themes
    produce identical hashes regardless of dict insertion order."""
    a = theme_hash({"primary_color": "#fff", "bg_color": "#000"})
    b = theme_hash({"bg_color": "#000", "primary_color": "#fff"})
    assert a == b


def test_hash_changes_on_value_change():
    """A single-byte change in any theme value should produce a
    different hash — the cache-bust is only useful if it changes."""
    a = theme_hash({"primary_color": "#fff"})
    b = theme_hash({"primary_color": "#ffe"})
    assert a != b


def test_hash_for_none_is_stable_nonempty():
    """No theme block → still a stable hash, so the page wrapper
    can always emit a cache-bust query param."""
    h = theme_hash(None)
    assert h
    assert h == theme_hash({})  # empty dict equivalent to None
