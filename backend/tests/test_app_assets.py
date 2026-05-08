# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the asset-serve route (P1 of
`docs/fr_catalog_app_ui_plan.md`).

The route reads from KV (`kv:_catalog/<app>/_assets/<filename>` +
`<filename>.meta`) and returns bytes with the FR's cache headers.
We use a fake Redis (just a dict) instead of a real one so the
test stays self-contained.
"""

from __future__ import annotations

import hashlib

import msgpack
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_app_assets
from api.routes_app_assets import router as app_assets_router


# ----- fake Redis -----

class _FakeRedis:
    """Mimics the subset of redis.asyncio used by the asset route."""

    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    def _set_sync(self, key: str, value: bytes):
        """Test helper — populate the fake KV directly."""
        self._kv[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    """The route imports `get_redis_client` directly, so patching
    `core.redis_client.get_redis_client` after import wouldn't bind
    here. Patch the symbol in the route module's own namespace."""
    fr = _FakeRedis()
    monkeypatch.setattr(routes_app_assets, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture
def app(fake_redis):
    a = FastAPI()
    a.include_router(app_assets_router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ----- helpers -----

def _publish(fake_redis: _FakeRedis, app: str, filename: str, *,
             payload: bytes, content_type: str):
    """Simulate a CLI publish: msgpack-pack bytes + meta and store at
    the FR's KV layout. Mirrors what `catalog_publish.publish_assets`
    does on the live server."""
    sha = hashlib.sha256(payload).hexdigest()
    meta = {"content_type": content_type, "sha256": sha, "size": len(payload)}
    fake_redis._set_sync(
        f"kv:_catalog/{app}/_assets/{filename}",
        msgpack.packb(payload, use_bin_type=True),
    )
    fake_redis._set_sync(
        f"kv:_catalog/{app}/_assets/{filename}.meta",
        msgpack.packb(meta, use_bin_type=True),
    )
    return sha


# ----- happy paths -----

def test_serves_png(client, fake_redis):
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    sha = _publish(fake_redis, "demo", "logo.png",
                   payload=body, content_type="image/png")
    r = client.get("/app/demo/_assets/logo.png")
    assert r.status_code == 200
    assert r.content == body
    assert r.headers["content-type"] == "image/png"
    assert "max-age=31536000" in r.headers["cache-control"]
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["etag"] == f'"{sha}"'


def test_serves_svg_with_correct_content_type(client, fake_redis):
    body = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    _publish(fake_redis, "demo", "logo.svg",
             payload=body, content_type="image/svg+xml")
    r = client.get("/app/demo/_assets/logo.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.content == body


def test_query_param_ignored(client, fake_redis):
    """The renderer (P3) emits `?v=<hash>` for cache-busting; the
    route just reads the path. Same response with and without."""
    body = b"\x89PNG\r\n\x1a\n"
    _publish(fake_redis, "demo", "logo.png",
             payload=body, content_type="image/png")
    r1 = client.get("/app/demo/_assets/logo.png")
    r2 = client.get("/app/demo/_assets/logo.png?v=abcdef12")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r1.headers["etag"] == r2.headers["etag"]


# ----- partial state / corruption -----

def test_404_when_bytes_missing(client, fake_redis):
    """Mid-publish kill window: meta landed but bytes didn't (or
    vice versa). The route treats partial state as 404 — defends
    the FR's atomicity contract from `docs/fr_catalog_app_ui.md`
    §5a even when reads race the publish flow."""
    fake_redis._set_sync(
        "kv:_catalog/demo/_assets/logo.png.meta",
        msgpack.packb({"content_type": "image/png", "sha256": "x", "size": 0},
                      use_bin_type=True),
    )
    r = client.get("/app/demo/_assets/logo.png")
    assert r.status_code == 404


def test_404_when_meta_missing(client, fake_redis):
    fake_redis._set_sync(
        "kv:_catalog/demo/_assets/logo.png",
        msgpack.packb(b"\x89PNG", use_bin_type=True),
    )
    r = client.get("/app/demo/_assets/logo.png")
    assert r.status_code == 404


def test_404_when_app_unknown(client, fake_redis):
    r = client.get("/app/unknown/_assets/logo.png")
    assert r.status_code == 404


def test_corrupted_records_404_not_500(client, fake_redis):
    """A KV record that isn't valid msgpack shouldn't propagate a
    500 to the customer — degrade to 404 and let the operator
    re-publish."""
    fake_redis._set_sync(
        "kv:_catalog/demo/_assets/logo.png", b"\xff\xff\xff",
    )
    fake_redis._set_sync(
        "kv:_catalog/demo/_assets/logo.png.meta", b"\xff\xff\xff",
    )
    r = client.get("/app/demo/_assets/logo.png")
    assert r.status_code == 404


# ----- filename safety -----

def test_uppercase_filename_404(client, fake_redis):
    """The filename allowlist matches lint — lowercase only."""
    r = client.get("/app/demo/_assets/LOGO.PNG")
    assert r.status_code == 404


def test_dot_prefix_filename_404(client, fake_redis):
    r = client.get("/app/demo/_assets/.hidden.png")
    assert r.status_code == 404


def test_meta_suffix_directly_blocked(client, fake_redis):
    """The `.meta` sidecar is internal — clients fetch the asset by
    its real filename and the route reads `.meta` server-side. A
    direct `GET /.../<filename>.meta` would otherwise bypass the
    content-type pinning."""
    _publish(fake_redis, "demo", "logo.png",
             payload=b"\x89PNG", content_type="image/png")
    r = client.get("/app/demo/_assets/logo.png.meta")
    assert r.status_code == 404


def test_traversal_blocked(client, fake_redis):
    # The route's path parameter is a single segment, so `..` would
    # have to be URL-encoded to even reach the handler. Even if it
    # did, the filename regex rejects it.
    r = client.get("/app/demo/_assets/..%2Fpasswd")
    # Could be 404 from FastAPI's path-parser (segment can't contain
    # `/`) or our regex; either is fine — the assertion is "not 200".
    assert r.status_code != 200
