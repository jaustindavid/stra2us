# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Form-submit handler tests (P3 of `docs/fr_catalog_app_ui_plan.md`).

P3 ships the **strict-naive** handler — every form field present
in the POST body is written verbatim. The pre-P4 footguns
(off-spec stomping, write_only-empty wiping the stored secret)
are documented behaviors the FR defers to P4's touched-state JS.

Tests cover:
* Each form field lands at `kv:<app>/<device>/<name>`.
* Type recovery via `json.loads` matches the existing admin
  endpoint's pattern (the `129` form-string round-trips as int).
* Encrypted-flag sidecar is preserved across the write.
* POST-redirect-GET (303) prevents refresh-double-submit.
* Path traversal via `/` in form names is rejected.
"""

from __future__ import annotations

import msgpack
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import dependencies, routes_app_form
from core import redis_client


# ----- fake Redis (subset shared with the other route tests) -----

class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    async def set(self, key, value):
        self._kv[key] = value

    async def delete(self, *keys):
        for k in keys:
            self._kv.pop(k, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    # Patch in every place the route reaches for a redis client.
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_app_form, "get_redis_client", lambda: fr)
    return fr


@pytest.fixture
def bypass_auth(monkeypatch):
    """The form handler runs the same auth + ACL pipeline as the
    GET. For unit-testing the write logic, bypass both — return a
    canned admin context, no-op the ACL check."""
    async def _ctx(request):
        return {"client_id": "test-admin"}

    async def _ok(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_app_form, "get_admin_context", _ctx)
    monkeypatch.setattr(routes_app_form, "check_acl", _ok)


@pytest.fixture
def client(fake_redis, bypass_auth):
    a = FastAPI()
    a.include_router(routes_app_form.router)
    return TestClient(a)


# ----- happy path -----

def test_form_writes_each_field_to_kv(client, fake_redis):
    r = client.post(
        "/app/critterchron/dev1",
        data={"display_mode": "weather", "ir_brightness": "75"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # POST-redirect-GET back to the page so refresh doesn't re-submit.
    assert r.headers["location"] == "/app/critterchron/dev1"

    # display_mode is a string per JSON-parse fallback.
    raw = fake_redis._kv["kv:critterchron/dev1/display_mode"]
    assert msgpack.unpackb(raw, raw=False) == "weather"
    # ir_brightness "75" round-trips through json.loads → 75 (int).
    raw = fake_redis._kv["kv:critterchron/dev1/ir_brightness"]
    assert msgpack.unpackb(raw, raw=False) == 75


def test_json_parse_recovers_native_types(client, fake_redis):
    """Mirrors the existing admin endpoint's coercion. Each form
    string is `json.loads`-ed; failures fall back to string."""
    client.post("/app/demo/dev1", data={
        "an_int": "42",
        "a_float": "3.14",
        "a_bool": "true",
        "a_string": "hello",  # JSON parse fails, stays string
        "json_string": '"quoted"',  # parses to "quoted"
    }, follow_redirects=False)

    def _v(k):
        return msgpack.unpackb(
            fake_redis._kv[f"kv:demo/dev1/{k}"], raw=False)
    assert _v("an_int") == 42
    assert _v("a_float") == 3.14
    assert _v("a_bool") is True
    assert _v("a_string") == "hello"
    assert _v("json_string") == "quoted"


def test_empty_value_writes_empty_string(client, fake_redis):
    """write_only fields ship empty in P3 (no touched-state JS).
    Form sends `name=` → server writes empty string. The FR
    acknowledges this as the pre-P4 footgun; tests document the
    behavior so a future change to it is loud."""
    r = client.post("/app/demo/dev1", data={"wifi_password": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    raw = fake_redis._kv["kv:demo/dev1/wifi_password"]
    assert msgpack.unpackb(raw, raw=False) == ""


# ----- encrypted flag preservation -----

def test_encrypted_flag_preserved_on_write(client, fake_redis):
    """Pre-existing encrypted record stays encrypted after a form
    write. The form-submit path doesn't have the device-side
    `X-Encrypted: 1` header signal, so we read the existing flag
    and restore it across the write."""
    fake_redis._kv["kv:demo/dev1/wifi_password:enc"] = b"1"

    client.post("/app/demo/dev1", data={"wifi_password": "newsecret"},
                follow_redirects=False)
    assert fake_redis._kv.get("kv:demo/dev1/wifi_password:enc") == b"1"
    raw = fake_redis._kv["kv:demo/dev1/wifi_password"]
    assert msgpack.unpackb(raw, raw=False) == "newsecret"


def test_no_encrypted_flag_when_record_was_plaintext(client, fake_redis):
    """A field that wasn't encrypted before doesn't get the flag
    set by a form write. Plaintext stays plaintext."""
    client.post("/app/demo/dev1", data={"greeting": "hello"},
                follow_redirects=False)
    assert "kv:demo/dev1/greeting:enc" not in fake_redis._kv


# ----- path safety -----

def test_form_field_with_slash_in_name_rejected(client, fake_redis):
    """A crafted form field whose name contains `/` could escape
    the device's KV namespace. The renderer never emits such
    names, but we defend at the handler too."""
    client.post(
        "/app/demo/dev1",
        data={"../malicious": "x", "valid_name": "y"},
        follow_redirects=False,
    )
    # Slash-bearing key was skipped; valid one wrote.
    assert "kv:demo/dev1/valid_name" in fake_redis._kv
    # No KV key landed at any path containing the crafted name.
    assert not any("malicious" in k for k in fake_redis._kv)


def test_empty_field_name_rejected(client, fake_redis):
    """Some browsers / form libs can produce empty `name=` pairs;
    skip them rather than write an unnamed key. Send the body
    as a raw urlencoded string to bypass httpx's dict
    deduplication (it rejects `""` as a field name)."""
    client.post(
        "/app/demo/dev1",
        content=b"=x&real=y",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert "kv:demo/dev1/real" in fake_redis._kv


# ----- soft 404 on bad ACL -----

def test_soft_404_redirects_to_landing(monkeypatch):
    """Following the GET path's behavior: an authenticated user
    who lacks ACL on this device gets a soft 303 to landing,
    same shape as "no such device." Avoids leaking
    "this device exists but you can't see it" via differentiated
    failure modes."""
    fr = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_app_form, "get_redis_client", lambda: fr)
    async def _ctx(request):
        return {"client_id": "test-admin"}
    async def _no(*args, **kwargs):
        raise HTTPException(status_code=403, detail="no acl")
    monkeypatch.setattr(routes_app_form, "get_admin_context", _ctx)
    monkeypatch.setattr(routes_app_form, "check_acl", _no)

    a = FastAPI()
    a.include_router(routes_app_form.router)
    c = TestClient(a)
    r = c.post("/app/x/y", data={"k": "v"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/"
    # No write attempted on the device's KV.
    assert "kv:x/y/k" not in fr._kv


# ----- multi-field write order -----

def test_multiple_fields_all_persisted(client, fake_redis):
    """Multi-field POST writes every key. Order isn't asserted —
    correctness is per-key, not per-iteration. (httpx's TestClient
    deprecated list-of-tuples for `data=`; passing dict form
    is enough to verify the handler iterates everything.)"""
    client.post(
        "/app/demo/dev1",
        data={"a": "1", "b": "2", "c": "3"},
        follow_redirects=False,
    )
    for name, expected in (("a", 1), ("b", 2), ("c", 3)):
        raw = fake_redis._kv[f"kv:demo/dev1/{name}"]
        assert msgpack.unpackb(raw, raw=False) == expected
