# Handoff: "Dump heartbeats" button in admin Active Clients table

**Release-notes one-liner:** Admin Active Clients table grows a
"Dump heartbeats" button per row — downloads the full 7-day
retention window of a device's heartbeat stream as JSONL for
offline debugging.

## What changed

### Server — `backend/src/api/routes_admin.py`

New endpoint `GET /api/admin/dump_heartbeats/{client_id}` and one
new module-level constant `_DUMP_HEARTBEATS_PAGE = 500`.

Handler flow:

1. `get_admin_context(request)` → authenticated admin ctx.
2. Look up the client's ACL; 404 if the client doesn't exist.
3. `_device_app_for_client(client_acl, client_id)` → resolves
   the client's app via the `<app>/<client_id>` ACL convention.
   400 with the documented detail message if absent.
4. `load_catalog_dict(app)` (from `routes_app_theme`) +
   `_resolve_telemetry_topic(app, client_id, catalog)` (from
   `routes_app`) → resolved heartbeat topic. Both reused exactly
   so the dump topic-matches the customer page's view.
5. `check_acl(ctx, f"q/{topic}", mode="read")` — the operative
   gate. A scoped admin who can't read that topic gets 403.
6. Stream a JSONL `StreamingResponse` with
   `Content-Disposition: attachment; filename="heartbeats-<cid>-<utc-stamp>.jsonl"`,
   `media_type="application/x-ndjson"`, `Cache-Control: no-store`.
   First line is always a `_meta` record
   (`topic`, `client_id`, `generated_at_iso`,
   `stream_max_age_days`); subsequent lines are matching entries
   newest-first.

Two new imports: `StreamingResponse` (FastAPI) + `datetime`/`timezone`.
Two new in-module imports: `_resolve_telemetry_topic` from
`api.routes_app` and `load_catalog_dict` from `api.routes_app_theme`.
`routes_app` doesn't import `routes_admin`, so no circularity.

Per-entry encoding follows the brief:

- `ts_ms` parsed from the stream id ms-prefix.
- `received_at_iso` derived from `ts_ms`, `Z`-suffixed.
- `data` is `msgpack.unpackb(..., raw=False)` or `None` on decode
  failure. **Note**: this diverges from `stream_monitor`'s
  fallback (which puts `raw_payload.hex()` *into* the `data`
  field). The dump always emits a separate `payload_hex`, so
  `data` stays semantically "decoded payload or null" — per the
  brief, intentional.
- `payload_hex` is `raw_payload.hex()` on every line.
- Per-entry `exp` filter is **not** applied — the stream's own
  7-day `EXPIRE` is the only retention boundary. Operators
  debugging stale devices want the stale entries.

Stream traversal walks **forward** via `XRANGE`, advancing the
`min` cursor with `(<last_id>` between pages (Redis 6.2+
exclusive-bound syntax, same idiom as `_xrevrange_filtered` but
opposite direction). Matches are collected into a list, then
emitted in reverse to satisfy the brief's "newest-first" output
requirement. No safety cap on max pages — the stream's 7-day
retention is the bound.

### Client — `backend/src/static/app.js`

Three localized changes:

- Active Clients row template (line ~268): inserted a third
  button `<button class="btn-sm" data-action="dumpHeartbeats"
  data-target="${id}">Dump heartbeats</button>` between Edit ACL
  and Revoke.
- New handler `async function dumpHeartbeats(id)` next to
  `revokeClient`. Calls the shared `_downloadDumpAt(url,
  fallback)` helper at line ~1769 — same helper
  `downloadBackup` / `downloadAppBackup` use — so non-2xx
  responses surface via the existing alert path, and
  `Content-Disposition` filenames are honored.
- `ACTIONS` dispatch table entry `dumpHeartbeats: (el) =>
  dumpHeartbeats(el.dataset.target)`.

No CSS changes, no modal, no toast/spinner. The brief explicitly
called this out as debug-only.

### Tests — `backend/tests/test_dump_heartbeats.py` (new)

Six cases, all green:

1. `test_happy_path_returns_meta_then_newest_first_matches` —
   interleaved publishers; verifies status, `Content-Disposition`,
   `_meta` shape, single-client filtering, newest-first order,
   and that `data` + `payload_hex` both populate.
2. `test_empty_match_set_yields_only_meta_line` — entries exist
   from other clients only; response is `_meta` + zero data lines.
3. `test_no_app_affinity_returns_400` — client with a
   non-device-shaped ACL; 400 with the documented detail.
4. `test_acl_deny_on_resolved_topic_returns_403` — admin scoped to
   `someother_app:rw` calling the endpoint for a client whose
   resolved topic is under `critterchron/...`; 403.
5. `test_bad_msgpack_payload_data_null_hex_preserved` — corrupt
   bytes in the stream; `data: null`, `payload_hex` matches input.
6. `test_paging_boundary_650_entries_all_present_newest_first` —
   650 entries from one client (page size 500). Pins the
   `XRANGE` paging loop; without correct cursor advancement a
   single-page implementation would silently truncate.

Test idiom mirrors `test_stream_monitor.py` and
`test_backup_restore.py`: call the handler as a plain async
function with a synthesized `Request` whose `state.admin_user`
is set; replace `get_redis_client` with a small in-memory fake
on `core.redis_client`, `api.dependencies`, `api.routes_admin`,
and `api.routes_app_theme` (the latter for the catalog read).

The fake's `xrange` honors `+`/`-` and the `(<id>` exclusive
lower-bound syntax — same shape as `_FakeStreamRedis.xrevrange`
in `test_stream_monitor.py`, but for the forward direction.

