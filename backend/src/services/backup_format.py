# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""v1.8.0 Sprint 7: backup/restore envelope format + serializers.

Wire format for the `GET /api/admin/backup[/app/<app>]` and
`POST /api/admin/restore[/app/<app>]` endpoints. Pure data-shape +
JSON (de)serialization — no Redis interaction here so the format
can be unit-tested without a server-side fixture, and so the
restore path can validate an envelope before touching state.

# Why a separate module

The format is a *quasi-public artifact*. Operators store dumps in
backup pipelines, version control, and disaster-recovery vaults
that will outlive any single release of the server. Stability of
the envelope schema (key names, encoding choices, field shapes)
matters more than internal tidiness. Concentrating it in one
module — versioned, documented, with round-trip tests pinning the
shape byte-for-byte — keeps the contract from drifting into a
hundred ad-hoc edits across `routes_admin.py`.

# Envelope schema (v1)

```jsonc
{
  "stra2us_backup_version": 1,
  "dump_kind": "whole" | "per-app",
  "app": "<name>" | null,         // populated only for per-app dumps
  "exported_at": "2026-05-14T10:23:45Z",
  "data": {
    "clients":      { "<id>":    {"secret": "...", "acl": {...}} },
    "admin_acls":   { "<user>":  {...} },
    "kv":           { "<key>":   {"value": "<base64>", "encrypted": bool} },
    "queues":       { "<topic>": [ {"id": "1-0", "fields": {"<f>": "<base64>"}} ] },
    "activity_log": [ {"id": "1-0", "fields": {"<f>": "<base64>"}} ] | null,
    "device_to_app": { "<id>": "<app>" }
  }
}
```

Design notes:

* **No separate `catalogs` section.** Catalogs live at `kv:_catalog/<app>`
  and assets at `kv:_catalog/<app>/_assets/...` — they're just KV keys.
  Folding into the `kv` section means one (de)serialize path and no
  special-casing on restore. The brief's "catalogs" section was
  speculative; this is the concrete reality.
* **Base64 for all binary.** KV values, queue payloads, and activity-
  log fields can be raw bytes (msgpack-encoded by clients, opaque to
  the server). Base64 keeps the envelope line-readable. The size
  premium (~33%) is acceptable for a backup format.
* **ACL JSON parsed, not stringified.** Stored in Redis as JSON
  strings; in the envelope they're decoded dicts so the dump is
  diff-friendly. Restore re-stringifies on write.
* **`:enc` sidecar folded into the KV entry.** Each `kv:<key>`
  shows up as one envelope entry with an `encrypted` boolean; the
  sidecar `kv:<key>:enc` is reconstructed on restore. Orphan
  sidecars (no paired value) are ignored on dump.
* **Consumer cursors excluded.** `cursor:<consumer>:q:<topic>` is
  operational state, not data — a restore should reset to "start
  consuming from now," not "resume where the dumping host left off."
* **Activity log opt-in.** Defaults to null; populated only when
  the operator requests `?include_logs=1`. Logs are typically the
  largest single contributor to dump size (~150k entries cap) and
  rarely load-bearing for restores.

# Per-app filtering rules

A *per-app dump* (`dump_kind: "per-app"`, `app: "<X>"`) includes:

* Clients whose ACL has any permission with prefix `<X>` exact or
  `<X>/...` (covers the device-on-app provisioning shape).
* KV entries whose key starts with `<X>/` or `_catalog/<X>` (which
  pulls assets via `_catalog/<X>/_assets/...` for free since they
  share the prefix).
* Queue topics starting with `<X>/`.
* Admin ACL rows that grant any access on `<X>/...` (so the per-app
  dump can be imported into another instance and still have admins
  who can manage it).
* `device_to_app` entries whose value is `<X>`.
* Activity-log entries (when included) whose `client_id` field
  matches one of the included clients.

Wildcard admins (`prefix: "*"`) are NOT included in per-app dumps —
they're instance-scoped operators, not per-app. Importer can add
them out-of-band on the destination.

# Version policy

Restore refuses envelopes with `stra2us_backup_version` ≠ 1.
Future revisions either bump this with a migration helper or add
optional sections; backward-incompatible changes bump the major.
The version is the only field guaranteed stable across releases.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import json
from typing import Any, Iterable


# Bumped only when the envelope schema changes incompatibly. Add-only
# changes (new optional sections) can stay on v1.
BACKUP_FORMAT_VERSION = 1


