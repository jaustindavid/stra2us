# TODO

## Near-term

- **Generalize `widget: radio` to any enum-backed field.** Today
  `widget: radio` is gated by lint to `type: string` + `enum:` only;
  `type: int` + enum and `type: bool` (implicit `[true, false]`)
  both fall through to `<select>` with no radio option. The lint
  restriction is historical (radio was added during P0 specifically
  for the string-enum case), not principled — a binary flag rendered
  as a radio is a UX win regardless of whether the underlying value
  is `"on"`/`"off"` or `1`/`0` or `true`/`false`.

  **Design principle (this iteration): `<select>` is the default
  for every enum-having field; `widget: radio` is the explicit
  opt-in.** Predictable default, no auto-magic. Keeps the
  catalog-author's mental model simple: "want radios? say so."
  Avoids surprise behavior where a `type: bool` field
  unilaterally renders differently from a `type: int` field
  that's also enum-shaped.

  Two changes:
  1. **Lint** (`catalog_lint.py:_lint_field_widget`): replace
     `if var.type != "string"` with `if var.enum is None`. The
     requirement becomes "must have an enum to pick from" rather
     than "must be a specific type." `type: bool` doesn't need
     an explicit enum (it's implicit `[true, false]`); the lint
     can special-case bool to skip the enum-required check, OR
     `type: bool` can be modeled as having a synthetic enum
     during validation.
  2. **Renderer** (`widget_renderer.py`): the `widget == "radio"`
     dispatch fires for any enum-having field, regardless of
     `type`. `_render_radio` may need small tweaks to handle
     non-string values — `JSON.stringify` the value for the
     radio's `value` attribute; the form-submit decoder
     already does `json.loads` fallback so `"1"` round-trips
     as int `1`, `"true"` as bool `True`.

  Subsumes the earlier "type: bool should render as a radio"
  TODO — under the new design, `type: bool` renders as `<select>`
  by default (consistent with every other enum-field), and
  `widget: radio` opts in. Operator who wants a true/false radio
  writes:

  ```yaml
  enabled:
    type: bool
    default: false
    widget: radio
  ```

  ~30 lines + test updates. Touches lint + renderer + a couple
  existing tests that assert specific shapes.

- ~~**Document the `write_only: true` + multi-writer-race discipline.**~~
  Closed 2026-05-10 alongside v1.6.8. The acute version of the
  race (load-page + Save-without-touching = wipe-stored-value)
  was the data-loss bug v1.6.8 fixed architecturally — the
  customer-page render now populates the plaintext into
  `data-original` so the clean-submit branch round-trips the
  value rather than clobbering it. The v1.6.4 lint warnings
  were rewritten in v1.6.8 to reflect the new framing (the
  third warning's rationale shifted from "data-loss footgun"
  to "plaintext-in-HTML exposure"), which subsumes most of
  what this TODO was going to document. A narrower race
  remains (browser holds a stale render from before a CLI
  set, then submits) but it's now a recoverable inconvenience
  rather than silent data loss, and pursuing it would push
  toward the ETag/versioning territory we explicitly rejected.

- **Surface the running release version in the admin UI.**
  Today's "what's actually deployed?" answer requires SSHing
  to the host and running `tools/stage status` (or
  `git rev-parse --short HEAD` in `$PROD_DIR`). For routine
  "did my deploy go?" / "is staging on v1.6.6 yet?" questions,
  a visible version badge in the admin sidebar (or a small
  status panel) closes the loop without leaving the browser.
  This TODO was filed mid-debug after a missed deploy step
  left staging on v1.6.5 while v1.6.6 instrumentation was
  expected — a "Running: v1.6.5 (4b28654)" badge would have
  caught it instantly.

  Three implementation shapes to weigh:
  1. **Build-time env var.** `tools/stage promote` and
     `tools/stage deploy` pass `--build-arg RELEASE_TAG=<tag>`
     (or `<commit>`); Dockerfile bakes it as `ENV
     STRA2US_RELEASE=<value>`. Backend reads the env at
     startup, exposes via a `/api/admin/release` endpoint.
     Cleanest separation; survives container restarts;
     doesn't depend on the runtime tree.
  2. **Runtime git read.** Backend on startup runs
     `git -C /app rev-parse --short HEAD` against the
     bind-mounted `./backend`. Cheap (one syscall) but
     requires the `.git` directory accessible inside the
     container — the current volume mount may or may not
     include it depending on `.dockerignore` shape.
  3. **VERSION file.** `tools/stage` writes a one-line
     `./backend/VERSION` file at deploy time; backend
     reads it. Simplest; no Dockerfile changes; works
     regardless of git/.dockerignore situation.

  Recommendation: shape **#1** for production (env var is
  the canonical "build artifact carries identity" pattern)
  with shape **#3** as the staging fallback (since staging
  rebuilds on every push and we want the SHA, not just the
  tag). Render in admin sidebar footer near the Sign-out
  link, with the format `v<X.Y.Z> (<short-sha>)` — clickable
  to copy the full SHA, optional.

  Small scope: ~30 lines backend + ~10 lines frontend +
  build plumbing for #1. Pairs naturally with the cache-bust
  automation TODO since both are "deploy hygiene"
  improvements.

