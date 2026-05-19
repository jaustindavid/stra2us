# Handoff: heartbeat tail for sparse publishers

**Release-notes one-liner:** Fixed the customer app page's activity
tail showing "No recent telemetry" for slow publishers on shared
topics; the server now pages through history to find the last few
matches instead of filtering after a fixed-size window.

## What changed

### Server — `backend/src/api/routes_admin.py`

`stream_monitor` (the `GET /api/admin/stream/q/<topic>` handler at
line ~1146) split into two paths:

- **Unfiltered** (no `client_id`): unchanged single XREVRANGE pass.
  Admin monitor tab's default polling preserved exactly.
- **Filtered** (one or more `client_id` values): paged backward
  scan in a new helper `_xrevrange_filtered`. Reads in batches of
  `_STREAM_FILTER_BATCH = 100` using an exclusive `(<id>` upper
  bound after each page, accumulates matches, stops when it has
  `limit` matches, when the page comes back shorter than the
  batch (stream exhausted), or when it has done
  `_STREAM_FILTER_MAX_BATCHES = 10` batches (safety cap for
  "client never wrote here" — bounds the worst case to ~1000
  scanned entries).

Multi-value `client_id` works as before: any entry whose
`client_id` field is in the set counts as a match. Per-entry
logic (`exp` filter, msgpack decode, payload shape) is unchanged.

### Client — `backend/src/static/app/app.js`

- `refreshTelemetry`: `limit=10` → `limit=3`. The tail now fits in
  the new "last 3 messages" framing without scrolling.
- `renderActivityList`: when the newest returned message is older
  than `heartbeatSeconds × ONLINE_MULTIPLIER` (the same threshold
  that flips the status badge out of "Online"), prepends a styled
  `<div class="activity-stale-blurb">` with copy "No recent
  heartbeats — showing the last N messages." The blurb is
  suppressed when the tail is empty (the existing "No recent
  telemetry from this device." empty-state still wins) and when
  the newest message is fresh.
- Reused `ONLINE_MULTIPLIER` rather than introducing a new
  constant, so the blurb threshold and the badge transition stay
  coherent.

### Client CSS — `backend/src/static/app/styles.css`

Added the `.app-device .activity-stale-blurb` rule next to the
existing `.activity-empty` styling: italic muted text with a
left-border accent and a faint tinted background so it reads as
meta-info, not a regular row.

### Test — `backend/tests/test_stream_monitor.py` (new)

Direct unit test against `stream_monitor` using a small async
`_FakeStreamRedis` that honors `xrevrange(max, min, count)` and
the `(<id>` exclusive bound syntax. Four cases:

1. `quiet_client` (3 of 53 entries, at indices 5/25/45) with
   `limit=3` returns all 3 — the bug case.
2. `chatty_client` with `limit=3` returns the three most recent
   chatty entries, newest first.
3. No `client_id` filter with `limit=3` returns 3 entries
   regardless of identity (regression guard on the unfiltered
   path).
4. `client_id=nobody_here` returns `[]` cleanly — the safety cap
   keeps the scan from spinning.

## What I deliberately did NOT change

- **`exp` semantics / `include_expired` query param.** The brief
  ruled this out and the bug we're fixing is filter-ordering, not
  expiry. Untouched.
- **Status-badge thresholds and bucket logic** (`ONLINE_MULTIPLIER`,
  `RECENT_MULTIPLIER`). Left alone per brief.
- **7-day stream retention at the device write path**
  (`routes_device.py` `EXPIRE q:<topic> 604800`). Left alone.
- **Admin monitor tab JS** (`backend/src/static/app.js` around
  `monitorPoll`). Its default polling has no `client_id` and still
  hits the unfiltered path; its UI filter still passes repeated
  `client_id` params which the new code handles. No behavioral
  change there, so no edit.
- **Did not reduce the 30s refresh cadence** — sparse publishers
  by definition don't need faster polling, and the brief didn't
  ask.

## Deviations from the brief

- Brief implementation note suggested constants "near the top of
  the function with a brief comment." I placed
  `_STREAM_FILTER_BATCH` and `_STREAM_FILTER_MAX_BATCHES` as
  module-level constants immediately above `stream_monitor` with
  a comment block. Same intent, slightly different placement so
  the helper function below can reference them naturally.
- Brief suggested test file location "alongside other admin route
  tests if any exist, or `backend/tests/test_stream_monitor.py`
  if not." There are device-page / admin-static tests but no
  dedicated routes_admin test file, so I went with the
  `test_stream_monitor.py` location. The test bypasses the
  FastAPI auth dependency by calling `stream_monitor` as a plain
  async function (the `_: dict = Depends(...)` arg takes its
  default), matching the pattern in `test_backup_restore.py`.

## Test results

- `backend/venv/bin/pytest backend/tests/test_stream_monitor.py
  -v` → **4 passed**.
- `backend/venv/bin/pytest backend/tests/` → **383 passed**
  (full backend test suite; no regressions).
- `tools/stage smoke` — **not run from this environment**;
  `docker` is not available on this host. Must be run from the
  staging host or a dev box with the staging stack reachable
  before merge/deploy, per the Rules of Operation. Expected to
  be a no-op for this change at the smoke level — smoke covers
  device HMAC paths and admin auth, neither of which is touched
  here. The new behavior is exercised by the unit test and the
  manual checks below.

## Manual checks for staging deploy

After `tools/stage deploy`:

- `/app/critterchron/elk_cheetah` — at most 3 rows, all from
  `elk_cheetah`. If the latest is older than 2 ×
  `heartbeat_interval_seconds`, the stale blurb shows above.
- `/app/critterchron/ricky_raccoon` (chatty publisher) — 3 fresh
  rows, no blurb.
- Admin monitor tab — open `/admin`, go to the topic monitor,
  confirm unfiltered feed still shows all publishers and the
  in-UI client filter still narrows the feed.

If you want a sanity check on the bug being real before
deploying, the redis-cli probe from the original brief still
works:

```sh
docker exec stra2us-iot redis-cli XREVRANGE q:critterchron/public/heartbeep + - COUNT 300 | grep -c elk_cheetah
```

A small positive number (e.g. 19) confirms the sparse-publisher
scenario.

## Sharp edges / follow-ups

- **`(<id>` exclusive bound requires Redis ≥ 6.2.** Production
  is on a modern Redis (`q:` streams already use 6.2+ features
  elsewhere). On older Redis the `(<id>` syntax fails at parse
  time — the call raises rather than silently returning
  duplicates, so a per-page dedup check wouldn't help. Real
  mitigation if this ever matters: version-probe at startup and
  fall back to an inclusive upper bound + a "skip if this id
  equals the previous page's last id" guard. Not worth adding
  speculatively given the project's existing Redis floor.
- **The safety cap is a heuristic.** ~1000 entries scanned to
  find `limit=3` matches is fine for `q:` streams with normal
  publish rates, but if a topic ever has 1000+ entries between
  matches for a real client (extremely sparse publisher on a
  very chatty topic), they'd hit the cap and see fewer than 3
  rows. Surface to watch: customer reports of "I see 1 row when
  I should see 3" on the device page despite the device clearly
  publishing.
- **The filtered path is now load-bearing for the customer app
  page.** If anyone refactors `stream_monitor` later, the paging
  contract (filter-then-limit when `client_id` is set) is what
  keeps the tail useful. The unit test pins the behavior.
- **Possible future work:** if the activity tail becomes the
  primary investigation surface, consider a "show more" affordance
  that bumps `limit` (e.g. to 20) for one-shot deeper looks
  without changing the default. Out of scope here.
