# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the running-release version surface (v1.7.0).

Two pieces under test:

* `core.version.get_release_version()` — reads `backend/VERSION`
  with a `"dev"` fallback. Cached after first call; tests reset
  the cache between cases via `_reset_cache_for_tests()`.
* `GET /api/admin/release` — public to any authed admin, returns
  `{"version": "<tag>"}`. The endpoint just wraps the reader, so
  test coverage of the reader plus a single endpoint integration
  test is sufficient.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import version as version_module


@pytest.fixture(autouse=True)
def _reset_version_cache():
    """Each test gets a fresh cache. Without this, the first
    test's file content leaks into the second test's reader call
    because the module-level `_cached` variable persists across
    the suite."""
    version_module._reset_cache_for_tests()
    yield
    version_module._reset_cache_for_tests()


# ----- reader: get_release_version() ------------------------------

def test_get_release_reads_file_contents(tmp_path, monkeypatch):
    """A populated VERSION file's contents are returned verbatim
    (modulo whitespace stripping)."""
    f = tmp_path / "VERSION"
    f.write_text("v1.7.0\n")
    monkeypatch.setattr(version_module, "_VERSION_PATH", f)
    version_module._reset_cache_for_tests()
    assert version_module.get_release_version() == "v1.7.0"


def test_get_release_strips_whitespace(tmp_path, monkeypatch):
    """Operators may end up with stray whitespace from text-editor
    line endings; strip rather than expose."""
    f = tmp_path / "VERSION"
    f.write_text("  v1.7.1  \n\n")
    monkeypatch.setattr(version_module, "_VERSION_PATH", f)
    version_module._reset_cache_for_tests()
    assert version_module.get_release_version() == "v1.7.1"


def test_get_release_falls_back_to_dev_when_file_missing(tmp_path, monkeypatch):
    """A missing VERSION file (fresh checkout, dev environment)
    returns `"dev"` — sentinel for "not a deployed build."""
    missing = tmp_path / "no-such-file"
    monkeypatch.setattr(version_module, "_VERSION_PATH", missing)
    version_module._reset_cache_for_tests()
    assert version_module.get_release_version() == "dev"


def test_get_release_falls_back_to_dev_when_file_empty(tmp_path, monkeypatch):
    """An empty file is treated like a missing one — fallback to
    `"dev"`. Operator who saves the file accidentally-empty gets
    the same harmless behavior."""
    f = tmp_path / "VERSION"
    f.write_text("")
    monkeypatch.setattr(version_module, "_VERSION_PATH", f)
    version_module._reset_cache_for_tests()
    assert version_module.get_release_version() == "dev"


def test_get_release_caches_after_first_call(tmp_path, monkeypatch):
    """First call reads from disk; subsequent calls return the
    cached value without re-reading. We confirm by changing the
    file content after the first call and asserting the cached
    value is still returned."""
    f = tmp_path / "VERSION"
    f.write_text("v1.7.0\n")
    monkeypatch.setattr(version_module, "_VERSION_PATH", f)
    version_module._reset_cache_for_tests()
    assert version_module.get_release_version() == "v1.7.0"
    # Change the file after the cache is warm.
    f.write_text("v1.7.99\n")
    # Cache hit returns the original value.
    assert version_module.get_release_version() == "v1.7.0"


# ----- endpoint integration: GET /api/admin/release ----------------

def test_release_endpoint_returns_version(monkeypatch):
    """End-to-end: the admin endpoint returns the same string
    `get_release_version()` would. Auth dependency is overridden
    so we exercise just the route — the auth gating itself is
    covered elsewhere."""
    from api.routes_admin import router as admin_router
    from api.dependencies import get_admin_context

    async def _ok():
        return {"username": "test-admin"}

    monkeypatch.setattr(version_module, "_cached", "v1.7.0")

    a = FastAPI()
    a.include_router(admin_router)
    a.dependency_overrides[get_admin_context] = _ok

    client = TestClient(a)
    r = client.get("/release")
    assert r.status_code == 200
    assert r.json() == {"version": "v1.7.0"}


def test_release_endpoint_returns_dev_when_unconfigured(monkeypatch, tmp_path):
    """No VERSION file → endpoint returns `"dev"`."""
    from api.routes_admin import router as admin_router
    from api.dependencies import get_admin_context

    async def _ok():
        return {"username": "test-admin"}

    monkeypatch.setattr(version_module, "_VERSION_PATH", tmp_path / "no-version")
    version_module._reset_cache_for_tests()

    a = FastAPI()
    a.include_router(admin_router)
    a.dependency_overrides[get_admin_context] = _ok

    client = TestClient(a)
    r = client.get("/release")
    assert r.status_code == 200
    assert r.json() == {"version": "dev"}
