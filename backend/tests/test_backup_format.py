# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Unit tests for v1.8.0 Sprint 7's backup envelope format.

This file tests the envelope shape + serialization in isolation —
no Redis, no HTTP, no endpoints. Integration tests for the actual
dump/restore endpoints live in `test_backup_restore.py`.

Why split: the envelope format is a *quasi-public artifact* — a
stored dump must be parseable by future versions of the server.
Pinning the wire shape with byte-level round-trip tests in a
dedicated file makes accidental format drift impossible to land
silently. A regression here = a CI red flag pointing at the exact
field that changed.
"""

from __future__ import annotations

import json

import pytest

from services.backup_format import (
    BACKUP_FORMAT_VERSION,
    BackupEnvelope,
    BackupFormatError,
    ClientRecord,
    KVRecord,
    StreamEntry,
    admin_acl_matches_app,
    client_matches_app,
    iso_now,
    kv_key_belongs_to_app,
    queue_topic_belongs_to_app,
)


# ----- round-trip: an envelope serialized then re-parsed must equal -

def _sample_envelope(*, with_logs: bool) -> BackupEnvelope:
    """A populated envelope covering every section the format
    supports. Used by round-trip tests below — a single fixture so
    a new section gets exercised by every existing test."""
    env = BackupEnvelope(
        dump_kind="whole",
        app=None,
        exported_at="2026-05-14T10:00:00Z",
    )
    env.clients = {
        "smoke-test-device": ClientRecord(
            secret="deadbeef" * 8,  # 64 hex chars
            acl={"permissions": [
                {"prefix": "_smoke/smoke-test-device", "access": "rw"},
                {"prefix": "_smoke/public",            "access": "rw"},
            ]},
        ),
        "critterchron-dev1": ClientRecord(
            secret="cafef00d" * 8,
            acl={"permissions": [
                {"prefix": "critterchron/dev1",   "access": "rw"},
                {"prefix": "critterchron/public", "access": "rw"},
            ]},
        ),
    }
    env.admin_acls = {
        "smoke":         {"permissions": [{"prefix": "*", "access": "r"}]},
        "critter-admin": {"permissions": [
            {"prefix": "critterchron",         "access": "rw"},
            {"prefix": "critterchron/public",  "access": "rw"},
        ]},
    }
    # Mix of byte and binary KV values, with + without the encrypted flag.
    env.kv = {
        "critterchron/dev1/wifi_ssid":   KVRecord(value=b"\xa6my-ssid"),
        "critterchron/dev1/wifi_pass":   KVRecord(value=b"\xb0\x00\xff", encrypted=True),
        "_catalog/critterchron":         KVRecord(value=b"yaml goes here"),
        "_catalog/critterchron/_assets/logo.png": KVRecord(value=b"\x89PNG\r\n\x1a\n"),
    }
    env.queues = {
        "critterchron/public/heartbeep": [
            StreamEntry(id="1700000000000-0", fields={
                "client_id": b"critterchron-dev1",
                "payload":   b"\x82\xa4tick\x01\xa2ts\xce\x65\x40\x10\x00",
            }),
            StreamEntry(id="1700000001000-0", fields={
                "client_id": b"critterchron-dev1",
                "payload":   b"\x82\xa4tick\x02",
            }),
        ],
    }
    env.device_to_app = {
        "smoke-test-device": "_smoke",
        "critterchron-dev1": "critterchron",
    }
    if with_logs:
        env.activity_log = [
            StreamEntry(id="1700000000500-0", fields={
                "method":  b"POST",
                "uri":     b"/q/critterchron/public/heartbeep",
                "status":  b"200",
            }),
        ]
    return env


def test_envelope_roundtrip_whole_with_logs():
    """Every section populates, every byte survives the JSON round-trip.
    This is the load-bearing test for format stability."""
    env = _sample_envelope(with_logs=True)
    doc = env.to_json()
    # JSON-serialize + re-parse to prove there's no Python-only state
    # leaking through the dict (e.g. a bytes value that json.dumps
    # would explode on).
    re_parsed = BackupEnvelope.from_json(json.loads(json.dumps(doc)))

    assert re_parsed.dump_kind == "whole"
    assert re_parsed.app is None
    assert re_parsed.clients["smoke-test-device"].secret == env.clients["smoke-test-device"].secret
    assert re_parsed.clients["smoke-test-device"].acl == env.clients["smoke-test-device"].acl
    assert re_parsed.admin_acls == env.admin_acls
    assert re_parsed.kv["critterchron/dev1/wifi_ssid"].value == b"\xa6my-ssid"
    assert re_parsed.kv["critterchron/dev1/wifi_pass"].encrypted is True
    assert re_parsed.kv["critterchron/dev1/wifi_pass"].value == b"\xb0\x00\xff"
    assert re_parsed.kv["_catalog/critterchron/_assets/logo.png"].value.startswith(b"\x89PNG")
    assert len(re_parsed.queues["critterchron/public/heartbeep"]) == 2
    assert re_parsed.queues["critterchron/public/heartbeep"][0].fields["client_id"] == b"critterchron-dev1"
    assert re_parsed.queues["critterchron/public/heartbeep"][0].fields["payload"].startswith(b"\x82\xa4tick")
    assert re_parsed.device_to_app == env.device_to_app
    assert re_parsed.activity_log is not None
    assert re_parsed.activity_log[0].fields["uri"] == b"/q/critterchron/public/heartbeep"


def test_envelope_roundtrip_without_logs_keeps_null():
    """`activity_log` defaults to None; the round-trip preserves that
    (not, say, an empty list)."""
    env = _sample_envelope(with_logs=False)
    re_parsed = BackupEnvelope.from_json(json.loads(json.dumps(env.to_json())))
    assert re_parsed.activity_log is None


def test_envelope_version_field_is_stable():
    """Pinned literal — bumping this constant is a deliberate
    format-incompat decision, not an accident. If this test starts
    failing, the changelog needs a version-policy note."""
    assert BACKUP_FORMAT_VERSION == 1
    doc = _sample_envelope(with_logs=False).to_json()
    assert doc["stra2us_backup_version"] == 1


# ----- per-app envelope shape ----------------------------------------

def test_envelope_per_app_requires_app_name():
    """A `dump_kind: "per-app"` envelope without an `app` field is
    structurally invalid — restore can't know which app to import."""
    bad = {
        "stra2us_backup_version": 1,
        "dump_kind": "per-app",
        "app": None,
        "exported_at": "",
        "data": {},
    }
    with pytest.raises(BackupFormatError, match="per-app dumps must name"):
        BackupEnvelope.from_json(bad)