- **Extend v1.6.6 instrumentation to catch `HTTPException(500)` too.**
  v1.6.6's activity-log tagging works for raw exceptions
  (TypeError, RedisError, etc.) — those propagate up through
  `await call_next` and hit the middleware's `except Exception`
  block. But `raise HTTPException(status_code=500, ...)` calls
  get converted to a `Response(status_code=500)` by Starlette's
  inner ExceptionMiddleware *before* reaching the activity-log
  middleware. The middleware sees a normal Response, no
  exception bubbles, and the activity log entry stays bare
  `Error (500)` without the `[ExceptionClass]` tag.

  Fix: in the post-call_next path, when `response.status_code
  == 500` and `exc_name` is None, peek the response body (which
  Starlette has already serialized from the HTTPException's
  `detail`) and surface that. Or — cleaner — install a
  FastAPI exception handler for HTTPException that records
  the detail to `request.state` before letting Starlette
  convert it; the activity-log middleware reads that state
  on the way out.

  Either shape: ~15 lines + a test that exercises a route
  raising `HTTPException(status_code=500, detail="X")` and
  asserts the activity-log entry includes `[X]` or
  `[HTTPException]`. Closes the instrumentation gap surfaced
  during v1.6.6 monitoring.

- **[HIGH] Automate the `app.js?v=N` cache-bust.** Whenever
  `backend/src/static/app/app.js` changes, the `<script src=
  "/app/_static/app.js?v=N">` references in `device.html` and
  `landing.html` must be bumped to a new `N` — otherwise browsers
  and Cloudflare's edge cache the old `?v=N` URL and serve stale
  JS even after a fresh deploy. Operator discipline alone is
  insufficient: this footgun chewed ~30 minutes of v1.6.5
  verification when the bug-#1 (peek-while-typing) commit
  modified `app.js` without bumping the version, and on prior
  releases the same pattern bit at least twice (filed
  informally in `csp_admin_audit.md` and `fr_catalog_app_ui_progress.md`'s
  "Three diagnostic gotchas" section).

  Two fix shapes worth considering:

  1. **Pre-commit hook.** Lints the staged diff: if `app.js`
     changed but the `?v=N` references in `device.html` /
     `landing.html` didn't, error out with a clear message
     ("bump `?v=N` in landing.html + device.html before
     committing"). Blocks the bad commit at its source.
     Lightweight, no build-time machinery, no runtime cost.
     Works with the existing git workflow without changes to
     `tools/stage`.

  2. **Build-time hash injection.** A small step in the
     Dockerfile (or `tools/stage deploy`) that hashes
     `app.js`, replaces the `?v=N` token in the HTML files
     with `?v=<hash>`. Removes the manual bump entirely;
     hash changes whenever the file content changes,
     cache-bust is automatic. More invasive than #1 (touches
     build pipeline; the hash leaks into the served HTML)
     but eliminates the operator-discipline failure mode.

  Recommendation: ship **#1** as the first fix — it closes the
  bite immediately with minimal moving parts. **#2** is the
  proper long-term answer; defer until #1 has lived through
  a few releases and we know what edge cases it catches.

  Should ride along with whatever change next touches the
  static surface. The hook lives at `.git/hooks/pre-commit`
  (or `.githooks/pre-commit` if we standardize via
  `core.hooksPath` to share across machines). Detection
  shape: `git diff --cached --name-only` includes
  `backend/src/static/app/app.js` AND does NOT include
  one of `backend/src/static/app/{landing,device}.html`
  → fail with the bump-required message.

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
  **Hotfix v1.6.2 (2026-05-09):** v1.6.1 stamped the cutoff
  in *every* caller of `monitorClear`, including the chip-
  toggle handler and `openMonitor(topic)` from the dashboard
  — so opening Monitor on a topic always started blank instead
  of showing the recent stream tail. Split into `monitorClear`
  (stamps cutoff, wired to the Clear button only) and
  `_monitorResetFeed` (soft reset — DOM + `seenIds`, no
  cutoff stamp) used by the two internal callers.

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