# Redis key prefixes we touch. Centralized here so the iterator
# helpers in the endpoint can use the same constants the
# (de)serializer uses to recognize them.
KEY_PREFIX_CLIENT_SECRET = "client:"            # client:<id>:secret
KEY_PREFIX_CLIENT_ACL_SUFFIX = ":acl"
KEY_PREFIX_CLIENT_SECRET_SUFFIX = ":secret"
KEY_PREFIX_ADMIN_ACL = "admin_acls:"            # admin_acls:<user>
KEY_PREFIX_KV = "kv:"                           # kv:<path>, kv:<path>:enc
KEY_SUFFIX_ENC = ":enc"
KEY_PREFIX_QUEUE = "q:"                         # q:<topic> (stream)
KEY_ACTIVITY_LOG = "system:activity_log"        # global stream
KEY_PREFIX_DEVICE_TO_APP = "device_to_app:"     # device_to_app:<id>
KEY_PREFIX_CURSOR = "cursor:"                   # excluded

# `_catalog/<app>` lives in the kv: namespace — full Redis key shape
# is `kv:_catalog/<app>`. Used by per-app filter logic.
CATALOG_PREFIX_IN_KV = "_catalog/"


# ---------- dataclasses (the in-memory envelope) -----------------


@dataclasses.dataclass
class ClientRecord:
    """One `client:<id>:*` Redis pair."""
    secret: str
    acl: dict      # parsed JSON


@dataclasses.dataclass
class KVRecord:
    """One `kv:<key>` value, with the encrypted-flag sidecar folded in."""
    value: bytes
    encrypted: bool = False


@dataclasses.dataclass
class StreamEntry:
    """One Redis-stream entry — opaque field bag, identified by stream-ID."""
    id: str
    fields: dict[str, bytes]


