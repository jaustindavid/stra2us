# Backup envelope format v1

Wire-format spec for the dump produced by `GET /api/admin/backup`
and consumed by `POST /api/admin/restore` (and their per-app
siblings `/backup/app/<app>` / `/restore/app/<app>`). Landed in
v1.8.0 Sprint 7.

This is a **quasi-public artifact**. Operators store dumps in
backup pipelines, version control, and disaster-recovery vaults
that will outlive any single release of the server. Stability of
the envelope schema (key names, encoding choices, field shapes)
matters more than internal tidiness.

## Schema

```jsonc
{
  "stra2us_backup_version": 1,
  "dump_kind": "whole" | "per-app",
  "app": "<name>" | null,         // populated only for per-app dumps
  "exported_at": "2026-05-14T10:23:45Z",
  "data": {
    "clients":       { "<id>":    { "secret": "...", "acl": { ... } } },
    "admin_acls":    { "<user>":  { ... } },
    "kv":            { "<key>":   { "value": "<base64>", "encrypted": bool } },
    "queues":        { "<topic>": [ { "id": "1-0", "fields": { "<f>": "<base64>" } } ] },
    "activity_log":  [ { "id": "1-0", "fields": { "<f>": "<base64>" } } ] | null,
    "device_to_app": { "<id>":    "<app>" }
  }
}
```

* `stra2us_backup_version` — pinned at `1` for this schema. A
  restore endpoint refuses anything else (including missing). The
  version is the only field guaranteed stable across releases; bump
  only when a backward-incompatible change is intentional.
* `dump_kind` — `"whole"` for instance-wide dumps, `"per-app"` for
  app-scoped. The per-app shape carries the same sections but
  filtered.
* `app` — null for whole-instance, the app name (string) for per-app.
* `exported_at` — UTC ISO-8601 with `Z` suffix (not `+00:00`), to
  second precision. Convention only; not parsed by the restore path.

### Section: `clients`

Map of `client_id` → `{ "secret": <hex>, "acl": <dict> }`. Mirrors
the Redis pair `client:<id>:secret` + `client:<id>:acl`. Secret is
the raw hex string (64 chars); ACL is the parsed JSON dict, not a
string.

### Section: `admin_acls`

Map of `username` → `<acl-dict>`. Mirrors `admin_acls:<user>`. The
admin ACL doc structure matches the client ACL — a `permissions`
list of `{prefix, access}` entries.

### Section: `kv`

Map of `kv_key` → `{ "value": <base64-bytes>, "encrypted": bool }`.
The key is the path *inside* the `kv:` namespace (no `kv:` prefix).
Values are raw bytes — msgpack-encoded by client writers, asset
bytes for `_catalog/<app>/_assets/...`, etc. — base64-encoded for
JSON transport.

The `encrypted` flag folds in the `kv:<key>:enc` sidecar. Restore
re-establishes the sidecar from this flag; orphan sidecars (no
paired value) are ignored on dump.

Note: **catalogs are KV entries, not a separate section.** The
catalog YAML lives at `kv:_catalog/<app>` and per-app assets at
`kv:_catalog/<app>/_assets/<filename>` — they round-trip through
the `kv` section with no special handling.

### Section: `queues`

Map of `topic` → list of stream entries. The topic is the path
*inside* the `q:` namespace (no `q:` prefix). Each entry is
`{ "id": <stream-id>, "fields": { <field>: <base64-bytes> } }`,
ordered by `id`. Stream IDs are preserved on restore so cross-host
migration keeps timestamps + ordering intact.

### Section: `activity_log`

Either `null` (the dump was made with `?include_logs=0` — the
default) or a list of stream entries in the same shape as
`queues[<topic>]`. Whole-instance dumps include every entry; per-
app dumps filter to entries whose `client_id` field matches one of
the included clients.

### Section: `device_to_app`

Map of `client_id` → `app`. Mirrors `device_to_app:<id>`, the
reverse-index that `/api/app/lookup_device` queries. Restoring it
saves a "rebuild on first request" round-trip; restore-without-it
self-heals via lookup_device's defensive scan but at one-time cost.

## Restore semantics

### Skip-existing vs `force_overwrite`