- **Thorough responsive pass on the admin + customer surfaces.**
  *(Rescoped 2026-05-09 after a mobile walkthrough.)* The original
  entry framed the customer page as the priority and called admin
  mobile "lower-priority"; the actual mobile session inverted that
  ranking — the customer page (`/app/<app>/<device>/...`) was
  already perfectly usable (viewport meta + type-aware widgets +
  16px+ font sizes already shipped in the catalog FR), while the
  admin shell's fixed-width 260px sidebar dominated the viewport
  on a phone. v1.6.5 landed a tactical media query that stacks
  the admin shell column-style at <720px; the urgent mobile
  pain is closed. What remains here is the longer "thorough
  responsive pass" — modal sizing on narrow viewports, table
  overflow handling, breakpoints at 600/900 for tablets, real
  on-device verification on iOS Safari + Android Chrome
  (DevTools mobile emulation misses iOS-Safari-specific quirks).
  Defer until a real on-device pain point surfaces or a future
  release has bandwidth for a focused half-day.

- **Gate `/app/` landing form behind OAuth (close lookup_device
  enumeration).** Today `/api/app/lookup_device` is intentionally
  public — the customer-page landing form needs a name → app
  lookup *before* OAuth can gate the per-device page. An
  unauthenticated attacker can probe the endpoint and learn
  (a) whether a given device name exists, (b) which app it
  belongs to. The endpoint's docstring (`routes_app.py:213`)
  flags this and suggests Cloudflare Turnstile / CAPTCHA at the
  edge as the mitigation; OAuth at the application layer
  achieves the same outcome with no third-party dependency.

  Shape (per the v1.6.8 design discussion):

  1. **Add `/app/` to the auth-required paths** in `main.py`'s
     auth middleware. Currently the landing page itself is
     public; under this change, an unauthed visitor gets
     redirected to `/oauth/google/login?next=/app/` before
     seeing the form. Subsequent visits within the OAuth
     session cookie lifetime are silent — one roundtrip per
     session, not per visit.
  2. **`/api/app/lookup_device` becomes implicitly auth-gated**
     by sharing the same `/app/` prefix path. Update its
     docstring to reflect the new model (drop the "Public — no
     auth required" framing + the Turnstile reference).
  3. **Per-device page** (`/app/<app>/<device>`) already does
     OAuth + ACL check; unchanged.

  Threat-model delta: enumeration goes from "anyone with
  internet" → "anyone with an OAuth-allowlisted Google account."
  Allowlisted accounts are people the operator already trusts
  enough to grant some ACL; even those are subject to Google's
  rate-limiting and account-reputation systems. Automated
  scraping by un-allowlisted bots becomes effectively impossible.

  UX cost: an unauthed visitor sees one OAuth login roundtrip
  before the landing form. No reduction in capability for
  legitimate users; first-time visitors see the same Google
  login they'd see on the per-device page anyway.

  Implementation scope: ~20 lines (middleware path-pattern
  addition + docstring updates) + a couple test adjustments
  (verify a no-cookie request to `/app/` and to
  `/api/app/lookup_device` 302s to OAuth instead of returning
  a result). Pairs naturally with the Basic Auth brute-force
  lockout TODO below — both are auth-surface tightening, can
  ship together or separately.

  Considered and rejected: option B (keep `/app/` public,
  detect 401 from lookup_device in JS, redirect from there).
  More JS code, preserves the "see the form without auth"
  UX that nobody asked for. Option A (this TODO) is cleaner.

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

- ~~**Device picker dedupe doesn't recognize wildcard coverage.**~~
  Landed 2026-05-09 in v1.6.3. New `_aclPrefixCovers(prefix, path)`
  helper in `backend/src/static/app.js` mirrors the backend's
  `_prefix_matches` (`*`, exact, parent-prefix). Three sites
  switched from `p.prefix === token` to
  `_aclPrefixCovers(p.prefix, token)`: the picker's "already
  granted" rendering predicate, and the two dedupe checks in
  `confirmDevicePicker` (the `<app>/<device>:rw` push and the
  `<app>/public:r` push). A user with `*:rw` now sees every
  device as already granted, and re-applying the picker no
  longer pushes redundant rows.

- ~~**Device picker modal overflows the viewport with many devices.**~~
  Landed 2026-05-09 in v1.6.3. `#devicePickerModal .modal-content`
  capped at `max-height: 90vh` + `display: flex; flex-direction:
  column`; `#devicePickerModal .device-picker-list` switched to
  `flex: 1 1 auto; min-height: 0` so the list shrinks instead of
  pushing the action buttons past the viewport bottom. Scoped to
  the device picker only — other modals are unchanged.

- ~~**"Last seen just now ago" on single-device screen.**~~ Landed
  2026-05-09 in v1.6.3. Moved the " ago" suffix into `formatAge`
  (`backend/src/static/app/app.js`) so the sub-5s case keeps
  returning "just now" while the 5s+ branches return "<N><unit>
  ago"; both callers (`Last seen ${...}` and the activity-row
  `<span class="activity-when">`) dropped their hardcoded " ago".

