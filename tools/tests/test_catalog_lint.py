# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Lint tests for catalog_lint (docs/fr_catalog_app_ui.md +
P0 of fr_catalog_app_ui_plan.md).

Every error path from the FR's lint table has a positive test here.
Bonus warnings (unused asset, slider-without-min/max,
duplicate-enum-value) are exercised under their own functions so a
later code change can flip the severity in one place and we'll
notice in CI.
"""

from __future__ import annotations

from stra2us_cli.catalog import Catalog, EnumChoice, Theme, Ui, Var
from stra2us_cli.catalog_lint import (
    Asset,
    LintIssue,
    errors,
    lint_asset_bundle,
    lint_catalog,
    warnings,
)


def _cat(*, vars=None, theme=None, ui=None, app="testapp") -> Catalog:
    return Catalog(
        app=app,
        vars=vars or {"x": Var(type="int", scope=["app"])},
        theme=theme,
        ui=ui,
    )


def _err_paths(issues):
    return [i.path for i in errors(issues)]


# ----- field-level: enum -----

def test_enum_only_on_int_or_string():
    cat = _cat(vars={"b": Var(type="bool", scope=["app"], enum=["x"])})
    assert "vars.b.enum" in _err_paths(lint_catalog(cat))


def test_enum_empty_list_rejected():
    cat = _cat(vars={"x": Var(type="string", scope=["app"], enum=[])})
    issues = lint_catalog(cat)
    assert any("at least one value" in i.message for i in errors(issues))


def test_numeric_enum_excludes_min_max():
    cat = _cat(vars={
        "n": Var(type="int", scope=["app"], enum=[1, 2, 3], min=0, max=10),
    })
    issues = errors(lint_catalog(cat))
    assert any("mutually exclusive" in i.message for i in issues)


def test_string_enum_with_min_max_complains_min_max_only():
    # min/max on a string field is the primary error here — caught by
    # _lint_field_numeric_bounds. The enum-with-min/max rule fires only
    # for numeric enum (FR explicit). Make sure we see the type error.
    cat = _cat(vars={
        "s": Var(type="string", scope=["app"], enum=["a", "b"], min=0),
    })
    issues = errors(lint_catalog(cat))
    paths = [i.path for i in issues]
    assert "vars.s.min" in paths


def test_enum_mixed_simple_and_object_rejected():
    cat = _cat(vars={
        "m": Var(
            type="string",
            scope=["app"],
            enum=["clock", EnumChoice(value="weather", label="Weather")],
        ),
    })
    issues = errors(lint_catalog(cat))
    assert any("no mixing" in i.message for i in issues)


def test_enum_int_entry_must_be_int():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], enum=[1, 2, "three"])})
    issues = errors(lint_catalog(cat))
    assert any(i.path.startswith("vars.n.enum[2]") for i in issues)


def test_enum_duplicate_value_warns():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], enum=["a", "b", "a"])})
    issues = warnings(lint_catalog(cat))
    assert any("duplicate enum value" in i.message for i in issues)


# ----- field-level: numeric bounds -----

def test_min_max_only_numeric():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], min=0, max=5)})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.s.min" in paths and "vars.s.max" in paths


def test_min_greater_than_max_rejected():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], min=10, max=5)})
    issues = errors(lint_catalog(cat))
    assert any("> `max`" in i.message for i in issues)


# ----- field-level: widgets -----

def test_widget_slider_only_on_int():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], widget="slider")})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.s.widget" in paths


def test_widget_slider_without_min_max_warns():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], widget="slider")})
    issues = warnings(lint_catalog(cat))
    assert any("falls back" in i.message for i in issues)


def test_widget_secret_only_on_string():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], widget="secret")})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.n.widget" in paths


# ----- field-level: secret-pairing warnings (v1.6.x) ---------------
# `widget: secret`, `write_only: true`, `encrypted: true` are three
# independent primitives that almost always belong together. The
# catalog grammar leaves them composable on purpose, but the lint
# warns when an author has set one without the others — to catch the
# Wi-Fi-password-rendered-as-plaintext class of mistake at publish
# time. Severity: warning (not error) so genuine edge cases stay
# expressible.

def test_secret_pairing_encrypted_without_widget_secret_warns():
    cat = _cat(vars={
        "wifi": Var(type="string", scope=["app"], encrypted=True),
    })
    issues = warnings(lint_catalog(cat))
    assert any(
        i.path == "vars.wifi.encrypted"
        and "without `widget: secret`" in i.message
        for i in issues
    )


def test_secret_pairing_widget_secret_without_encrypted_warns():
    cat = _cat(vars={
        "wifi": Var(type="string", scope=["app"], widget="secret",
                    write_only=True),
    })
    issues = warnings(lint_catalog(cat))
    assert any(
        i.path == "vars.wifi.widget"
        and "without `encrypted: true`" in i.message
        for i in issues
    )


def test_secret_pairing_widget_secret_without_write_only_warns():
    cat = _cat(vars={
        "wifi": Var(type="string", scope=["app"], widget="secret",
                    encrypted=True),
    })
    issues = warnings(lint_catalog(cat))
    assert any(
        i.path == "vars.wifi.widget"
        and "without `write_only: true`" in i.message
        for i in issues
    )


def test_secret_pairing_full_triplet_clean():
    """All three set: no pairing warnings, no errors."""
    cat = _cat(vars={
        "wifi": Var(type="string", scope=["app"], widget="secret",
                    write_only=True, encrypted=True),
    })
    issues = lint_catalog(cat)
    assert errors(issues) == []
    # No pairing warnings on the wifi var (other unrelated warnings
    # in the catalog would still pass through, hence the targeted check).
    pairing_paths = {"vars.wifi.encrypted", "vars.wifi.widget"}
    pairing_warnings = [
        i for i in warnings(issues) if i.path in pairing_paths
    ]
    assert pairing_warnings == []


def test_secret_pairing_no_secret_no_encrypted_no_warnings():
    """Bare string field: no pairing warnings — the lint only nudges
    when at least one of the triplet is set."""
    cat = _cat(vars={
        "name": Var(type="string", scope=["app"]),
    })
    issues = warnings(lint_catalog(cat))
    pairing_paths = {"vars.name.encrypted", "vars.name.widget"}
    assert not any(i.path in pairing_paths for i in issues)


def test_widget_radio_requires_enum():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], widget="radio")})
    issues = errors(lint_catalog(cat))
    assert any("requires `enum:`" in i.message for i in issues)


def test_widget_radio_string_with_enum_ok():
    cat = _cat(vars={
        "s": Var(type="string", scope=["app"], widget="radio", enum=["a", "b"]),
    })
    assert errors(lint_catalog(cat)) == []


# ----- field-level: string-only hints -----

def test_multiline_only_on_string():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], multiline=True)})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.n.multiline" in paths


def test_max_length_must_be_positive():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], max_length=0)})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.s.max_length" in paths


def test_pattern_only_on_string():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], pattern=".*")})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.n.pattern" in paths


def test_invalid_regex_pattern_rejected():
    cat = _cat(vars={"s": Var(type="string", scope=["app"], pattern="(unbalanced")})
    issues = errors(lint_catalog(cat))
    assert any("invalid regex" in i.message for i in issues)


def test_write_only_only_on_string():
    cat = _cat(vars={"n": Var(type="int", scope=["app"], write_only=True)})
    paths = _err_paths(lint_catalog(cat))
    assert "vars.n.write_only" in paths


def test_help_markdown_size_cap():
    big = "x" * 5000
    cat = _cat(vars={"s": Var(type="string", scope=["app"], help_markdown=big)})
    issues = errors(lint_catalog(cat))
    assert any("STRA2US_MARKDOWN_MAX_BYTES" in i.message for i in issues)


# ----- theme -----

def test_theme_color_must_be_hex():
    cat = _cat(theme=Theme(primary_color="purple"))
    paths = _err_paths(lint_catalog(cat))
    assert "theme.primary_color" in paths


def test_theme_color_rejects_function_syntax():
    """The lint rule explicitly disallows rgb(...), var(...), and any
    function-syntax color value — those carry CSS-injection risk that
    hex literals don't. Per the FR's per-key validation table."""
    for bad in ("rgb(255,0,0)", "var(--app-bg)", "expression(alert(1))"):
        cat = _cat(theme=Theme(primary_color=bad))
        assert "theme.primary_color" in _err_paths(lint_catalog(cat))


def test_theme_color_accepts_short_and_alpha_hex():
    for good in ("#fff", "#5b3fb8", "#5b3fb8aa"):
        cat = _cat(theme=Theme(primary_color=good))
        assert errors(lint_catalog(cat)) == []


def test_theme_font_must_be_in_allowlist():
    cat = _cat(theme=Theme(font_family="Comic Sans MS"))
    assert "theme.font_family" in _err_paths(lint_catalog(cat))


def test_theme_logo_asset_filename_shape():
    cat = _cat(theme=Theme(logo_asset="../etc/passwd"))
    assert "theme.logo_asset" in _err_paths(lint_catalog(cat))


def test_theme_logo_asset_must_exist_when_listing_provided():
    cat = _cat(theme=Theme(logo_asset="logo.svg"))
    issues = errors(lint_catalog(cat, asset_listing=set()))
    assert any("not in bundle" in i.message for i in issues)


def test_theme_logo_asset_present_in_bundle_ok():
    cat = _cat(theme=Theme(logo_asset="logo.svg"))
    assert errors(lint_catalog(cat, asset_listing={"logo.svg"})) == []


# ----- v1.6.7: theme.favicon_asset (TODO #7) ---------------------
# Same shape of validation as `logo_asset`: filename pattern +
# asset-must-exist + counted as "referenced" by the unused-asset
# warning so the catalog can ship a per-app favicon without
# tripping the "asset present but not referenced" warning.

def test_theme_favicon_asset_filename_shape():
    cat = _cat(theme=Theme(favicon_asset="../etc/passwd"))
    assert "theme.favicon_asset" in _err_paths(lint_catalog(cat))


def test_theme_favicon_asset_must_exist_when_listing_provided():
    cat = _cat(theme=Theme(favicon_asset="favicon.svg"))
    issues = errors(lint_catalog(cat, asset_listing=set()))
    assert any("not in bundle" in i.message for i in issues)


def test_theme_favicon_asset_present_in_bundle_ok():
    cat = _cat(theme=Theme(favicon_asset="favicon.svg"))
    assert errors(lint_catalog(cat, asset_listing={"favicon.svg"})) == []


def test_theme_favicon_asset_counted_as_referenced():
    """A favicon asset shouldn't trip the 'unused asset' warning —
    it's referenced just like the logo, even though the
    reference is in the HTML head (not the catalog YAML body)."""
    cat = _cat(theme=Theme(favicon_asset="favicon.svg"))
    warnings_for_favicon = [
        i for i in warnings(lint_catalog(cat, asset_listing={"favicon.svg"}))
        if "favicon.svg" in i.path or "favicon.svg" in i.message
    ]
    assert warnings_for_favicon == []


def test_theme_logo_alt_length_capped():
    cat = _cat(theme=Theme(logo_alt="a" * 200))
    assert "theme.logo_alt" in _err_paths(lint_catalog(cat))


def test_theme_product_name_length_capped():
    cat = _cat(theme=Theme(product_name="a" * 200))
    assert "theme.product_name" in _err_paths(lint_catalog(cat))


def test_theme_product_name_rejects_control_chars():
    cat = _cat(theme=Theme(product_name="hello\nworld"))
    assert "theme.product_name" in _err_paths(lint_catalog(cat))


# ----- ui block -----

def test_ui_markdown_size_cap():
    big = "x" * 5000
    cat = _cat(ui=Ui(header_markdown=big))
    issues = errors(lint_catalog(cat))
    assert any(i.path == "ui.header_markdown" for i in issues)


# ----- bonus warnings -----

def test_unused_asset_warns():
    cat = _cat(theme=Theme(logo_asset="logo.svg"))
    issues = lint_catalog(cat, asset_listing={"logo.svg", "stray.png"})
    warns = warnings(issues)
    assert any(i.path == "_assets/stray.png" for i in warns)


# ----- asset bundle limits -----

def test_asset_bundle_per_file_size():
    issues = lint_asset_bundle([
        Asset("big.png", "image/png", 1_000_000),
    ])
    assert any("STRA2US_ASSET_MAX_BYTES" in i.message for i in errors(issues))


def test_asset_bundle_total_size():
    issues = lint_asset_bundle([
        Asset(f"f{i}.png", "image/png", 200_000) for i in range(15)
    ])
    assert any("BUNDLE_MAX_BYTES" in i.message for i in errors(issues))


def test_asset_bundle_content_type_allowlist():
    issues = lint_asset_bundle([
        Asset("evil.gif", "image/gif", 10),
    ])
    assert any("content type" in i.message for i in errors(issues))


def test_asset_bundle_filename_shape():
    issues = lint_asset_bundle([
        Asset("UPPER.PNG", "image/png", 10),
    ])
    paths = _err_paths(issues)
    assert "_assets/UPPER.PNG" in paths


def test_asset_bundle_duplicate_filename():
    issues = lint_asset_bundle([
        Asset("logo.png", "image/png", 10),
        Asset("logo.png", "image/png", 10),
    ])
    assert any("duplicate" in i.message for i in errors(issues))


# ----- happy path -----

def test_critterchron_combined_example_clean():
    """The FR's full critterchron example should lint cleanly when
    paired with a logo.svg in `_assets/`. If a future change to the
    rules breaks this, we want to see it loud."""
    cat = Catalog(
        app="critterchron",
        theme=Theme(
            primary_color="#5b3fb8",
            accent_color="#ffb86c",
            bg_color="#f7f3eb",
            text_color="#2a2a2a",
            font_family="system-ui",
            logo_asset="logo.svg",
            logo_alt="Critterchron",
            product_name="Critterchron",
        ),
        ui=Ui(header_markdown="## hi", footer_markdown="(c)"),
        vars={
            "display_mode": Var(
                type="string", scope=["app"], default="clock",
                enum=["clock", "weather", "photo", "off"],
            ),
            "ir_brightness": Var(
                type="int", scope=["app"], default=50,
                min=0, max=100, widget="slider",
            ),
            "wifi_password": Var(
                type="string", scope=["app"], default="",
                widget="secret", write_only=True, max_length=63,
            ),
            "greeting": Var(
                type="string", scope=["app"], default="hi!",
                multiline=True, max_length=200,
            ),
            "start_time": Var(
                type="string", scope=["app"], default="07:00",
                pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$",
                help_markdown="24-hour `HH:MM`.",
            ),
        },
    )
    issues = lint_catalog(cat, asset_listing={"logo.svg"})
    assert errors(issues) == [], errors(issues)
