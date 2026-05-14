# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Integration tests for v1.8.0 Sprint 7's backup/restore.

Goes one level above the envelope round-trip tests in
`test_backup_format.py`: populate a fake Redis with a representative
mix of state, dump, restore to an empty fake Redis, assert state
matches. Also exercises skip-existing vs `force_overwrite`, per-app
filtering, and the defense-in-depth check that per-app restore
rejects out-of-namespace keys.

Test scope deliberately wider than `test_backup_format.py` because
this is the file that catches:
  * a serializer that forgets a section (no Redis key shape covered)
  * an iterator that misses a key family
  * a restore path that writes the wrong key shape
  * a per-app filter that snags / misses something it shouldn't

The actual HTTP endpoint wiring gets one smoke test at the bottom —
the bulk of correctness lives in the service-level integration
tests so failures point at exactly the line that broke.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    """Project convention (mirrors test_value_resolver.py): run an
    async coroutine inside a sync test. Avoids the pytest-asyncio
    dep — the rest of the suite uses this pattern."""
    return asyncio.run(coro)


# ----- fake Redis with stream support -------------------------------

class _FakeRedis:
    """Subset large enough for backup_io: KV (get/set/exists/delete/keys)
    plus Redis-stream ops (xadd / xrange).

    Stream behavior matches the bit of redis-py we actually use:
      * `xadd(stream, fields, id=...)` appends with the given ID.
      * `xrange(stream, min, max)` returns `[(id, fields), ...]` in
        ID order. Min/max are "-" / "+" sentinels — we ignore them
        and return the whole stream (sufficient for backup-paths,
        which don't paginate).
      * Field values are stored as bytes; keys as bytes (matching
        real redis-py's default decode_responses=False).
    """

    def __init__(self):
        self._kv: dict[str, bytes] = {}
        # Streams keyed by full Redis key (e.g. "q:topic" or
        # "system:activity_log"). Each value is an ordered list of
        # (id_bytes, {field_bytes: value_bytes}).
        self._streams: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}

    # ----- KV -----

    async def get(self, key):
        return self._kv.get(key)

    async def set(self, key, value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._kv[key] = value

    async def delete(self, *keys):
        for k in keys:
            self._kv.pop(k, None)
            self._streams.pop(k, None)

    async def exists(self, key):
        if key in self._kv:
            return 1
        if key in self._streams:
            return 1
        return 0

    async def keys(self, pattern):
        # Real Redis KEYS searches across types. We need to include
        # stream keys too so the backup iterators find them.
        all_keys = set(self._kv.keys()) | set(self._streams.keys())
        return [k.encode("utf-8") for k in all_keys
                if fnmatch.fnmatch(k, pattern)]

    # ----- streams -----

    async def xadd(self, key, fields, id=None):
        # Normalize fields to bytes-keys + bytes-values, matching how
        # real redis-py stores them after serialization.
        norm: dict[bytes, bytes] = {}
        for k, v in fields.items():
            kk = k if isinstance(k, bytes) else k.encode("utf-8")
            vv = v if isinstance(v, bytes) else v.encode("utf-8")
            norm[kk] = vv
        entries = self._streams.setdefault(key, [])
        if id is None:
            # Auto-id: use 1-based count. Tests don't exercise this
            # path (they always pass id=); kept for completeness.
            id = f"{len(entries) + 1}-0".encode("utf-8")
        elif isinstance(id, str):
            id = id.encode("utf-8")
        entries.append((id, norm))
        return id

    async def xrange(self, key, min="-", max="+", count=None):
        # We ignore min/max bounds (backup iterators always want all).
        return list(self._streams.get(key, []))

    async def xrevrange(self, key, max="+", min="-", count=None):
        return list(reversed(self._streams.get(key, [])))


# ----- fixtures ------------------------------------------------------

@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    from core import redis_client
    from api import routes_admin, dependencies
    from services import backup_io  # noqa: F401 — referenced by routes via import-in-fn

    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(routes_admin, "get_redis_client", lambda: fr)
    monkeypatch.setattr(dependencies, "get_redis_client", lambda: fr)
    return fr


def _seed_two_apps(fr: _FakeRedis) -> None:
    """Populate the fake Redis with two apps' worth of state. Both
    follow the device-on-app provisioning shape; the second exists to
    catch per-app filter leaks ("did we accidentally include
    petwatch in the critterchron dump?")."""
    # critterchron: 1 device, KV data, a catalog with one asset, queue.
    fr._kv["client:critterchron-dev1:secret"] = b"cafef00d" * 8
    fr._kv["client:critterchron-dev1:acl"] = json.dumps({
        "permissions": [
            {"prefix": "critterchron/critterchron-dev1", "access": "rw"},
            {"prefix": "critterchron/public",            "access": "rw"},
        ]
    }).encode("utf-8")
    fr._kv["kv:critterchron/critterchron-dev1/wifi_ssid"] = b"\xa6myssid"
    fr._kv["kv:critterchron/critterchron-dev1/wifi_pass"] = b"\xb0\x00\xff"
    fr._kv["kv:critterchron/critterchron-dev1/wifi_pass:enc"] = b"1"
    fr._kv["kv:_catalog/critterchron"] = b"name: critterchron\nvars: {}\n"
    fr._kv["kv:_catalog/critterchron/_assets/logo.png"] = b"\x89PNG\r\n\x1a\n"
    fr._kv["device_to_app:critterchron-dev1"] = b"critterchron"
    fr._streams["q:critterchron/public/heartbeep"] = [
        (b"1700000000000-0", {b"client_id": b"critterchron-dev1",
                              b"payload":   b"\x82\xa4tick\x01"}),
        (b"1700000001000-0", {b"client_id": b"critterchron-dev1",
                              b"payload":   b"\x82\xa4tick\x02"}),
    ]

    # petwatch: separate app; everything here should be excluded
    # from per-app critterchron dumps.
    fr._kv["client:petwatch-dev1:secret"] = b"deadbeef" * 8
    fr._kv["client:petwatch-dev1:acl"] = json.dumps({
        "permissions": [
            {"prefix": "petwatch/petwatch-dev1", "access": "rw"},
            {"prefix": "petwatch/public",        "access": "rw"},
        ]
    }).encode("utf-8")
    fr._kv["kv:petwatch/petwatch-dev1/whatever"] = b"shouldnotleak"
    fr._kv["device_to_app:petwatch-dev1"] = b"petwatch"

    # Admin ACLs: one wildcard, one per-app, one for the other app.
    fr._kv["admin_acls:smoke"] = json.dumps({
        "permissions": [{"prefix": "*", "access": "r"}]
    }).encode("utf-8")
    fr._kv["admin_acls:critter-admin"] = json.dumps({
        "permissions": [{"prefix": "critterchron", "access": "rw"}]
    }).encode("utf-8")
    fr._kv["admin_acls:petwatch-admin"] = json.dumps({
        "permissions": [{"prefix": "petwatch", "access": "rw"}]
    }).encode("utf-8")

    # Activity log: two entries from different clients.
    fr._streams["system:activity_log"] = [
        (b"1700000000500-0", {b"client_id": b"critterchron-dev1",
                              b"method":    b"POST",
                              b"uri":       b"/q/critterchron/public/heartbeep"}),
        (b"1700000000600-0", {b"client_id": b"petwatch-dev1",
                              b"method":    b"POST",
                              b"uri":       b"/q/petwatch/public/ping"}),
    ]


# ----- collect: whole-instance --------------------------------------

def test_collect_whole_includes_every_section(fake_redis):
    async def go():
        """Whole-instance collect should pick up every load-bearing key
        family. If a new section gets added to the envelope and this test
        doesn't notice, that's a serialization-coverage bug."""
        from services.backup_io import collect_whole_envelope
        _seed_two_apps(fake_redis)
        env = await collect_whole_envelope(fake_redis, include_logs=False)
        # Clients
        assert set(env.clients.keys()) == {"critterchron-dev1", "petwatch-dev1"}
        # Admin ACLs (wildcard included for whole-instance)
        assert set(env.admin_acls.keys()) == {"smoke", "critter-admin", "petwatch-admin"}
        # KV — including the catalog + asset under critterchron, and petwatch's data.
        assert "critterchron/critterchron-dev1/wifi_ssid" in env.kv
        assert env.kv["critterchron/critterchron-dev1/wifi_pass"].encrypted is True
        assert "_catalog/critterchron" in env.kv
        assert "_catalog/critterchron/_assets/logo.png" in env.kv
        assert "petwatch/petwatch-dev1/whatever" in env.kv
        # Queues
        assert "critterchron/public/heartbeep" in env.queues
        assert len(env.queues["critterchron/public/heartbeep"]) == 2
        # device_to_app
        assert env.device_to_app == {
            "critterchron-dev1": "critterchron",
            "petwatch-dev1":     "petwatch",
        }
        # Activity log opt-in: not included by default.
        assert env.activity_log is None
    _run(go())


def test_collect_whole_with_include_logs_pulls_activity_log(fake_redis):
    async def go():
        from services.backup_io import collect_whole_envelope
        _seed_two_apps(fake_redis)
        env = await collect_whole_envelope(fake_redis, include_logs=True)
        assert env.activity_log is not None
        # Whole-instance log: every entry, no filtering.
        assert len(env.activity_log) == 2


    # ----- collect: per-app filter --------------------------------------
    _run(go())


def test_collect_per_app_filters_to_app_only(fake_redis):
    async def go():
        """The per-app dump must include the requested app's data and
        exclude every other app's. Wildcard admins are excluded too —
        they're instance-scoped, not per-app."""
        from services.backup_io import collect_per_app_envelope
        _seed_two_apps(fake_redis)
        env = await collect_per_app_envelope(fake_redis, "critterchron", include_logs=True)

        # ONLY critterchron-dev1; petwatch-dev1 excluded.
        assert set(env.clients.keys()) == {"critterchron-dev1"}
        # Admin ACLs: critter-admin (matches), smoke (wildcard, excluded),
        # petwatch-admin (different app, excluded).
        assert set(env.admin_acls.keys()) == {"critter-admin"}
        # KV: critterchron's data + catalog + asset. NOT petwatch's data.
        kv_keys = set(env.kv.keys())
        assert "critterchron/critterchron-dev1/wifi_ssid" in kv_keys
        assert "_catalog/critterchron" in kv_keys
        assert "_catalog/critterchron/_assets/logo.png" in kv_keys
        assert not any("petwatch" in k for k in kv_keys), \
            f"per-app dump leaked petwatch data: {kv_keys}"
        # Queues
        assert set(env.queues.keys()) == {"critterchron/public/heartbeep"}
        # device_to_app: only critterchron entries.
        assert env.device_to_app == {"critterchron-dev1": "critterchron"}
        # Activity log: filtered to entries whose client_id is in the
        # included client set.
        assert env.activity_log is not None
        assert len(env.activity_log) == 1
        # `_collect_stream` normalizes field-keys to str (value bytes
        # stay raw — fields can be legitimately binary).
        assert env.activity_log[0].fields["client_id"] == b"critterchron-dev1"


    # ----- full round-trip: dump → restore matches ---------------------
    _run(go())


def test_whole_roundtrip_restores_to_empty_redis(monkeypatch):
    async def go():
        """The headline test: a whole-instance dump from one Redis,
        restored to an empty Redis, should produce byte-identical state
        in every section. This is the "did the backup work" guarantee."""
        from services.backup_io import (
            apply_envelope,
            collect_whole_envelope,
        )
        src = _FakeRedis()
        _seed_two_apps(src)
        env = await collect_whole_envelope(src, include_logs=True)

        dst = _FakeRedis()
        result = await apply_envelope(dst, env, force_overwrite=False)

        # Every section restored to a fresh destination (no skips).
        assert result["clients"]["skipped"] == []
        assert result["kv"]["skipped"] == []
        assert result["queues"]["skipped"] == []
        assert result["rejected_outside_app_filter"] == []

        # KV survived byte-for-byte (including binary asset bytes).
        assert dst._kv["kv:critterchron/critterchron-dev1/wifi_ssid"] == b"\xa6myssid"
        assert dst._kv["kv:critterchron/critterchron-dev1/wifi_pass"] == b"\xb0\x00\xff"
        assert dst._kv["kv:critterchron/critterchron-dev1/wifi_pass:enc"] == b"1"
        assert dst._kv["kv:_catalog/critterchron/_assets/logo.png"].startswith(b"\x89PNG")
        # Client credentials survived.
        assert dst._kv["client:critterchron-dev1:secret"] == b"cafef00d" * 8
        # ACL re-serialized but semantically equal.
        assert json.loads(dst._kv["client:critterchron-dev1:acl"]) == \
            json.loads(src._kv["client:critterchron-dev1:acl"])
        # Streams: IDs + field values preserved.
        assert dst._streams["q:critterchron/public/heartbeep"][0][0] == b"1700000000000-0"
        assert dst._streams["q:critterchron/public/heartbeep"][0][1][b"payload"] == b"\x82\xa4tick\x01"
        # Activity log restored.
        assert len(dst._streams["system:activity_log"]) == 2


    # ----- skip-existing vs force_overwrite ----------------------------
    _run(go())


def test_apply_skips_existing_by_default(monkeypatch):
    async def go():
        from services.backup_io import apply_envelope, collect_whole_envelope
        src = _FakeRedis()
        _seed_two_apps(src)
        env = await collect_whole_envelope(src, include_logs=False)

        # Destination has a conflicting client already.
        dst = _FakeRedis()
        dst._kv["client:critterchron-dev1:secret"] = b"a-different-secret"
        dst._kv["client:critterchron-dev1:acl"] = b'{"permissions": []}'

        result = await apply_envelope(dst, env, force_overwrite=False)

        # Should skip — original value preserved.
        assert "critterchron-dev1" in result["clients"]["skipped"]
        assert dst._kv["client:critterchron-dev1:secret"] == b"a-different-secret"
    _run(go())


def test_apply_force_overwrite_replaces_existing(monkeypatch):
    async def go():
        from services.backup_io import apply_envelope, collect_whole_envelope
        src = _FakeRedis()
        _seed_two_apps(src)
        env = await collect_whole_envelope(src, include_logs=False)

        dst = _FakeRedis()
        dst._kv["client:critterchron-dev1:secret"] = b"a-different-secret"

        result = await apply_envelope(dst, env, force_overwrite=True)

        assert "critterchron-dev1" in result["clients"]["overwritten"]
        assert dst._kv["client:critterchron-dev1:secret"] == b"cafef00d" * 8
    _run(go())


def test_apply_clears_stale_enc_sidecar_on_plaintext_restore(monkeypatch):
    async def go():
        """If a key was encrypted before and the dump restores a now-
        plaintext value, the stale `:enc` sidecar MUST be cleared —
        otherwise reads would try to decrypt plaintext and corrupt."""
        from services.backup_io import apply_envelope
        from services.backup_format import BackupEnvelope, KVRecord
        env = BackupEnvelope(dump_kind="whole", app=None, exported_at="")
        env.kv = {"some/key": KVRecord(value=b"plaintext", encrypted=False)}

        dst = _FakeRedis()
        # Stale sidecar from a previous (encrypted) era.
        dst._kv["kv:some/key:enc"] = b"1"

        await apply_envelope(dst, env, force_overwrite=True)

        assert dst._kv["kv:some/key"] == b"plaintext"
        assert "kv:some/key:enc" not in dst._kv


    # ----- per-app restore defense-in-depth ----------------------------
    _run(go())


def test_per_app_restore_rejects_cross_app_keys(monkeypatch):
    async def go():
        """A per-app restore must NOT write keys outside `<app>/...`,
        even if the envelope lies about its scope. The endpoint takes
        the URL `<app>` as authoritative; backup_io.apply_envelope
        enforces it via app_filter."""
        from services.backup_io import apply_envelope
        from services.backup_format import (
            BackupEnvelope, ClientRecord, KVRecord,
        )

        # Construct an envelope that CLAIMS to be per-app for `critterchron`
        # but smuggles in petwatch data. Restore-with-app_filter must
        # reject the petwatch bits.
        env = BackupEnvelope(
            dump_kind="per-app", app="critterchron", exported_at="",
        )
        env.clients = {
            "critterchron-dev1": ClientRecord(
                secret="ok", acl={"permissions": [
                    {"prefix": "critterchron/dev1", "access": "rw"},
                ]},
            ),
            "petwatch-dev1": ClientRecord(
                secret="smuggled", acl={"permissions": [
                    {"prefix": "petwatch/dev1", "access": "rw"},
                ]},
            ),
        }
        env.kv = {
            "critterchron/dev1/data":  KVRecord(value=b"ok"),
            "petwatch/dev1/smuggled":  KVRecord(value=b"should not land"),
            "_catalog/critterchron":   KVRecord(value=b"ok-catalog"),
            "_catalog/petwatch":       KVRecord(value=b"smuggled-catalog"),
        }

        dst = _FakeRedis()
        result = await apply_envelope(
            dst, env, force_overwrite=False, app_filter="critterchron",
        )

        # The critterchron entries land.
        assert "critterchron-dev1" in result["clients"]["restored"]
        assert "critterchron/dev1/data" in result["kv"]["restored"]
        assert "_catalog/critterchron" in result["kv"]["restored"]
        # The petwatch entries get rejected — not written, and listed.
        assert "client:petwatch-dev1" in result["rejected_outside_app_filter"]
        assert "kv:petwatch/dev1/smuggled" in result["rejected_outside_app_filter"]
        assert "kv:_catalog/petwatch" in result["rejected_outside_app_filter"]
        # Verify nothing rejected actually made it to the destination.
        assert "kv:petwatch/dev1/smuggled" not in dst._kv
        assert "client:petwatch-dev1:secret" not in dst._kv
        assert "kv:_catalog/petwatch" not in dst._kv


    # ----- stream re-population preserves IDs --------------------------
    _run(go())


def test_restore_preserves_stream_ids(monkeypatch):
    async def go():
        """For cross-host migration, stream IDs MUST survive — they encode
        timestamps + ordering. A restore that re-XADDs with auto-IDs would
        break activity-log timestamps + queue retention math."""
        from services.backup_io import apply_envelope, collect_whole_envelope
        src = _FakeRedis()
        _seed_two_apps(src)
        env = await collect_whole_envelope(src, include_logs=True)
        dst = _FakeRedis()
        await apply_envelope(dst, env, force_overwrite=False)

        src_ids = [eid for eid, _ in src._streams["q:critterchron/public/heartbeep"]]
        dst_ids = [eid for eid, _ in dst._streams["q:critterchron/public/heartbeep"]]
        assert src_ids == dst_ids
    _run(go())


def test_restore_skips_existing_stream_by_default(monkeypatch):
    async def go():
        """Queue + activity-log streams are skip-if-exists for safety —
        per-entry merging is out of scope for v1. force_overwrite=True
        DELs + repopulates."""
        from services.backup_io import apply_envelope, collect_whole_envelope
        src = _FakeRedis()
        _seed_two_apps(src)
        env = await collect_whole_envelope(src, include_logs=False)

        dst = _FakeRedis()
        # Pre-populate the destination's queue.
        dst._streams["q:critterchron/public/heartbeep"] = [
            (b"9999-0", {b"client_id": b"existing"}),
        ]
        result = await apply_envelope(dst, env, force_overwrite=False)

        assert "critterchron/public/heartbeep" in result["queues"]["skipped"]
        # Destination stream unchanged — still has the pre-existing entry only.
        assert dst._streams["q:critterchron/public/heartbeep"] == [
            (b"9999-0", {b"client_id": b"existing"}),
        ]


    # ----- HTTP endpoint smoke (one round-trip via TestClient) ---------
    _run(go())


@pytest.fixture
def test_app(fake_redis):
    """Wire just the admin router + override the superuser dependency
    so the endpoints are reachable without admin auth."""
    from api.routes_admin import router as admin_router
    from api.dependencies import require_admin_superuser

    a = FastAPI()
    a.include_router(admin_router)
    a.dependency_overrides[require_admin_superuser] = lambda: {
        "client_id": "test-superuser", "is_admin": True,
    }
    return a, TestClient(a)


def test_get_backup_endpoint_returns_versioned_envelope(test_app):
    """GET /backup returns a JSON envelope with the right version,
    dump_kind, and the sensitive-data header set."""
    app, client = test_app
    from core.redis_client import get_redis_client
    fr = get_redis_client()
    _seed_two_apps(fr)

    r = client.get("/backup")
    assert r.status_code == 200
    assert r.headers.get("X-Stra2us-Sensitive") == "true"
    assert "stra2us_backup_whole_" in r.headers.get("content-disposition", "")
    doc = r.json()
    assert doc["stra2us_backup_version"] == 1
    assert doc["dump_kind"] == "whole"
    # `activity_log` defaults off → null.
    assert doc["data"]["activity_log"] is None


def test_get_backup_app_filters_namespaces(test_app):
    app, client = test_app
    from core.redis_client import get_redis_client
    fr = get_redis_client()
    _seed_two_apps(fr)

    r = client.get("/backup/app/critterchron")
    assert r.status_code == 200
    doc = r.json()
    assert doc["dump_kind"] == "per-app"
    assert doc["app"] == "critterchron"
    assert "critterchron-dev1" in doc["data"]["clients"]
    assert "petwatch-dev1" not in doc["data"]["clients"]


def test_restore_endpoint_round_trip(test_app):
    """POST /restore accepts an envelope and writes it back to Redis.
    Verifies the dump → restore path is consistent end-to-end through
    the HTTP layer."""
    app, client = test_app
    from core.redis_client import get_redis_client
    fr = get_redis_client()
    _seed_two_apps(fr)

    dump = client.get("/backup").json()

    # Wipe destination, restore.
    fr._kv.clear()
    fr._streams.clear()

    r = client.post("/restore", json=dump)
    assert r.status_code == 200
    result = r.json()
    assert "critterchron-dev1" in result["clients"]["restored"]
    # State is back.
    assert fr._kv["client:critterchron-dev1:secret"] == b"cafef00d" * 8


def test_restore_app_refuses_mismatched_envelope_app(test_app):
    """POST /restore/app/<X> with an envelope claiming app=Y is an
    operator footgun — refuse with 400."""
    app, client = test_app
    bogus = {
        "stra2us_backup_version": 1,
        "dump_kind": "per-app",
        "app": "petwatch",
        "exported_at": "",
        "data": {},
    }
    r = client.post("/restore/app/critterchron", json=bogus)
    assert r.status_code == 400
    assert "does not match URL" in r.json()["detail"]


def test_restore_endpoint_rejects_unknown_version(test_app):
    app, client = test_app
    bad = {
        "stra2us_backup_version": 99,
        "dump_kind": "whole",
        "app": None,
        "exported_at": "",
        "data": {},
    }
    r = client.post("/restore", json=bad)
    assert r.status_code == 400
    assert "unsupported envelope version" in r.json()["detail"]
