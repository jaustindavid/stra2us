# Dispatch: surface heartbeat history for sparse publishers on the customer app page

_Brief for a fresh agent. Self-contained — you do not need access
to the originating conversation. Read this, scan the linked
files, ask clarifying questions only if something here is
contradictory; otherwise build._

## What you're changing

The customer-facing app page at `/app/<app>/<device>/...` (e.g.
`/app/critterchron/elk_cheetah`) renders a status badge + recent
telemetry tail at the bottom. Today, for "sparse publishers on a
busy shared topic," the tail almost always shows _"No recent
telemetry from this device"_ even though the device is publishing
fine and you can see its history in the admin monitor tab.

The cause is a server-side ordering bug, not a client display
bug. Fix is server-side primarily, with a small client-side
quality-of-life improvement layered on top.

## Why this matters (dual intent)

The activity tail at the bottom of the device page has two
distinct jobs:

1. **Liveness signal** — is this device online, stale, or
   offline? The status badge above the tail covers this for the
   most-recent message, but if the tail shows nothing the
   customer assumes "broken" even when the badge says
   "Recently active."
2. **Investigation surface** — when something looks wrong, the
   customer wants to see the last few messages to understand
   what the device was doing. An empty tail is useless for this.

The change preserves the liveness signal (existing status
badge stays; new "stale" blurb supplements it) while making
the investigation surface useful for slow publishers.

## The bug, concretely

Devices publish telemetry to Redis streams keyed
`q:<topic>`. The topic is catalog-declared per app
(`telemetry_topic` field, default `{app}/public/heartbeep`).
Many devices in an app publish to the **same** shared topic;
each entry's `client_id` field identifies which device.

The app page fetches recent activity via:

```
GET /api/admin/stream/q/<topic>?client_id=<device>&limit=10
```

The server handler ([`backend/src/api/routes_admin.py`](../../backend/src/api/routes_admin.py),
function `stream_monitor` at line 1146) does the wrong thing
in this exact order:

```python
records = await redis.xrevrange(f"q:{topic}", max="+", min="-", count=limit)
# ...
for msg_id, fields in records:
    # filter by exp ...
    if client_id and cid not in client_id:
        continue
    # ...
```

Read `limit` raw entries → THEN filter by client_id. For a
shared topic with 10+ publishers where the device of interest
heartbeats slowly, ~10 raw entries contain zero or one match
for that client_id. Verified empirically: a 300-entry
XREVRANGE window for `q:critterchron/public/heartbeep` contains
19 entries from `elk_cheetah` (~1 in 16). At `limit=10`, the
app page sees this client zero times most of the time.

Confirm yourself before changing anything:

```sh
docker exec stra2us-iot redis-cli XREVRANGE q:critterchron/public/heartbeep + - COUNT 300 | grep -c elk_cheetah
# Expected: a small positive number, e.g. 19
```

## What to change

### 1. Server: filter-then-limit, not limit-then-filter

In `stream_monitor`
([`backend/src/api/routes_admin.py:1146`](../../backend/src/api/routes_admin.py)):