@dataclasses.dataclass
class BackupEnvelope:
    """In-memory representation of a dump. Helper methods build the
    JSON-safe dict for HTTP responses; `from_json` parses + validates
    a dump for restore."""
    dump_kind: str                              # "whole" | "per-app"
    app: str | None
    exported_at: str                            # iso8601 UTC
    clients: dict[str, ClientRecord] = dataclasses.field(default_factory=dict)
    admin_acls: dict[str, dict] = dataclasses.field(default_factory=dict)
    kv: dict[str, KVRecord] = dataclasses.field(default_factory=dict)
    queues: dict[str, list[StreamEntry]] = dataclasses.field(default_factory=dict)
    activity_log: list[StreamEntry] | None = None
    device_to_app: dict[str, str] = dataclasses.field(default_factory=dict)

    # ---- JSON-safe dict construction (response shape) ----

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-serializable dict for the HTTP response."""
        return {
            "stra2us_backup_version": BACKUP_FORMAT_VERSION,
            "dump_kind": self.dump_kind,
            "app": self.app,
            "exported_at": self.exported_at,
            "data": {
                "clients": {
                    cid: {"secret": rec.secret, "acl": rec.acl}
                    for cid, rec in self.clients.items()
                },
                "admin_acls": self.admin_acls,
                "kv": {
                    key: {
                        "value": _b64(rec.value),
                        "encrypted": rec.encrypted,
                    }
                    for key, rec in self.kv.items()
                },
                "queues": {
                    topic: [_stream_to_json(e) for e in entries]
                    for topic, entries in self.queues.items()
                },
                "activity_log": (
                    None if self.activity_log is None
                    else [_stream_to_json(e) for e in self.activity_log]
                ),
                "device_to_app": dict(self.device_to_app),
            },
        }

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> "BackupEnvelope":
        """Parse + validate a dump dict (e.g. just-decoded JSON body
        of a /restore POST). Raises `BackupFormatError` with a
        human-readable message on any structural problem — caller
        translates to a 400."""
        if not isinstance(doc, dict):
            raise BackupFormatError("envelope must be a JSON object")
        version = doc.get("stra2us_backup_version")
        if version != BACKUP_FORMAT_VERSION:
            raise BackupFormatError(
                f"unsupported envelope version {version!r}; "
                f"this server understands v{BACKUP_FORMAT_VERSION}"
            )
        kind = doc.get("dump_kind")
        if kind not in ("whole", "per-app"):
            raise BackupFormatError(
                f"dump_kind must be 'whole' or 'per-app' (got {kind!r})"
            )
        app = doc.get("app")
        if kind == "per-app" and not isinstance(app, str):
            raise BackupFormatError(
                "per-app dumps must name the app in 'app'"
            )
        data = doc.get("data")
        if not isinstance(data, dict):
            raise BackupFormatError("envelope 'data' must be an object")

        env = cls(
            dump_kind=kind,
            app=app,
            exported_at=doc.get("exported_at") or "",
        )

        for cid, raw in (data.get("clients") or {}).items():
            if not isinstance(raw, dict) or "secret" not in raw:
                raise BackupFormatError(f"client {cid!r} missing 'secret'")
            env.clients[cid] = ClientRecord(
                secret=str(raw["secret"]),
                acl=raw.get("acl") or {},
            )

        for user, acl in (data.get("admin_acls") or {}).items():
            if not isinstance(acl, dict):
                raise BackupFormatError(
                    f"admin_acls[{user!r}] must be an object"
                )
            env.admin_acls[user] = acl

        for key, raw in (data.get("kv") or {}).items():
            if not isinstance(raw, dict) or "value" not in raw:
                raise BackupFormatError(f"kv[{key!r}] missing 'value'")
            env.kv[key] = KVRecord(
                value=_b64decode(raw["value"], where=f"kv[{key!r}].value"),
                encrypted=bool(raw.get("encrypted", False)),
            )

        for topic, entries in (data.get("queues") or {}).items():
            if not isinstance(entries, list):
                raise BackupFormatError(
                    f"queues[{topic!r}] must be a list"
                )
            env.queues[topic] = [
                _stream_from_json(e, where=f"queues[{topic!r}]") for e in entries
            ]

        raw_log = data.get("activity_log")
        if raw_log is not None:
            if not isinstance(raw_log, list):
                raise BackupFormatError("activity_log must be a list or null")
            env.activity_log = [
                _stream_from_json(e, where="activity_log") for e in raw_log
            ]

        for k, v in (data.get("device_to_app") or {}).items():
            env.device_to_app[k] = str(v)

        return env


class BackupFormatError(ValueError):
    """Structural problem with an envelope. Caller maps to HTTP 400."""


# ---------- per-app filter predicates ----------------------------


def client_matches_app(acl: dict, app: str) -> bool:
    """True if any permission in `acl` grants access scoped to `app`.

    Matches the device-on-app ACL shape (`<app>` exact or `<app>/...`
    prefix). Wildcard permissions (`prefix: "*"`) are NOT considered
    a match — they're instance-scoped, not per-app.
    """
    for perm in (acl.get("permissions") or []):
        prefix = perm.get("prefix") or ""
        if prefix == app or prefix.startswith(f"{app}/"):
            return True
    return False


def kv_key_belongs_to_app(kv_key: str, app: str) -> bool:
    """`kv_key` is the path *inside* the `kv:` namespace (no `kv:` prefix).
    True for keys under `<app>/...` or the catalog tree `_catalog/<app>[/...]`."""
    if kv_key == app or kv_key.startswith(f"{app}/"):
        return True
    if kv_key == f"{CATALOG_PREFIX_IN_KV}{app}":
        return True
    if kv_key.startswith(f"{CATALOG_PREFIX_IN_KV}{app}/"):
        return True
    return False


def queue_topic_belongs_to_app(topic: str, app: str) -> bool:
    """`topic` is the path *inside* the `q:` namespace. True for
    `<app>/...` topics (covers `<app>/public/heartbeep` and the
    per-device-namespace shape alike)."""
    return topic == app or topic.startswith(f"{app}/")


def admin_acl_matches_app(acl: dict, app: str) -> bool:
    """Per-app admin filter — same shape as `client_matches_app` but
    we don't include wildcard admins (they belong to the destination
    instance's operator set, not to any particular app's data)."""
    return client_matches_app(acl, app)


# ---------- helpers ---------------------------------------------


def _b64(value: bytes | None) -> str:
    """Base64-encode raw bytes for JSON transport. None → empty string
    so the envelope round-trips through `json.dumps` without nulls in
    binary positions."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, *, where: str) -> bytes:
    if not isinstance(value, str):
        raise BackupFormatError(f"{where} must be a base64 string")
    try:
        return base64.b64decode(value, validate=False)
    except Exception as e:
        raise BackupFormatError(f"{where}: invalid base64 ({e})") from e


def _stream_to_json(entry: StreamEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "fields": {k: _b64(v) for k, v in entry.fields.items()},
    }


def _stream_from_json(raw: Any, *, where: str) -> StreamEntry:
    if not isinstance(raw, dict) or "id" not in raw or "fields" not in raw:
        raise BackupFormatError(
            f"{where}: stream entry must have 'id' and 'fields'"
        )
    fields_raw = raw["fields"]
    if not isinstance(fields_raw, dict):
        raise BackupFormatError(f"{where}: 'fields' must be an object")
    fields: dict[str, bytes] = {}
    for k, v in fields_raw.items():
        fields[str(k)] = _b64decode(v, where=f"{where}.fields[{k!r}]")
    return StreamEntry(id=str(raw["id"]), fields=fields)


def iso_now() -> str:
    """UTC iso8601 with second precision — `exported_at` timestamp.
    Z suffix not `+00:00` for readability + because it's a common
    backup-file convention."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
