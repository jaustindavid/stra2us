# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""End-to-end render tests for the device page (P3).

Wires `device_page → load_catalog_dict → resolve_value →
render_page → template substitution` so a regression anywhere in
the chain shows up here. The unit-level tests for each piece live
in their own files; this file is the "all the wires touch."

Auth + ACL are bypassed (the GET path's authz hasn't changed in
P3); these tests focus on the new render path.
"""

from __future__ import annotations

import asyncio

import msgpack
import pytest
import yaml

from api import routes_app, routes_app_theme
from services import markdown_cache


class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    def stash_catalog(self, app: str, body: dict):
        self._kv[f"kv:_catalog/{app}"] = msgpack.packb(
            yaml.safe_dump(body), use_bin_type=True,
        )

    def stash_value(self, key: str, value):
        self._kv[f"kv:{key}"] = msgpack.packb(value, use_bin_type=True)

    def stash_encrypted(self, key: str, value):
        self.stash_value(key, value)
        self._kv[f"kv:{key}:enc"] = b"1"


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    # Patch every site that pulls a redis client. Each module
    # imports `get_redis_client` at module load → patches must
    # land in the importer's namespace.
    monkeypatch.setattr(routes_app, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_app_theme, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset module-level caches: the device.html template (so
    template edits show up immediately) and the markdown cache
    (so each test sees fresh sanitize counts)."""
    monkeypatch.setattr(routes_app, "_DEVICE_TEMPLATE", None)
    markdown_cache.clear()
    yield
    markdown_cache.clear()


def _run(coro):
    return asyncio.run(coro)


# ----- baseline -----

def test_renders_full_form_for_published_catalog(fake_redis):
    fake_redis.stash_catalog("critterchron", {
        "app": "critterchron",
        "theme": {"primary_color": "#5b3fb8", "product_name": "Critterchron"},
        "vars": {
            "display_mode": {
                "type": "string", "scope": ["app", "device"],
                "label": "Display mode", "default": "clock",
                "enum": ["clock", "weather", "off"],
            },
        },
    })
    response = _run(routes_app._render_device_page("critterchron", "dev1"))
    body = response.body.decode("utf-8")
    # Inline form rendered, not the legacy "Loading…" placeholder.
    assert '<form method="post" action="/app/critterchron/dev1"' in body
    assert '<select name="display_mode"' in body
    assert "Loading settings" not in body
    # Body data-attrs carry context for app.js.
    assert 'data-app="critterchron"' in body
    assert 'data-device="dev1"' in body
    assert 'data-telemetry-topic="critterchron/public/heartbeep"' in body
    assert 'data-heartbeat-seconds="60"' in body
    # Theme link still wired in head (P2 behavior unchanged).
    assert '<link rel="stylesheet" href="/app/critterchron/_theme.css?v=' in body


def test_no_catalog_renders_polite_placeholder(fake_redis):
    """A device-page render with no published catalog shouldn't
    500 — show the "no catalog yet" hint and let the customer
    know what to do."""
    response = _run(routes_app._render_device_page("uncataloged", "dev1"))
    body = response.body.decode("utf-8")
    assert "No catalog published" in body
    assert "<form" not in body  # no form when no catalog
    # Still emits the body data-attrs with the convention defaults.
    assert 'data-telemetry-topic="uncataloged/public/heartbeep"' in body
    assert 'data-heartbeat-seconds="60"' in body


# ----- value resolution end-to-end -----

def test_per_device_value_overrides_app_default(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {
            "brightness": {
                "type": "int", "scope": ["app", "device"],
                "label": "Brightness", "min": 0, "max": 100,
                "widget": "slider", "default": 50,
            },
        },
    })
    fake_redis.stash_value("demo/dev1/brightness", 75)
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert 'value="75"' in body
    assert 'data-original="75"' in body


def test_off_spec_stored_value_renders_warning_and_clamps(fake_redis):
    """The FR's central example flowed through end-to-end: 129
    stored, catalog max=100 → warning badge + clamped widget +
    raw 129 on data-original."""
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {
            "brightness": {
                "type": "int", "scope": ["app", "device"],
                "label": "Brightness", "min": 0, "max": 100,
                "widget": "slider",
            },
        },
    })
    fake_redis.stash_value("demo/dev1/brightness", 129)
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert "setting-warning" in body
    assert "<strong>129</strong>" in body
    assert 'data-original="129"' in body
    assert 'value="100"' in body  # clamped


def test_catalog_default_used_when_kv_chain_empty(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {
            "mode": {
                "type": "string", "scope": ["app", "device"],
                "label": "Mode", "default": "clock",
                "enum": ["clock", "weather"],
            },
        },
    })
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert 'value="clock" selected' in body


def test_app_scope_value_used_when_no_device_override(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {
            "mode": {
                "type": "string", "scope": ["app", "device"],
                "label": "Mode", "default": "clock",
                "enum": ["clock", "weather"],
            },
        },
    })
    fake_redis.stash_value("demo/public/mode", "weather")
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert 'value="weather" selected' in body


# ----- markdown blocks -----

def test_header_and_footer_markdown_rendered(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "ui": {
            "header_markdown": "## Welcome\n\nSettings sync within ~30s.",
            "footer_markdown": "Made by [us](https://example.com).",
        },
        "vars": {
            "x": {"type": "int", "scope": ["app"], "label": "X"},
        },
    })
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert "<h2>Welcome</h2>" in body
    assert 'href="https://example.com"' in body
    # rel + target added by the sanitizer's post-process.
    assert 'rel="noopener noreferrer"' in body
    assert 'target="_blank"' in body


# ----- telemetry config carried to <body> -----

def test_catalog_telemetry_topic_substituted(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "telemetry_topic": "{app}/devices/{device}/heartbeat",
        "heartbeat_interval_seconds": 300,
        "vars": {
            "x": {"type": "int", "scope": ["app"], "label": "X"},
        },
    })
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert 'data-telemetry-topic="demo/devices/dev1/heartbeat"' in body
    assert 'data-heartbeat-seconds="300"' in body


# ----- only labelled vars surface to customer -----

def test_response_carries_cache_control_no_store(fake_redis):
    """`window.location.reload()` after the JS form submit
    (P4) must always re-render against fresh KV. Without
    `Cache-Control: no-store` browsers may serve the prior
    render — which hides the state change the customer just
    made and masks whether touched-state serialize behaved
    correctly. Caught during P4 staging walkthrough; this
    regression test stops it returning."""
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"], "label": "X"}},
    })
    response = _run(routes_app._render_device_page("demo", "dev1"))
    assert response.headers.get("cache-control") == "no-store"


def test_unlabelled_vars_omitted(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {
            "shown": {"type": "int", "scope": ["app"], "label": "Shown"},
            "hidden": {"type": "string", "scope": ["app"], "ops_only": True},
        },
    })
    response = _run(routes_app._render_device_page("demo", "dev1"))
    body = response.body.decode("utf-8")
    assert 'data-var="shown"' in body
    assert 'data-var="hidden"' not in body
    assert 'name="hidden"' not in body