def test_envelope_whole_allows_null_app():
    """Conversely, whole-instance dumps DON'T require app — and
    `app: null` is the canonical form."""
    env = BackupEnvelope(dump_kind="whole", app=None, exported_at="")
    re_parsed = BackupEnvelope.from_json(env.to_json())
    assert re_parsed.app is None
    assert re_parsed.dump_kind == "whole"


# ----- version-policy enforcement ------------------------------------

def test_envelope_rejects_unknown_version():
    """The whole point of the version field: a future v2 dump must
    NOT be silently mis-parsed as v1. Restore should reject loudly
    and let the operator find a server that understands v2."""
    bad = {
        "stra2us_backup_version": 99,
        "dump_kind": "whole",
        "app": None,
        "exported_at": "",
        "data": {},
    }
    with pytest.raises(BackupFormatError, match="unsupported envelope version"):
        BackupEnvelope.from_json(bad)


def test_envelope_rejects_missing_version():
    """An envelope with no version field at all is also not a v1 dump
    by definition — same rejection path."""
    with pytest.raises(BackupFormatError, match="unsupported envelope version"):
        BackupEnvelope.from_json({"dump_kind": "whole", "app": None, "data": {}})


# ----- structural validation -----------------------------------------

def test_envelope_rejects_non_dict():
    with pytest.raises(BackupFormatError, match="JSON object"):
        BackupEnvelope.from_json("not a dict")  # type: ignore[arg-type]


def test_envelope_rejects_bad_dump_kind():
    with pytest.raises(BackupFormatError, match="dump_kind must be"):
        BackupEnvelope.from_json({
            "stra2us_backup_version": 1,
            "dump_kind": "delta",  # not supported
            "app": None,
            "exported_at": "",
            "data": {},
        })


