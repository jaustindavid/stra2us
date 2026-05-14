# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the v1.7.1 Sprint 4 `/api/admin/visible_clients`
endpoint — the scope-aware companion to `/keys` used by the
Activity Logs view's filter-chip population.

The shape: returns only client_ids the caller's ACL covers
(by `_prefix_matches` semantics). No secrets, no ACL bodies.
A superuser (`*:rw`) sees everything; a scoped admin sees the
subset their permissions cover; an admin with no permissions
gets an empty list (not 403).

This endpoint exists because the pre-v1.7.1 Activity Logs view
called `/keys` (superuser-only) to populate filter chips. That
broke for scoped admins: 403 → JS error → blank page. The
scope-aware companion fixes that.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ----- fake Redis subset (mirrors test_lookup_device_reverse_index) -

class _FakeRedis:
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

    async def keys(self, pattern):
        import fnmatch
        return [k.encode("utf-8")
                for k in self._kv.keys()
                if fnmatch.fnmatch(k, pattern)]


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    from core import redis_client
    from api import routes_admin, dependencies
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_admin, "get_redis_client", lambda: fr)
    monkeypatch.setattr(dependencies, "get_redis_client", lambda: fr)
    return fr


def _stash_device_client(fake_redis, client_id: str, app: str):
    """Drop a device-shaped client in the fake Redis — the
    `<app>/<client_id>:rw` + `<app>/public:rw` ACL pattern that
    `provision_device` writes."""
    fake_redis._kv[f"client:{client_id}:secret"] = b"dummy-secret-hex"
    acl = {
        "permissions": [
            {"prefix": f"{app}/{client_id}", "access": "rw"},
            {"prefix": f"{app}/public", "access": "rw"},
        ]
    }
    fake_redis._kv[f"client:{client_id}:acl"] = json.dumps(acl).encode("utf-8")


def _stash_internal_client(fake_redis, client_id: str, perms: list):
    """Drop a non-device-shaped client (e.g. an internal probe with
    a custom permissions list)."""
    fake_redis._kv[f"client:{client_id}:secret"] = b"dummy-secret-hex"
    acl = {"permissions": perms}
    fake_redis._kv[f"client:{client_id}:acl"] = json.dumps(acl).encode("utf-8")


@pytest.fixture
def client(fake_redis):
    """Build a TestClient + override `get_admin_context` per-test
    via the `caller_acl` fixture-funcs below."""
    from api.routes_admin import router as admin_router

    a = FastAPI()
    a.include_router(admin_router)
    return a, TestClient(a)


def _with_caller_acl(app: FastAPI, permissions: list):
    """Helper: override the admin-context dependency to return a
    caller with the given permissions."""
    from api.dependencies import get_admin_context

    async def _ctx():
        return {
            "client_id": "test-admin",
            "acl": {"permissions": permissions},
            "is_admin": True,
        }

    app.dependency_overrides[get_admin_context] = _ctx


# ----- superuser sees everything -----

def test_superuser_sees_all_device_clients(client, fake_redis):
    app, test_client = client
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")
    _stash_device_client(fake_redis, "ricky_raccoon", "critterchron")
    _stash_device_client(fake_redis, "bessie_bear", "anotherapp")
    _with_caller_acl(app, [{"prefix": "*", "access": "rw"}])

    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert set(r.json()) == {"tommy_tanuki", "ricky_raccoon", "bessie_bear"}


# ----- scoped admin sees just their slice -----

def test_scoped_admin_sees_only_matching_clients(client, fake_redis):
    """An admin scoped to `critterchron/*` should see critterchron
    devices but not anotherapp's devices."""
    app, test_client = client
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")
    _stash_device_client(fake_redis, "ricky_raccoon", "critterchron")
    _stash_device_client(fake_redis, "bessie_bear", "anotherapp")
    _with_caller_acl(app, [{"prefix": "critterchron", "access": "rw"}])

    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert set(r.json()) == {"tommy_tanuki", "ricky_raccoon"}


def test_per_device_scoped_admin_sees_only_that_device(client, fake_redis):
    """The narrowest case: admin's ACL is for ONE device. They see
    just that device in their visible list."""
    app, test_client = client
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")
    _stash_device_client(fake_redis, "ricky_raccoon", "critterchron")
    _with_caller_acl(app, [{"prefix": "critterchron/tommy_tanuki", "access": "rw"}])

    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert r.json() == ["tommy_tanuki"]


# ----- empty-permissions admin gets empty list, not 403 -----

def test_no_permissions_returns_empty_list(client, fake_redis):
    """A misconfigured admin row with no permissions gets an empty
    list back, not a 403. The Activity Logs view renders without
    filter chips — same fail-safe shape as a network failure."""
    app, test_client = client
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")
    _with_caller_acl(app, [])

    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert r.json() == []


# ----- non-device-shaped clients -----

def test_non_device_client_visible_only_to_wildcard(client, fake_redis):
    """An internal probe with a custom ACL (not the
    `<app>/<client_id>` shape) is visible only to a wildcard
    admin. Prevents scoped admins from accidentally seeing
    internal infrastructure clients."""
    app, test_client = client
    _stash_internal_client(fake_redis, "smoke-probe", [
        {"prefix": "smoke", "access": "rw"},
    ])
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")

    # Scoped admin: doesn't see the smoke-probe (no device-shape
    # match) but does see their own app's device.
    _with_caller_acl(app, [{"prefix": "critterchron", "access": "rw"}])
    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert r.json() == ["tommy_tanuki"]

    # Wildcard admin: sees everything.
    _with_caller_acl(app, [{"prefix": "*", "access": "rw"}])
    r = test_client.get("/visible_clients")
    assert r.status_code == 200
    assert set(r.json()) == {"smoke-probe", "tommy_tanuki"}


# ----- no secrets / acl bodies in response -----

def test_response_contains_only_ids(client, fake_redis):
    """Hard contract: the response is a flat list of strings (IDs).
    No secrets, no ACL bodies, no other client metadata. The
    Activity Logs view only needs IDs for filter chips; widening
    the response would leak info."""
    app, test_client = client
    _stash_device_client(fake_redis, "tommy_tanuki", "critterchron")
    _with_caller_acl(app, [{"prefix": "*", "access": "rw"}])

    body = test_client.get("/visible_clients").json()
    assert isinstance(body, list)
    assert all(isinstance(x, str) for x in body)
