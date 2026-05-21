# Dispatch: "Dump heartbeats" button in admin Active Clients table

_Brief for a fresh agent. Self-contained — you do not need access
to the originating conversation. Read this, scan the linked
files, ask only if something here is contradictory; otherwise
build._

## What you're building

A debug affordance for the operator. The `/admin` page has an
"Active Clients" table listing every registered HMAC client with
"Edit ACL" / "Revoke" buttons per row. Add a third button per row
— **"Dump heartbeats"** — that downloads a JSONL file containing
every entry the device has in its heartbeat stream within the
stream's 7-day retention window.

Today this is a one-off `docker exec stra2us-iot python3 -c '...'`
incantation that reads the stream, filters by `client_id`, and
prints raw records. The button automates that exact thing so the
operator can grab a dump in two clicks instead of crafting a
Python one-liner each time.

## Why this matters

Debugging a misbehaving device usually means correlating its
recent heartbeat publishes against firmware logs or operator
observations. The `stream_monitor` endpoint that powers the admin
monitor tab is polling-shaped (newest few, exp-filtered, capped) —
useful for *current* state, useless for forensic *history*. This
endpoint fills the gap: full history, full fidelity, easy to
grep/jq downstream.

It is **explicitly a debugging tool, not a customer-facing
feature** — exposed only in the admin UI behind the existing
admin auth + topic-ACL gate.

## Design decisions (already settled)

These are not open questions; they have answers:

1. **Topic scope: catalog-declared heartbeat topic only.** Resolve
   the topic the same way the customer app page does — via
   `_resolve_telemetry_topic` in `backend/src/api/routes_app.py`
   (catalog's `telemetry_topic` with `{app}` / `{device}`
   substitution, falling back to `{app}/public/heartbeep`). Dump
   only that one stream, filtered to this client_id. Multi-topic
   dumps are out of scope for v1.

2. **Output format: JSONL.** One JSON object per line:

   ```jsonl
   {"ts_ms": 1779200762778, "received_at_iso": "2026-05-17T03:46:02Z", "client_id": "rachel_raccoon", "exp": 1779204362, "data": <decoded>, "payload_hex": "da..."}
   ```

   Both `data` (msgpack-decoded) and `payload_hex` (the raw bytes
   as hex) — decoded for ergonomics, raw for forensic completeness
   when the payload won't round-trip cleanly through msgpack
   decode (corrupted payload, future schema, etc.). Newest-first
   order, matching `stream_monitor`'s convention.

3. **Include expired entries.** Use `XRANGE`-style scanning that
   does NOT filter by the per-message `exp` field. The stream's
   own 7-day `EXPIRE` is the only retention boundary. This is the
   `XRANGE` semantics from the existing debug script — operators
   debugging a stale device want the stale entries.

## Backend

### New endpoint

`GET /api/admin/dump_heartbeats/{client_id}`

Path param: `client_id` (the HMAC client whose heartbeats to
dump).

Returns: `application/x-ndjson` streaming response, with
`Content-Disposition: attachment; filename="heartbeats-<client_id>-<utc-stamp>.jsonl"`
where `<utc-stamp>` is `YYYYMMDDTHHMMSSZ`.

### Auth

`require_admin_queue("read")` doesn't fit cleanly because the
topic is *derived* in the handler, not passed in. Use the same
pattern but explicit inside the handler:

```python
ctx = await get_admin_context(request)
# ... resolve topic ...
await check_acl(ctx, f"q/{topic}", mode="read")
```

This gates the dump behind the same per-topic ACL the rest of
the queue-read paths use. An operator scoped to `critterchron:rw`
can dump heartbeats from any client whose resolved topic falls
under `critterchron/...`; a wildcard admin can dump from any
client.

### Resolution + edge cases

