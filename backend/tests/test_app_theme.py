# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the per-app theme stylesheet route (P2).

The route reads the catalog YAML from KV (msgpack-wrapped str),
parses the `theme:` block, hands it to `serialize_theme_css`, and
returns CSS with `Cache-Control: immutable` + ETag headers. Tests
use a fake Redis (just a dict) like `test_app_assets.py`.
"""

from __future__ import annotations

import msgpack
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_app_theme
from api.routes_app_theme import router as app_theme_router


# ----- fake Redis -----

class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    def _stash_catalog(self, app: str, body: dict):
        """Helper: write a catalog YAML to the fake KV in the same
        msgpack-wrapped-str shape the CLI publishes."""
        yaml_text = yaml.safe_dump(body)
        self._kv[f"kv:_catalog/{app}"] = msgpack.packb(yaml_text, use_bin_type=True)


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    monkeypatch.setattr(routes_app_theme, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture
def client(fake_redis):
    a = FastAPI()
    a.include_router(app_theme_router)
    return TestClient(a)


# ----- happy paths -----

def test_serves_full_theme(client, fake_redis):
    fake_redis._stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {
            "primary_color": "#5b3fb8",
            "accent_color": "#ffb86c",
            "bg_color": "#f7f3eb",
            "text_color": "#2a2a2a",
            "font_family": "system-ui",
        },
    })
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "max-age=31536000" in r.headers["cache-control"]
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["etag"]
    body = r.text
    assert '[data-app="demo"]' in body
    assert "--app-primary: #5b3fb8" in body
    assert "--app-font: system-ui" in body


def test_partial_theme_emits_only_set_keys(client, fake_redis):
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#fff"},
    })
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 200
    body = r.text
    assert "--app-primary: #fff" in body
    assert "--app-bg" not in body  # not set in catalog


def test_no_theme_block_returns_empty_rule(client, fake_redis):
    """Catalog exists but has no `theme:` — return a valid empty
    rule. The customer page falls back to stra2us defaults via
    `var(--app-x, <default>)`. Confirms the no-branding case works
    end to end."""
    fake_redis._stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 200
    body = r.text
    assert '[data-app="demo"]' in body
    assert "{\n}" in body
    # No declarations leaked from the empty body.
    assert "--app-" not in body


def test_query_param_ignored(client, fake_redis):
    """The `?v=<hash>` is a cache-bust signal for the browser; the
    route doesn't validate it (would be a `?v` mismatch race
    otherwise). Same response with and without."""
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#fff"},
    })
    r1 = client.get("/app/demo/_theme.css")
    r2 = client.get("/app/demo/_theme.css?v=ignored")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.text == r2.text
    assert r1.headers["etag"] == r2.headers["etag"]


# ----- 404 / fallback -----

def test_404_when_catalog_unpublished(client):
    r = client.get("/app/never-published/_theme.css")
    assert r.status_code == 404


def test_misshaped_catalog_yaml_404(client, fake_redis):
    """A catalog whose YAML doesn't parse → 404 rather than
    propagating a 500. Operator can re-publish; the customer
    doesn't need a detailed error."""
    fake_redis._kv["kv:_catalog/demo"] = msgpack.packb(
        "not: valid: yaml: [", use_bin_type=True,
    )
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 404


def test_misshaped_theme_block_treated_as_empty(client, fake_redis):
    """If `theme:` exists but isn't a dict (e.g. someone wrote
    `theme: "rebrand-me"`), treat it as no-theme. Lint would have
    caught this at publish; this is defense in depth."""
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": "should-be-a-dict",
    })
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 200
    assert "{\n}" in r.text
    assert "--app-" not in r.text


def test_corrupted_msgpack_404(client, fake_redis):
    fake_redis._kv["kv:_catalog/demo"] = b"\xff\xff\xff"
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 404


# ----- hash bumps on theme change -----

def test_etag_changes_when_theme_changes(client, fake_redis):
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#fff"},
    })
    r1 = client.get("/app/demo/_theme.css")
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#000"},
    })
    r2 = client.get("/app/demo/_theme.css")
    assert r1.headers["etag"] != r2.headers["etag"]
    assert r1.text != r2.text


# ----- adversarial: lint-bypass values don't escape -----

def test_lint_bypass_color_silently_dropped(client, fake_redis):
    """A catalog YAML with a malformed color (somehow past lint or
    edited directly into KV) should not produce an injection-shaped
    rule. The serializer's re-validation is the second line of
    defense per FR's `Theme CSS serialization is data-not-string`."""
    fake_redis._stash_catalog("demo", {
        "app": "demo", "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#fff; } body { background: red"},
    })
    r = client.get("/app/demo/_theme.css")
    assert r.status_code == 200
    body = r.text
    # No second rule was emitted.
    assert body.count("[data-app=") == 1
    assert "body {" not in body
    assert "background: red" not in body