Default is **skip-existing**, per-key for scalar sections and per-
stream for queue/activity-log sections. The response body lists
exactly what landed:

```json
{
  "clients":      { "restored": [...], "skipped": [...], "overwritten": [...] },
  "admin_acls":   { "restored": [...], "skipped": [...], "overwritten": [...] },
  "kv":           { "restored": [...], "skipped": [...], "overwritten": [...] },
  "queues":       { "restored": [...], "skipped": [...], "overwritten": [...] },
  "device_to_app":{ "restored": [...], "skipped": [...], "overwritten": [...] },
  "activity_log": { "restored": <int>, "skipped": <bool>, "overwritten": <bool> },
  "rejected_outside_app_filter": [...]
}
```

Pass `?force_overwrite=1` to replace existing values wholesale.

### Per-app sandbox

`POST /restore/app/<app>` takes the URL's `<app>` as the
authoritative filter. Any key falling outside `<app>/...` /
`_catalog/<app>` is rejected before any write, even if the
envelope's `app` field claims otherwise (defense in depth — an
envelope could be tampered or mis-built). Rejected keys appear in
`rejected_outside_app_filter`.

If the envelope's `app` field disagrees with the URL's `<app>`,
the restore is refused with HTTP 400 — that's an obvious operator
mistake worth flagging.

### Stream restore: per-stream, not per-entry

For queues + activity log, if the target stream exists and
`force_overwrite=False`, the entire stream is skipped. With
`force_overwrite=True` the existing stream is DELed and re-
populated using the original entry IDs. Per-entry merge is out of
scope for v1; operators wanting that should restore into a fresh
instance.

## Per-app filter rules

A *per-app dump* (`dump_kind: "per-app"`, `app: "<X>"`) includes:

* **Clients** whose ACL has any permission with prefix `<X>` exact
  or `<X>/...`.
* **KV** entries whose key starts with `<X>/` or `_catalog/<X>`
  (which pulls assets via `_catalog/<X>/_assets/...` for free).
* **Queue topics** starting with `<X>/`.
* **Admin ACL** rows that grant any access on `<X>/...`. Wildcard
  admins (`prefix: "*"`) are excluded — they're instance-scoped
  (operator identity), not app-data.
* **`device_to_app`** entries whose value is `<X>`.
* **Activity log** entries (when included) whose `client_id`
  matches one of the included clients.

## Encoding choices

* **Base64 for all binary.** KV values, queue payloads, and
  activity-log fields can be raw bytes (msgpack-encoded by clients,
  opaque to the server). Base64 keeps the envelope line-readable;
  the ~33% size premium is acceptable for a backup format.
* **JSON envelope, not msgpack.** Operators sometimes need to grep
  dumps. Line-readability beats binary compactness for this use
  case.
* **ACL JSON parsed, not stringified.** Stored in Redis as JSON
  strings; in the envelope they're decoded dicts so the dump is
  diff-friendly. Restore re-stringifies on write.

## Version policy

* `1` (current) — initial format, defined here.
* Adding optional sections is backward-compatible and stays on v1.
* Renaming or removing fields is incompat and requires a bump.
* On bump, ship a migration helper that converts old envelopes
  forward. Restore endpoints refuse anything they don't natively
  understand.

## What's NOT in the dump

Excluded by design (operational state, not data):

* `cursor:<consumer>:q:<topic>` — consumer position tracking;
  restore should start consuming "from now," not from a stale
  position the source host left behind.
* `kv:<key>:meta` sidecars (asset metadata) — wait, scratch that:
  asset meta IS regular KV (no `:enc`-style fold), so it IS in the
  dump. Anything matching `kv:*` lands in `kv` except orphan `:enc`
  sidecars.

## Security

Dumps contain HMAC client secrets, admin ACL grants, and at-rest
encrypted KV plaintext (via the wire-encryption inverse: anything
the server can read, the dump captures). Treat dumps with
password-manager sensitivity.

Server response headers:
* `X-Stra2us-Sensitive: true` — signal for downstream proxies +
  logging pipelines to scrub.
* `Cache-Control: no-store` — keep dumps out of intermediary
  caches.
* `Content-Disposition: attachment; filename=stra2us_backup_<scope>_<ts>.json`
  — friendly operator-readable filename.