- **Resolve the client's app**: use the existing
  `_device_app_for_client` helper in `routes_admin.py` (it reads
  the client's ACL and looks for an `<app>/<client_id>` prefix).
- **If `_device_app_for_client` returns None**: 400 with
  `{"detail": "Client has no app affinity; cannot resolve a
  heartbeat topic. Use the redis-cli directly for clients without
  a catalog."}`. Don't fall back to a guess.
- **Resolve the topic**: use `load_catalog_dict(app)` from
  `backend/src/api/routes_app_theme.py` (already imported into
  `routes_app.py` at line 33; this is the canonical loader,
  handles missing catalog and YAML parse failures gracefully —
  returning `{}` or `None` as appropriate). Then call
  `_resolve_telemetry_topic(app, client_id, catalog)` from
  `routes_app.py:121` to apply the `{app}` / `{device}`
  substitution and default. **Do not** re-implement either of
  those — that would create two sources of truth for "what topic
  does this device publish to" and they would drift.
- **Catalog-read ACL**: do NOT add a separate `check_acl` on
  `kv/_catalog/<app>` inside this endpoint. The operative gate
  is the `q/<topic>` ACL check; the catalog is consulted purely
  to resolve which topic to gate on, and surfaces no data to
  the operator. (Other admin endpoints that *return* catalog
  contents do gate the catalog read separately; this one
  doesn't.)
- **Leading `_meta` line on every dump (not optional)**: the
  first JSONL line of every response is a metadata record:
  `{"_meta": {"topic": "<resolved-topic>", "client_id":
  "<client_id>", "generated_at_iso": "<utc-iso>", "stream_max_age_days": 7}}`.
  An operator who opens an empty-looking file or a
  one-line file knows immediately what they asked for and that
  the call succeeded. The downstream tooling guidance is "lines
  whose first key is `_meta` are metadata, all others are
  entries" — trivial to grep around.
- **Empty match set**: the `_meta` line still emits; zero data
  lines follow. A file with only the `_meta` line means "the
  endpoint succeeded; no entries from this client_id in
  q:<topic>'s retention window."

### Streaming, not buffering

For a chatty device 7 days of heartbeats at 30s intervals is
~20k entries; at ~500 bytes each that's a ~10MB JSONL file.
Not huge but big enough that buffering it in memory before
sending is wasteful and adds latency.

Use `fastapi.responses.StreamingResponse` with an `async`
generator that:

1. Walks the stream via `await redis.xrange(f"q:{topic}", "-",
   "+", count=N)` in pages (suggested N=500), advancing the
   `min` cursor with the `(<last_id>` exclusive form between
   pages — same idiom as `_xrevrange_filtered` in
   `routes_admin.py:1223`, but forward not backward.
2. For each page, filters to entries where the `client_id`
   field equals the path's client_id.
3. Yields one JSON line per match (terminated with `\n`).
4. Stops when a page comes back shorter than `count` (stream
   exhausted).

No safety-cap on max pages — the stream's own 7-day retention
caps the worst case. If a client really has 100k matching
entries the dump is large but bounded.

### JSON encoding

- `ts_ms`: parsed from the XRANGE entry ID — the stream ID is
  `<ms-since-epoch>-<seq>`; take the prefix before the `-`,
  cast to int. (Same idiom as `stream_monitor` at
  `routes_admin.py:1191-1193`, which derives `received_at` the
  same way — there's no separate timestamp field on the entry.)
- `data`: msgpack-decoded with `msgpack.unpackb(raw_payload,
  raw=False)`. On decode failure, set `data: null` and rely on
  `payload_hex` to carry the bytes. **Note**: this diverges
  from `stream_monitor`'s decode-failure behavior (which falls
  back to `raw_payload.hex()` in the `data` field itself, at
  `routes_admin.py:1207-1211`). The divergence is deliberate
  — this endpoint emits a separate `payload_hex` field on
  every line for forensic completeness, so the `data` field
  stays semantically "decoded payload or null." Don't unify
  with `stream_monitor`'s behavior; the brief explicitly wants
  the split.
- `payload_hex`: `raw_payload.hex()`. Emitted on every line,
  not just on decode failure.
- `received_at_iso`: `datetime.fromtimestamp(ts_ms / 1000,
  tz=timezone.utc).isoformat().replace("+00:00", "Z")`.
- `exp`: int, as stored.
- Default-string-key, default-encoder; no need for custom
  serialization beyond the above.

## Frontend

### Button placement

In [`backend/src/static/app.js:267-268`](../../backend/src/static/app.js)
the per-row action cell currently looks like:

```js
<button class="btn-sm" data-action="openAclModal" data-target="${id}">Edit ACL</button>
<button class="btn-sm danger" data-action="revokeClient" data-target="${id}">Revoke</button>
```

Add a third button between them:

```js
<button class="btn-sm" data-action="dumpHeartbeats" data-target="${id}">Dump heartbeats</button>
```

### Click handler

`dumpHeartbeats` action: reuse the existing
`_downloadDumpAt(url, fallbackName)` helper at
[`backend/src/static/app.js:1769`](../../backend/src/static/app.js)
(used by `downloadBackup` and `downloadPerAppBackup` at
1751-1762). It does the fetch + Blob + `<a download>`
ceremony and respects the server's `Content-Disposition`
filename. Call it with
`/api/admin/dump_heartbeats/<client_id>` and a fallback
filename like `heartbeats-<client_id>.jsonl`.

If the server returns 400 (no app affinity) or 403 (ACL
denied), the helper's existing error handling should surface
it. Match the failure-path UX of the existing dump buttons —
if there's an inline error region near the table, use it; if
not, an alert/toast is acceptable. Don't pop a white-page
browser error.

### No new modal, no progress bar, no styling beyond `.btn-sm`

This is a debug-only button. It's not customer-facing. Don't
add UX polish that wasn't asked for.

## What NOT to change

- **`stream_monitor`** (the existing polling endpoint at
  `/api/admin/stream/q/<topic>`). Its semantics — newest-first,
  capped, exp-filtered, optionally client_id-filtered via the
  paged `_xrevrange_filtered` helper — are deliberately
  different from this dump endpoint. Don't merge them.
- **`_xrevrange_filtered`** itself. Read it for the cursor-paging
  idiom, but don't reuse it directly — it walks backward and
  caps; the dump walks forward and doesn't cap (within the
  stream).
- **Stream retention** (`EXPIRE q:<topic> 604800` in
  `routes_device.py`).
- **The Active Clients table's existing layout** beyond inserting
  one button per row.

## Verification

### Smoke

```sh
tools/stage smoke
```

Must be green before and after. This change doesn't touch any
smoke-covered path; smoke is a hygiene check.

### Unit test (new)

`backend/tests/test_dump_heartbeats.py`:

1. **Happy path**: seed `q:critterchron/public/heartbeep` with
   interleaved entries from `chatty`, `rachel_raccoon`, and
   `other`. Call the endpoint for `rachel_raccoon`. Assert:
   - Status 200.
   - `Content-Disposition` header present with the expected
     filename shape.
   - Body parses as JSONL — each line valid JSON.
   - First line is the `_meta` record with the expected keys
     (`topic`, `client_id`, `generated_at_iso`,
     `stream_max_age_days`).
   - Every data line has `client_id == "rachel_raccoon"`.
   - Data lines are in newest-first order.
   - Both `data` (decoded) and `payload_hex` populated on
     every data line.
2. **Empty match set**: stream has entries but none from this
   client. Assert 200 with the `_meta` line present and zero
   data lines following.
3. **No app affinity**: client whose ACL has no `<app>/<cid>`
   prefix. Assert 400 with the documented detail message.
4. **ACL deny on the resolved topic**: a scoped admin (e.g.
   `someother_app:rw`) calls the endpoint for a client whose
   resolved topic is `critterchron/...`. Assert 403.
5. **Bad msgpack payload**: seed an entry with raw non-msgpack
   bytes. Assert the JSONL line has `data: null` and
   `payload_hex` matches.
6. **Paging boundary**: seed >600 matching entries (e.g. 650
   from `noisy_client`) so the inner XRANGE pagination has to
   advance the cursor at least twice. Call the endpoint for
   `noisy_client`. Assert all 650 entries are present, in
   newest-first order, with no duplicates and no gaps. This
   pins the paged-XRANGE loop — without it, a single-XRANGE
   implementation passes tests 1-5 while silently truncating
   real dumps at 500 entries.

Test pattern: call the handler as a plain async function (same
auth-bypass idiom as `test_stream_monitor.py` and
`test_backup_restore.py`).

### Manual

After `tools/stage deploy`:

- Open `/admin` on staging, find a real client in the table
  (e.g. one of the raccoons), click "Dump heartbeats."
  Browser should prompt to save a file named
  `heartbeats-rachel_raccoon-<stamp>.jsonl`.
- Open the file: should be one JSON object per line, every
  line's `client_id` matches the client, lines newest-first.
- Try a client with no app affinity (the smoke user, or a
  legacy client without an `<app>/<cid>` ACL prefix). Should
  see an inline error in the row, no download.
- Confirm the admin monitor tab still works (sanity — you
  shouldn't have touched it).

## Reading list (optional context)

- [`backend/src/api/routes_app.py`](../../backend/src/api/routes_app.py)
  — `_resolve_telemetry_topic` is the source of truth for how
  the customer page picks the heartbeat topic. The dump must
  resolve identically.
- [`backend/src/api/routes_admin.py`](../../backend/src/api/routes_admin.py)
  — `_device_app_for_client` (line ~171), `stream_monitor`
  (line ~1156), `_xrevrange_filtered` (line ~1223). Read for
  patterns, not to reuse directly.
- [`backend/src/api/routes_device.py`](../../backend/src/api/routes_device.py)
  — the write path; confirms the field shape (`payload`, `exp`,
  `client_id`) you're reading back.
- [`docs/fr_application_view.md`](../fr_application_view.md) —
  the heartbeat-topic convention (`telemetry_topic` field, default
  `{app}/public/heartbeep`).
- [`docs/dispatch/heartbeat-tail-sparse-publishers.md`](heartbeat-tail-sparse-publishers.md)
  — the most recent stream-related change. Read for context on
  why filter-then-limit matters; not directly relevant here but
  good background.

The Rules of Operation in [`README.md`](../../README.md) apply.

## Handoff doc — your last act

When done, leave a sibling file:

`docs/dispatch/admin-dump-heartbeats.handoff.md`

Cover (matching the shape of the prior dispatch handoff in this
directory):

- 1-line release-notes summary at the top.
- What you actually changed (files + brief summary).
- What you deliberately did NOT change and why (tangential
  things you found but kept out of scope).
- Any deviations from this brief and the reasoning.
- Test results (smoke output + new unit tests).
- Sharp edges and follow-up candidates the next person should
  know.

Keep it tight. Future cuttlefish will read it cold.
