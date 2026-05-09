# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Server-side catalog upload validation (followup #4 from
`docs/fr_catalog_app_ui_progress.md`).

The CLI's `catalog publish` runs full `stra2us_cli.catalog_lint`
before posting. The server-side gate catches the cases that
*bypass* the CLI — raw-KV-editor mistakes, scripts POSTing
straight to `/kv/_catalog/<app>`, older CLI versions without the
lint integration. Minimum-viable scope at this followup level:
structural shape (parses as YAML, top-level is a dict, has
required `app` + `vars` keys). Full lint integration arrives
with followup #2 (build-context consolidation), once
`stra2us_cli.catalog_lint` is importable from the backend.

These tests target the helpers directly — no full HTTP
roundtrip needed (the gate is plain functions invoked by the
existing kv POST handler). The gate's wiring is exercised by
the existing `routes_device.write_kv` flow under the catalog
publish smoke tests.
"""

from __future__ import annotations

import msgpack
import pytest
import yaml
from fastapi import HTTPException

from api.routes_device import (
    _is_catalog_yaml_key,
    _validate_catalog_yaml_upload,
)


def _packed_yaml(body: dict) -> bytes:
    """Build the wire shape `client.put(key, yaml_text)` produces:
    msgpack-pack the YAML string."""
    return msgpack.packb(yaml.safe_dump(body), use_bin_type=True)


# ----- key shape -----

def test_catalog_yaml_key_is_two_segments_under_underscore_catalog():
    assert _is_catalog_yaml_key("_catalog/critterchron")
    assert _is_catalog_yaml_key("_catalog/myapp")


def test_asset_keys_not_catalog_yaml():
    """Asset bytes / meta / index sit deeper under
    `_catalog/<app>/_assets/...` and aren't subject to YAML
    validation — they're opaque bytes."""
    assert not _is_catalog_yaml_key("_catalog/critterchron/_assets/logo.svg")
    assert not _is_catalog_yaml_key("_catalog/critterchron/_assets/logo.svg.meta")
    assert not _is_catalog_yaml_key("_catalog/critterchron/_assets_index")


def test_unrelated_keys_not_catalog_yaml():
    assert not _is_catalog_yaml_key("kv/critterchron/dev1/brightness")
    assert not _is_catalog_yaml_key("critterchron/public/heartbeep")
    assert not _is_catalog_yaml_key("_catalog")  # too few segments
    assert not _is_catalog_yaml_key("")


# ----- valid uploads pass -----

