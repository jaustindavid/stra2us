# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Parser tests for the catalog UI hint extension
(docs/fr_catalog_app_ui.md, P0 of fr_catalog_app_ui_plan.md).

These tests cover the schema shape only — that the parser accepts
the FR's new keys, rejects unknown ones, and stores them on the
right model. Cross-field semantic constraints (e.g. min<=max) are
catalog_lint's job; see test_catalog_lint.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stra2us_cli.catalog import CatalogError, EnumChoice, load_catalog


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "test.s2s.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_field_level_ui_hints_load(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          brightness:
            type: int
            scope: [app]
            min: 0
            max: 100
            step: 5
            widget: slider
            help: "0=off, 100=max"
          mode:
            type: string
            scope: [app]
            enum: [clock, weather, "off"]
            widget: radio
          greeting:
            type: string
            scope: [app]
            multiline: true
            max_length: 200
          start_time:
            type: string
            scope: [app]
            pattern: "^[0-9]{2}:[0-9]{2}$"
            help_markdown: "24-hour `HH:MM`."
          wifi_password:
            type: string
            scope: [app]
            widget: secret
            write_only: true
    """)
    cat = load_catalog(p)
    b = cat.vars["brightness"]
    assert b.min == 0 and b.max == 100 and b.step == 5
    assert b.widget == "slider"
    assert cat.vars["mode"].enum == ["clock", "weather", "off"]
    assert cat.vars["mode"].widget == "radio"
    assert cat.vars["greeting"].multiline is True
    assert cat.vars["greeting"].max_length == 200
    assert cat.vars["start_time"].pattern == "^[0-9]{2}:[0-9]{2}$"
    assert cat.vars["start_time"].help_markdown.startswith("24-hour")
    assert cat.vars["wifi_password"].widget == "secret"
    assert cat.vars["wifi_password"].write_only is True


def test_object_form_enum_loads(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: string
            scope: [app]
            enum:
              - {value: clock, label: "Clock face"}
              - {value: weather, label: "Weather"}
    """)
    cat = load_catalog(p)
    items = cat.vars["mode"].enum
    assert all(isinstance(e, EnumChoice) for e in items)
    assert items[0].value == "clock" and items[0].label == "Clock face"


def test_unknown_widget_value_accepted_for_forward_compat(tmp_path):
    """Per FR's "Forward compatibility": unknown `widget:` values
    must NOT fail catalog load — old servers running new catalogs
    have to keep working. Renderer dispatch falls through to the
    type-default at render time. Tested in the backend's
    `test_widget_renderer.py` — a `widget: future_glow_orb` on a
    string var renders as plain `<input type="text">`.

    Surfaced during P3 staging walkthrough: P0 had this as
    `widget: Widget | None` (`Literal[...]`), which silently
    broke the FR's forward-compat promise. Loosened to
    `widget: str | None`."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: string
            scope: [app]
            widget: future_glow_orb
    """)
    cat = load_catalog(p)
    assert cat.vars["mode"].widget == "future_glow_orb"


def test_unknown_top_level_field_rejected(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          x: {type: int, scope: [app]}
        cosmic_rays: true
    """)
    with pytest.raises(CatalogError, match="cosmic_rays"):
        load_catalog(p)


def test_theme_block_loads(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        theme:
          primary_color: "#5b3fb8"
          accent_color: "#ffb86c"
          font_family: system-ui
          logo_asset: logo.svg
          product_name: Critterchron
        vars:
          x: {type: int, scope: [app]}
    """)
    cat = load_catalog(p)
    assert cat.theme is not None
    assert cat.theme.primary_color == "#5b3fb8"
    assert cat.theme.font_family == "system-ui"
    assert cat.theme.logo_asset == "logo.svg"


def test_unknown_theme_key_rejected(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        theme:
          shadow_color: "#000"
        vars:
          x: {type: int, scope: [app]}
    """)
    with pytest.raises(CatalogError, match="shadow_color"):
        load_catalog(p)


def test_ui_block_loads(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        ui:
          header_markdown: |
            ## Configure your widget
          footer_markdown: "(c) 2026"
        vars:
          x: {type: int, scope: [app]}
    """)
    cat = load_catalog(p)
    assert cat.ui is not None
    assert cat.ui.header_markdown.startswith("## Configure")
    assert cat.ui.footer_markdown == "(c) 2026"


def test_unknown_ui_key_rejected(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        ui:
          sidebar_markdown: "x"
        vars:
          x: {type: int, scope: [app]}
    """)
    with pytest.raises(CatalogError, match="sidebar_markdown"):
        load_catalog(p)


def test_yaml_truthy_enum_value_rejected(tmp_path):
    """YAML 1.1's `off`/`on`/`yes`/`no` parse as booleans, which would
    coerce silently into 0/1 via the `str | int | EnumChoice` union.
    Reject the bare form so the author writes `'off'` explicitly."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: string
            scope: [app]
            enum: [clock, weather, off]
    """)
    with pytest.raises(CatalogError, match="YAML 1.1"):
        load_catalog(p)


def test_combined_critterchron_uses_quoted_enum_values(tmp_path):
    """The FR example writes `enum: [clock, weather, photo, off]`
    unquoted; given the YAML 1.1 footgun above, the *correct*
    catalog quotes `off`. Asserting both forms behave as expected
    here keeps the contract honest in one place."""
    # Just confirm quoted form is fine; unquoted is covered above.
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: string
            scope: [app]
            enum: [clock, weather, photo, "off"]
    """)
    cat = load_catalog(p)
    assert cat.vars["mode"].enum == ["clock", "weather", "photo", "off"]


def test_min_max_step_must_be_numeric(tmp_path):
    """The parser stores `min`/`max`/`step` as `int|float|None`. A
    string here is a structural error caught at load time. Whether
    these are *applicable* to the var's type is lint's job."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          x:
            type: int
            scope: [app]
            min: "zero"
    """)
    with pytest.raises(CatalogError):
        load_catalog(p)


def test_combined_critterchron_example_loads(tmp_path):
    """The full example from the FR's "Combined example" section
    (lightly trimmed). Smoke check that the parser accepts a real
    catalog using every new key."""
    p = _write(tmp_path, """
        app: critterchron
        theme:
          primary_color: "#5b3fb8"
          accent_color: "#ffb86c"
          bg_color: "#f7f3eb"
          text_color: "#2a2a2a"
          font_family: system-ui
          logo_asset: logo.svg
          logo_alt: Critterchron
          product_name: Critterchron
        ui:
          header_markdown: "## hi"
          footer_markdown: "Critterchron, Inc."
        vars:
          display_mode:
            type: string
            scope: [app]
            default: clock
            enum: [clock, weather, photo, "off"]
            help: "What the display shows when idle"
          ir_brightness:
            type: int
            scope: [app]
            default: 50
            min: 0
            max: 100
            widget: slider
          wifi_password:
            type: string
            scope: [app]
            default: ""
            widget: secret
            write_only: true
            max_length: 63
          greeting:
            type: string
            scope: [app]
            default: "hi!"
            multiline: true
            max_length: 200
          start_time:
            type: string
            scope: [app]
            default: "07:00"
            pattern: "^([01][0-9]|2[0-3]):[0-5][0-9]$"
            help_markdown: "24-hour `HH:MM`."
    """)
    cat = load_catalog(p)
    assert cat.app == "critterchron"
    assert len(cat.vars) == 5
    assert cat.theme.product_name == "Critterchron"
    assert cat.ui.footer_markdown == "Critterchron, Inc."