- ~~**Form-submit stuffs catalog defaults into every untouched field.**~~
  Landed 2026-05-10 in v1.6.7. `ResolvedValue.from_default` plumbed
  through the renderer as `data-from-default="true"` on the input;
  touched-state serializer skips clean+from-default fields, so the
  resolution chain (per-device → app-scope → catalog default) keeps
  producing the same value on the next page load rather than
  materializing per-device overrides for every untouched field.

  *Original entry preserved below for history.*

  Observed on the customer device page during v1.6.5 verification:
  the operator edited one field (wifi_password), clicked Save, and
  every *other* field on the form ended up with its catalog
  `default:` value persisted to per-device KV — including fields
  the operator never touched. Intended behavior is touched-only
  writes: untouched fields stay at their resolution-chain value
  (catalog default → app-scope → device override) and don't
  materialize a per-device override on save.

  Likely cause is in the touched-state serializer's clean-field
  branch (`backend/src/static/app/forms/touched_state.js` —
  comment block at lines ~21-22): *"dirty == false → `data-original`
  value goes through verbatim."* `data-original` for a field whose
  current value came from the catalog default (`from_default=True`
  in `value_resolver.ResolvedValue`) is set to that default
  string, so the serializer emits it, the form-submit writes it,
  and the device's KV gets a stale-on-arrival per-device override.

  Two-part fix to consider:
  1. **Renderer side.** Emit a `data-from-default="true"` attribute
     on inputs whose current value came via step 3 of the
     resolution chain. Mirrors what the page already knows about
     the source of `current`.
  2. **Serializer side.** Skip clean fields with
     `data-from-default="true"` from the form-submit payload —
     they shouldn't materialize a per-device override the
     operator didn't ask for. Dirty `data-from-default` fields
     still go through (operator explicitly chose to override
     the default).

  ~30 lines + tests. Worth a careful pass through the FR's
  P4 spec to make sure we're not undoing an intentional design
  choice; the spec text and the observed behavior may diverge.

