# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""v1.8.0 Sprint 7: Redis-side serialization for the backup/restore envelope.

Bridges the pure-data `backup_format.py` envelope to the actual
Redis state. Two halves:

* `collect_*_envelope(...)` — read Redis, produce a populated
  `BackupEnvelope` ready for `to_json()`.
* `apply_envelope(...)` — write the contents of an envelope back to
  Redis, honoring skip-existing vs `force_overwrite` semantics.

Kept separate from `backup_format.py` so the envelope schema can be
unit-tested without a fake Redis, and kept separate from
`routes_admin.py` so the I/O code isn't tangled with HTTP plumbing.
The routes file calls these helpers and wraps their output in JSON
responses.

# Skip-existing semantics

For *scalar* keys (clients, admin_acls, kv, device_to_app), skip-
existing checks the key's presence and either skips or overwrites
on a per-key basis — partial-overlap restores naturally produce a
mix of "restored" + "skipped".

For *streams* (queues, activity_log), it's per-stream not per-entry:
if the target stream exists at all and `force_overwrite=False`, the
whole stream is skipped. With `force_overwrite=True` the existing
stream is DELed and re-populated entry-by-entry, preserving the
original stream IDs (so timestamps + relative ordering survive a
cross-host migration). Per-entry conflict resolution mid-stream is
out of scope for v1 — operators wanting that should restore into a
fresh instance.

# Per-app restore is sandboxed

