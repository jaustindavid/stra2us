# Roadmap

Sprint-shaped plans for the work coming up. Each sprint is one
release, self-contained enough that a fresh team (or a fresh
Claude session) can pick it up cold without re-litigating the
design decisions.

## How to use this document

* **Each sprint section is the engineering brief.** Read top
  to bottom: motivation, scope, files, tests, verification.
* **Follow [`docs/release_cycle.md`](release_cycle.md) for the
  deploy mechanics.** The roadmap describes WHAT to change;
  the release runbook describes HOW to ship it.
* **Don't skip the "Decisions to make" section** of each
  sprint. Those are the design questions that need an answer
  before the implementation can proceed; some are checked off
  in advance, some need owner input.
* **Mark sprints done as you ship them.** Strike the title +
  add a `Landed YYYY-MM-DD in vX.Y.Z` note, same shape as
  closed-out TODO entries.

## Cycle: v1.7.x

Seven sprints bundled into **three releases**. The sprints are
the engineering-brief units (each section below has its own
motivation/scope/tests/verification); the releases are the
shipping units. Estimated ~6 days of focused work across 2-3
weeks if you do one release per few-day arc with verify +
promote + rest between.

| Release | Sprints | Time | Theme |
|---|---|---|---|
| **v1.7.1** | 1 + 2 + 3 + 4 | ~2 days | Polish + plumbing — four small disjoint changes |
| **v1.7.2** | 5 + 6 | ~1½ days | Device-flow tooling — CLI + smoke that uses it |
| **v1.7.3** *(or v1.8.0)* | 7 | 2-3 days | Backup/restore — its own arc |

### Why this bundling

**v1.7.1 (Sprints 1+2+3+4):** zero file overlap between the
four sprints — `catalog_lint.py`/`widget_renderer.py` (Sprint 1),
`tools/stage` shell (Sprint 2), `main.py` middleware (Sprint 3),
`routes_admin.py`+`app.js` (Sprint 4) are all distinct. Four
commits on one branch, individually revertable if any one of
them flares on staging. Cumulative ~2 days of code; staging
verification is one cycle for four small fixes.

**v1.7.2 (Sprints 5+6):** bundled by dependency. Sprint 6's
whole point is "use Sprint 5's CLI for the smoke test." Shipping
them separately means v1.7.2 lands the CLI and v1.7.3 lands its
first real consumer — splitting that arc is artificial. Two
commits, one branch, single verify path.

**v1.7.3 / v1.8.0 (Sprint 7):** solo. Backup/restore is big
enough on its own and has format-stability concerns (the dump
envelope is a quasi-public artifact). Its own release means the
changelog entry can be substantive and the version bump can
signal "real new capability."

### Sprint index

| Sprint | In release | Time | Depends on |
|---|---|---|---|
| 1. Generalize `widget: radio` | v1.7.1 | ½ day | — |
| 2. Auto-write `backend/VERSION` from `tools/stage` | v1.7.1 | ½ day | — |
| 3. Gate `/app/` landing form behind OAuth | v1.7.1 | ½ day | — |
| 4. Scoped admins see Activity Logs | v1.7.1 | ½ day | — |
| 5. Synthetic device-traffic CLI | v1.7.2 | 1 day | — |
| 6. Beefier smoke test (device flows) | v1.7.2 | ½ day | Sprint 5 |
| 7. Backup/restore: whole-instance + per-app | v1.7.3 (or v1.8.0) | 2-3 days | — |

---

## ~~Sprint 1 — Generalize `widget: radio` to any enum-backed field~~

**Landed 2026-05-13 in v1.7.1.** Dispatch in `widget_renderer.py`
covers int+enum, float+enum, and bool (synthetic `[true, false]`);
`catalog_lint.py` relaxed accordingly.

**Target release:** v1.7.1 (bundled with Sprints 2, 3, 4)

### Why

Today `widget: radio` is gated by lint to `type: string` +
`enum:` only. That's historical (radio was added in P0 for the
string-enum case), not principled. A catalog author writing a
binary flag with `type: int` + `enum: [{value: 1, label: "On"},
{value: 0, label: "Off"}]` would expect to be able to opt into
radios — today they can't.