- ~~**Customer app page 404s on `/favicon.ico`.**~~
  Landed 2026-05-10 in v1.6.7. New default favicon at
  `backend/src/static/app/favicon.png` (256×256 PNG downsized
  from the admin's 2048×2048 source). `<link rel="icon">` emitted
  in both `landing.html` and `device.html`. Per-app override via
  `theme.favicon_asset` in the catalog Theme model — validated by
  `catalog_lint` and substituted into the device page via a new
  `{{FAVICON_HREF}}` template placeholder. Lint also counts
  `favicon_asset` as referenced so the unused-asset warning
  doesn't false-positive on per-app favicons.

  *Original entry preserved below for history.*

  Browsers
  speculatively request `/favicon.ico` from the page's origin;
  the customer app serves none, so the console shows a 404
  (cosmetic but noisy, and operators reading the console for
  real errors get false-positive noise). Two-part fix to match
  the catalog-driven theming intent:
  1. **Per-app favicon from the catalog.** If the catalog
     declares one (parallel to `theme.logo_asset` — maybe
     `theme.favicon_asset`), serve that for `/favicon.ico` (or
     more correctly, emit `<link rel="icon" href="/app/<app>/_assets/<file>">`
     in `device.html` / `landing.html`). Means the customer's
     browser tab + bookmark icon match the product brand.
  2. **Default fallback.** When no catalog favicon is set,
     serve a default Stra2us icon (or a 1x1 transparent
     PNG so the browser's request still resolves to a 200).
     Either ship a default `favicon.ico` at `/app/_static/`
     and emit the `<link rel="icon">` unconditionally pointing
     to it (catalog favicon overrides), or wire a route that
     redirects /favicon.ico to the right asset. Either ends
     the 404 noise.
  Small scope (~30 lines), pairs naturally with the existing
  `theme.logo_asset` work in the catalog FR.

- ~~**`lookup_device` doesn't find provisioned-but-unwritten devices.**~~
  Landed 2026-05-10 in v1.6.7. New `device_to_app:<client_id>`
  reverse index, written at provision time by `provision_device`
  and cleared at revoke time. `lookup_device` consults it first
  (O(1) GET) and falls back to the SCAN for legacy devices,
  backfilling the index entry on SCAN hit so the legacy population
  self-heals. Closes both the workflow gap (provision → configure
  → flash now works) and the docstring's pre-v1.6.7 perf note
  about scan-on-demand being O(N).

  *Original entry preserved below for history.*

  `provision_device` (`backend/src/api/routes_admin.py:193`) creates
  the device's HMAC secret + ACL but writes zero KV records.
  `lookup_device` (`backend/src/api/routes_app.py:208`) establishes
  "device exists" by scanning `kv:*/<name>/*` — so a device that's
  been provisioned but hasn't done its first KV write yet
  (no heartbeat, no admin-side `stra2us set`) returns 404 from the
  customer landing form's name lookup. Workaround for now: write
  any KV value through admin UI / CLI to make the device
  discoverable.

  Fix: write a `device_to_app:<client_id>` reverse index at
  provision time (already foreshadowed in `lookup_device`'s
  docstring as the perf fix for scan-on-demand). `lookup_device`
  consults the reverse index first (O(1)), falls back to KV scan
  for legacy devices provisioned before the fix. Both bugs (this
  one + the perf concern) close in one change. ~30 lines + tests.

- ~~**`stra2us set` should honor the catalog's `encrypted:` field, not the `--encrypted` CLI flag.**~~
  Landed 2026-05-10 in v1.6.7. `cmd_set` now reads `var.encrypted`
  from the loaded catalog and ignores the `--encrypted` flag.
  Mismatches surface as stderr diagnostics (warning when operator
  passed the flag against a catalog that says no; info when omitted
  on a catalog that says yes). The raw-KV `stra2us put` path is
  unchanged — no catalog consulted, `--encrypted` operator-controlled.
  `Var.encrypted` field comment updated to reflect that Stra2us now
  acts on it; help text on `set --encrypted` marks the flag as
  deprecated for catalog keys.

  *Original entry preserved below for history.*

  Today the CLI's catalog-aware
  write path (`cmd_set` in `tools/stra2us_cli/cli.py:581`) passes
  `encrypted=args.encrypted` straight through to the wire — the
  operator picks whether to encrypt at write time, and the
  catalog's `encrypted: true` is a documentation-only hint
  (`Var.encrypted` comment in `catalog.py:100-107`: *"Stra2us
  itself does not act on this field"*). That's the wrong split
  of responsibilities: an operator setting a catalog-declared
  key should not be able to skip the encryption the catalog
  declares, and shouldn't have to remember to pass a flag to
  match the catalog's own metadata.

  Proposed split:
  1. **`stra2us set <target> <key> <value>` (catalog-aware).** The
     catalog is the source of truth; encrypt iff
     `cat.vars[key].encrypted` is true. The `--encrypted` CLI flag
     becomes a no-op (deprecate with a warning, or remove
     outright in a major bump). The catalog hint stops being a
     hint and becomes an enforced contract.
  2. **`stra2us put <key> <value>` (raw KV, no catalog).** No
     catalog consulted; `--encrypted` stays operator-controlled.
     Same shape as today. This is the escape hatch for
     non-catalog data and for clients implementing their own
     encryption discipline.

  Knock-on changes:
  * Update the `Var.encrypted` field comment in
    `tools/stra2us_cli/catalog.py:100-107` — it currently says
    "Stra2us itself does not act on this field," which becomes
    untrue.
  * The v1.6.4 secret-pairing lint (warns on `widget: secret`
    without `encrypted: true`) keeps making sense as
    catalog-author guidance; this TODO closes the loop on the
    write side so the catalog's `encrypted: true` is no longer
    a paperwork promise.
  * Existing data that's `:enc=false` despite the catalog saying
    `encrypted: true` becomes a recognizable drift pattern —
    file a separate "republish + re-set encrypted-flagged
    catalog values" runbook entry the first time we hit it in
    practice.

  Rough scope: small (~10 lines in `cmd_set`, plus a deprecation
  warning + comment update + 1-2 tests). Could ship as a
  standalone v1.6.6 or fold into whatever the next CLI-touching
  release is.

- ~~**Catalogs view lists asset keys as if they were catalogs.**~~
  Landed 2026-05-09 in v1.6.5. `fetchCatalogList()` in
  `backend/src/static/app.js` now filters the `/kv_scan` results
  to keys whose tail past `_catalog/` contains no `/` — bare
  `_catalog/<app>` only. Mirrors the catalog-vs-asset shape rule
  documented in `routes_device.py:22-26` (catalogs are exactly
  two segments; 3+ segments are asset payloads).

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
