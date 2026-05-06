# TODO

## Near-term

- **Revisit top-level README for freestanding-repo visibility.** The
  README is now front-and-center on the GitHub repo page. The
  current structure was inherited from the monorepo and emphasizes
  feature/architecture detail. For a fresh visitor it should lead
  with: what this is, who it's for, a one-paragraph "what does it
  do," then "how to deploy" (which is now correct). Consider
  whether the C++ SDK + CLI client sections belong here or in their
  own docs. Lower priority — the README is functionally correct as
  of today's edits.

- **Rewrite `docs/admin_auth_architecture.md` to reflect post-v1.5
  reality.** Currently describes only the htpasswd + session-cookie
  flow and explicitly says the project operates "without bloated
  system dependencies like OAuth OIDC wrappers" — both untrue
  post-v1.5. Should now cover: hostname-aware middleware (browser
  host → OAuth, device host → htpasswd rescue), the `next=`
  round-trip cookie, OAuth callback issuing the same `admin_session`
  cookie that htpasswd does. Cross-reference
  `fr_v15_incremental.md` for phase context.

- **Rename `docs/staging.md` to clarify scope.** The file is
  about *local-host* bring-up for running the live test suite, not
  about the docker-based staging environment (which is in
  `docs/staging_environment.md`). The shared name is confusing.
  Suggested rename: `docs/local_dev.md` or `docs/host_dev_runtime.md`.

- **Audit `docs/fr_v15_auth.md` against shipped reality.** The
  original auth FR was written before implementation; some
  decisions changed during the rebuild (host naming, redirect URI
  shape, middleware structure). Either annotate sections that
  diverge with "see fr_v15_incremental.md for as-shipped" or do a
  cleanup pass to align.

- **Add copyright/license header to source files.** The repo has a
  LICENSE file (PolyForm Noncommercial 1.0.0) but individual source
  files lack a copyright/license notice. Add a one-line header to
  each authored source file (Python, JS, Bash). Suggested form:
  ```
  # Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
  # See LICENSE in the repo root.
  ```
  (`//` for JS, `#` for Python/Bash.) Mechanical pass — a small
  script that detects the right comment style by extension and
  inserts at line 1 (or after the shebang) is the easiest path.
  Skip vendored / third-party files (none today, but check).
  Not load-bearing for the license itself; helps anyone reading a
  single file see the terms without hunting for LICENSE.

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

- **Add an admin logout endpoint.** Today there's no way to sign
  out of either auth path. To clear an htpasswd session you have to
  delete the cookie AND fully quit Chrome (Basic Auth creds are
  cached per browser process); to clear an OAuth session you have
  to clear cookies and the 7-day `admin_session` cookie. Both make
  testing auth flows painful — discovered during Phase 4 verification.
  Build `GET /admin/logout`: clears `admin_session`, `oauth_state`,
  `oauth_redirect_to` cookies; returns a `WWW-Authenticate: Basic
  realm="logged-out"` response with a different realm string than
  the live one (Chrome treats different realms as different
  credential namespaces, busting the Basic Auth cache); add a small
  "Sign out" link in the admin UI header. ~15-30 lines total.

- **`tools/stage nuke` — reset staging Redis + re-seed.** Today
  staging Redis state persists across deploys (intentional —
  preserves provisioned clients, logs, ACLs between test cycles).
  When a schema change lands that's incompatible with the previous
  state, the operator needs a one-shot way to wipe staging and
  start clean. Shape: `tools/stage nuke` stops the staging stack,
  removes the `redis_data_staging` volume, brings staging back up,
  and re-runs `tools/stage seed-users`. Confirms with the operator
  before destroying state (no `--yes` shortcut by default; staging
  data is sometimes painful to recreate). Not needed today — filing
  so it exists when we hit the first incompatible schema change.

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

- **Build a staging environment.** Today, "rebuild" means rebuilding
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
