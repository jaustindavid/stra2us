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
    body = _packed_yaml({"vars": {"x": {"type": "int"}}})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "`app:`" in exc.value.detail


def test_missing_vars_key_rejected():
    body = _packed_yaml({"app": "demo"})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "`vars:`" in exc.value.detail


def test_vars_not_a_dict_rejected():
    """A common operator-mistake shape: copying example YAML
    from the wrong format and ending up with `vars: []`."""
    body = _packed_yaml({"app": "demo", "vars": []})
    with pytest.raises(HTTPException) as exc:
        _validate_catalog_yaml_upload(body)
    assert exc.value.status_code == 400
    assert "`vars:` must be a mapping" in exc.value.detail