def test_envelope_rejects_kv_missing_value():
    """Each kv entry needs at minimum a `value` field — without it the
    envelope is structurally meaningless."""
    with pytest.raises(BackupFormatError, match="missing 'value'"):
        BackupEnvelope.from_json({
            "stra2us_backup_version": 1,
            "dump_kind": "whole",
            "app": None,
            "exported_at": "",
            "data": {"kv": {"some/key": {"encrypted": False}}},
        })


def test_envelope_rejects_kv_invalid_base64():
    with pytest.raises(BackupFormatError, match="invalid base64"):
        BackupEnvelope.from_json({
            "stra2us_backup_version": 1,
            "dump_kind": "whole",
            "app": None,
            "exported_at": "",
            "data": {"kv": {"some/key": {"value": "!!!not-base64!!!"}}},
        })


def test_envelope_rejects_stream_entry_without_id():
    with pytest.raises(BackupFormatError, match="must have 'id'"):
        BackupEnvelope.from_json({
            "stra2us_backup_version": 1,
            "dump_kind": "whole",
            "app": None,
            "exported_at": "",
            "data": {"queues": {"t": [{"fields": {}}]}},
        })


# ----- per-app filter predicates -------------------------------------

@pytest.mark.parametrize("acl,app,expected", [
    # Device-on-app shape: prefix is `<app>/<thing>` → match.
    ({"permissions": [{"prefix": "critterchron/dev1", "access": "rw"}]}, "critterchron", True),
    # Exact-app prefix (rare but possible).
    ({"permissions": [{"prefix": "critterchron", "access": "rw"}]}, "critterchron", True),
    # Different app → no match.
    ({"permissions": [{"prefix": "petwatch/dev1", "access": "rw"}]}, "critterchron", False),
    # Wildcard explicitly does NOT count as a per-app match — wildcards
    # are instance-scoped (operator-level) rather than app-data.
    ({"permissions": [{"prefix": "*", "access": "rw"}]}, "critterchron", False),
    # Empty / missing permissions list.
    ({}, "critterchron", False),
    ({"permissions": []}, "critterchron", False),
    # Substring-but-not-prefix: `critter` is not a prefix of `critterchron`
    # at a slash boundary. App `critter` shouldn't snag `critterchron`.
    ({"permissions": [{"prefix": "critterchron/dev1", "access": "rw"}]}, "critter", False),
])
def test_client_matches_app(acl, app, expected):
    assert client_matches_app(acl, app) is expected


def test_admin_acl_matches_app_mirrors_client():
    """Same predicate shape — admin ACL filtering treats `prefix`
    entries identically to client ACL entries."""
    acl = {"permissions": [{"prefix": "critterchron/public", "access": "r"}]}
    assert admin_acl_matches_app(acl, "critterchron") is True
    assert admin_acl_matches_app(acl, "petwatch") is False


@pytest.mark.parametrize("key,app,expected", [
    # Direct app-namespace match.
    ("critterchron/dev1/wifi_ssid", "critterchron", True),
    # Catalog key for the app.
    ("_catalog/critterchron", "critterchron", True),
    # Asset under the catalog.
    ("_catalog/critterchron/_assets/logo.png", "critterchron", True),
    # Different app's catalog.
    ("_catalog/petwatch", "critterchron", False),
    # Different app's data.
    ("petwatch/dev1/wifi_ssid", "critterchron", False),
    # Don't snag `critter` from `critterchron`.
    ("critterchron/dev1/wifi_ssid", "critter", False),
])
def test_kv_key_belongs_to_app(key, app, expected):
    assert kv_key_belongs_to_app(key, app) is expected


@pytest.mark.parametrize("topic,app,expected", [
    ("critterchron/public/heartbeep", "critterchron", True),
    ("critterchron",                   "critterchron", True),
    ("petwatch/public/heartbeep",      "critterchron", False),
    # Don't snag `critter` from `critterchron`.
    ("critterchron/public/heartbeep", "critter", False),
])
def test_queue_topic_belongs_to_app(topic, app, expected):
    assert queue_topic_belongs_to_app(topic, app) is expected


# ----- exported_at helper --------------------------------------------

def test_iso_now_has_z_suffix():
    """Operator-readable timestamp convention: `2026-05-14T10:23:45Z`,
    not `2026-05-14T10:23:45+00:00`. The format docstring promises this."""
    ts = iso_now()
    assert ts.endswith("Z")
    # Sanity-check the rough shape (don't pin minutes/seconds).
    assert ts[:4].isdigit() and ts[4] == "-"
