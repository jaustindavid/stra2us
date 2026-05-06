# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Live tests for `/api/admin/me` — the identity-and-scope endpoint.

`/me` is load-bearing for both /admin (nav gating) and /app/<app>/<device>
(customer-surface bootstrap). It must correctly identify the caller and
derive a `scope_kind` hint from their ACL shape so the JS doesn't have
to re-derive it in three places.

Tests provision Redis ACLs directly (bypassing /api/admin/admin_users
which requires a superuser), then verify /me returns the expected
shape for each persona.

Skipped unless a local stra2us is reachable. Expects env:

    STRA2US_HOST          e.g. http://127.0.0.1:8153
    STRA2US_ADMIN_USER    htpasswd username with *:rw on /admin_acls
                          (just to confirm the endpoint reaches Redis;
                          the persona tests provision their own users)
    STRA2US_ADMIN_PASS    matching password

Plus direct redis-cli access (these tests write `admin_acls:test_me_*`
keys directly rather than through the admin API).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest
import requests


def _env_ready() -> bool:
    return all(
        os.environ.get(k)
        for k in ("STRA2US_HOST", "STRA2US_ADMIN_USER", "STRA2US_ADMIN_PASS")
    )


pytestmark = pytest.mark.skipif(
    not _env_ready(),
    reason="needs STRA2US_HOST/ADMIN_USER/ADMIN_PASS",
)


def _host() -> str:
    h = os.environ["STRA2US_HOST"].rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h


def _redis_db() -> str:
    """Parse the db number out of REDIS_URL if set, else default 0."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    # Crude: take whatever's after the last '/'
    return url.rsplit("/", 1)[-1] or "0"


def _redis_set(key: str, value: str) -> None:
    subprocess.run(
        ["redis-cli", "-n", _redis_db(), "set", key, value],
        check=True, capture_output=True,
    )


def _redis_del(key: str) -> None:
    subprocess.run(
        ["redis-cli", "-n", _redis_db(), "del", key],
        check=True, capture_output=True,
    )


def _provision_admin_user(username: str, password: str, acl_perms: list) -> None:
    """Create htpasswd entry + Redis ACL for a test admin user."""
    backend_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "backend")
    )
    venv_python = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.exists(venv_python):
        pytest.skip("backend venv not found at expected path")
    subprocess.run(
        [venv_python, "create_admin.py", username, password],
        cwd=backend_dir, check=True, capture_output=True,
    )
    _redis_set(
        f"admin_acls:{username}",
        json.dumps({"permissions": acl_perms}),
    )


def _cleanup_admin_user(username: str) -> None:
    """Drop the test user from htpasswd and Redis."""
    htpasswd_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "backend", "admin.htpasswd")
    )
    if os.path.exists(htpasswd_path):
        with open(htpasswd_path) as f:
            lines = f.readlines()
        with open(htpasswd_path, "w") as f:
            for line in lines:
                if not line.startswith(f"{username}:"):
                    f.write(line)
    _redis_del(f"admin_acls:{username}")


def _me(user: str, password: str) -> dict:
    r = requests.get(
        f"{_host()}/api/admin/me",
        auth=(user, password),
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------


def test_me_requires_auth():
    """No basic auth → 401, not a leak of the field."""
    r = requests.get(f"{_host()}/api/admin/me", timeout=5)
    assert r.status_code == 401


def test_me_superadmin():
    """`*:rw` perm → scope_kind=superadmin, is_superuser=true,
    no scope_app or scope_device populated."""
    user = f"test_me_super_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    try:
        _provision_admin_user(user, pw, [{"prefix": "*", "access": "rw"}])
        me = _me(user, pw)
        assert me["username"] == user
        assert me["is_superuser"] is True
        assert me["scope_kind"] == "superadmin"
        assert me["scope_app"] is None
        assert me["scope_device"] is None
    finally:
        _cleanup_admin_user(user)


def test_me_device_scoped_narrow():
    """The post-migration target ACL shape (one rw on <app>/<device>,
    plus public:r and _catalog:r). Read perms must be ignored when
    deriving scope — they're scaffolding, not identity."""
    user = f"test_me_device_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    try:
        _provision_admin_user(user, pw, [
            {"prefix": "critterchron/ricky_raccoon", "access": "rw"},
            {"prefix": "critterchron/public",        "access": "r"},
            {"prefix": "_catalog/critterchron",      "access": "r"},
        ])
        me = _me(user, pw)
        assert me["is_superuser"] is False
        assert me["scope_kind"] == "device"
        assert me["scope_app"] == "critterchron"
        assert me["scope_device"] == "ricky_raccoon"
    finally:
        _cleanup_admin_user(user)