## What I deliberately did NOT change

- **`stream_monitor`** and `_xrevrange_filtered`. The brief's
  "What NOT to change" called these out; they're polling-shaped
  and `exp`-filtered, deliberately different from the dump.
- **Stream retention** (`EXPIRE q:<topic> 604800` in
  `routes_device.py`). Untouched.
- **Active Clients table layout** beyond the one new button.
  No column changes, no width tweaks, no per-row icon
  reshuffling.
- **Separate catalog-read ACL gate.** Per the brief, the only
  ACL gate is `check_acl(..., f"q/{topic}", "read")`. The
  catalog is consulted to *learn* which topic to gate on; the
  endpoint never returns catalog contents.
- **`_downloadDumpAt`** itself. Reused as-is — its existing
  `alert()` on non-2xx is the failure-path UX. No inline
  per-row error region exists on the Active Clients table, and
  the brief explicitly accepts an alert.
- **No "include expired" query param.** The brief picked "always
  include expired"; there's no toggle.

## Deviations from the brief

- **Forward-XRANGE + reverse-emit, not true streaming.** The
  brief is explicit on two points that pull in opposite
  directions: "Walks the stream via `await redis.xrange(...)` in
  pages... forward not backward" *and* "Newest-first order,
  matching `stream_monitor`'s convention." True streaming +
  newest-first would require XREVRANGE. I honored the literal
  XRANGE-forward instruction and collect matches into a list,
  then emit in reverse. Memory cost is bounded by the same 7-day
  retention that bounds the dump size overall (~10MB worst case
  per the brief's own sizing); the `StreamingResponse` wrapper
  still gives the client early headers and incremental bytes
  during emission, just not during the redis traversal. If the
  next person decides the brief's "newest-first" intent
  outweighs its "forward" intent, swap the loop to `xrevrange`
  with `cursor = "("+last_id"` as an exclusive *upper* bound and
  drop the `reversed(...)` — straightforward, and the existing
  tests would still pass.
- **404 on missing client.** The brief doesn't enumerate
  "client_id doesn't exist" as a distinct case; my handler
  raises 404 when the client has no ACL row, since
  `_device_app_for_client` on a missing ACL would otherwise
  cascade to the 400 "no app affinity" path with a misleading
  detail. Surfaced as 404 is closer to the caller's mental
  model. Doesn't conflict with any test in the brief.

## Test results

- `backend/venv/bin/pytest backend/tests/test_dump_heartbeats.py -v`
  → **6 passed**.
- `backend/venv/bin/pytest backend/tests/`
  → **389 passed** (was 383 before; +6 new tests, no
  regressions).
- `tools/stage smoke` — **not run from this environment;
  docker is not available on this host.** Must be verified on
  staging before merge/deploy, per the Rules of Operation.
  Expected to be a no-op at the smoke level: this change only
  adds one admin endpoint + one admin UI button, neither of
  which is on a smoke-covered path. The new behavior is
  exercised by the unit test and the manual checks below.

## Manual checks for staging deploy

After `tools/stage deploy`:

- Open `/admin`, find a real client in the Active Clients table
  (e.g. one of the raccoons), click "Dump heartbeats." Browser
  should prompt to save
  `heartbeats-<client_id>-<YYYYMMDDTHHMMSSZ>.jsonl`.
- Open the file. First line should be a `_meta` object with
  `topic`, `client_id`, `generated_at_iso`,
  `stream_max_age_days: 7`. Subsequent lines, one JSON object
  each, should all have `client_id == "<that-client>"` and be
  newest-first by `ts_ms`. Both `data` and `payload_hex` set on
  every line.
- Try a client with no app affinity (the smoke user, or any
  legacy client without an `<app>/<cid>` ACL prefix). Should see
  the alert "Dump failed: HTTP 400. See server logs." — no
  download.
- Try as a scoped admin who doesn't cover the client's topic.
  Should see "Dump failed: HTTP 403. See server logs."
- Confirm the admin monitor tab on `/admin` still works (sanity
  — wasn't touched).

## Sharp edges / follow-ups

- **`(<id>` exclusive bound requires Redis ≥ 6.2.** Same Redis
  floor as `_xrevrange_filtered`; production is well above.
  Flagged here only because the dump is a *second* dependency on
  that syntax.
- **No safety cap on max pages.** Per the brief, the stream's
  7-day retention is the bound. Worst-case scan is the full
  retained stream (~tens of thousands of entries) — fine, but
  worth knowing if anyone ever loosens the retention TTL.
- **`data: null` vs `payload_hex` for forensic recovery.** This
  endpoint deliberately splits decoded `data` from raw
  `payload_hex`; `stream_monitor` doesn't. If you find yourself
  writing a third reader, consider unifying — but be deliberate:
  the dump's "raw on every line" is for forensic use, not for
  rendering in a UI.
- **Frontend failure UX is an `alert()`.** Acceptable per brief,
  but the moment Active Clients grows an inline status region,
  point `dumpHeartbeats` at it instead of the alert.
- **Possible follow-up:** range-bounded dump (`?since=<ms>` /
  `?until=<ms>` query params) to grab just the relevant window
  for a specific incident. Out of scope for v1; would chop the
  worst-case payload size for chatty publishers.
- **Possible follow-up:** server-side gzip of the JSONL body
  (`Content-Encoding: gzip`). The brief's ~10MB sizing is
  highly compressible; this is a free 5-10× wire-size win if
  the operator ever pulls dumps remotely. Skipped for now —
  `_downloadDumpAt` doesn't request `Accept-Encoding`
  explicitly, but the browser does, so this would Just Work if
  the server-side flip ever lands.
