# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the catalog schema + coercion.

No network, no files beyond what pytest's tmp_path fixture creates.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stra2us_cli.catalog import (
    Catalog,
    CatalogError,
    Var,
    coerce_value,
    kv_path,
    load_catalog,
)


# ----- load / parse -----

def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "test.s2s.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_minimal_catalog_loads(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            default: 5
    """)
    cat = load_catalog(p)
    assert cat.app == "testapp"
    assert cat.version == 1
    assert cat.vars["foo"].default == 5


def test_unknown_field_rejected(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            wobble: true
    """)
    with pytest.raises(CatalogError, match="wobble"):
        load_catalog(p)


def test_app_name_shape(tmp_path):
    p = _write(tmp_path, """
        app: BadName
        vars:
          foo: {type: int, scope: [app]}
    """)
    with pytest.raises(CatalogError, match="app name"):
        load_catalog(p)


def test_app_name_rejects_underscore_prefix(tmp_path):
    """`_catalog/*` is where published catalogs get stashed (spec §6).
    Apps can't be named anything starting with `_` so a malicious or
    confused author can't collide with the reserved namespace."""
    for reserved in ("_catalog", "_foo", "_"):
        p = _write(tmp_path, f"""
            app: {reserved}
            vars:
              foo: {{type: int, scope: [app]}}
        """)
        with pytest.raises(CatalogError, match="app name"):
            load_catalog(p)


def test_var_name_shape(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          Bad-Name: {type: int, scope: [app]}
    """)
    with pytest.raises(CatalogError, match="variable name"):
        load_catalog(p)


def test_default_and_per_device_mutually_exclusive(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            default: 5
            default_per_device: true
    """)
    with pytest.raises(CatalogError, match="default_per_device"):
        load_catalog(p)


def test_default_and_per_platform_mutually_exclusive(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: float
            scope: [app]
            default: 2.5
            default_per_platform: true
    """)
    with pytest.raises(CatalogError, match="default_per_platform"):
        load_catalog(p)


def test_per_device_and_per_platform_mutually_exclusive(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: float
            scope: [app]
            default_per_device: true
            default_per_platform: true
    """)
    with pytest.raises(CatalogError, match="default_per_platform"):
        load_catalog(p)


def test_default_per_platform_alone_ok(tmp_path):
    """Motivating case: critterchron's `light_exponent` — Particle/CDS
    driver defaults to 2.5, ESP32/BH1750 defaults to 0.5. Catalog says
    "look in hal/<platform>/src/" rather than lying about one literal."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          light_exponent:
            type: float
            scope: [app, device]
            default_per_platform: true
    """)
    cat = load_catalog(p)
    assert cat.vars["light_exponent"].default_per_platform is True
    assert cat.vars["light_exponent"].default is None


def test_range_requires_numeric(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: string
            scope: [app]
            range: [0, 10]
    """)
    with pytest.raises(CatalogError, match="range"):
        load_catalog(p)


def test_range_lo_hi_order(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            range: [10, 5]
    """)
    with pytest.raises(CatalogError, match="lo > hi"):
        load_catalog(p)


def test_enum_requires_values(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: enum
            scope: [app]
    """)
    with pytest.raises(CatalogError, match="values"):
        load_catalog(p)


def test_enum_default_must_be_in_values(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          mode:
            type: enum
            scope: [app]
            values: [a, b]
            default: c
    """)
    with pytest.raises(CatalogError, match="not in values"):
        load_catalog(p)


def test_default_in_range(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            range: [0, 10]
            default: 100
    """)
    with pytest.raises(CatalogError, match="outside range"):
        load_catalog(p)


def test_scope_unique(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app, app]
    """)
    with pytest.raises(CatalogError, match="unique"):
        load_catalog(p)


def test_vars_cannot_be_empty(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        vars: {}
    """)
    with pytest.raises(CatalogError, match="at least one"):
        load_catalog(p)


# ----- app-view fields (telemetry_topic, heartbeat_interval_seconds, label) -----

def test_telemetry_topic_accepted(tmp_path):
    """The customer-facing app view tails this topic for status +
    activity. Per docs/fr_application_view.md."""
    p = _write(tmp_path, """
        app: testapp
        telemetry_topic: "{app}/public/heartbeep"
        vars:
          foo:
            type: int
            scope: [app]
            default: 1
    """)
    cat = load_catalog(p)
    assert cat.telemetry_topic == "{app}/public/heartbeep"


def test_telemetry_topic_rejects_redis_key_prefix(tmp_path):
    """`q:foo` is the Redis key, not the topic name. Catch the
    paste-mistake at validate time."""
    p = _write(tmp_path, """
        app: testapp
        telemetry_topic: "q:testapp/public/heartbeep"
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="topic name, not a Redis key"):
        load_catalog(p)


