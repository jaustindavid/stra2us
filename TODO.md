# TODO

## Near-term

- ~~**Add BuildKit cache mounts to `backend/Dockerfile`.**~~
  Landed 2026-05-09 in v1.6.1. `--mount=type=cache,...` on the
  three remote-fetching `RUN` lines (apt-get for system deps,
  pip for backend requirements, pip for stra2us_cli). Dropped
  the `rm -rf /var/lib/apt/lists/*` that the cache mount makes
  unnecessary (cache mounts aren't committed to image layers,
  so the image stays slim regardless). Verify on next rebuild:
  the second build after this one should be noticeably faster
  for the apt + pip layers; `docker buildx du` shows the cache
  dirs populated.

- ~~**Monitor tab "Clear" button repopulates seconds later.**~~
  Landed 2026-05-09 in v1.6.1. New `monitorClearedAfter`
  module-level variable (`backend/src/static/app.js`) gets
  stamped with `Math.floor(Date.now() / 1000)` in
  `monitorClear()`; the polling fetch's render loop skips
  messages with `received_at <= monitorClearedAfter`.

- ~~**Intermittent "Sign-in session expired or was forged."**~~
  Instrumented 2026-05-09 in v1.6.1. The CSRF-mismatch branch
  in `routes_oauth.py:oauth_callback` now emits a
  WARNING-level log under the `stra2us.oauth` channel
  (`csrf_mismatch` event) with `case=cookie_missing` vs
  `cookie_mismatch`, plus UA + Referer prefixes. Next time the
  symptom is reported, the log distinguishes the three
  suspected causes without further investigation. **Fix
  itself is still pending** — instrumentation is the
  prerequisite (need data to know which case to fix);
  re-evaluate after a release of telemetry.

- ~~**Bypass Cloudflare cache for `/admin/*` assets.**~~ Landed
  2026-05-06: CF Cache Rule on the austindavid.com zone with
  expression `http.host contains "stra2us" and starts_with(http.request.uri.path, "/admin/")`
  → bypass cache. Verified `cf-cache-status: DYNAMIC` afterward.

- ~~**Make seed/bootstrap idempotent.**~~ Both halves landed
  2026-05-06: `bootstrap-host.sh::seed_htpasswd` merges per-user
  (instead of skip-if-exists), and `tools/stage seed-users` skips
  already-provisioned users with `--force` to override. Tested
  locally via `tools/tests/test_bootstrap_seed.sh` for the file
  side; staging integration confirms the Redis side.

- ~~**Add smoke-test coverage for `/api/admin/security_warnings`.**~~
  Done 2026-05-06. New `[security warnings endpoint]` block in
  `tools/smoke_test.sh` after the activity-log section: authenticated
  GET, asserts 200 + `warnings` array shape, reports the count
  informationally without asserting the contents (deployment-state
  observation, not a regression).

- **Revisit backup/restore — provide a way to dump an entire app.**
  The existing `/api/admin/keys/backup` only exports client
  credentials (`client:<id>:secret` + `client:<id>:acl`). For the
  v1.5 prod cutover we had to migrate a lot more state by copying
  the Redis data dir wholesale: `admin_acls:*` rows (admin user
  ACLs), KV data including `<app>/<resource>` and the new
  KV-stored firmware blobs, queue contents, activity log, catalogs
  (`_catalog/<app>/...`). A `cp -a redis_data/` works for a same-
  host migration but not for cross-host moves or per-app exports.
  Two complementary needs:
  1. **Whole-instance dump** — extends the existing backup to cover
     all of the above, suitable for full-server migrations or
     periodic offline backups. Format: JSON or msgpack envelope.
  2. **Per-app dump** — given an app name (e.g. `critterchron`),
     export all its clients + ACLs + KV data + catalog + queues
     (or selected subsets). Useful for cloning an app environment,
     onboarding a new instance, or surgical restores.
  Both should also support **restore** with at least the existing
  "skip-existing / force-overwrite" semantics. Sensitive data
  (HMAC secrets, OAuth tokens) keeps the existing "treat like a
  password manager export" warnings.