Also subsumes the original "`type: bool` should auto-render as
radio" TODO. The decision in that discussion: every enum-backed
field renders as `<select>` by default; `widget: radio` is the
explicit opt-in. Predictable across types; no auto-magic per
type. (The bool-renders-as-radio TODO was closed when this
generalization was filed.)

### Scope

* `tools/stra2us_cli/catalog_lint.py:_lint_field_widget` — change
  the radio rule from `if var.type != "string"` to
  `if var.enum is None`. The requirement becomes "must have an
  enum to pick from" rather than "must be a specific type."
  `type: bool` needs a special-case: either skip the
  enum-required check, or synthesize a `[true, false]` enum
  during validation.
* `backend/src/services/widget_renderer.py` — the
  `widget == "radio"` dispatch fires for any enum-having field,
  regardless of `type`. `_render_radio` may need tweaks for
  non-string values: `JSON.stringify` the value for the radio's
  `value` attribute; the form-submit decoder already does
  `json.loads` fallback so `"1"` round-trips as int `1`,
  `"true"` as bool `True`.

### Files touched

* `tools/stra2us_cli/catalog_lint.py` (~5 lines)
* `backend/src/services/widget_renderer.py` (~10 lines, plus
  minor `_render_radio` tweaks)
* `tools/tests/test_catalog_lint.py` (~30 lines of new cases)
* `backend/tests/test_widget_renderer.py` (~30 lines of new cases)

### Test strategy

Backend renderer tests:
* `type: int` + `enum: [...]` + `widget: radio` → renders a
  radio group with each value
* `type: bool` + `widget: radio` → renders a true/false radio
  group (without requiring explicit enum)
* Existing `type: string` + `widget: radio` test still passes

Tools lint tests:
* `widget: radio` on enum-having int field → no error (was
  error pre-v1.7.1)
* `widget: radio` on field without enum → still an error
* `widget: radio` on `type: bool` → no error

### Verification on staging

Publish a test catalog with `type: bool` + `widget: radio` to
staging. Open the customer page; confirm the field renders as a
radio group, not a `<select>`. Then change the catalog back to
default (no `widget: radio`); re-render; confirm it goes back to
`<select>`.

### Dependencies

None.

### Gotchas / decisions

* **`_render_radio` value escaping:** non-string values currently
  un-handled. Decide: `JSON.stringify(value)` in the radio's
  `value=` attribute, or `String(value)`? The former preserves
  type information; the latter is simpler but means a round-trip
  for an int field becomes `"1"` then back to `1` via the
  form-submit decoder's `json.loads` fallback. Both work; pick
  whichever feels cleaner to the implementer.
* **Cache-bust:** no static-asset changes — the renderer is
  server-side. Skip the cache-bust ceremony.

---

## ~~Sprint 2 — Auto-write `backend/VERSION` from `tools/stage`~~

**Landed 2026-05-13 in v1.7.1.** `_write_version_file` helper in
`tools/stage`; called from both `cmd_deploy` and `cmd_promote`
after the `git checkout` step. `backend/VERSION` now defaults to
`dev` so a fresh tree is identifiable.

**Target release:** v1.7.1 (bundled with Sprints 1, 3, 4)

### Why

v1.7.0 shipped the runtime-side of the release-version display
(`backend/VERSION` → `core/version.py` → `GET /api/admin/release`
→ admin sidebar badge), but the file is bumped manually as part
of each release commit. Easy to forget — and the consequence is
that the badge shows stale info until someone notices.

The cache-bust pre-commit hook (v1.6.9) solves the analogous
problem for static-asset `?v=N`; this is the same shape, just
for the VERSION file. Once automated, the operator never thinks
about the file again.

### Scope

* **`tools/stage cmd_promote <tag>`** — write `<tag>` verbatim
  to `$PROD_DIR/backend/VERSION` *before* `docker compose build`,
  inside the prod git checkout. Commit it locally on the
  `deploy` branch the script already maintains? Probably not —
  the file is a build artifact, not source-of-truth. Leaving it
  uncommitted in the prod working tree is fine; the bind-mount
  picks it up at container start.
