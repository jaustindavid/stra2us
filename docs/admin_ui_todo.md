# Admin UI — worklist

Light polish items for the admin dashboard. Catalog-specific UI items
live in [catalog_todo.md](catalog_todo.md) instead.

## Active

- ~~**Differentiate KV hit vs miss in activity log status.**~~ Landed
  2026-05-04. `routes_device.py:read_kv` stashes
  `request.state.kv_hit` (true if the value existed, false if the
  handler returned the `not_found` body). The activity-log
  middleware in `main.py` reads it and emits `Hit (200)` /
  `Miss (200)` for KV GETs — falls back to `Success (200)` for KV
  writes and other endpoints. Surfaced from "I'd expect to see
  everyone polling critterchron/&lt;device&gt;/ir but not everyone
  is" — answer turned out to be "they probably are, but hits and
  misses looked identical." Now `grep 'Miss (200)' activity-log`
  finds devices polling for unset keys.

- **Activity Logs UI control / pagination.** The fetch limit is now a
  hardcoded 2000 entries (bumped from 200 on 2026-05-04 — was "kinda
  shallow"). Server-side stream retention is `MAXLEN ~ 150000` so
  there's plenty more to show; the UI just doesn't expose it. Worth
  adding either a dropdown (200/500/1000/2000/5000) or proper
  pagination ("load older") with cursor management. Existing endpoint
  already supports `min` filtering via Redis stream cursors. Lazy
  effort: ~20 LOC for dropdown, ~80 LOC for pagination.

- **Logout link in the admin UI nav.** No way to "switch users" today —
  basic-auth credentials are browser-cached, the `admin_session` cookie
  persists, and the only workaround is incognito-per-user or manually
  clearing site data via DevTools. Surfaces sharply when testing
  scoped-admin personas (Phase 0 of
  [fr_application_view.md](fr_application_view.md)) or operating
  multiple personas day-to-day.

  Implementation sketch: new route `GET /admin/logout` that deletes
  the `admin_session` cookie and serves a "logged out" landing page.
  Browsers handle basic-auth re-prompting inconsistently — a 401
  response can sometimes evict the cached credentials, but not
  reliably. Worst case: the page tells the user "close all your
  browser windows to fully log out," which is honest if ugly.

  Likely rolls up into the broader v1.5 customer-auth UX work (real
  login page, self-service password reset, etc.) called out as open
  question #3 in the application view FR — basic-auth's browser-
  managed credential cache is part of the same problem. File as
  standalone for now; may get absorbed.

## Closed

- **First-call lag on `/kv_scan` (~5s).** Filed 2026-04-23, fixed
  2026-04-25. Diagnosis via the new `system:perf_log` stream
  ([core/perf_log.py](../backend/src/core/perf_log.py)) showed every
  device endpoint (not just `/kv_scan`) was paying 100-300ms per
  request because [core/redis_client.py](../backend/src/core/redis_client.py)
  was creating a fresh `Redis()` instance per `get_redis_client()`
  call, throwing away the warm pool every time. Cached the client at
  module scope; cold/warm `/kv_scan` now indistinguishable (~3ms),
  30-parallel peek fan-out drops from `max(per-request)` of ~300ms to
  ~23ms total. Also dropped the redundant `xtrim` from the activity-log
  middleware — `MAXLEN ~ 150000` already bounds the stream.
- **`/api/admin/logs` 180ms baseline.** Surfaced 2026-04-25 via
  the perf-log stream after the singleton fix above didn't move the
  needle on this endpoint. Root cause: `fetch_count = limit * 10` in
  [routes_admin.py](../backend/src/api/routes_admin.py) over-fetched
  for the scoped-admin case, but for wildcard admins (whose ACL
  filter passes everything) that meant deserializing 1800 stream
  entries in Python per page request and discarding them. Fixed by
  detecting wildcard ACL and skipping the multiplier; route went
  from ~177ms → ~24ms. Phase breakdown (`xrevrange` vs `filter_loop`)
  added to the perf log so this kind of regression is visible next time.

## Final post-investigation latency snapshot (2026-04-25)

After the three fixes above, traced with `STRA2US_PERF_LOG_THRESHOLD_MS=1`
on real traffic:

| Endpoint | Before | After | Notes |
|---|---|---|---|
| Device `/kv/*` reads | 200-300ms | 2-3ms | singleton fix, ~100x |
| `/api/admin/logs` | 180ms | 22-29ms | wildcard fix; XREVRANGE on a 150K-entry stream is ~18ms of that, intrinsic |
| `/api/admin/peek/kv/...` | unknown | 2-3ms | redis_get=0.25ms, rest is framework |
| `openDeviceDetail` 28-peek fan-out | 200ms each, sequential | all ≤19ms, mostly 5-10ms | singleton + warm pool |
| `/api/admin/kv_scan` | original "5s cold" complaint | 2-4ms | singleton fix |

Threshold restored to default 100ms after capture; `system:perf_log` now
back to outlier-only mode and serves as ongoing performance telemetry.

## See also

- [catalog_todo.md](catalog_todo.md) parking lot — Catalog → Devices
  tab stale after edit, Raw tab column widths for long device IDs.