- ~~**Fix or retire host-side smoke test runs.**~~ Fixed
  2026-05-06. Root cause: Synology's squid proxy was transparently
  intercepting outbound HTTP from the host and trying to hairpin
  on its own (which doesn't work). Two-part fix:
  1. **`tools/smoke_test.sh`** now wraps every curl invocation
     with `--noproxy '*' --max-time 10`, making the smoke test
     proxy-independent regardless of the operator's environment.
  2. **`--skip-device` flag** retained as a fallback for any
     network where the hairpin path genuinely doesn't work.
  Verified 9/9 from the container host with squid disabled in
  Synology network settings. Manual curl from host can still be
  flaky against the public hostname (likely persistent iptables
  interception that survives the squid toggle), but the smoke
  test gets through reliably with the baked-in `--noproxy '*'`.
  `tools/stage smoke --skip-device` retained for any env where
  hairpin truly fails.

- ~~**Revisit top-level README for freestanding-repo visibility.**~~
  Trimmed 2026-05-06: license + status notes added near top, long
  C++ SDK + CLI sections collapsed into pointers to
  `docs/client_spec.md` and `tools/README.md`, changelog trimmed to
  most-recent + pointer to git log + FR docs. README went from 401
  lines to 311.

- ~~**Rewrite `docs/admin_auth_architecture.md` to reflect post-v1.5
  reality.**~~ Done 2026-05-06: full rewrite covering both auth
  paths (OAuth on browser host, htpasswd on device host),
  middleware logic, RESCUE_USERS pattern, bootstrap-default
  rescue password mechanism, logout flow, what's gated vs public.

- ~~**Rename `docs/staging.md` to clarify scope.**~~ Renamed to
  `docs/local_dev.md` 2026-05-06; reference in README updated;
  file's own header updated to disambiguate from
  `docs/staging_environment.md`.

- ~~**Audit `docs/fr_v15_auth.md` against shipped reality.**~~
  Done 2026-05-06: prepended a "superseded by
  fr_v15_incremental.md" status block listing the major
  divergences (hostname naming, rescue path mechanism, staging
  environment, phased rollout discipline). Body preserved as
  historical record.

- ~~**Add copyright/license header to source files.**~~ Done
  2026-05-06: 32 files (Python, JS, Bash) updated with
  `# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0`
  header. Shebangs preserved; syntax-checked; bootstrap-seed test
  still 8/8 green.

- **Automate pre-build / external-file staging.** The backend
  container today depends on hand-built artifacts that exist on the
  host before `docker compose build && up -d` runs — at minimum
  `backend/admin.htpasswd`, possibly also `backend/.env` content,
  the `firmware/` dir, etc. None of that is captured in code, so a
  fresh checkout on a clean host won't come up cleanly. Two related
  needs:
  1. **Audit:** enumerate every file/state the running container
     assumes is present on the host. Write it down somewhere
     authoritative (likely a "Bring-up" section in
     `docs/staging_environment.md`, applicable to prod too).
  2. **Automate:** a `tools/bootstrap` (or `tools/stage bootstrap`
     for staging-specific) that takes a clean checkout and produces
     the missing pieces — generates a starter `admin.htpasswd` with
     a randomly-generated rescue password printed once, creates
     empty `firmware/` and `redis_data*/` dirs with right perms,
     etc. Idempotent — re-running on an already-bootstrapped tree
     is a no-op.
  Particularly critical for staging (`tools/stage up` should "just
  work" on a fresh host) but applies to any prod-on-a-new-machine
  recovery scenario.

- **Mobile-friendly customer app page.** The customer-facing
  `/app/<app>/<device>/...` page is desktop-leaning today (table
  layouts, smallish touch targets, no viewport meta). Customers
  configuring a critterchron from their phone deserve a usable
  experience: viewport meta tag, fluid layout, touch-sized
  controls (40px+ tap targets), input types that trigger the
  right mobile keyboards (`type=number`, `type=email`, `type=tel`,
  etc.), responsive breakpoints. Bundles naturally with the
  catalog widget + theming work in
  [`docs/fr_catalog_app_ui.md`](docs/fr_catalog_app_ui.md) since
  both touch the customer UI surface — could be folded into that
  FR's implementation pass, or done as a small separate sweep
  beforehand. Admin UI mobile is a separate (lower-priority)
  concern; this entry is specifically about the customer surface.

- **Implement Basic Auth brute-force detection & lockout.** FR is
  in [`docs/fr_basic_auth_lockout.md`](docs/fr_basic_auth_lockout.md).
  Sliding-window failure counter keyed by `(source_ip, username)`,
  per-(IP, username) lockout with `Retry-After`, Redis-backed state,
  failed-attempts logged to a new `auth_log` stream. Not blocking
  (strong rescue password is the load-bearing mitigation today),
  but adds defense-in-depth + operator visibility. ~half-day of
  focused work plus tests; no UI changes required initially. Open
  questions captured in the FR.

- **First-class "global admin" recognition + UI affordances.** A
  user with `*:rw` is the de-facto superuser; the UI doesn't say
  that explicitly today, and several flows are awkward as a result.
  Three related improvements that should land together:
  1. **Tag a user as a global admin** — single button in the Admin
     Users row (or in the Edit ACL modal) that's sugar for "set
     permissions to `[{prefix:'*', access:'rw'}]`." Removes the
     "type the right shape into the manual rule field" step.
  2. **Recognize `*:rw` as a label**, not just data — render a
     `global admin` badge (or similar) on rows whose ACL has any
     `*:rw` permission. Distinguishes them visually from scoped
     admins. Powers other UI gating downstream.
  3. **Disable the device picker for global admins** — opening
     "+ Select Devices" for a user with `*:rw` is meaningless
     (they already cover everything). Hide the button (or replace
     with a "covers everything (wildcard)" hint), avoiding the
     confusion that surfaced the wildcard-dedupe TODO.
  Bundles cleanly: a single `is_global_admin(acl)` helper covers
  all three checks. About a half-day of work end-to-end.

- **Device picker dedupe doesn't recognize wildcard coverage.** A
  user with `*:rw` (or `<app>:rw` covering a whole app) opens the
  device picker and sees every device as un-granted, even though the
  wildcard logically covers all of them. Should treat `*` and
  parent-prefix matches as "already granted" in the picker's
  disabled-checkbox logic. Implementation: replace the exact-string
  dedupe in `confirmDevicePicker` / the rendering predicate
  (`aclCurrentPermissions.some(p => p.prefix === token)`) with a
  coverage check that mirrors the backend's `_prefix_matches`
  semantics (`*`, exact, or parent-prefix). Same predicate also
  improves the "(already granted)" labeling in the picker.

- **Device picker modal overflows the viewport with many devices.**
  In Admin Users → Edit ACL → "+ Select Devices", a long device list
  pushes the modal beyond the bottom of the screen — the picker list
  has `max-height: 50vh` but the surrounding modal-content has no
  height constraint, so headers + hints + the 50vh list + actions
  can total >100vh. Fix: cap `.modal-content` (or just `.acl-modal-content`)
  at something like 90vh, make it `display: flex; flex-direction: column`,
  let the list section grow with `flex: 1 1 auto` so it shrinks to
  fit available space. ~5 lines of CSS.

- **"Last seen just now ago" on single-device screen.** Time-format
  bug: the relative-time formatter is concatenating "just now" with
  "ago" when the duration is sub-threshold. Should be "just now"
  (no "ago") for very recent events, and "<N> <unit> ago" for older
  ones. Lives somewhere in `backend/src/static/app.js` or the
  `/app/` view templates — short grep for "just now" / "ago" should
  find it.

- **Scoped admins can't see Activity Logs view.** A non-superuser
  admin (e.g. `austin`, ACL has `critterchron/...` prefixes but no
  `*:rw`) sees a blank Activity Logs page and no filter chips. Root
  cause: the page calls `/api/admin/keys` first to populate filter
  chips; `/keys` is gated on `require_admin_superuser` → 403 for
  scoped admins; the frontend's error chain prevents the subsequent
  `/api/admin/logs` call (which IS scope-aware and would return
  correct entries) from running. Two-part fix:
  1. **Backend:** add `/api/admin/visible_clients` (or similar) that
     returns only the `client_id`s whose ACL paths the caller's
     permissions cover. No secrets, no ACL contents — just IDs.
     `/keys` stays superuser-locked since it's tied to the
     client-management UI which scoped admins shouldn't access.
  2. **Frontend:** in `fetchLogs()` (`backend/src/static/app.js`),
     swap the `/keys` call for `/visible_clients`, and make a
     `/visible_clients` failure non-fatal — render logs without
     chips rather than render nothing.
  Worth doing before Phase 5 (provisioning UI), since that work will
  also have to think about scoped-admin views.

- ~~**Add an admin logout endpoint.**~~ Landed 2026-05-06 as v1.5.3:
  `GET /admin/logout` (carved out of admin auth gating), Sign out
  link in the sidebar footer. Hostname-aware response — 200 + clean
  HTML page on the OAuth path, 401 + `realm="logged-out"` on the
  device path (the realm change busts Chrome's Basic Auth cache,
  which is otherwise un-clearable without quitting the browser).

- **`tools/stage nuke` — reset staging Redis + re-seed.** *(Deferred:
  build this when a real schema-incompatible change forces the issue,
  not speculatively. Today's persistent staging state is fine.)* Today
  staging Redis state persists across deploys (intentional —
  preserves provisioned clients, logs, ACLs between test cycles).
  When a schema change lands that's incompatible with the previous
  state, the operator needs a one-shot way to wipe staging and
  start clean. Shape: `tools/stage nuke` stops the staging stack,
  removes the `redis_data_staging` volume, brings staging back up,
  and re-runs `tools/stage seed-users`. Confirms with the operator
  before destroying state (no `--yes` shortcut by default; staging
  data is sometimes painful to recreate). Filing so it exists when
  the trigger arrives — but build it then, not now, because the
  shape will be informed by the actual incompatibility.

- **Synthetic device-traffic CLI for staging top-up.** A short-lived
  job that posts signed device traffic to a target host for a
  configured duration. Built as a subcommand of the existing
  `tools/stra2us_cli` Python client (which already has HMAC signing,
  msgpack body construction, and the wire format implemented). Rough
  shape: `stra2us synth-traffic --target iot-staging.stra2us.austindavid.com:8253
  --client-id staging-probe --duration 5m --rate 2Hz`. Reads the
  client's HMAC secret from a flag or `~/.stra2us/credentials`.
  Runs to completion, prints summary stats. Use cases: kicking off
  device traffic before a staging smoke run when no real LAN device
  is heartbeating, generating load for testing, validating a
  device-path code change without rebooting a real device.
  Complements the LAN-only real staging devices (which are the
  primary traffic source); this is the on-demand top-up. See
  [`docs/staging_environment.md`](docs/staging_environment.md)
  for surrounding context.

- ~~**Build a staging environment.**~~ Landed 2026-05-06 as Phase
  4.5 of the v1.5 rollout (see `docs/staging_environment.md` and
  `docs/fr_v15_incremental.md`). Original entry preserved below for
  history.

  Today, "rebuild" means rebuilding
  the live container; the only safety net is `tools/smoke_test.sh`
  catching regressions after the fact, with a manual image re-tag
  before each rebuild as a workaround snap-back. A real staging
  environment — separate compose stack, separate hostname, separate
  Cloudflare tunnel, fed by the same image build pipeline — would
  let us validate dep bumps, OAuth changes, and middleware edits
  before they touch the production hostname. The smoke test already
  accepts `STRA2US_BROWSER_HOST` / `STRA2US_DEVICE_HOST` env vars,
  so it can target staging unchanged. Open questions: what runs on
  staging (a separate VM, a docker network alias, a second Pi)? How
  does staging get a real device's heartbeat for the activity-log
  check (mirror a subset of device traffic, or a synthetic
  HMAC-signed probe)? Until staging exists, image-tag-before-rebuild
  is the manual workaround.

  Scope to bring in when staging lands:
  - A deploy script (`tools/deploy.sh` or similar) that handles
    `docker compose build` + `up -d` + waiting for the cloudflared
    tunnel to finish registering all connections before the smoke
    test runs. Today this is a manual "sleep 12, then run smoke"
    or a `grep -q connIndex=3` poll on `docker compose logs
    cloudflared`. Belongs in deploy orchestration, not the smoke
    test itself.

- ~~**Draft a "Stra2us client implementor's guide" / spec.**~~ Landed
  2026-05-03 as [`docs/client_spec.md`](docs/client_spec.md). Covers
  wire basics, request signing, response verification, msgpack value
  shapes (str/bin parity, absent-key signals, ext family for
  encrypted), HMAC-keystream cipher, Connection: close discipline,
  streaming-fetch chunk-callback pattern, tel-thread sizing, errlog
  surfacing convention, catalog `ops_only`/`encrypted` distinctions,
  drift-test patterns, pointers into the three reference impls, and a
  10-item validation checklist for new clients. Intended to be the
  starting point for any fourth-platform port (nRF52, RP2040, etc.)
  before they have to re-derive things from scattered prose.

  *Original scoping notes (kept for context):*

  Sections worth covering, drawn from what bit us during the existing
  implementations:

  - **Wire basics.** HTTP/1.1, msgpack body format. Endpoints
    (`GET/POST /kv/{key}`, `POST /q/{topic}`, etc.). Required request
    headers (`X-Client-ID`, `X-Timestamp`, `X-Signature`); response
    headers (`X-Response-Timestamp`, `X-Response-Signature`).
  - **Request signing.** HMAC-SHA256 over `URI || body || timestamp`.
    Body is empty bytes for GET. Timestamp is ASCII decimal.
    Signature hex-encoded.
  - **Response verification.** Streaming HMAC update during body read
    (don't require buffering the whole body — relevant for ~1MB OTA
    fetches). Drift window (±300s). Constant-time hex compare. Fail
    closed: a 2xx response without the signing headers MUST be
    rejected.
  - **msgpack value shapes.** str family (`0xa0-0xbf` fixstr, `0xd9`
    str8, `0xda` str16, `0xdb` str32) and bin family (`0xc4` bin8,
    `0xc5` bin16, `0xc6` bin32) — server may emit either for the same
    logical string value, so client must accept both. Numeric types
    for int/float values. The "absent key" signals: nil (`0xc0`) and
    fixmap envelope (`0x80-0x8f`) — both must be handled silently as
    "not found," not as protocol errors. Ext family (`0xd4-0xd8`
    fixext, `0xc7-0xc9` ext8/16/32) for encrypted values
    (per `fr_encrypted_values.md`).
  - **Encryption.** HMAC-keystream cipher: `keystream =
    HMAC-SHA256(secret, "stra2us-kvenc-v1" || nonce_BE || counter)`,
    nonce = response timestamp uint32, counter increments per 32-byte
    block. Marker: msgpack ext type 0x21. Decryption transparently
    feeds the plaintext to the existing str/bin parser; client
    callers don't need to know it was encrypted.
  - **Connection lifecycle.** `Connection: close` honored — server
    FINs the socket after every response; client must close its end
    too or risk reusing a half-closed socket. ESP32-specific: bug
    where `WiFiClient::connected()` doesn't see the FIN until
    propagated; force-close after body read regardless.
  - **Streaming fetches** for large values (~1MB OTA blobs).
    Chunk-callback pattern (see `kv_fetch_stream_` in critterchron's
    `Stra2usClient.cpp`). HMAC over streamed bytes, not buffered.
  - **Error categorization & operator surfacing.** Recommended
    pattern: errlog ring buffer + heartbeat surfacing
    (`err=cat:detail`). Categories observed in critterchron: Net,
    OtaFetch, Boot, Other — useful starting set.
  - **Catalog interplay.** `ops_only`, `encrypted` flags are
    consumer-side hints, not server-enforced (with the exception of
    `encrypted` per-record flag in storage, which IS server-driven —
    distinguish carefully). Drift-test pattern enforcing call-site /
    catalog-entry agreement is recommended for the consumer's CI.
  - **Threading model.** Telemetry runs on its own thread/task,
    network I/O isolated from render path. Stack sizing matters
    (≥8KB for IR-OTA-enabled critterchron tel thread on Particle —
    see `debug_ota_hardfault_stack` memory note).
  - **Worked examples.** Pointers into the three reference
    implementations: `tools/stra2us_cli/client.py` (Python),
    `critterchron/hal/particle/src/Stra2usClient.{h,cpp}` (Particle
    C++), `critterchron/hal/esp32/src/Stra2usClient.{h,cpp}` (ESP32
    C++).

  Worth doing before any *fourth* client implementation. Not urgent
  while just the existing three are in play.

- ~~**Catalog edit UI: prefill input with current value.**~~ Landed
  2026-05-03. Each scope's input now prefills from `_fetchScopeValue`
  (which already drove the "Current:" display); string-typed vars
  render as `<textarea rows="2">` so long values
  (`brightness_schedule`, `wifi_password`) aren't truncated;
  `peek_kv`'s plaintext view of encrypted records is what the operator
  sees, since the admin holds the keys.

  *Show/hide for encrypted records also landed* (same day): three
  surfaces masked by default with Reveal toggles — dashboard KV
  editor, catalog per-scope editor, and the catalog "Current:" line.
  The Peek modal stays unmasked since it's an explicit "show me what's
  there" action.