def test_minimal_valid_catalog_passes():
    body = _packed_yaml({
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    _validate_catalog_yaml_upload(body)  # no exception


def test_full_critterchron_v2_shape_passes():
    """The demo catalog from `tools/examples/critterchron_v2.s2s.yaml`
    must pass — that's the canonical valid shape."""
    body = _packed_yaml({
        "app": "critterchron",
        "theme": {"primary_color": "#5b3fb8", "logo_asset": "logo.svg"},
        "ui": {"header_markdown": "## Hi"},
        "vars": {
            "display_mode": {
                "type": "string", "scope": ["app", "device"],
                "label": "Display mode", "enum": ["clock", "off"],
            },
        },
    })
    _validate_catalog_yaml_upload(body)


# ----- malformed uploads rejected -----

def test_non_msgpack_payload_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(b"\xff\xff\xff not msgpack")
    assert exc.value.status_code == 400
    assert "msgpack" in exc.value.detail


def test_msgpack_non_string_payload_rejected():
    """The CLI msgpack-packs the YAML *text*. A msgpack of any
    other type — int, dict, list — means somebody bypassed the
    CLI's protocol and is sending binary directly."""
    body = msgpack.packb({"some": "dict"}, use_bin_type=True)
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "YAML string" in exc.value.detail


def test_malformed_yaml_rejected():
    body = msgpack.packb("app: demo\nvars:\n  x: [unbalanced",
                         use_bin_type=True)
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "malformed YAML" in exc.value.detail


def test_top_level_not_a_dict_rejected():
    """A YAML payload like `- foo` parses to a list; catalog
    needs a mapping. Catches operators who paste the wrong file."""
    body = msgpack.packb("- foo\n- bar", use_bin_type=True)
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "mapping" in exc.value.detail


def test_missing_app_key_rejected():
    """Post-#2 the schema layer (pydantic) catches missing `app:`
    rather than the bespoke shape check; error message format
    differs but the rejection behavior is unchanged."""
    body = _packed_yaml({"vars": {"x": {"type": "int"}}})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "app" in exc.value.detail.lower()
    # The error wraps from "schema validation failed" or similar;
    # don't pin the exact wording, just confirm we routed via the
    # schema path.
    assert "schema validation failed" in exc.value.detail


def test_missing_vars_key_rejected():
    body = _packed_yaml({"app": "demo"})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "vars" in exc.value.detail.lower()
    assert "schema validation failed" in exc.value.detail


def test_vars_not_a_dict_rejected():
    """A common operator-mistake shape: copying example YAML
    from the wrong format and ending up with `vars: []`."""
    body = _packed_yaml({"app": "demo", "vars": []})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "vars" in exc.value.detail.lower()


# ----- post-#2: full lint enforcement at upload -----
#
# Pre-#2, the gate only validated *structural* shape. With the
# build-context consolidation in #2, `stra2us_cli.catalog_lint`
# is importable from the backend, so the upload path now runs
# the FR's full lint table — same rules the CLI runs at publish.

def test_bad_theme_color_rejected_at_upload():
    """`theme.primary_color` must match the hex regex per FR
    Part 2. A bare keyword like `purple` would have passed the
    pre-#2 gate (which only checked YAML/dict shape); now it
    fails with a field-pointing lint error."""
    body = _packed_yaml({
        "app": "demo",
        "theme": {"primary_color": "purple"},
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "theme.primary_color" in exc.value.detail
    assert "lint failed" in exc.value.detail


def test_mutually_exclusive_enum_min_max_rejected_at_upload():
    """The FR explicitly: numeric `enum` and `min`/`max` are
    mutually exclusive. Pre-#2 this would have passed the upload
    (and only failed at render or via the CLI's lint). Now it
    fails server-side too."""
    body = _packed_yaml({
        "app": "demo",
        "vars": {
            "n": {
                "type": "int", "scope": ["app"],
                "enum": [1, 2, 3], "min": 0, "max": 10,
            },
        },
    })
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "vars.n" in exc.value.detail
    assert "mutually exclusive" in exc.value.detail


def test_disallowed_font_rejected_at_upload():
    body = _packed_yaml({
        "app": "demo",
        "theme": {"font_family": "Comic Sans MS"},
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "theme.font_family" in exc.value.detail


def test_oversized_help_markdown_rejected_at_upload():
    body = _packed_yaml({
        "app": "demo",
        "vars": {
            "s": {
                "type": "string", "scope": ["app"],
                "help_markdown": "x" * 5000,
            },
        },
    })
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "help_markdown" in exc.value.detail
    assert "STRA2US_MARKDOWN_MAX_BYTES" in exc.value.detail


def test_logo_asset_existence_check_skipped_server_side():
    """Server-side lint runs with `asset_listing=None` — there's
    no bundle context at upload time. The FR's `theme.logo_asset
    references … but _assets/… not in bundle` rule fires only
    when a listing is provided. Catalogs that name a logo_asset
    pass the upload; the CLI's publish path enforces existence."""
    body = _packed_yaml({
        "app": "demo",
        "theme": {"logo_asset": "logo.svg"},
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    # Should NOT raise — asset_listing=None at upload means the
    # existence check is intentionally skipped.
    _validate_catalog_yaml_upload(body)


def test_critterchron_full_example_passes():
    """The FR's combined example must pass full server-side
    lint. Locks in the contract that legitimate catalogs aren't
    over-rejected."""
    body = _packed_yaml({
        "app": "critterchron",
        "theme": {
            "primary_color": "#5b3fb8",
            "accent_color": "#ffb86c",
            "font_family": "system-ui",
            "logo_asset": "logo.svg",
            "logo_alt": "Critterchron",
            "product_name": "Critterchron",
        },
        "ui": {
            "header_markdown": "## Configure your Critterchron",
            "footer_markdown": "Critterchron, Inc.",
        },
        "vars": {
            "display_mode": {
                "type": "string", "scope": ["app", "device"],
                "label": "Display mode",
                "enum": ["clock", "weather", "off"],
            },
            "ir_brightness": {
                "type": "int", "scope": ["app", "device"],
                "label": "Brightness",
                "min": 0, "max": 100, "widget": "slider",
            },
        },
    })
    # No exception means it passed all three layers.
    _validate_catalog_yaml_upload(body)