def test_telemetry_topic_rejects_leading_slash(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        telemetry_topic: "/testapp/public/heartbeep"
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="should not start or end with"):
        load_catalog(p)


def test_telemetry_topic_rejects_empty(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        telemetry_topic: "   "
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="non-empty"):
        load_catalog(p)


def test_heartbeat_interval_accepted(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        heartbeat_interval_seconds: 300
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    cat = load_catalog(p)
    assert cat.heartbeat_interval_seconds == 300


def test_heartbeat_interval_rejects_zero(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        heartbeat_interval_seconds: 0
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="must be positive"):
        load_catalog(p)


def test_heartbeat_interval_rejects_negative(tmp_path):
    p = _write(tmp_path, """
        app: testapp
        heartbeat_interval_seconds: -10
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="must be positive"):
        load_catalog(p)


def test_label_accepted_on_var(tmp_path):
    """Customer-facing title; presence is the visibility gate for
    the /app/<app>/<device> view."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          wifi_password:
            type: string
            scope: [app, device]
            label: WiFi password
            help: WPA2 PSK
    """)
    cat = load_catalog(p)
    assert cat.vars["wifi_password"].label == "WiFi password"


def test_label_rejects_blank(tmp_path):
    """Empty-string labels would render as blank card titles —
    almost certainly a mistake. Operator should either commit to
    a real title or omit the field to hide the var from /app."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          foo:
            type: int
            scope: [app]
            default: 1
            label: "   "
    """)
    with pytest.raises(CatalogError, match="non-empty"):
        load_catalog(p)


def test_label_absent_var_loads_normally(tmp_path):
    """No-label is the default; var is hidden from /app but still
    works in /admin and on devices."""
    p = _write(tmp_path, """
        app: testapp
        vars:
          internal_thing:
            type: int
            scope: [app]
            default: 1
    """)
    cat = load_catalog(p)
    assert cat.vars["internal_thing"].label is None


def test_top_level_unknown_field_still_rejected(tmp_path):
    """The new fields are additive — typos at the top level still get
    caught (Catalog is `extra=forbid`)."""
    p = _write(tmp_path, """
        app: testapp
        telemetry_topick: foo
        vars:
          foo: {type: int, scope: [app], default: 1}
    """)
    with pytest.raises(CatalogError, match="telemetry_topick"):
        load_catalog(p)


# ----- coerce -----

def _var(**kwargs) -> Var:
    return Var(**kwargs)


def test_coerce_int():
    v = _var(type="int", scope=["app"], range=(0, 100))
    assert coerce_value(v, "42") == 42


def test_coerce_int_rejects_float_string():
    v = _var(type="int", scope=["app"])
    with pytest.raises(CatalogError, match="expected int"):
        coerce_value(v, "3.14")


def test_coerce_int_range():
    v = _var(type="int", scope=["app"], range=(0, 100))
    with pytest.raises(CatalogError, match="outside catalog range"):
        coerce_value(v, "200")


def test_coerce_float():
    v = _var(type="float", scope=["app"])
    assert coerce_value(v, "3.14") == pytest.approx(3.14)


def test_coerce_bool_truthy():
    v = _var(type="bool", scope=["app"])
    for s in ("true", "TRUE", "1", "yes", "on", "Y"):
        assert coerce_value(v, s) is True, s


def test_coerce_bool_falsy():
    v = _var(type="bool", scope=["app"])
    for s in ("false", "0", "no", "off", "N"):
        assert coerce_value(v, s) is False, s


def test_coerce_bool_bad():
    v = _var(type="bool", scope=["app"])
    with pytest.raises(CatalogError, match="bool-ish"):
        coerce_value(v, "maybe")


def test_coerce_enum():
    v = _var(type="enum", scope=["app"], values=["day", "night"])
    assert coerce_value(v, "day") == "day"
    with pytest.raises(CatalogError, match="not in allowed"):
        coerce_value(v, "dusk")


def test_coerce_string_passthrough():
    v = _var(type="string", scope=["app"])
    assert coerce_value(v, "hello world") == "hello world"


# ----- kv_path -----

def test_kv_path_app_scope_lands_under_public():
    """App-scope writes go under `<app>/public/` per the namespace
    convention from docs/fr_application_view.md, not directly under
    `<app>/`. This is what makes a customer's narrow ACL
    (`<app>/<device>:rw` + `<app>/public:r`) able to read app-scope
    defaults without granting cross-device read."""
    assert kv_path("myapp", "heartbeep", None) == "myapp/public/heartbeep"


def test_kv_path_device_scope_unchanged():
    """Device-scope path is unchanged by the namespace migration —
    devices keep using their own `<app>/<device>/<key>` paths and
    don't need to know about public/."""
    assert kv_path("myapp", "heartbeep", "ricky") == "myapp/ricky/heartbeep"