A per-app restore endpoint passes its app name to `apply_envelope`
as `app_filter`. Keys that fall outside `<app>/...` / `_catalog/<app>`
get rejected before any write — defense in depth against an envelope
that claims to be per-app but actually contains cross-app data. The
result dict surfaces these as `rejected_outside_app_filter` so the
operator can see what the import refused to touch.
"""

from __future__ import annotations

import json
from typing import Any

from .backup_format import (
    BackupEnvelope,
    ClientRecord,
    KEY_ACTIVITY_LOG,
    KEY_PREFIX_ADMIN_ACL,
    KEY_PREFIX_CLIENT_SECRET,
    KEY_PREFIX_DEVICE_TO_APP,
    KEY_PREFIX_KV,
    KEY_PREFIX_QUEUE,
    KEY_SUFFIX_ENC,
    KVRecord,
    StreamEntry,
    admin_acl_matches_app,
    client_matches_app,
    iso_now,
    kv_key_belongs_to_app,
    queue_topic_belongs_to_app,
)


# ---------- helpers ----------------------------------------------


def _s(v: Any) -> str:
    """bytes-or-str → str. Redis client returns bytes by default."""
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return v


def _b(v: Any) -> bytes:
    """bytes-or-str → bytes (for raw KV / stream field values)."""
    if isinstance(v, str):
        return v.encode("utf-8")
    return v


async def _scan_keys(redis, pattern: str) -> list[str]:
    """`KEYS` wrapper that normalizes bytes → str. `KEYS` is O(N) but
    fine for backup paths (rare, manual, operator-driven)."""
    raw = await redis.keys(pattern)
    return [_s(k) for k in raw]


# ---------- COLLECT (Redis → envelope) ---------------------------


async def collect_whole_envelope(
    redis,
    *,
    include_logs: bool,
) -> BackupEnvelope:
    """Read every load-bearing key out of Redis and pack it into an
    envelope ready for `to_json()`. Activity log is opt-in because
    it's typically the largest single contributor (~150k entries cap)
    and rarely useful on the destination side."""
    env = BackupEnvelope(
        dump_kind="whole",
        app=None,
        exported_at=iso_now(),
    )
    await _collect_clients(redis, env, app=None)
    await _collect_admin_acls(redis, env, app=None)
    await _collect_kv(redis, env, app=None)
    await _collect_queues(redis, env, app=None)
    await _collect_device_to_app(redis, env, app=None)
    if include_logs:
        env.activity_log = await _collect_stream(redis, KEY_ACTIVITY_LOG)
    return env


async def collect_per_app_envelope(
    redis,
    app: str,
    *,
    include_logs: bool,
) -> BackupEnvelope:
    """Same as `collect_whole_envelope` but every section is filtered
    to entries that belong to `app` (see `backup_format.py` filter
    predicates for the rules)."""
    env = BackupEnvelope(
        dump_kind="per-app",
        app=app,
        exported_at=iso_now(),
    )
    await _collect_clients(redis, env, app=app)
    await _collect_admin_acls(redis, env, app=app)
    await _collect_kv(redis, env, app=app)
    await _collect_queues(redis, env, app=app)
    await _collect_device_to_app(redis, env, app=app)
    if include_logs:
        # Activity log: filter to entries whose client_id matches one
        # of the per-app clients we just collected. Done last so the
        # client set is known.
        included_clients = set(env.clients.keys())
        full = await _collect_stream(redis, KEY_ACTIVITY_LOG)
        env.activity_log = [
            e for e in full
            if _s(e.fields.get("client_id", b"")) in included_clients
        ]
    return env


async def _collect_clients(redis, env: BackupEnvelope, *, app: str | None) -> None:
    secret_keys = await _scan_keys(redis, f"{KEY_PREFIX_CLIENT_SECRET}*:secret")
    for k in secret_keys:
        # Shape: client:<id>:secret. Slice to extract <id>.
        client_id = k[len(KEY_PREFIX_CLIENT_SECRET):-len(":secret")]
        secret = _s(await redis.get(k))
        acl_raw = await redis.get(f"{KEY_PREFIX_CLIENT_SECRET}{client_id}:acl")
        acl = json.loads(_s(acl_raw)) if acl_raw else {}
        if app is not None and not client_matches_app(acl, app):
            continue
        env.clients[client_id] = ClientRecord(secret=secret, acl=acl)


async def _collect_admin_acls(redis, env: BackupEnvelope, *, app: str | None) -> None:
    keys = await _scan_keys(redis, f"{KEY_PREFIX_ADMIN_ACL}*")
    for k in keys:
        user = k[len(KEY_PREFIX_ADMIN_ACL):]
        raw = await redis.get(k)
        if not raw:
            continue
        try:
            acl = json.loads(_s(raw))
        except json.JSONDecodeError:
            continue   # corrupt row; skip silently rather than crash dump
        if app is not None and not admin_acl_matches_app(acl, app):
            continue
        env.admin_acls[user] = acl


async def _collect_kv(redis, env: BackupEnvelope, *, app: str | None) -> None:
    kv_keys = await _scan_keys(redis, f"{KEY_PREFIX_KV}*")
    # First pass: collect values, defer the `:enc` sidecar pairing.
    values: dict[str, bytes] = {}
    enc_flags: set[str] = set()
    for full_key in kv_keys:
        # full_key = "kv:<rest>". The `rest` may itself end in `:enc`.
        rest = full_key[len(KEY_PREFIX_KV):]
        if rest.endswith(KEY_SUFFIX_ENC):
            # `kv:<key>:enc` is the encrypted-flag sidecar.
            base = rest[:-len(KEY_SUFFIX_ENC)]
            enc_flags.add(base)
            continue
        if app is not None and not kv_key_belongs_to_app(rest, app):
            continue
        raw = await redis.get(full_key)
        if raw is None:
            continue
        values[rest] = _b(raw)
    for key, value in values.items():
        env.kv[key] = KVRecord(value=value, encrypted=(key in enc_flags))


async def _collect_queues(redis, env: BackupEnvelope, *, app: str | None) -> None:
    q_keys = await _scan_keys(redis, f"{KEY_PREFIX_QUEUE}*")
    for full_key in q_keys:
        topic = full_key[len(KEY_PREFIX_QUEUE):]
        if app is not None and not queue_topic_belongs_to_app(topic, app):
            continue
        env.queues[topic] = await _collect_stream(redis, full_key)


async def _collect_device_to_app(redis, env: BackupEnvelope, *, app: str | None) -> None:
    keys = await _scan_keys(redis, f"{KEY_PREFIX_DEVICE_TO_APP}*")
    for k in keys:
        cid = k[len(KEY_PREFIX_DEVICE_TO_APP):]
        val = _s(await redis.get(k))
        if app is not None and val != app:
            continue
        env.device_to_app[cid] = val


async def _collect_stream(redis, key: str) -> list[StreamEntry]:
    """XRANGE the whole stream. For backup purposes we want every
    entry, so no min/max bounds, no count cap. Operators with
    pathologically large streams should opt out of activity_log."""
    raw = await redis.xrange(key, min="-", max="+")
    out: list[StreamEntry] = []
    for entry_id, fields in raw:
        # `fields` is a dict[bytes, bytes]; normalize keys to str,
        # leave values as bytes (they may legitimately be binary).
        norm = {_s(k): _b(v) for k, v in fields.items()}
        out.append(StreamEntry(id=_s(entry_id), fields=norm))
    return out


# ---------- APPLY (envelope → Redis) -----------------------------


def _new_result() -> dict[str, Any]:
    return {
        "clients":        {"restored": [], "skipped": [], "overwritten": []},
        "admin_acls":     {"restored": [], "skipped": [], "overwritten": []},
        "kv":             {"restored": [], "skipped": [], "overwritten": []},
        "queues":         {"restored": [], "skipped": [], "overwritten": []},
        "device_to_app":  {"restored": [], "skipped": [], "overwritten": []},
        "activity_log":   {"restored": 0, "skipped": False, "overwritten": False},
        "rejected_outside_app_filter": [],
    }


async def apply_envelope(
    redis,
    env: BackupEnvelope,
    *,
    force_overwrite: bool,
    app_filter: str | None = None,
) -> dict[str, Any]:
    """Write the envelope's contents back to Redis. `app_filter`, when
    set, refuses any entry that doesn't belong to that app — defense
    in depth for per-app restores. Returns a structured per-section
    result for the HTTP response body."""
    result = _new_result()

    # ----- clients -----
    for cid, rec in env.clients.items():
        if app_filter and not client_matches_app(rec.acl, app_filter):
            result["rejected_outside_app_filter"].append(f"client:{cid}")
            continue
        existing = await redis.get(f"{KEY_PREFIX_CLIENT_SECRET}{cid}:secret")
        if existing and not force_overwrite:
            result["clients"]["skipped"].append(cid)
            continue
        await redis.set(f"{KEY_PREFIX_CLIENT_SECRET}{cid}:secret", rec.secret)
        await redis.set(f"{KEY_PREFIX_CLIENT_SECRET}{cid}:acl", json.dumps(rec.acl))
        (result["clients"]["overwritten" if existing else "restored"]).append(cid)

    # ----- admin_acls -----
    for user, acl in env.admin_acls.items():
        if app_filter and not admin_acl_matches_app(acl, app_filter):
            result["rejected_outside_app_filter"].append(f"admin_acls:{user}")
            continue
        existing = await redis.get(f"{KEY_PREFIX_ADMIN_ACL}{user}")
        if existing and not force_overwrite:
            result["admin_acls"]["skipped"].append(user)
            continue
        await redis.set(f"{KEY_PREFIX_ADMIN_ACL}{user}", json.dumps(acl))
        (result["admin_acls"]["overwritten" if existing else "restored"]).append(user)

    # ----- kv (+ :enc sidecar) -----
    for key, rec in env.kv.items():
        if app_filter and not kv_key_belongs_to_app(key, app_filter):
            result["rejected_outside_app_filter"].append(f"kv:{key}")
            continue
        existing = await redis.exists(f"{KEY_PREFIX_KV}{key}")
        if existing and not force_overwrite:
            result["kv"]["skipped"].append(key)
            continue
        await redis.set(f"{KEY_PREFIX_KV}{key}", rec.value)
        if rec.encrypted:
            await redis.set(f"{KEY_PREFIX_KV}{key}{KEY_SUFFIX_ENC}", b"1")
        else:
            # Clear any stale sidecar — restoring a now-plaintext
            # value with the old encrypted flag would mis-decrypt.
            await redis.delete(f"{KEY_PREFIX_KV}{key}{KEY_SUFFIX_ENC}")
        (result["kv"]["overwritten" if existing else "restored"]).append(key)

    # ----- queues (per-stream, not per-entry) -----
    for topic, entries in env.queues.items():
        if app_filter and not queue_topic_belongs_to_app(topic, app_filter):
            result["rejected_outside_app_filter"].append(f"q:{topic}")
            continue
        stream_key = f"{KEY_PREFIX_QUEUE}{topic}"
        existed = await redis.exists(stream_key)
        if existed and not force_overwrite:
            result["queues"]["skipped"].append(topic)
            continue
        if existed:
            await redis.delete(stream_key)
        for e in entries:
            # XADD with the original ID preserves cross-host timestamps.
            # Fields are bytes; the redis lib accepts a dict and serializes.
            await redis.xadd(stream_key, e.fields, id=e.id)
        (result["queues"]["overwritten" if existed else "restored"]).append(topic)

    # ----- device_to_app -----
    for cid, app_val in env.device_to_app.items():
        if app_filter and app_val != app_filter:
            result["rejected_outside_app_filter"].append(f"device_to_app:{cid}")
            continue
        existing = await redis.get(f"{KEY_PREFIX_DEVICE_TO_APP}{cid}")
        if existing and not force_overwrite:
            result["device_to_app"]["skipped"].append(cid)
            continue
        await redis.set(f"{KEY_PREFIX_DEVICE_TO_APP}{cid}", app_val)
        (result["device_to_app"]["overwritten" if existing else "restored"]).append(cid)

    # ----- activity_log -----
    if env.activity_log is not None:
        existed = await redis.exists(KEY_ACTIVITY_LOG)
        if existed and not force_overwrite:
            result["activity_log"]["skipped"] = True
        else:
            if existed:
                await redis.delete(KEY_ACTIVITY_LOG)
                result["activity_log"]["overwritten"] = True
            for e in env.activity_log:
                await redis.xadd(KEY_ACTIVITY_LOG, e.fields, id=e.id)
            result["activity_log"]["restored"] = len(env.activity_log)

    return result
