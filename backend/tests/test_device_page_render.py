# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for `device_page` template rendering (P2).

Targets the `_render_device_page` helper directly — auth + ACL
behavior is unchanged from pre-P2 and tested elsewhere; this
suite covers the new behavior: substituting `{{APP}}` and
`{{THEME_HASH}}` from the catalog.
"""

from __future__ import annotations

import asyncio

import msgpack
import pytest
import yaml

from api import routes_app, routes_app_theme


# ----- fake Redis (shared shape with the other test modules) -----

class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    def stash_catalog(self, app: str, body: dict):
        self._kv[f"kv:_catalog/{app}"] = msgpack.packb(
            yaml.safe_dump(body), use_bin_type=True,
        )


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    # `_render_device_page` calls `load_theme` which lives in the
    # theme route module — patch the symbol there.
    monkeypatch.setattr(routes_app_theme, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture(autouse=True)
def reset_template_cache(monkeypatch):
    """The template is cached at module level; tests should read
    the file fresh so changes to device.html show up immediately
    when running against the working tree."""
    monkeypatch.setattr(routes_app, "_DEVICE_TEMPLATE", None)


def _run(coro):
    """`asyncio.run()` creates and tears down its own loop — no
    deprecation warning, and isolates each test's loop state from
    the others."""
    return asyncio.run(coro)


# ----- happy path -----

def test_data_app_attr_substituted(fake_redis):
    fake_redis.stash_catalog("critterchron", {
        "app": "critterchron",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#5b3fb8"},
    })
    response = _run(routes_app._render_device_page("critterchron"))
    body = response.body.decode("utf-8")
    assert 'data-app="critterchron"' in body
    # Placeholder fully substituted — no leftover braces.
    assert "{{APP}}" not in body
    assert "{{THEME_HASH}}" not in body


def test_theme_link_includes_hash_for_published_catalog(fake_redis):
    fake_redis.stash_catalog("critterchron", {
        "app": "critterchron",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#5b3fb8"},
    })
    response = _run(routes_app._render_device_page("critterchron"))
    body = response.body.decode("utf-8")
    # The link tag is present and points at the per-app theme route.
    assert '<link rel="stylesheet" href="/app/critterchron/_theme.css?v=' in body
    # The hash is non-empty.
    import re
    m = re.search(r'_theme\.css\?v=([0-9a-f]+)"', body)
    assert m
    assert len(m.group(1)) >= 4


def test_theme_link_present_with_empty_hash_when_no_catalog(fake_redis):
    """Catalog never published — the page wrapper still emits the
    `<link>` (URL with empty `?v=`), and the route 404s. Browser
    falls back to the inline default in `:root`. Operational
    expectation: a deploy-without-publish doesn't produce a broken
    page, just an unbranded one."""
    response = _run(routes_app._render_device_page("never-published"))
    body = response.body.decode("utf-8")
    assert 'data-app="never-published"' in body
    assert '_theme.css?v="' in body  # query param empty, not missing


def test_response_is_html_not_file_attachment(fake_redis):
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    response = _run(routes_app._render_device_page("demo"))
    # HTMLResponse, not FileResponse — Content-Type set explicitly,
    # no Content-Disposition header.
    assert response.media_type == "text/html"
    assert "content-disposition" not in {k.lower() for k in response.headers}


def test_static_assets_link_unchanged(fake_redis):
    """The existing `<link rel="stylesheet" href="/app/_static/styles.css">`
    must stay intact after the per-app theme link is added — the
    base stylesheet defines the `--app-*` defaults."""
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    response = _run(routes_app._render_device_page("demo"))
    body = response.body.decode("utf-8")
    assert '/app/_static/styles.css' in body


def test_app_js_link_unchanged(fake_redis):
    """`/app/_static/app.js` powers the customer page's data
    fetching. Must stay loaded for the page to function. Catches
    accidental template breakage."""
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
    })
    response = _run(routes_app._render_device_page("demo"))
    body = response.body.decode("utf-8")
    assert '/app/_static/app.js' in body


# ----- placeholder safety -----

def test_no_unsubstituted_placeholders_in_output(fake_redis):
    """Rough guard against typo'd or new-but-unhandled placeholders
    leaking into the served HTML."""
    fake_redis.stash_catalog("demo", {
        "app": "demo",
        "vars": {"x": {"type": "int", "scope": ["app"]}},
        "theme": {"primary_color": "#fff"},
    })
    response = _run(routes_app._render_device_page("demo"))
    body = response.body.decode("utf-8")
    assert "{{" not in body
    assert "}}" not in body
