# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the v1.6.7 device-name reverse index.

Pre-v1.6.7 the customer landing form's `lookup_device` endpoint
established "device exists" by SCAN-ing for any `kv:*/<name>/*`
record. A device that was provisioned (admin UI / CLI) but hadn't
yet done its first KV write returned 404 — forcing the operator
workflow into "provision → flash → device heartbeats → configure"
instead of the natural "provision → configure → flash."

v1.6.7 writes a `device_to_app:<name>` reverse index at provision
time. `lookup_device` consults the index first (O(1)) and falls
back to the SCAN for legacy devices, backfilling the index on a
SCAN hit so the legacy population self-heals.

These tests pin both halves of that contract.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ----- fake Redis with the subset of ops these endpoints touch -----

class _FakeRedis:
    """Minimal Redis stand-in. Stores str → bytes (matching real
    Redis client default) and supports the GET / SET / DELETE /
    SCAN ops that lookup_device + provision_device hit."""

    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    async def set(self, key, value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._kv[key] = value

    async def delete(self, *keys):
        for k in keys:
            self._kv.pop(k, None)

    async def scan(self, cursor=0, match=None, count=100):
        # Single-iteration scan: returns all matching keys at once
        # with cursor=0. Real Redis paginates; the tests don't care.
        import fnmatch
        keys = [k for k in self._kv.keys() if fnmatch.fnmatch(k, match or "*")]
        return 0, [k.encode("utf-8") for k in keys]


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    from core import redis_client
    from api import routes_admin, routes_app, dependencies
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_admin, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_app, "get_redis_client", lambda: fr)
    monkeypatch.setattr(dependencies, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture
def client(fake_redis):
    """Build an app with admin + app routers and override the
    admin-superuser dependency. `Depends(require_admin_superuser)`
    binds at route-registration time, so replacing the function
    via monkeypatch doesn't help — we use FastAPI's first-class
    `dependency_overrides` instead."""
    from api.routes_admin import router as admin_router
    from api.routes_app import router as app_router
    from api.dependencies import require_admin_superuser

    async def _ok():
        return {"username": "test-admin", "is_superuser": True}

    a = FastAPI()
    a.include_router(admin_router)
    a.include_router(app_router)
    a.dependency_overrides[require_admin_superuser] = _ok
    return TestClient(a)


# ----- provision-time write ----------------------------------------

def test_provision_writes_reverse_index(client, fake_redis):
    """A new device's provisioning writes
    `device_to_app:<id>` → `<app>` so the customer landing
    form's lookup can resolve it immediately, even before any
    KV records exist for the device."""
    r = client.post("/provision_device", json={
        "client_id": "tommy_tanuki",
        "app": "critterchron",
    })
    assert r.status_code == 200
    assert fake_redis._kv.get("device_to_app:tommy_tanuki") == b"critterchron"


def test_re_provision_overwrites_reverse_index(client, fake_redis):
    """Re-running provision_device on an existing client (e.g.
    retrofitting the device-on-app ACL onto a pre-existing
    client) re-asserts the reverse-index entry. The set is
    unconditional, so this is just confirming the write
    happens both code-paths (created and existing)."""
    fake_redis._kv["client:tommy_tanuki:secret"] = b"existing-secret-hex"

    r = client.post("/provision_device", json={
        "client_id": "tommy_tanuki",
        "app": "critterchron",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is False  # existing client
    assert fake_redis._kv.get("device_to_app:tommy_tanuki") == b"critterchron"


# ----- lookup uses the reverse index --------------------------------

def test_lookup_finds_via_reverse_index(client, fake_redis):
    """A device with a reverse-index entry resolves O(1) — no
    SCAN required, even when the device has zero KV records."""
    fake_redis._kv["device_to_app:tommy_tanuki"] = b"critterchron"
    # Intentionally no kv:critterchron/tommy_tanuki/* records — the
    # whole point is that lookup works before the device heartbeats.

    r = client.get("/api/app/lookup_device?name=tommy_tanuki")
    assert r.status_code == 200
    assert r.json() == {"app": "critterchron"}


def test_lookup_404_when_device_unknown(client, fake_redis):
    """No reverse-index entry, no KV records → 404. The honest
    'never heard of this device' answer."""
    r = client.get("/api/app/lookup_device?name=ghost_device")
    assert r.status_code == 404


# ----- legacy devices: SCAN fallback + backfill ---------------------

def test_lookup_falls_back_to_scan_for_legacy(client, fake_redis):
    """A device provisioned before v1.6.7 has no reverse-index
    entry. Once it's done at least one KV write, the SCAN
    fallback finds it (preserves pre-v1.6.7 behavior — no
    operator migration required)."""
    # Simulate a legacy device: KV records exist, but no
    # reverse-index entry.
    fake_redis._kv["kv:critterchron/legacy_dev/wifi_ssid"] = b"\xa3foo"
    assert "device_to_app:legacy_dev" not in fake_redis._kv

    r = client.get("/api/app/lookup_device?name=legacy_dev")
    assert r.status_code == 200
    assert r.json() == {"app": "critterchron"}


def test_lookup_backfills_reverse_index_on_scan_hit(client, fake_redis):
    """When the SCAN fallback finds a legacy device, the
    reverse-index entry materializes as a side effect — the
    next lookup goes O(1). Self-healing migration."""
    fake_redis._kv["kv:critterchron/legacy_dev/wifi_ssid"] = b"\xa3foo"
    assert "device_to_app:legacy_dev" not in fake_redis._kv

    r = client.get("/api/app/lookup_device?name=legacy_dev")
    assert r.status_code == 200
    # Index entry now materialized.
    assert fake_redis._kv.get("device_to_app:legacy_dev") == b"critterchron"


# ----- deletion clears the reverse index ----------------------------

def test_revoke_clears_reverse_index(client, fake_redis):
    """Deleting a device's client record also clears the
    reverse-index entry. Otherwise the lookup would cheerfully
    return the (now-defunct) app name for a device whose
    secret + ACL are gone."""
    fake_redis._kv["client:tommy_tanuki:secret"] = b"abc"
    fake_redis._kv["client:tommy_tanuki:acl"] = json.dumps({
        "permissions": [{"prefix": "critterchron/tommy_tanuki", "access": "rw"}]
    }).encode("utf-8")
    fake_redis._kv["device_to_app:tommy_tanuki"] = b"critterchron"

    r = client.delete("/keys/tommy_tanuki")
    assert r.status_code == 200
    assert "device_to_app:tommy_tanuki" not in fake_redis._kv


# ----- input validation (preserved from pre-v1.6.7) -----------------

def test_lookup_rejects_slash_in_name(client, fake_redis):
    """Names containing `/` are refused without scanning —
    defends against probes targeting arbitrary path shapes.
    Behavior preserved unchanged from pre-v1.6.7."""
    r = client.get("/api/app/lookup_device?name=foo/bar")
    assert r.status_code == 404


def test_lookup_rejects_empty_name(client, fake_redis):
    r = client.get("/api/app/lookup_device?name=")
    assert r.status_code == 404
