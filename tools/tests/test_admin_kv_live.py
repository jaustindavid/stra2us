# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Live tests for the admin `/api/admin/kv/{key}` POST encrypted-flag plumbing.

The device-side path (`POST /kv/{key}` with `X-Encrypted: 1`) is covered
by `test_encrypted_live.py`. This file pins the *admin* path —
`KVPayload.encrypted: bool` and the corresponding sidecar set/clear
in `routes_admin.set_kv` — which during the FR was only verified by
hand through the dashboard UI.

Skipped unless a local stra2us is reachable. Expects env:

    STRA2US_HOST          e.g. http://127.0.0.1:8153
    STRA2US_ADMIN_USER    admin htpasswd username (rw on `_test/*`)
    STRA2US_ADMIN_PASS    matching password
    STRA2US_CLIENT_ID     a device client (rw on `_test/*`)
    STRA2US_SECRET_HEX    matching device secret

The device creds are used to verify the round-trip — the admin POST
sets the flag, then the device-facing GET (which is what real devices
hit) returns ext 0x21 vs. plaintext msgpack accordingly.

The admin user needs an ACL granting write on the test prefix; in
Redis this is `admin_acls:<user>` shaped as
`{"permissions":[{"prefix":"_test","access":"rw"}]}`.
"""

from __future__ import annotations

import os
import uuid

import msgpack
import pytest
import requests

from stra2us_cli.client import KVENC_EXT_TYPE, Stra2usClient


TEST_PREFIX = "_test"


def _env_ready() -> bool:
    return all(
        os.environ.get(k)
        for k in (
            "STRA2US_HOST",
            "STRA2US_ADMIN_USER",
            "STRA2US_ADMIN_PASS",
            "STRA2US_CLIENT_ID",
            "STRA2US_SECRET_HEX",
        )
    )


pytestmark = pytest.mark.skipif(
    not _env_ready(),
    reason="needs STRA2US_HOST/ADMIN_USER/ADMIN_PASS/CLIENT_ID/SECRET_HEX",
)


def _host() -> str:
    h = os.environ["STRA2US_HOST"].rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h


def _admin_auth() -> tuple[str, str]:
    return os.environ["STRA2US_ADMIN_USER"], os.environ["STRA2US_ADMIN_PASS"]


def _device_client() -> Stra2usClient:
    return Stra2usClient(
        base_url=_host(),
        client_id=os.environ["STRA2US_CLIENT_ID"],
        secret_hex=os.environ["STRA2US_SECRET_HEX"],
    )


def _admin_post_kv(key: str, payload: dict) -> requests.Response:
    """POST /api/admin/kv/{key} with `payload` as JSON. Caller controls
    whether `encrypted` is present, so we can exercise the Pydantic
    default path explicitly."""
    return requests.post(
        f"{_host()}/api/admin/kv/{key}",
        auth=_admin_auth(),
        json=payload,
        timeout=5,
    )


def _admin_delete_kv(key: str) -> None:
    requests.delete(
        f"{_host()}/api/admin/kv/{key}",
        auth=_admin_auth(),
        timeout=5,
    )


def _admin_peek_kv(key: str) -> dict:
    r = requests.get(
        f"{_host()}/api/admin/peek/kv/{key}",
        auth=_admin_auth(),
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


def _device_raw_get(key: str) -> bytes:
    """HMAC-signed GET as the device would see it. Returns raw response
    body so we can inspect the wire form (ext 0x21 vs. plaintext)."""
    import hashlib
    import hmac
    import time

    client = _device_client()
    ts = int(time.time())
    uri = client._kv_uri(key)
    secret = client._secret_bytes()
    payload = uri.encode("utf-8") + b"" + str(ts).encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    r = requests.get(
        client.base_url + uri,
        headers={
            "X-Client-ID": client.client_id,
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        },
        timeout=5,
    )
    r.raise_for_status()
    return r.content


def _unique_key() -> str:
    return f"{TEST_PREFIX}/admin_enc_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------


def test_admin_post_encrypted_true_marks_sidecar():
    """`{value, encrypted: true}` → device GET returns ext 0x21, peek
    reports the flag set."""
    key = _unique_key()
    try:
        r = _admin_post_kv(key, {"value": "admin-set-secret", "encrypted": True})
        assert r.status_code == 200, r.text

        peek = _admin_peek_kv(key)
        assert peek["encrypted"] is True

        body = _device_raw_get(key)
        decoded = msgpack.unpackb(body, raw=False)
        assert isinstance(decoded, msgpack.ExtType)
        assert decoded.code == KVENC_EXT_TYPE
    finally:
        _admin_delete_kv(key)


def test_admin_post_encrypted_false_is_plaintext():
    """`{value, encrypted: false}` → device GET returns ordinary
    msgpack, peek reports flag unset."""
    key = _unique_key()
    try:
        r = _admin_post_kv(key, {"value": "admin-set-public", "encrypted": False})
        assert r.status_code == 200, r.text

        peek = _admin_peek_kv(key)
        assert peek["encrypted"] is False

        body = _device_raw_get(key)
        decoded = msgpack.unpackb(body, raw=False)
        assert not isinstance(decoded, msgpack.ExtType)
        assert decoded == "admin-set-public"
    finally:
        _admin_delete_kv(key)


def test_admin_post_omitting_encrypted_field_defaults_to_false():
    """Pydantic default — `KVPayload.encrypted: bool = False` — must keep
    the sidecar absent. Pre-FR clients that don't know about the field
    still send `{value}` only and must continue to write plaintext."""
    key = _unique_key()
    try:
        r = _admin_post_kv(key, {"value": "legacy-shaped-payload"})
        assert r.status_code == 200, r.text

        peek = _admin_peek_kv(key)
        assert peek["encrypted"] is False

        body = _device_raw_get(key)
        decoded = msgpack.unpackb(body, raw=False)
        assert not isinstance(decoded, msgpack.ExtType)
    finally:
        _admin_delete_kv(key)


def test_admin_post_demote_clears_sidecar():
    """Re-POST without `encrypted: true` must drop the sidecar — this is
    the FR's "I changed my mind" semantic. Easy to break by adding a
    `if encrypted: set sidecar` branch without the matching `else: del`,
    and the failure mode is silent (key still reads plaintext on the
    *server* side, but appears encrypted in the admin list because the
    sidecar lingers)."""
    key = _unique_key()
    try:
        # Mark encrypted
        r = _admin_post_kv(key, {"value": "secret-v1", "encrypted": True})
        assert r.status_code == 200
        assert _admin_peek_kv(key)["encrypted"] is True

        # Demote
        r = _admin_post_kv(key, {"value": "now-public-v2", "encrypted": False})
        assert r.status_code == 200
        assert _admin_peek_kv(key)["encrypted"] is False

        # Wire form must now be plaintext, not ext 0x21
        body = _device_raw_get(key)
        decoded = msgpack.unpackb(body, raw=False)
        assert not isinstance(decoded, msgpack.ExtType)
        assert decoded == "now-public-v2"
    finally:
        _admin_delete_kv(key)


def test_admin_delete_clears_sidecar():
    """DELETE must drop both the value and the sidecar in one shot.
    A leaked sidecar would make a freshly-POSTed plaintext value appear
    encrypted on the next read."""
    key = _unique_key()
    try:
        _admin_post_kv(key, {"value": "transient", "encrypted": True})
        assert _admin_peek_kv(key)["encrypted"] is True

        _admin_delete_kv(key)

        # Re-create as plaintext at the same key — must not inherit a
        # leaked sidecar.
        _admin_post_kv(key, {"value": "fresh"})
        peek = _admin_peek_kv(key)
        assert peek["status"] == "ok"
        assert peek["encrypted"] is False
    finally:
        _admin_delete_kv(key)