* **`tools/stage cmd_deploy <ref>`** — same, but with the
  staging clone's working tree. For staging, the value should
  include the short SHA + branch name (e.g.
  `v1.6.9-cut-4b28654` or just `4b28654`) so the badge
  distinguishes "running v1.7.0 as tagged" from "running this
  in-flight commit." Format suggestion:
  `<tag-or-ref> (<short-sha>)`.
* **Backend reads it unchanged** via `core/version.py`. No
  Python changes needed — the runtime side already handles
  arbitrary string content.

### Files touched

* `tools/stage` (~20 lines of shell across `cmd_promote` +
  `cmd_deploy`)
* `tools/tests/test_stage.sh` if such a thing exists, or new
  smoke for the file-write step

### Test strategy

Most of the testable behavior is in `tools/stage` itself — a
shell function `_write_version_file <ref> <target-dir>` is the
new unit. A small bash test (or just a manual smoke during
sprint verification) confirms the file lands with the right
content after each subcommand.

### Verification on staging

```bash
# Deploy a branch to staging
tools/stage deploy origin/v1.7.2-auto-version
# Then exec into the container and read the file:
tools/stage bash -c "cat /app/VERSION"
# Expected: something like "4b28654 (v1.7.2-auto-version)" or
# whatever shape the implementation chose.
# Hard-reload the admin page; sidebar badge shows the new value.
```

For prod:

```bash
tools/stage promote v1.7.2
# Then on prod host:
docker exec stra2us-iot cat /app/VERSION
# Expected: "v1.7.2"
# Hard-reload an admin page on prod; sidebar shows "v1.7.2".
```

### Dependencies

None. The runtime-side already exists from v1.7.0.

### Gotchas / decisions