When `client_id` is set (one or more values), the function must
return up to `limit` entries **matching the filter**, not up
to `limit` raw entries (most of which get discarded). When
`client_id` is unset (admin monitor's default), preserve the
current behavior exactly — return up to `limit` raw entries.

Implementation shape: paged XREVRANGE walking backward, batched
reads, accumulate matches, stop when you've got `limit` matches
OR you've walked back further than the stream's retention
window allows OR you've hit a configurable max-batches safety
cap. Suggested batch size: 100. Suggested max batches: 10 (so
we never scan more than ~1000 entries to find `limit`
matches). Both should be constants near the top of the
function with a brief comment about the rationale.

Stream is bounded (7-day `EXPIRE` at the stream level — set by
the device write path in `routes_device.py`). The paged scan
terminates naturally at the oldest retained entry. The safety
cap is for pathological "client never wrote to this topic"
cases where you'd otherwise scan the entire stream looking for
matches that don't exist.

Preserve all existing per-entry logic (exp filter, msgpack
decode, payload shape).

### 2. Client: limit=3, plus a stale-data blurb

In [`backend/src/static/app/app.js`](../../backend/src/static/app/app.js):

- `refreshTelemetry` (around line 315): change `limit=10` to
  `limit=3`.
- `renderActivityList` (around line 380): if the newest
  message's age exceeds `heartbeatSeconds * 2`, prepend a
  styled `<div class="activity-stale-blurb">` (you'll add the
  CSS) with copy like _"No recent heartbeats — showing the last
  3 messages."_ The activity rows render normally below.
- The empty-state ("No recent telemetry from this device.")
  stays for the case where the API returned zero entries (truly
  no history within the stream's 7-day retention window).

Reuse `ONLINE_MULTIPLIER` (= 2) for the stale-blurb threshold —
the badge bucket already changes at this same age, so the
blurb and the "Offline" / "Recently active" badge transition
coherently. Don't introduce a new constant.

### 3. Do NOT add an `include_expired` flag

An earlier draft of this brief considered adding
`?include_expired=1` to the stream endpoint. **Rejected.** The
per-message `exp` field is currently used by the server to
filter aging entries from the activity tail; expired entries
should remain hidden — they're stale by definition. Confirmed
the bug we're fixing is filter-ordering, not `exp` semantics.

## What NOT to break

- **Admin monitor tab** ([`backend/src/static/app.js:2052`](../../backend/src/static/app.js)
  in `monitorPoll`) hits the same endpoint without a
  `client_id` filter by default. Its behavior must not change.
  When the operator sets one or more filters via the UI, those
  pass through as repeated `client_id` query params; the
  new paged logic must handle multi-value `client_id` correctly
  (any entry matching any provided id counts as a match).
- Existing status badge thresholds (`ONLINE_MULTIPLIER`,
  `RECENT_MULTIPLIER`) and bucket logic in `app.js` (around
  line 348) — leave alone.
- The 7-day stream retention at the device write path
  (`routes_device.py`, the `EXPIRE q:<topic> 604800` line) —
  leave alone.

## Verification

### Smoke (must pass)

```sh
tools/stage smoke
```

This exercises the basic device HMAC paths and admin endpoints
end-to-end. Must be green before and after your change.

### New test for the sparse-publisher regression

Add a test that exercises exactly the case being fixed:

- Seed a topic with 50 entries from `chatty_client` and 3
  entries from `quiet_client` (interleaved, with `quiet_client`
  entries at positions 5, 25, 45 so they're spread out).
- Call the endpoint with `limit=3&client_id=quiet_client`.
- Assert the response contains all 3 `quiet_client` entries.
- Add a second case: `limit=3&client_id=chatty_client` returns
  the 3 most recent `chatty_client` entries.
- Add a third case: no `client_id` filter, `limit=3` returns 3
  entries irrespective of identity (preserved old behavior).
- Add a fourth case: `client_id` matches nothing in the stream,
  `limit=3` returns `[]` cleanly (no infinite scan; the
  max-batches cap must kick in).

Test file location: alongside other admin route tests if any
exist, or `backend/tests/test_stream_monitor.py` if not.

### Manual

After deploying to staging:

- Load `/app/critterchron/elk_cheetah` in a browser. Should
  see at most 3 telemetry rows from `elk_cheetah` (only). If
  the most-recent is older than 2 × `heartbeat_interval_seconds`,
  the stale blurb appears above the rows.
- Load `/app/critterchron/ricky_raccoon` (a chatty publisher).
  Should see 3 rows, no stale blurb (recent).
- Check the admin monitor tab is unchanged — filter UI still
  works, unfiltered still shows everything.

## Reading list (optional context)

You do not need to read all of these. The brief above is
self-contained. Pull these in only if you hit something
ambiguous:

- [`docs/fr_application_view.md`](../fr_application_view.md) —
  defines the customer-facing app page and the telemetry tail
  semantics. The `heartbeat_interval_seconds` catalog field is
  documented here.
- [`docs/fr_catalog_app_ui.md`](../fr_catalog_app_ui.md) — the
  broader catalog-driven UI design the app page lives in.
  Relevant if you need to know what counts as "catalog metadata"
  vs "field configuration."
- [`docs/client_spec.md`](../client_spec.md) — how devices
  publish; the `q/<topic>` convention.
- [`docs/stra2us_device_identity.md`](../stra2us_device_identity.md)
  — device naming conventions (HMAC client_id = device name by
  convention).

The Rules of Operation in [`README.md`](../../README.md) apply.
Note especially: small commits, smoke must pass at every
checkpoint, and resist diagnostic-by-vibes.

## Handoff doc — your last act

When you're done, leave a sibling file:

`docs/dispatch/heartbeat-tail-sparse-publishers.handoff.md`

Cover:

- What you actually changed (files + brief summary).
- What you deliberately did NOT change and why (if you found
  things tangential to the brief that were tempting but
  out-of-scope).
- Any deviations from this brief and the reasoning.
- Test results (smoke output + the new unit tests).
- Anything the next person should know — sharp edges you
  noticed, places where the design is now load-bearing in a way
  it wasn't before, follow-up work worth filing.
- A 1-line summary at the top suitable for inclusion in a
  release-notes block.

Keep it tight. Future cuttlefish will read it cold; assume they
have no idea what conversation produced your change.