def test_me_device_scoped_legacy_broad():
    """Pre-migration ACL shape (rw on <app>/<device>, broad r on <app>).
    Should still derive as device — the broad read is ignored. Important
    for Phase 0a-to-0b transition where austin has the legacy shape."""
    user = f"test_me_legacy_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    try:
        _provision_admin_user(user, pw, [
            {"prefix": "critterchron/ricky_raccoon", "access": "rw"},
            {"prefix": "critterchron",               "access": "r"},
        ])
        me = _me(user, pw)
        assert me["scope_kind"] == "device"
        assert me["scope_app"] == "critterchron"
        assert me["scope_device"] == "ricky_raccoon"
    finally:
        _cleanup_admin_user(user)


def test_me_app_scoped():
    """One rw at the app level → scope_kind=app, scope_device=None."""
    user = f"test_me_app_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    try:
        _provision_admin_user(user, pw, [
            {"prefix": "critterchron", "access": "rw"},
        ])
        me = _me(user, pw)
        assert me["scope_kind"] == "app"
        assert me["scope_app"] == "critterchron"
        assert me["scope_device"] is None
    finally:
        _cleanup_admin_user(user)


def test_me_multi_device_falls_to_custom():
    """Two rw perms (a hypothetical owner of two devices) → custom.
    UI treats custom as 'show everything, server enforces' — no special
    multi-device UX in v1."""
    user = f"test_me_multi_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    try:
        _provision_admin_user(user, pw, [
            {"prefix": "critterchron/ricky_raccoon",  "access": "rw"},
            {"prefix": "critterchron/rachel_raccoon", "access": "rw"},
        ])
        me = _me(user, pw)
        assert me["scope_kind"] == "custom"
        assert me["scope_app"] is None
        assert me["scope_device"] is None
    finally:
        _cleanup_admin_user(user)


def test_me_unprovisioned_user_returns_empty_perms():
    """htpasswd entry exists, no Redis ACL row → strict deny-all envelope.
    Mirrors load_admin_acl's documented behaviour."""
    user = f"test_me_unprov_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    backend_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "backend")
    )
    venv_python = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.exists(venv_python):
        pytest.skip("backend venv not found")
    try:
        subprocess.run(
            [venv_python, "create_admin.py", user, pw],
            cwd=backend_dir, check=True, capture_output=True,
        )
        # Deliberately no admin_acls:<user> set
        me = _me(user, pw)
        assert me["acl"]["permissions"] == []
        assert me["is_superuser"] is False
        assert me["scope_kind"] == "custom"
    finally:
        _cleanup_admin_user(user)


def test_me_acl_is_returned_verbatim():
    """The full ACL is returned to the caller — JS may need it for
    fine-grained gating beyond what scope_kind covers."""
    user = f"test_me_acl_{uuid.uuid4().hex[:8]}"
    pw = "testpass"
    perms = [
        {"prefix": "critterchron/ricky_raccoon", "access": "rw"},
        {"prefix": "critterchron/public",        "access": "r"},
    ]
    try:
        _provision_admin_user(user, pw, perms)
        me = _me(user, pw)
        assert me["acl"]["permissions"] == perms
    finally:
        _cleanup_admin_user(user)