* **Where the file lands.** `$PROD_DIR/backend/VERSION` —
  this is the bind-mounted directory, so the container's
  `/app/VERSION` reflects host-side content live. No
  container restart required (though `tools/stage promote`
  recreates the container anyway via `docker compose up -d`,
  so it's moot).
* **Format choice.** For prod: just the tag (`v1.7.2`).
  For staging: include the SHA because staging rebuilds on
  every push and "v1.7.2" alone would be misleading mid-cycle.
  Recommend `<tag-or-branch> (<short-sha>)` for both, with the
  tag-or-branch being the deploy ref.
* **Should the VERSION file be committed?** No. It's a build
  artifact derived from the deploy ref. Committing it would
  fight with the auto-write. Add it to `.gitignore` if it
  isn't already; the operator who manually bumped it for
  v1.7.0 (the file checked in to mark the cycle wrap) should
  be the only committed instance.

  Actually — the v1.7.0 commit already has a checked-in
  `backend/VERSION` containing `v1.7.0`. After Sprint 2 lands,
  that committed file becomes the "default during local dev /
  fresh checkouts" fallback; deploys overwrite it on the host
  filesystem. The operator may want to `git rm` the file as
  part of this sprint, OR keep it as a dev-default. Pick one
  and document.
* **Pre-commit hook to remind manual bumpers?** Once the
  automation is in place, manual bumps become unnecessary,
  but if an operator commits a backend/VERSION change by
  hand the hook could nudge "you don't need to do this any
  more; tools/stage handles it." Optional polish.

---

## ~~Sprint 3 — Gate `/app/` landing form behind OAuth~~

**Landed 2026-05-13 in v1.7.1.** `_path_needs_admin_auth` in
`main.py` lost its `/app/` and `/api/app/lookup_device` carve-outs;
both now require admin auth. New 18-test
`backend/tests/test_auth_path_gating.py` pins the new + preserved
behaviors.

**Target release:** v1.7.1 (bundled with Sprints 1, 2, 4)

### Why

`/api/app/lookup_device` is the customer-landing-form's
name → app resolver. Today it's intentionally public — the
form needs to look up a device's app *before* the per-device
page's OAuth kicks in. But that public surface is enumerable:
an unauthenticated attacker can probe device names and learn
which exist, which app each belongs to.

Pre-v1.7.x the docstring (`routes_app.py:213`) flagged this and
suggested Cloudflare Turnstile / CAPTCHA at the edge. OAuth at
the application layer achieves the same outcome with no
third-party dependency, since admin allowlisting on Google
OAuth is already in place.

### Scope

* **Add `/app/` to the auth-required paths** in `main.py`'s
  auth middleware. Currently the landing page itself is
  public; under this change, an unauthed visitor gets
  redirected to `/oauth/google/login?next=/app/` before
  seeing the form. Subsequent visits within the OAuth session
  cookie lifetime are silent — one roundtrip per session, not
  per visit.
* **`/api/app/lookup_device` becomes implicitly auth-gated**
  by sharing the same `/app/` prefix path. Update its
  docstring to reflect the new model (drop the "Public — no
  auth required" framing + the Turnstile reference).
* **Per-device page** (`/app/<app>/<device>`) already does
  OAuth + ACL check; unchanged.

### Files touched

* `backend/src/main.py` (~5 lines — path-pattern addition in
  the auth middleware)
* `backend/src/api/routes_app.py` (~5 lines — docstring update
  on `lookup_device`)
* `backend/tests/test_routes_app.py` (or wherever the route
  tests live) — verify no-cookie request to `/app/` and
  `/api/app/lookup_device` 302 to OAuth rather than 200
  (~30 lines)

### Test strategy

Backend integration tests with a TestClient that doesn't
send the OAuth session cookie:

* `GET /app/` → 302 with `Location: /oauth/google/login?next=...`
* `GET /api/app/lookup_device?name=x` → 302
* With cookie set: both return their expected results

The OAuth flow itself isn't under test here — just the gating.

### Verification on staging

1. Open `https://stra2us-staging.austindavid.com/app/` in
   an **incognito window** (no existing cookie).
2. Confirm redirect to Google OAuth login.
3. Complete OAuth (Google account allowlisted on staging).
4. Land back at `/app/` — see the landing form.
5. Type a device name; lookup succeeds; redirect to the
   per-device page.

Pre-sprint: step 2 doesn't happen — you land directly on the
form, no auth.

### Dependencies

None.

### Gotchas / decisions

* **Threat-model delta.** Enumeration goes from "anyone with
  internet" → "anyone with an OAuth-allowlisted Google account."
  Allowlisted accounts are people the operator already trusts
  enough to grant some ACL; even those are subject to Google's
  rate-limiting and account-reputation systems. Automated
  scraping by un-allowlisted bots becomes effectively
  impossible.
* **UX cost.** One OAuth roundtrip at session start for an
  unauthed visitor. Subsequent visits within the session
  cookie's lifetime are silent. Probably ~7 days of session.
* **Option B rejected.** During the design discussion, an
  alternative was to keep `/app/` public and have JS detect
  the 401 from `lookup_device` to redirect from there. That
  preserves "see the form without auth" but is more JS code
  and nobody asked for the public-form UX. Option A (gate
  `/app/` itself) is cleaner.
* **Drop the Turnstile reference.** The pre-v1.7.x docstring
  on `lookup_device` mentions Cloudflare Turnstile as the
  intended mitigation. Update it to point at the new OAuth
  gating instead.

---

## ~~Sprint 4 — Scoped admins see Activity Logs~~

**Landed 2026-05-13 in v1.7.1.** New
`GET /api/admin/visible_clients` (scope-aware client list); `app.js`
swapped `loadLogClients` *and* `loadMonitorClients` (addendum after
the first staging run) to consume it instead of the wildcard `/keys`.
6 new tests in `backend/tests/test_visible_clients.py`.

**Target release:** v1.7.1 (bundled with Sprints 1, 2, 3)

### Why

Today a non-superuser admin (e.g., `austin` with ACL only
covering `critterchron/...` prefixes) opens the Activity Logs
view and sees a blank page with no filter chips. Root cause: the
admin JS calls `/api/admin/keys` first to populate filter chips;
`/keys` is gated on `require_admin_superuser` → 403 for scoped
admins; the frontend's error chain prevents the subsequent
`/api/admin/logs` call (which IS scope-aware and would return
correct entries) from running.

This blocks any future admin work that scoped admins should
reach — worth doing before v1.7.x grows further.

### Scope

* **Backend** — new `GET /api/admin/visible_clients` returns
  only the `client_id`s whose ACL paths the caller's permissions
  cover. No secrets, no ACL contents — just IDs. `/keys` stays
  superuser-locked since it's tied to the client-management UI
  that scoped admins shouldn't access. The scope-filtering logic
  can reuse `_prefix_matches` from `api/dependencies.py:162` and
  the v1.6.7 reverse-index (`device_to_app:<id>`) to enumerate
  candidate client_ids efficiently.
* **Frontend** — `fetchLogs()` in `backend/src/static/app.js`
  swaps its `/keys` precall for `/visible_clients`. The new
  endpoint failing should be non-fatal: render logs without
  filter chips rather than render nothing.

### Files touched

* `backend/src/api/routes_admin.py` (new endpoint, ~30 lines)
* `backend/src/static/app.js:loadLogClients` (~10 lines)
* `backend/src/static/index.html` (cache-bust bump for app.js)
* `backend/tests/test_admin_visible_clients.py` (new, ~80
  lines): superuser sees everything; scoped admin sees just
  their slice; unauthed → 401

### Test strategy

The interesting cases are scope-filtering. With a fixture admin
context holding `[{"prefix": "critterchron/*", "access": "rw"}]`
the endpoint should return only client_ids whose `device_to_app:<id>`
maps to `critterchron`. A wildcard `*:rw` admin should see all
client_ids. An admin with no matching permissions should get an
empty list, not 403.

### Verification on staging

Log in as a scoped admin user (set up via `tools/stage seed-users`
with a narrow ACL or via `provision_device` against a test app).
Open Activity Logs view. Pre-sprint: blank page. Post-sprint:
entries visible, filter chips populated with the visible
client_ids only.

### Dependencies

None. The v1.6.7 reverse-index makes the scope-filtering cheap;
without it the implementation would have to fall back to a full
ACL scan, which is also fine just slower.

### Gotchas / decisions

* **Cache-bust:** admin app.js + index.html change → bump
  `?v=N`. Pre-commit hook will catch a missed bump.
* **Endpoint name:** `visible_clients` is clear but a little
  passive-voice; `accessible_clients` or `my_clients` are
  alternatives. Pick one and move on.
* **Edge case:** an admin who's never been granted any ACL
  (or whose `admin_acls:<user>` row was wiped) should get an
  empty array, not an error. Document this in the endpoint
  docstring.

---

## ~~Sprint 5 — Synthetic device-traffic CLI~~

**Landed 2026-05-14 in v1.7.2.** `stra2us synth-traffic` subcommand
+ `tools/stra2us_cli/synth.py` action loop (q-only / kv-only / both
modes, rate ceiling at 100 Hz with `--allow-high-rate` bypass,
deadline-aware pacing). New `post_queue()` on `Stra2usClient`. 16
new tests in `tools/tests/test_synth_traffic.py`. Caught + fixed
during Sprint 6 bring-up: kv-PUT errors used to `continue` and
bypass the per-tick pacer — a fast-failing PUT could drive a "1 Hz"
loop at ~4000 Hz. Refactored to a `put_ok` flag + nested GET;
pinned with `test_run_kv_put_failure_does_not_bypass_pacer`.

**Target release:** v1.7.2 (bundled with Sprint 6, which depends on this)

### Why

Two needs:
1. **Staging warm-up.** When staging is fresh or quiet, the
   smoke tests don't exercise much device-side traffic. A
   one-shot synthetic-traffic generator lets the operator
   warm up the activity log + telemetry surface on demand.
2. **Foundation for Sprint 6.** The beefier smoke test wants
   to POST signed device traffic; this CLI is the
   primitive that smoke can call.

Also useful for testing device-path code changes without
rebooting a real device.

### Scope

New `stra2us synth-traffic` subcommand:

```bash
stra2us synth-traffic \
    --target iot-staging.stra2us.austindavid.com:8253 \
    --client-id staging-probe \
    --secret <hex>            # or from ~/.stra2us/credentials \
    --duration 5m \
    --rate 2Hz \
    [--queue critterchron/public/heartbeep] \
    [--kv-key critterchron/dev1/wifi_ssid] \
    [--mode q-only | kv-only | both]
```

Reads HMAC secret from a flag or `~/.stra2us/credentials`. Runs
to completion, prints summary stats (`5m elapsed, 600 q-POSTs,
0 errors`). Reuses existing HMAC signing + msgpack body code
from `tools/stra2us_cli/client.py`.

### Files touched

* `tools/stra2us_cli/synth.py` (new, ~80 lines): the action
  loop
* `tools/stra2us_cli/cli.py` (~50 lines): new subcommand parser
  + dispatch
* `tools/tests/test_synth_traffic.py` (~80 lines): unit-test
  the action loop against a recording stub client

### Test strategy

Unit tests focus on the action loop's behavior at controlled
rate + duration. Stub the HTTP client; assert the right number
of calls are made, in the right shape (signed requests, correct
URI, correct msgpack body). A live test (skipped unless a host
is configured) mirroring `test_publish_live.py`'s pattern is
nice-to-have but not required.

### Verification on staging

```bash
stra2us synth-traffic --target iot-staging.stra2us.austindavid.com:8253 \
    --client-id smoke-probe --duration 30s --rate 1Hz
```

Observe activity log entries appearing at the right rate. Stop
early with Ctrl+C; confirm the summary stats line is printed.

### Dependencies

None.

### Gotchas / decisions

* **Rate-limiting safety.** Refuse rates above some sane ceiling
  (~100Hz) to avoid accidental DoS on staging. Operator can
  override with `--allow-high-rate` if they really mean it.
* **Don't leak the secret.** Avoid printing the secret in error
  messages or summary stats.
* **Client_id pattern.** Choose a clear "test traffic" pattern
  (e.g., `smoke-probe`, `synth-*`) so operators can grep it
  out of activity logs when reviewing real device traffic.
* **Mode flag.** Decide: is the default mode `q-only` (just
  heartbeat-shaped POSTs), `both` (POST + GET to round-trip a
  KV write/read), or configurable from the start? Recommend
  `both` default for richness; document overrides.

---

## ~~Sprint 6 — Beefier smoke test (device flows)~~

**Landed 2026-05-14 in v1.7.2.** `tools/smoke_test_device.sh` drives
the synth-traffic CLI against staging and reports in the same
PASS/FAIL shape as `smoke_test.sh`. New `tools/stage smoke-device`
and `seed-smoke-device` subcommands; `tools/stage smoke` runs the
device-flow checks too (skippable with `--skip-device-flow`).
Smoke device `smoke-test-device` on app `_smoke`
(`_smoke/<id>:rw` + `_smoke/public:rw` ACL). Verified end-to-end
on staging: signed queue POSTs, KV PUTs, and KV GETs with
round-trip equality all green.

**Target release:** v1.7.2 (bundled with Sprint 5, on which this depends)

### Why

Today's `tools/smoke_test.sh` validates the public hostnames
respond (200/404 patterns) but doesn't exercise the actual
device-protocol surface — HMAC-signed POST/GET against `/q/`
and `/kv/` with response-signature verification. A regression
in HMAC handling, msgpack framing, or encryption would slip
through current smoke. The beefier test catches it.

### Scope

Extend (or sibling-of) `tools/smoke_test.sh`:

1. POST to `/q/<topic>` (queue write) — verify response
   signature.
2. PUT to `/kv/<path>` (KV write) — verify response signature.
3. GET on the same path (KV read) — verify response, decode
   msgpack body, assert it matches what was just written.

Builds on Sprint 5's CLI: invoke `stra2us synth-traffic` (or its
underlying primitives in `tools/stra2us_cli/client.py`) rather
than re-implementing HMAC + msgpack in bash.

Bootstrap a known smoke-test device + secret on staging via
`tools/stage seed-users` (or a new `tools/stage seed-smoke-device`
subcommand).

### Files touched

* `tools/smoke_test_device.sh` (new, ~60 lines) — or extend
  the existing `smoke_test.sh`
* `tools/stage` (~20 lines) — new subcommand `tools/stage
  smoke-device` for ergonomics
* `tools/stage seed-users` or sibling — seed the smoke-test
  device's ACL

### Test strategy

Smoke tests test themselves — there's no meta-test infrastructure
to add. The validation is "does it pass against staging" and
"does intentionally breaking something fail loudly."

### Verification on staging

`tools/stage smoke-device` returns green. Then delete the
smoke-test device's ACL temporarily; re-run; confirm the smoke
fails with a clear error message naming the failing step.

### Dependencies

* Sprint 5 (synth-traffic CLI primitives)

### Gotchas / decisions

* **Smoke device client_id pattern.** Pick something clearly
  "internal" (e.g., `smoke-test-device`) so operators don't
  confuse it with real devices in activity-log filtering. Should
  be excluded from typical operator-of-interest queries.
* **ACL shape.** The smoke device needs ACLs that match the
  test paths it'll exercise — typically `<smoke-app>/<smoke-id>:rw`
  for queue + KV, plus `<smoke-app>/public:r` if testing app-
  scope reads. Reuse `provision_device`'s device-on-app shape.
* **Self-registration via reverse index.** v1.6.7's
  reverse-index lookup means the smoke device shows up in
  `/api/app/lookup_device` once provisioned, without needing
  to do its first KV write. No special handling required.
* **Cleanup.** Decide whether the smoke test cleans up after
  itself (delete the written KV key on success) or leaves data
  behind. Recommend leave-behind — historical data on a known
  smoke client_id is useful for "is this test still running?"
  triage; let `tools/stage nuke` (when filed) handle the
  occasional reset.

---

## ~~Sprint 7 — Backup/restore: whole-instance + per-app dumps~~

**Landed 2026-05-14 in v1.8.1** (the format-stability framing won —
the dump envelope is now a quasi-public contract). `GET /backup` +
`GET /backup/app/<app>`, `POST /restore` + `POST /restore/app/<app>`
with `?force_overwrite=1` and `?include_logs=1` semantics. Pure-data
envelope module `services/backup_format.py` (with 31 round-trip
tests pinning the format byte-for-byte) split from Redis-side
`services/backup_io.py` (15 integration tests). Per-app restore is
sandboxed: keys outside `<app>/...` / `_catalog/<app>` are rejected
even if the envelope claims otherwise. Full schema documented in
[`docs/fr_backup_envelope_v1.md`](fr_backup_envelope_v1.md). Admin
UI (download buttons, per-app rows, unified auto-detect restore,
per-section result render) shipped in the same release. The
v1.8.0 tag slot is skipped — backend + UI bundled as v1.8.1.

**Target release:** v1.7.3 — *or v1.8.0 if you decide this
release warrants the minor bump given backup formats are a
public-ish artifact.*

### Why

The existing `/api/admin/keys/backup` exports only client
credentials (`client:<id>:secret` + `client:<id>:acl`). The
v1.5 prod cutover required migrating much more state by copying
the Redis data dir wholesale: `admin_acls:*` rows, KV data
(including `<app>/<resource>` and KV-stored firmware blobs),
queue contents, activity log, catalogs (`_catalog/<app>/...`).
A `cp -a redis_data/` works for same-host migration but not for
cross-host moves or per-app exports.

Two complementary needs:
1. **Whole-instance dump** — covers all of the above, suitable
   for full-server migrations or periodic offline backups.
2. **Per-app dump** — given an app name (e.g. `critterchron`),
   export all its clients + ACLs + KV data + catalog + queues
   (or selected subsets). Useful for cloning an app environment,
   onboarding a new instance, or surgical restores.

### Scope

Two API verbs at `/api/admin/`:

```
GET  /backup                       # whole-instance dump
GET  /backup/app/<app>             # per-app dump
POST /restore                      # whole-instance restore
POST /restore/app/<app>            # per-app restore
```

Plus restore semantics:
* `?force_overwrite=1` — replace existing values
* default — skip existing, log what was skipped

**Envelope format:** version-tagged JSON. v1 schema:

```json
{
  "stra2us_backup_version": 1,
  "dump_kind": "whole" | "per-app",
  "app": "<name>" | null,
  "exported_at": "<iso8601>",
  "data": {
    "clients": { "<id>": {"secret": "...", "acl": {...}} },
    "admin_acls": { "<user>": {...} },
    "kv": { "<key>": {"value": "<base64-msgpack>", "encrypted": bool} },
    "catalogs": { "<app>": {"yaml": "...", "assets": {...}} },
    "queues": { "<topic>": [<entries>] },
    "activity_log_excluded": true | false
  }
}
```

`base64-msgpack` for KV values: msgpack handles binary cleanly;
base64 makes the dump line-readable. Decision needed (see
gotchas).

Activity log: excluded by default (often 100k+ entries; usually
not load-bearing for restores); opt-in with `?include_logs=1`.

### Files touched

* `backend/src/services/backup_format.py` (new, ~120 lines):
  envelope schema + serialize/deserialize helpers
* `backend/src/api/routes_admin.py` (~200 lines for dump +
  restore handlers)
* `backend/tests/test_backup_restore.py` (~250 lines):
  round-trip a populated fake-redis, assert every byte type
  round-trips
* `backend/src/static/app.js` + `index.html` — admin UI
  buttons for trigger + download. *Could defer to a v1.7.6
  follow-up if Sprint 7 is feeling big.*

### Test strategy

Unit tests for the envelope format (round-trip a sample dict).
Integration tests: populate a fake Redis with a representative
mix (clients, admin_acls, KV with mixed encrypted/plaintext,
catalogs, queue entries), dump, parse, restore to an empty fake
Redis, assert the state matches byte-for-byte. Test the skip-
existing + force-overwrite semantics. Test per-app filtering
(per-app dump shouldn't leak other apps' data).

### Verification on staging

1. Dump staging → save the JSON locally.
2. Bring up a fresh docker compose stack (or `tools/stage nuke`
   if that's filed by then).
3. Restore the dump → run smoke tests → click through admin UI.
4. Verify catalog publishes still work; encrypted KV records
   still decrypt; activity log either present-or-empty matches
   the dump options used.

### Dependencies

None directly. Pairs well with `tools/stage nuke` (a deferred
operational TODO) since "dump + nuke + restore" is the
disaster-recovery story.

### Gotchas / decisions

* **JSON vs msgpack envelope.** JSON: line-readable, diffable,
  larger. Msgpack: binary, compact, opaque to humans. JSON
  recommended — operators will sometimes want to grep dumps.
* **Encrypted records.** The `:enc` sidecar must travel with
  the value. The dump format encodes this as a `{"value": ...,
  "encrypted": true}` shape; restore re-sets the sidecar on
  write.
* **Sensitive data warnings.** Dumps contain HMAC secrets,
  OAuth tokens, encrypted-at-rest plaintext (via the wire-
  encryption inverse). Treat dumps as password-manager-export
  sensitivity. Document in the response headers + the admin
  UI prompt.
* **Activity log inclusion default.** Off, because logs can
  be huge and usually aren't load-bearing. `?include_logs=1`
  opt-in.
* **Firmware blobs.** Stored in KV as binary. Include or
  exclude by default? Recommend include, since they're
  load-bearing for device restore. Document the size implication
  ("expect dumps of 10-100MB on a populated fleet").
* **Per-app filtering edge cases.** Devices that exist on
  multiple apps (rare but possible). Decide: include in any
  per-app dump where they have an ACL match, OR require an
  explicit "primary app" tag. Probably the former — simpler
  and matches the spirit of "give me everything this app
  touches."
* **Format versioning.** Restore should refuse formats it
  doesn't understand (`stra2us_backup_version: 99`). Future
  formats can include a migration helper that converts old
  envelopes forward.

### Why this might be v1.8.0 instead

If the implementation surfaces real design questions (especially
around the envelope format and per-app filtering edge cases),
the result is a public-ish artifact that future-you and others
will care about format-stability of. That's "real feature"
territory, not "incremental polish." The version bump signals
that.

---

*(Future cycles append below as their roadmap takes shape.)*
