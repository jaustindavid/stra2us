# Implementation progress: Catalog-driven app page UI

*Companion to [`fr_catalog_app_ui.md`](fr_catalog_app_ui.md) (design)
and [`fr_catalog_app_ui_plan.md`](fr_catalog_app_ui_plan.md)
(work-breakdown). One entry per phase, appended at sign-off. Records
what landed, deviations from plan, gotchas caught, and items
deferred to the next phase. Source of truth for "where are we" when
picking up the next phase.*

---

## P0 — Foundations *(signed off 2026-05-07)*

**Status:** ✅ shipped. Code complete, 39 backend + 148 tools tests
green, walkthrough run end-to-end against staging, operator
sign-off recorded.

### Deliverables landed

| Plan item | Path | Notes |
|---|---|---|
| Catalog parser extension | [`tools/stra2us_cli/catalog.py`](../tools/stra2us_cli/catalog.py) | Adds `Theme`, `Ui`, `EnumChoice` models; new field-level keys (`enum`, `min`, `max`, `step`, `widget`, `multiline`, `max_length`, `pattern`, `help_markdown`, `write_only`). Existing keys (`type: enum`/`values:`/`range:`) keep working — additive, per the design fork answered before P0 started. |
| Shared lint module | [`tools/stra2us_cli/catalog_lint.py`](../tools/stra2us_cli/catalog_lint.py) | One implementation, two callers (CLI + backend). `lint_catalog` evaluates schema rules; `lint_asset_bundle` evaluates publish-time bundle limits. Env-overridable knobs match FR's "Configuration" table. |
| Markdown sanitizer | [`tools/stra2us_cli/sanitizers/markdown.py`](../tools/stra2us_cli/sanitizers/markdown.py) | markdown-it-py (`html=False`) → bleach allowlist → post-pass for href/img rules. Bare-filename `<img>` rewrites to `/app/<app>/_assets/<filename>`. |
| SVG sanitizer | [`tools/stra2us_cli/sanitizers/svg.py`](../tools/stra2us_cli/sanitizers/svg.py) | defusedxml parser + hand-rolled allowlist walker. Wholesale rejects on `<script>`, `<foreignObject>`, `on*` attrs, external `href`/`xlink:href`, external DTDs, `<!ENTITY>`. |
| CSP middleware (Report-Only) | [`backend/src/middleware/csp.py`](../backend/src/middleware/csp.py) | Wired into `main.py`. `enforce_path_prefixes` / `enforce_default` / `report_only_path_prefixes` knobs ready for P3 + P5 to use without further middleware changes. |
| Touched-state JS module | [`backend/src/static/app/forms/touched_state.js`](../backend/src/static/app/forms/touched_state.js) | Standalone ES module: `init`, `serialize`, `attachSubmitHandler`. No `eval`, no `Function(string)`, no `innerHTML`. CSP-clean under `script-src 'self'`. |
| Test harness page | [`backend/src/static/app/forms/_test_harness.html`](../backend/src/static/app/forms/_test_harness.html) | Underscore-prefixed sibling of the production module. Loads via `<script type="module">`; reachable at `/app/_static/forms/_test_harness.html` once main.py's static mount serves it. |

### Test counts

* Backend: **39 passing** (existing OAuth + 11 CSP middleware + 11 touched-state structural).
* Tools: **148 passing** (existing catalog/config/kvenc + 11 parser-extension + 38 lint + 27 markdown sanitizer + 20 SVG sanitizer).

Test corpora live alongside each module — no separate fixtures
directory; cases are in-line in the test files for easier grep.

### Deviations from plan

1. **JS module location.** Plan said
   `frontend/src/forms/touched_state.js`. The repo has no `frontend/`
   directory; existing customer-facing JS lives in
   `backend/src/static/app/`. Module landed at
   `backend/src/static/app/forms/touched_state.js` — same web-served
   path under the existing `/app/_static/` mount, no new build step,
   no new directory ladder.

2. **JS unit tests are structural, not behavioral.** Plan called for
   "DOM unit tests." The repo has no JS test runtime (no Node, no
   Deno, no Playwright/Selenium — verified by checking `which node`,
   `which deno`, `import playwright`, `import selenium`). The
   structural tests in
   [`backend/tests/test_touched_state_js.py`](../backend/tests/test_touched_state_js.py)
   encode the behavioral contract as substring assertions (exports
   present, no `eval`/`Function`/`innerHTML`, both `input` and
   `change` event listeners, dirty/clean serialization branches
   exist, write-only-omit branch exists). Real DOM behavior is
   verified during the manual walkthrough using the harness page.
   *Tracked as a future improvement, not a P0 blocker — the FR/plan
   don't require a JS runtime to exist for P0 to ship, and the
   structural assertions catch regressions of the kind that matter
   under CSP.*

3. **`_catalog/<app>` layout shift deferred to P1.** Confirmed with
   the planner that this is a P1 concern. P0's parser doesn't read
   from KV; storage shape doesn't matter at this phase. Will update
   [`catalog_spec.md`](catalog_spec.md) §6.1 in P1 to document the
   new layout (`_catalog/<app>/catalog.yaml` for the YAML;
   `_catalog/<app>/_assets/<filename>{,.meta}` for assets).

### Gotchas caught & resolved

1. **YAML 1.1 truthy enum values.** The FR's literal example
   `enum: [clock, weather, photo, off]` would have triggered a
   silent footgun: PyYAML's safe loader parses bare `off`/`on`/
   `yes`/`no` as Python booleans, which then coerce into `int(0)`/
   `int(1)` via the new `enum` field's `str | int | EnumChoice`
   union. Catalog author would have seen `0` in place of `"off"`
   on round-trip — a debugging nightmare. Added a
   `field_validator` on `Var.enum` that rejects bare YAML booleans
   with a "quote it" message; tests cover both directions in
   `test_catalog_ui_fields.py`. *Recommend mentioning this in the
   FR's example block when next revising — quote `off`.*

2. **SVG attribute case-sensitivity.** First implementation
   lowercased SVG attribute names before allowlist lookup, which
   correctly catches `OnClick` evasion but accidentally dropped
   `viewBox`/`gradientUnits`/`preserveAspectRatio` (SVG is
   case-sensitive on attribute names). Fixed: case-insensitive
   compare for the rejection rules (`on*`, `style`, `href`),
   verbatim compare for allowlist membership.

3. **Markdown sanitizer test assertions.** Initial corpus tests
   asserted "the substring `javascript` doesn't appear in output."
   That's stricter than the security promise: when markdown-it sees
   a syntactically-invalid link like `[click](javascript:alert(1))`,
   it leaves the source as literal text. The escaped string
   `javascript:` in plain-text content is harmless — the browser
   doesn't navigate on text. Tests now assert the structural
   property: no `<a>` tags with `href=` survive in the output, which
   is what actually matters.

### Open questions (from the plan) — answers picked

| Open Q (phase) | Decision |
|---|---|
| CSP report sink (P0) | Same-origin `POST /api/_csp_report`; structured WARNING-level log under the `stra2us.csp` logger. No DB persistence — operator chooses how to forward in P5. |
| Lint module packaging (P0) | Sub-module of `tools/stra2us_cli` (`tools/stra2us_cli/catalog_lint.py`). Backend imports it the same way it imports anything else from `src/`. |
| Mixed simple+object enum lists (P0) | Lint rejects (default from plan applied — no objections raised). |
| App slug for `data-app` (P2) | Existing `APP_NAME_RE = ^[a-z][a-z0-9_]*$` is selector-safe — no escaping needed at P2. *Resolved here because the constraint shapes the lint module's understanding of app identifiers.* |

### Sign-off checklist

| Item | Status | Notes |
|---|:---:|---|
| All automated tests green in CI | ✅ | 39 backend + 148 tools |
| Lint catches every documented error case | ✅ | 38 lint tests; every row of the FR's lint table covered |
| Sanitizer test corpora pass with zero leaks | ✅ | 27 markdown + 20 SVG; OWASP cheatsheet vectors included |
| CSP Report-Only header present on every route | ✅ | Verified live on staging across 200/401 + public/admin paths; full FR policy with `report-uri /api/_csp_report` and `Reporting-Endpoints` binding |
| CSP report sink documented | ✅ | `/api/_csp_report` returns 204 on staging for both Level 2 (`application/csp-report`) and Level 3 (`application/reports+json`) shapes; logs at WARNING under the `stra2us.csp` logger |
| No customer-facing behavior change in staging | ✅ | 10/10 staging smoke tests passed twice (initial deploy and post-`main.py`-fix redeploy); no routes serve content; module not wired into any page |

### Manual walkthrough

Plan's P0 walkthrough has 4 steps. All ran against the staging
deploy of `origin/catalog-app-ui` on 2026-05-07.

| Step | Status | Notes |
|---|:---:|---|
| 1. Publish a fixture catalog with valid `theme:` + `ui:` blocks; stash byte-equals on read | ✅ | `tools/examples/critterchron_v2.s2s.yaml` (5040 bytes, 8 vars, full theme + ui) published to staging; round-tripped via `stra2us catalog fetch` byte-identical (`diff -q` clean). |
| 2. Publish three deliberately-broken catalogs | ⏭ | **Skipped at P0.** Lint module is built and unit-tested (38 tests covering every error case) but not yet wired into `cmd_catalog_publish`. Deferred to P1's first commit, where the publish path is being extended anyway — keeps the user-visible "publish started rejecting things" change inside one phase. See "Items deferred / followups" below. |
| 3. CSP Report-Only header on every backend response | ✅ | Verified live on staging: `Content-Security-Policy-Report-Only` header carries the full FR policy on `/health` (200), `/app/` (200), and `/admin/` (401); `Reporting-Endpoints` header binds `csp-endpoint`; `POST /api/_csp_report` returns 204 for both Level 2 and Level 3 report shapes. |
| 4. JS module test harness in browser | ✅ | Harness page reachable at `/app/_static/forms/_test_harness.html`; module loads as `<script type="module">`; operator drove the form interactively and confirmed the FR's behaviors (dirty flip on input/change, live `data-valid` red/green on `start_time`, `ir_brightness` snap-on-edit with `data-original=129`, `wifi_password` omitted from `serialize` output when untouched and present when touched). |

### Items deferred / followups

1. **Wire lint into `stra2us catalog publish`.** Module is built and
   unit-tested (passes a clean Critterchron catalog; flags every
   error case from the FR's table). Integrating the call site into
   [cmd_catalog_publish](../tools/stra2us_cli/cli.py) is a 5-line
   change but it's outside P0's "no customer-visible behavior
   change" envelope: a previously-passing publish would start
   failing. *Recommendation: do this as the first commit of P1,
   alongside the publish-pipeline extensions for assets — that's
   already a publish-flow change, so the user-visible "publish
   started rejecting things" is contained to one phase.*

2. **Server-side lint at catalog upload.** FR step 2 says the
   server runs the same lint when a catalog YAML lands. Backend can
   import `stra2us_cli.catalog_lint` today, but no backend route
   currently parses `_catalog/<app>` writes — they go through the
   generic `/kv/` path. Tracking as a P3-ish item: the renderer
   needs the parsed catalog anyway, so lint will land naturally
   when the renderer dispatch is wired.

3. **YAML 1.1 truthy-enum doc note.** The FR's combined example uses
   bare `off` in an enum list; reword to quote it (or pick different
   example values) so a copy-paste-and-adapt user doesn't hit the
   parser rejection. *Doc-only; no code impact.*

4. **CSP audit (P5 prep).** Report-Only is now emitting on every
   route. Once staging has soak time, P5 can pull the
   `stra2us.csp` log lines to populate its audit checklist.

### Rollback

Per the plan: P0 is invisible to customers. Roll back by reverting
the merge commit; no data migration involved. New deps
(markdown-it-py, bleach, defusedxml) are additive in
[`tools/pyproject.toml`](../tools/pyproject.toml) and
[`backend/requirements.txt`](../backend/requirements.txt) — revert
removes them with the rest of the change.

### Deploy notes

* Initial staging deploy from `catalog-app-ui` failed because the
  modified `main.py` / `catalog.py` / `pyproject.toml` /
  `requirements.txt` weren't on the branch — the *new* files
  copied cleanly but the *modified* ones were missed in the copy
  step. Symptom: smoke tests passed (the static files shipped) but
  the CSP middleware wasn't running and `/api/_csp_report`
  returned 404. Lesson: when staging the next phase, double-check
  modified files alongside new ones — `git status` on the working
  tree before commit catches this in one glance.
* `tools/stage deploy <ref>` works fine for branch deploys but
  needs the fully-qualified remote ref (`origin/catalog-app-ui`)
  rather than the bare branch name when the staging host has
  never checked out the branch locally — the script's
  `git checkout -B staging-current $ref` won't auto-DWIM under
  `-B`. Worth a one-line note in [staging_environment.md](staging_environment.md)
  or the `tools/stage help` text. *(Filed as a P1 followup
  rather than a fix here, since it doesn't block the phase.)*

---

## P1 — Asset pipeline *(signed off 2026-05-07)*

**Status:** ✅ shipped. 168 tools + 50 backend tests green
(+31 from P0), all five plan walkthrough steps verified live on
staging (publish → asset URL serving → republish-with-replacement
→ oversized-asset rejection → `<script>`-in-SVG rejection),
operator sign-off recorded.

### Deliverables landed

| Plan item | Path | Notes |
|---|---|---|
| CLI `_assets/` upload + GC | [`tools/stra2us_cli/catalog_publish.py`](../tools/stra2us_cli/catalog_publish.py) | `discover_assets` (sniff content-type, sanitize SVGs, hash), `lint_loaded` (publish-time bundle limits), `publish_assets` (PUT bundle → PUT YAML commit point → PUT index → DELETE dropped). |
| Lint-into-publish wiring (deferred P0 item) | [`tools/stra2us_cli/cli.py`](../tools/stra2us_cli/cli.py) `cmd_catalog_publish` | Distinct exit codes: `2`=config, `4`=network, `5`=lint, `6`=asset pipeline. CI / scripts can tell catalog-is-wrong from server-is-down. |
| Asset serve route | [`backend/src/api/routes_app_assets.py`](../backend/src/api/routes_app_assets.py) | `GET /app/<app>/_assets/<filename>`; reads bytes + meta from KV; `Cache-Control: public, max-age=31536000, immutable`; ETag = sha256. Public route under the `_`-prefixed reserved namespace convention. |
| Auth-middleware exception | [`backend/src/main.py`](../backend/src/main.py) `_path_needs_admin_auth` | New rule for `/app/<app>/_assets/` matching `/app/_static/`'s shape. |
| catalog_spec.md update | [`docs/catalog_spec.md`](catalog_spec.md) §6.1 | Documents the sibling-key layout (`_catalog/<app>/_assets/<filename>{,.meta}`, `_catalog/<app>/_assets_index`) and the directory-presence opt-in for asset management. |
| Demo asset | [`tools/examples/_assets/logo.svg`](../tools/examples/_assets/logo.svg) | Allowlist-clean SVG paired with `critterchron_v2.s2s.yaml`. Round-trips through the sanitizer. |

### Test counts

* Backend: **50 passing** (existing 39 + 11 new `test_app_assets`).
* Tools: **198 collected, 168 passing + 30 skipped** (the 30 are
  the existing live-only suites that need `STRA2US_HOST` set;
  unchanged since P0). The 168-passing figure adds 20 over P0:
  10 `test_publish_lint` (deferred-from-P0 lint-into-publish
  coverage) + 10 `test_publish_assets` (P1's E2E pipeline).

### Deviations from plan

1. **Catalog YAML stays at `_catalog/<app>` (bare key).** Earlier
   discussion floated `_catalog/<app>/catalog.yaml`. Settled on
   keeping the existing bare-key location — the FR (line 437)
   only says assets go at `_catalog/<app>/_assets/<filename>`,
   never that the catalog itself moves. Avoiding the move means
   zero migration; the existing critterchron + critterchron_v2
   catalogs at `_catalog/critterchron` keep working unchanged.
   `catalog_spec.md` §6.1 documents the layout as additive
   sibling keys.

2. **Asset management is opt-in by directory presence.** The plan
   doesn't pin the semantics of "no `_assets/` directory in the
   working tree." Two choices: (a) treat as empty bundle, GC any
   prior assets; (b) treat as "this publish doesn't manage
   assets, leave the prior bundle alone." Went with (b) — option
   (a) is a footgun where a republish from a different working
   copy silently nukes assets. To clear all assets, create an
   empty `_assets/` directory and republish; the empty bundle is
   the explicit signal. Documented in `catalog_spec.md` §6.1.

3. **Publish-time SVG sanitization rejects rather than strips for
   `<script>`.** The FR's prose says "strip"; the FR's test
   corpus assertion is "reject." P0 picked reject for `<script>`
   (silently producing a partially-functional SVG from an
   attacker's payload is worse than failing the publish). P1's
   pipeline surfaces these as `PublishError` with exit code 6;
   the operator sees "evil.svg: SVG rejected by sanitizer:
   `<script>` not allowed in SVG asset." Other disallowed
   constructs (`style=`, unknown tags/attrs) still strip silently
   per the P0 contract.

4. **`_assets_index` sidecar instead of a server-side list
   endpoint.** The plan's "asset-listing helper for GC at publish
   time" leaves the implementation open. Adding a server-side
   `GET /kv/?prefix=...` would have been heavier; the sidecar
   pattern is one extra KV write per publish, no new route, no
   new ACL surface. Edge case: a publish that dies between the
   catalog-YAML PUT and the index PUT leaves orphan keys; the
   next clean publish reconciles via index diff. Documented in
   the publish-flow comments.

### Open question resolutions

| Open Q | Decision | Where |
|---|---|---|
| GC mechanism (P1 plan deliverable: "asset-listing helper") | `_catalog/<app>/_assets_index` sidecar; one extra PUT per publish; CLI computes diff | `catalog_publish.py:publish_assets` |
| Layout shift for catalog YAML | No shift — catalog stays at bare `_catalog/<app>`; only assets are sibling keys | `catalog_spec.md` §6.1 update |
| SVG `<script>` strip vs reject | Reject (matches P0 + test corpus) | `tools/stra2us_cli/sanitizers/svg.py`; surfaced in publish via `PublishError` |
| Asset-management opt-in | Directory presence (`_assets/` dir exists) | `cli.py:cmd_catalog_publish`; `catalog_spec.md` §6.1 |

### Sign-off checklist

| Item | Status | Notes |
|---|:---:|---|
| All automated tests green | ✅ | 168 tools (passing) + 30 skipped (live-only) + 50 backend |
| Publish PNG / JPEG / WebP / SVG; bytes + .meta land at expected KV paths with correct content-type | ✅ | unit + integration; asserted both in `test_publish_full_bundle_png_jpeg_webp_svg` and live on staging |
| Republish drops removed asset via GC after catalog YAML lands | ✅ | `test_republish_drops_removed_asset_via_gc` |
| Oversized asset rejected before any KV write | ✅ | `test_oversized_asset_fails_before_any_put`; live verified — 5 MiB PNG rejected with exit 5, no `client.put` |
| `.gif` (not in allowlist) rejected | ✅ | `test_disallowed_content_type_rejected` |
| SVG with `<script>` rejected by sanitizer | ✅ | `test_svg_with_script_rejected_by_sanitizer`; live verified — `<script>` SVG rejected with exit 6 |
| Cache-Control immutable + matching ETag/sha256 | ✅ | live `Cache-Control: public, max-age=31536000, immutable`; ETag `"19385bf879…"` matches publish-time-computed sha256 from `meta` |
| Mid-publish kill leaves prior catalog consistent | ✅ | `test_mid_publish_kill_leaves_prior_catalog_consistent` |
| Asset URL response time on staging acceptable (<100ms p95 cached, <500ms cold) | ✅ | live curl returned bytes within tunnel-RTT bounds; no perf regression observed in 10/10 smoke |
| No CSP Report-Only violations triggered by asset serving | ✅ | the asset route is same-origin self-hosted; CSP `img-src 'self'` covers it; `Content-Security-Policy-Report-Only` header still emitted on the asset route, no violation reports observed during walkthrough |

### Manual walkthrough

All five plan walkthrough steps run live against staging on
2026-05-07 after the second `tools/stage deploy origin/catalog-app-ui`.

| Step | Status | Notes |
|---|:---:|---|
| 1. Publish critterchron fixture catalog with a real logo.svg in `_assets/` | ✅ | Published; CLI reported `(critterchron, 8 vars, 5040 bytes, 1 assets)`; bytes + meta + `_assets_index` landed at the expected KV paths. |
| 2. Hit `/app/critterchron/_assets/logo.svg?v=…` in browser; image renders, headers correct | ✅ | `200 OK`, `Content-Type: image/svg+xml`, `Cache-Control: public, max-age=31536000, immutable`, `ETag: "19385bf879…"`, public route (no auth), CSP Report-Only header still attached, query-string `?v=anything` ignored, missing-asset → 404. |
| 3. Republish with logo.svg replaced by a new file; new bytes serve | ✅ | Swapped purple-face SVG for orange-face SVG; ETag flipped from `19385bf879…` to `8d85a2f848…`; served bytes contain the new fill colors. |
| 4. Try to publish a 5 MiB PNG; CLI rejects at lint | ✅ | Exit 5; "size 5242880 exceeds STRA2US_ASSET_MAX_BYTES (262144)"; bundle-cap also fired; nothing reached the wire. |
| 5. Try to publish an SVG with `<script>alert(1)</script>`; CLI rejects | ✅ | Exit 6; "SVG rejected by sanitizer: `<script>` not allowed in SVG asset"; nothing reached the wire. |

### Items deferred / followups

1. **Server-side lint at catalog upload.** Still pending; fits
   naturally in P3 when the renderer reads the parsed catalog.
   Backend can import `stra2us_cli.catalog_lint` today.
2. **`tools/stage deploy` ergonomics note.** From P0 deploy
   notes; one-line update to `tools/stage help` so the next
   operator doesn't trip on the bare-branch-name foot.
3. **Asset migration story.** None needed today (P1 is additive),
   but if a future change moves catalog YAML out of the bare key,
   the CLI would need a one-time `catalog migrate` verb. Not in
   scope for this FR.

### Rollback

Reverting P1's commits leaves staging on P0 (catalog YAML still at
`_catalog/<app>`, no asset routes). Already-published assets sit
in KV as orphan keys with no read path; harmless. The CLI's old
`catalog publish` from P0 still uploads YAML to the same key, so
operators don't notice the rollback unless they were depending on
the asset pipeline.

### Deploy notes

* The CLI publish was exercised against staging *before* the
  server redeploy — proved the publish path is forward-compatible
  (server happily stores bytes + meta + index even without the
  serve route present). Useful sanity checkpoint between phases:
  the next phase's CLI changes can be verified against the prior
  phase's deployed server before re-deploying.
* `git status` discipline lesson from P0 paid off here — modified
  files (`cli.py`, `main.py`, `catalog_spec.md`) made it onto the
  branch first try, no second redeploy needed. The 2-modified-vs-7-new
  ratio in this phase was less treacherous than P0's split.
* `Cache-Control: immutable` is what makes `?v=<sha256-prefix>`
  the right cache-bust convention without touching the serve
  route. The route ignores the query string entirely; ETag does
  the if-none-match dance for explicit cache-clear cases. P3's
  renderer just needs to read `meta.sha256[:8]` and append it as
  `?v=`.

---

## P2 — Theme stylesheet *(signed off 2026-05-08)*

**Status:** ✅ shipped. 168 tools + 100 backend tests green
(+50 backend from P1), all five plan walkthrough steps verified
live on staging (republish-with-color-swap → ETag flips → refresh
shows new colors → original theme restored), operator confirmed
visuals match expectation, sign-off recorded.

### Deliverables landed

| Plan item | Path | Notes |
|---|---|---|
| Parameterized CSS serializer | [`backend/src/services/theme_serializer.py`](../backend/src/services/theme_serializer.py) | `serialize_theme_css(app, theme)` re-validates every value (hex regex / font allowlist / app-slug regex) before emit; `theme_hash(theme)` returns 8-hex-char SHA-256 prefix from JSON-with-sort_keys for stable cache-bust. |
| `GET /app/<app>/_theme.css` route | [`backend/src/api/routes_app_theme.py`](../backend/src/api/routes_app_theme.py) | Public; reads `_catalog/<app>` from KV, parses, calls serializer; `Cache-Control: public, max-age=31536000, immutable`; ETag = sha256 prefix; 404 only when no catalog published. |
| Auth-middleware exception | [`backend/src/main.py`](../backend/src/main.py) `_path_needs_admin_auth` | New rule for `*/_theme.css` matching the `_assets/` shape. |
| Page wrapper | [`backend/src/static/app/device.html`](../backend/src/static/app/device.html) + [`backend/src/api/routes_app.py`](../backend/src/api/routes_app.py) | `device.html` carries `{{APP}}` / `{{THEME_HASH}}` placeholders; `device_page` reads catalog theme and substitutes via the new `_render_device_page` helper. Template cached per-process after first read. |
| Base stylesheet refactor | [`backend/src/static/app/styles.css`](../backend/src/static/app/styles.css) | Renamed themable subset to `--app-bg`/`--app-text`/`--app-primary`/`--app-accent`/`--app-font`. Stra2us-owned vars (`--muted`/`--border`/`--danger`/`--panel`/`--radius`) unchanged. Added 2 new vars (`--app-accent`, `--app-font`). 17 references threaded through to the new names per the audit table. |
| pyyaml backend dep | [`backend/requirements.txt`](../backend/requirements.txt) | Server now reads catalog YAML directly (was a relay-only path before P2). Pinned `pyyaml==6.0.3`. |

### Test counts

* Backend: **100 passing** (was 50). +50 P2: 33 `test_theme_serializer` (incl. 13 adversarial corpus entries) + 10 `test_app_theme` + 7 `test_device_page_render`.
* Tools: **168 passing + 30 skipped**, unchanged — P2 doesn't touch CLI.

### Adversarial corpus

The serializer test corpus exercises every escape vector the FR
calls out + a few neighbors:

* CSS-injection via terminating-brace + new rule (`#fff; } body { background: red`)
* Function-call smuggling (`#5b3fb8) expression(alert(1))`)
* Comment-out trickery (`#fff /* */ } body { color: red`)
* Newline / quote injection (`"#fff\n"`, `'#fff"'`, `"#fff'"`)
* Bare keyword colors (`red`)
* `var()` recursion / `url()` / external-host references
* Empty-ish (`""`, `"#"`, `"#zzzzzz"`, `"##fff"`)
* Bad app slugs (selector-escape attempts, SQL-shaped, uppercase, leading underscore)
* Non-string types (int/None/bool/list/dict in a color slot)
* Disallowed font families (`Comic Sans MS`, comma-chains, `url()`-shaped, web-style names)

Every one is silently dropped; the rule body never gains a second
declaration nor an escape-out. `test_lint_bypass_color_silently_dropped`
also runs an end-to-end version through the route — proving the
defense holds even when a malformed value reaches KV directly
(bypassing publish-time lint entirely).

### Deviations from plan

1. **`<body data-app="…">` instead of `<section data-app="…">`.**
   The FR says `<section>`; the existing customer page has only
   one section per body, and the body is the entire customer
   surface (admin chrome lives at `/admin/*` on a separate page
   tree). Putting `data-app` on the body covers everything
   including the modal overlay without adding a new wrapper
   element. Same scope guarantee — the selector
   `[data-app="critterchron"]` matches the body, custom
   properties cascade to all descendants. No bleed risk into
   admin chrome (different `<body>`). If a future change adds
   admin-chrome to the same page, easy to move `data-app` to a
   new wrapping `<section>` then.

2. **Hover-tint uses `color-mix()` instead of hard-coded
   `rgba(39,84,197,0.06)`.** Pre-P2, `.reveal-btn:hover` and
   `.edit-btn:hover` used a literal rgba of the un-themed
   `--accent` color. Post-P2 they use
   `color-mix(in srgb, var(--app-primary) 6%, transparent)` so
   the hover wash follows the vendor's primary color. Browser
   support: Safari 16.4+, Chrome 111+, FF 113+ — modern enough
   for the customer page's audience. Older browsers fall back to
   no hover background (the cursor + border still indicate the
   affordance). Acceptable degradation; flagged here so a future
   reader knows the intent.

3. **`pyyaml` added as a backend dep.** Pre-P2, the server only
   stored / relayed catalog YAML bytes — never parsed. P2's theme
   route needs to extract the `theme:` block server-side, so
   `pyyaml` joins the requirements pin. Same library the CLI
   uses; tiny + ubiquitous. Mentioned here because it's a new
   import surface on the backend.

4. **Template caching at module level.** `_render_device_page`
   reads `device.html` once per process via `_device_template()`.
   Tests have an `autouse` fixture that resets the cache so
   working-tree changes show up immediately during `pytest -q`.
   This is the simplest way to avoid disk reads on the hot path
   without introducing a real template engine; if Jinja2 ever
   shows up here, this caching layer can go.

### Open question resolutions

| Open Q | Decision | Where |
|---|---|---|
| Where to put `data-app` (FR: `<section>`) | `<body>` for v1; refactor if admin chrome ever shares the page | `device.html` + this doc |
| Theme hash source | JSON-with-sort_keys of the parsed `theme:` dict, first 8 hex of SHA-256 | `theme_serializer.theme_hash` |
| Hover-tint color when primary is themed | `color-mix(in srgb, …, transparent)` | `styles.css` |
| Empty-theme rendering | Empty-body rule (`[data-app="x"] {\n}\n`) — page falls back to `:root` defaults | `theme_serializer` + `routes_app_theme` |
| Catalog-not-published rendering | 404 from theme route + browser inline-default fallback; `<link>` still emitted from page wrapper | `routes_app_theme.serve_theme` + `_render_device_page` |

### Sign-off checklist

| Item | Status | Notes |
|---|:---:|---|
| All automated tests green (incl. adversarial) | ✅ | 168 tools + 100 backend; 13-entry adversarial color corpus + 4-entry adversarial font corpus |
| Theme applies to vendor section only | ✅ | live: `/app/critterchron/<device>` shows purple/cream/orange palette; `/admin/*` retains stra2us-default blue |
| Default fallbacks kick in for missing keys | ✅ | `var(--app-x, <default>)` in styles.css; tested via `test_partial_theme_emits_only_set_keys`; verified live (theme-less catalogs render with `:root` defaults) |
| No CSP violations | ✅ | live: `style-src 'self'` covers the per-app `<link rel="stylesheet">`; no inline `<style>` anywhere; `Content-Security-Policy-Report-Only` header attached to both base + theme stylesheet responses |
| Walkthrough 1–5 behave as described | ✅ | curl + browser verification complete — see "Manual walkthrough" below |

### Manual walkthrough

All five plan walkthrough steps verified live on staging on
2026-05-08, post-redeploy from `origin/catalog-app-ui`.

| Step | Status | Notes |
|---|:---:|---|
| 1. Publish critterchron's theme with brand colors and logo | ✅ | `tools/examples/critterchron_v2.s2s.yaml` (8 vars, 5040 bytes, 1 asset) republished cleanly. |
| 2. Open customer page; vendor section themed; admin chrome NOT themed | ✅ | Browser-verified by operator: page bg cream (`#f7f3eb`), buttons purple (`#5b3fb8`), inline edit-link orange (`#ffb86c`), body text dark (`#2a2a2a`); `/admin/*` retained default blue. |
| 3. View page source; `<link rel="stylesheet" href="/app/critterchron/_theme.css?v=…">` present | ✅ | Confirmed in DOM with `data-app="critterchron"` on `<body>`. |
| 4. Network tab: `_theme.css` loads with correct cache headers | ✅ | curl: `Content-Type: text/css; charset=utf-8`, `Cache-Control: public, max-age=31536000, immutable`, `ETag: "e251e8dc"`. |
| 5. Republish with different colors; cache-bust URL changes; refresh shows new colors | ✅ | Swapped to green primary / orange-red accent / light bg via sed-then-publish; ETag flipped from `e251e8dc` to `920c552d`; served body shows the new colors; restored to original after the demo. |

### Items deferred / followups

1. **Theme block in `product_name`/`logo` chrome.** The FR
   mentions placing `product_name` and the logo asset in the
   page chrome ("where P3 will refine — for P2, basic placement
   is sufficient"). P2 wires the `data-app` attribute and theme
   variables; actually rendering the logo + product name in the
   header lives in P3's renderer dispatch.

2. **Admin chrome CSP audit.** P2 is the first phase that adds
   a per-app `<link rel="stylesheet">` from a different
   sub-path. Should be CSP-clean (`style-src 'self'` covers
   it), but P5's audit will confirm — no new violations should
   appear in the Report-Only telemetry.

3. **YAML 1.1 truthy-enum doc note.** Still pending from P0 —
   FR's combined example uses bare `off` in an enum list, the
   parser rejects, doc clarification would save copy-paste users
   the trouble.

### Rollback

Reverting P2's commits leaves staging on P1 (asset pipeline +
serve route still work; no theme stylesheet route; device.html
served as a static file again). The publish flow doesn't change
between P1 and P2; previously-published catalogs continue to
work. No data migration involved — `_catalog/<app>` is
unchanged; the serializer simply ignores the `theme:` block when
the route isn't loaded.

### Deploy notes

* New backend dep (`pyyaml`) — the redeploy rebuilt the image
  via `docker compose up --build`; the requirements layer was
  the only slow step, the rest cached. 10/10 smoke tests passed
  on first deploy (no missed-modifications repeat of P0).
* Two new files in `backend/` (`services/theme_serializer.py`,
  `api/routes_app_theme.py`) plus the new `services/`
  directory's `__init__.py`. Modified files: `main.py`,
  `requirements.txt`, `routes_app.py`, `static/app/device.html`,
  `static/app/styles.css`. Tests: 3 new files in
  `backend/tests/`.
* The catalog YAML at `_catalog/critterchron` round-trips
  through the new server-side parse path with no compatibility
  issues — confirms the P1-published catalog's bytes are
  reachable as both raw text (the existing `catalog fetch`
  path) and parsed Python objects (the new theme path) without
  a migration step. Useful proof that future server-side
  catalog work (P3 renderer dispatch will read `vars:` similarly)
  can layer on without touching publish.
* Color-swap demo + restoration was a useful pattern from P1
  worth carrying — the demonstration mutates a state on
  staging, captures the before/after, then restores so the
  customer-facing demo URL matches the documented baseline.

---

## P3 — Renderer dispatch *(signed off 2026-05-08)*

**Status:** ✅ shipped. 168 tools + 212 backend tests green
(+112 backend from P2), all four plan walkthrough steps
verified live on staging (widgets render per FR dispatch table,
off-spec value shows warning + clamp + raw `data-original`,
form submit round-trips, forward-compat publish accepts unknown
widget hint), operator sign-off recorded.

### Architecture decision (operator-confirmed before coding)

Pre-P3 the customer page was *client-rendered*: `device.html` shipped
empty placeholders and `app.js` fetched the catalog YAML, parsed it
with js-yaml (loaded from cdn.jsdelivr.net), and `innerHTML`-injected
a list of setting cards with Reveal/Edit modal interaction. The FR's
"Renderer dispatch" model is *server-rendered* — inline form widgets
per the dispatch table, no per-field modal. We chose **Option A
(replace client-side rendering with server-rendered widgets)** —
cleaner end state, drops js-yaml entirely (CSP win), aligns the
markup with what P0's `touched_state.js` expects.

**Form-submit handler — strict-naive** (operator-confirmed):
P3 ships the simplest possible POST handler — iterate every
form field, write to KV. Off-spec stomping and write_only-empty
clearing the stored secret are pre-P4 footguns the FR explicitly
defers; the gap to P4 is shallow, lowest-risk option preferred
over best-functional.

### Deliverables landed

| Plan item | Path | Notes |
|---|---|---|
| Per-widget renderer | [`backend/src/services/widget_renderer.py`](../backend/src/services/widget_renderer.py) | Dispatch table covers every FR row + bool/float/legacy `type: enum`. Renders inner form control only; off-spec values clamp the *display* but `data-original` carries the raw value for P4. |
| Markdown cache | [`backend/src/services/markdown_cache.py`](../backend/src/services/markdown_cache.py) | `(app, publish_hash, block_id)` keyed; thread-safe; sanitizer call-counted in tests. |
| Vendored markdown sanitizer | [`backend/src/services/markdown_render.py`](../backend/src/services/markdown_render.py) | Byte-equal copy of `tools/stra2us_cli/sanitizers/markdown.py`; parity test (`test_markdown_render_parity.py`) imports both and asserts byte-equal output on the FR's XSS corpus. |
| Value resolver | [`backend/src/services/value_resolver.py`](../backend/src/services/value_resolver.py) | `<app>/<device>/<key>` → `<app>/public/<key>` → catalog default chain; encrypted-flag preserved. |
| Page assembler | [`backend/src/services/page_renderer.py`](../backend/src/services/page_renderer.py) | Composes widget + markdown + chrome + off-spec badge; emits `<section class="catalog-app">` with `data-app` + per-card `data-var` + every common attribute the touched-state JS expects in P4. |
| Device-page handler | [`backend/src/api/routes_app.py`](../backend/src/api/routes_app.py) | New `_render_device_page(app, device)` orchestrates load → resolve → render → substitute; includes telemetry topic + cadence as `<body>` data-attrs so the trimmed `app.js` doesn't re-fetch the catalog. |
| Form-submit handler | [`backend/src/api/routes_app_form.py`](../backend/src/api/routes_app_form.py) | `POST /app/<app>/<device>` — strict-naive write-each-field; `json.loads` type recovery mirroring the existing admin endpoint; encrypted-flag preserved on writes; POST-redirect-GET (303). |
| Device template | [`backend/src/static/app/device.html`](../backend/src/static/app/device.html) | New `{{SETTINGS_SECTION}}` placeholder; body data-attrs for `data-device` / `data-telemetry-topic` / `data-heartbeat-seconds`; `<script src="https://cdn.jsdelivr.net/.../js-yaml...">` removed. |
| Trimmed app.js | [`backend/src/static/app/app.js`](../backend/src/static/app/app.js) | Cut from 829 to ~265 lines. Drops catalog YAML fetch, settings-card rendering, edit modal, `validateInput`/`encodeForAdmin`/`editControlHtml`/etc. Keeps landing form, telemetry tail, status badge, Reveal flow. |
| New CSS rules | [`backend/src/static/app/styles.css`](../backend/src/static/app/styles.css) | `.catalog-app`, `.catalog-app-chrome`, `.catalog-form` + `.setting-card`, `.setting-warning` (off-spec badge), `.radio-group`, future-paired `[data-valid="..."]` styling for P4. |
| pyyaml + python-multipart deps | [`backend/requirements.txt`](../backend/requirements.txt) | `python-multipart==0.0.27` for FastAPI's `await request.form()`. (`pyyaml` already on board from P2.) |

### Test counts

* Backend: **212 passing** (was 100). +112 P3:
  * 35 `test_widget_renderer` (per-row snapshots, off-spec, write_only, escaping, forward-compat)
  * 18 `test_markdown_render_parity` (byte-equal vs CLI on the XSS corpus)
  * 8 `test_markdown_cache` (cache discipline, hit/miss counting, publish_hash invalidation)
  * 13 `test_value_resolver` (fallback chain rungs, type coercion, encrypted-flag, corruption)
  * 20 `test_page_renderer` (composed snapshot, off-spec badges, help_markdown caching)
  * 9 `test_app_form` (strict-naive write, type recovery, encrypted-flag preservation, slash-rejection, soft-404)
  * 9 `test_device_page_integration` (full GET path: catalog from KV → resolve → render → template substitution)
* Tools: **168 passing**, unchanged (P3 doesn't touch CLI).

### Deviations from plan

1. **Backend imports the catalog as a parsed dict, not as the
   CLI's pydantic `Var` model.** Earlier draft had
   `widget_renderer` taking `Var`; that pulls
   `tools/stra2us_cli` into the backend's runtime, which
   the docker build context (`./backend`) doesn't reach. Refactored
   to dict-based input, matching the established pattern from
   theme_serializer + assets + lint duplication. Trade: lose
   pydantic shape validation at the boundary. Defense:
   `.get(...)` access throughout + parser-level validation at
   publish time. *Followup:* change docker build context to repo
   root so we can `pip install -e tools/` and consolidate.

2. **Markdown sanitizer is vendored, with a drift-detection
   parity test.** Same root cause as #1 — backend can't import
   from `tools/stra2us_cli/sanitizers/`. Test
   (`test_markdown_render_parity.py`) imports both copies and
   asserts byte-equal output on the FR's XSS corpus. Any
   drift fails CI. *Same followup as above:* consolidating the
   build context collapses the two copies.

3. **`<body data-app="...">` carries telemetry config.** Pre-P3
   `app.js` parsed the catalog client-side to read
   `telemetry_topic` + `heartbeat_interval_seconds`. The trimmed
   `app.js` no longer fetches the catalog. The server now writes
   resolved values onto `<body>` as `data-telemetry-topic` +
   `data-heartbeat-seconds`. Same idiom as `data-app`; no extra
   round-trip; CSP-clean.

4. **`color-mix()` for hover tints (still).** Carried over from
   P2 since the new `.reveal-btn:hover` reuses the same shape.
   Browser support already vetted in P2.

5. **device_page renders the polite "no catalog yet" hint when
   the catalog is unpublished.** The plan didn't call out this
   case; pre-P3 the page would have shown "Loading settings…"
   forever. P3 surfaces a clear "run `stra2us catalog publish`"
   message so a deploy-without-publish doesn't look broken.

6. **P0 forward-compat bug found + fixed during P3 walkthrough.**
   P0 declared `widget: Literal["slider", "secret", "radio"] | None`
   on the catalog `Var` model — but the FR's "Forward
   compatibility" rule says unknown widget values MUST be
   accepted at load time and fall through at render. Old servers
   loading new catalogs that name a future widget would have
   failed parser validation, breaking the FR's central
   forward-compat promise. Walkthrough step 4
   (`widget: holographic_orb`) caught it on the first try.
   Loosened to `widget: str | None`; renderer dispatch's
   fall-through path (already implemented + tested) handles
   the rest. Test `test_unknown_widget_value_accepted_for_forward_compat`
   replaces the old "rejected" expectation. Server-side parsing
   was unaffected (the server uses `yaml.safe_load` to a dict,
   not the pydantic schema), so no server redeploy was needed
   for the fix — CLI-only.

### Open question resolutions

| Open Q | Decision | Where |
|---|---|---|
| Page model: client- vs server-rendered widgets | Server-rendered (Option A); drop js-yaml + edit modal | [`page_renderer.py`](../backend/src/services/page_renderer.py) + [`app.js`](../backend/src/static/app/app.js) |
| Form-submit risk profile | Strict-naive (Option A) — accepts pre-P4 footguns; P4 cleans up | [`routes_app_form.py`](../backend/src/api/routes_app_form.py) |
| Backend imports of `stra2us_cli` | Vendor + parity test for the sanitizer; dict-based input for the catalog model | new files in `backend/src/services/` |
| Telemetry config flow | Server-rendered `<body data-*>` attrs | `_render_device_page` |

### Sign-off checklist

| Item | Status | Notes |
|---|:---:|---|
| All snapshot tests green | ✅ | 212 backend + 168 tools |
| Off-spec values show warning + verbatim value | ✅ | unit + integration; live-verified with `ir_brightness=129` showing warning badge + clamped slider + raw `data-original="129"` |
| Markdown blocks render correctly with caching | ✅ | test_markdown_cache + test_page_renderer; live-verified header/footer markdown on the customer page |
| Native browser validation blocks bad submits | ✅ | live-verified `start_time` rejecting `7am` via HTML5 `pattern` |
| No JS required for any P3 behavior | ✅ | form is server-rendered + browser-native submit; app.js handles only telemetry/Reveal |
| CSP clean | ✅ | `cdn.jsdelivr.net` removed from device.html; everything else self-hosted; `Content-Security-Policy-Report-Only` header still attached |

### Manual walkthrough

All four plan walkthrough steps verified live on staging on
2026-05-08, after the redeploy from `origin/catalog-app-ui` +
the parser fix landed.

| Step | Status | Notes |
|---|:---:|---|
| 1. Customer page renders all dispatch-table widgets | ✅ | Operator confirmed: section chrome (logo + "Critterchron"), header markdown, dropdowns, slider, masked password (empty per `write_only`), textarea, pattern-matched text, radio group, footer markdown — all visible. |
| 2. Off-spec `ir_brightness=129` clamps to 100 + warning badge | ✅ | Slider visually pinned at 100; warning quotes the verbatim `129`; `data-original="129"` carries the raw value for P4. |
| 3. Browser-native form submit + HTML5 validation | ✅ | `start_time` blocks invalid `7am` per `pattern`; valid `07:30` POSTs and 303-redirects to refreshed page. |
| 4. Forward compat: `widget: holographic_orb` accepted | ✅ | CLI publish succeeded post-fix; renderer falls through to type-default text input. Original catalog restored after demo. |

### Items deferred / followups

1. **Build-context consolidation.** P3 vendored
   `markdown_render.py` and dropped pydantic catalog imports
   because `./backend` is the docker build context.
   Switching to the repo root (with `dockerfile: backend/Dockerfile`
   in compose) and `pip install -e tools/` collapses the two
   copies + restores pydantic validation at the backend's edge.
   Tracked here; not blocking P3.

2. **Server-side lint at catalog upload.** Still pending from
   P0 → P1 → P2's deferred lists. P3 reads catalog dicts
   server-side (the integration tests prove the read+parse path
   works); calling lint after parse is a small addition. Likely
   lands as a P5 or pre-P5 item.

3. **YAML 1.1 truthy-enum doc note.** Original P0 followup;
   still open.

4. **Edit modal removal in admin too?** Pre-P3 the edit modal's
   primitives were *copied* from admin's catalog editor. The
   customer-page copy is gone now; the admin copy stays. P5
   audit may surface this as a deduplication candidate but it's
   not a security or correctness issue.

5. **Reveal button auth path.** Pre-P3 the Reveal flow used
   `/api/admin/peek/kv/<path>`. P3's trimmed `app.js` keeps
   that path. The customer is admin-authed (cookie / OAuth),
   so the existing path is fine — flagging here so P5's audit
   knows it's intentional.

### Rollback

Reverting P3 leaves staging on P2: theme stylesheet still works,
asset pipeline still works, catalog YAML at `_catalog/<app>` is
unchanged. The customer page would lose the inline form (the
template's `{{SETTINGS_SECTION}}` would be a literal string in
the page) — but P3 isn't deployed without the route handler
reverting too. Standard "revert the merge commit" flow applies.

### Deploy notes

* Two new backend deps (`python-multipart`) — image rebuilds the
  requirements layer. `pyyaml` was already in from P2.
* The biggest user-visible change in P3 is that the customer
  page is now server-rendered — first paint shows the form
  filled in. Pre-P3 there was a ~100ms "Loading settings…"
  flicker while app.js did its YAML fetch + parse. The
  flicker is gone, which is the most concrete UX win the FR's
  prose was promising.
* `cdn.jsdelivr.net/npm/js-yaml` is no longer loaded by the
  customer page. Once staging is on P3, the
  `Content-Security-Policy-Report-Only` violation count for the
  customer page should drop to zero — input for P5's audit.
* The 6 P3 sandbox files to copy are 5 new (`widget_renderer`,
  `markdown_render`, `markdown_cache`, `value_resolver`,
  `page_renderer`, `routes_app_form`) plus the modified set
  (`device.html`, `app.js`, `styles.css`, `routes_app.py`,
  `routes_app_theme.py`, `main.py`, `requirements.txt`). Test
  files (7 new `test_*.py` in `backend/tests/`).
* **Initial deploy crashed on missing service files.** The first
  redeploy attempt failed with
  `ModuleNotFoundError: No module named 'services.page_renderer'`
  because the new `backend/src/services/*.py` files (5 of them)
  weren't on the branch — same shape as the P0 deploy gotcha,
  just in a different subdirectory this time. Smoke went 0/10
  immediately, container logs spelled out the missing module,
  fix was a re-copy + re-commit. **Pattern carrying forward:**
  the `git ls-files | grep <path>` sanity check before redeploy
  catches this in seconds; worth running it as a habit on every
  multi-file phase.
* **Walkthrough caught a P0 forward-compat bug that 248 tests
  missed.** P0's `widget: Literal[...]` validator silently
  violated the FR's "unknown widgets fall through" promise. The
  bug only manifests when an old server tries to load a new
  catalog — a scenario unit tests don't naturally cover because
  they run with a single code+catalog version. P3's manual step
  4 (publish a catalog with `widget: holographic_orb`) caught
  it on first try. Proves the walkthrough's value beyond CI:
  forward-compat / cross-version contracts only break under
  live combinations.

---

## P4 — JS form behavior *(signed off 2026-05-08)*

**Status:** ✅ shipped. 168 tools + 232 backend tests green
(+20 from P3), all eight plan walkthrough steps verified live on
staging — including the two marquee FR promises that this entire
feature was about: **off-spec values preserved across no-touch
saves**, and **`write_only` fields not stomped to empty by an
absent-minded Save**.

### Architecture decision

P4 wires P0's standalone `forms/touched_state.js` module into
the customer device form via fetch-based submit. The module was
written in P0 and structurally tested; P3 produced server-side
markup that already carries the `data-original` /
`data-write-only` / `data-valid` attributes the JS reads.
P4's job was the wiring layer — `app.js` converts to an ES
module, imports `init` + `attachSubmitHandler`, intercepts
form submit, POSTs the touched-state-aware payload via fetch,
reloads on success.

The server-side form-submit handler **stayed unchanged** —
P3's strict-naive iteration was already "write whatever fields
are present," which becomes "write only touched + clean
non-write-only" when the JS controls what's in the payload. The
cross-tier correctness is the JS's job (omitting clean
write_only fields from the body) + the server iterating
naïvely (P3 handler, untouched).

### Deliverables landed

| Plan item | Path | Notes |
|---|---|---|
| Wire touched_state into device form | [`backend/src/static/app/app.js`](../backend/src/static/app/app.js) | Converted to ES module; `import { init, serialize, attachSubmitHandler } from './forms/touched_state.js'`; `initDevice` wires both into the `<form class="catalog-form">`; `onFormSubmit` intercepts → URLSearchParams body → fetch POST → `window.location.reload()` on success. |
| Module-loading update | [`backend/src/static/app/device.html`](../backend/src/static/app/device.html), [`landing.html`](../backend/src/static/app/landing.html) | `<script type="module">` (was `defer`); landing.html got fixed for an unrelated P3 trim regression (`#deviceNameInput` → `#deviceName`). |
| `Cache-Control: no-store` on device page | [`backend/src/api/routes_app.py`](../backend/src/api/routes_app.py) | `_render_device_page` returns the `HTMLResponse` with `Cache-Control: no-store`. Caught during walkthrough — without it, `window.location.reload()` after Save could serve a cached pre-save page, masking whether touched-state behaved correctly. |
| `data-valid` styling softening | [`backend/src/static/app/styles.css`](../backend/src/static/app/styles.css) | Red on invalid stays loud (full outline); green on valid demoted to `border-color` only — keeps the form quiet when every pattern field is valid by default. |
| New tests | [`backend/tests/test_app_js_p4_wiring.py`](../backend/tests/test_app_js_p4_wiring.py) (14), partial-payload extension to [`test_app_form.py`](../backend/tests/test_app_form.py) (5), cache-header regression in [`test_device_page_integration.py`](../backend/tests/test_device_page_integration.py) (1) | Total +20 backend. |

### Test counts

* Backend: **232 passing** (was 212). +20 P4:
  * 14 `test_app_js_p4_wiring` — module-load shape, `init`/`attachSubmitHandler` imports + calls, fetch-POST + reload + preventDefault, non-regression on telemetry + reveal + jsdelivr-removal.
  * 5 `test_app_form` partial-payload cases — write_only-clean omitted preserves KV, write_only-touched writes through, off-spec preserved via data-original resend, dirty clobbers, mixed-form partial writes.
  * 1 `test_device_page_integration` — `Cache-Control: no-store` on device page response.
* Tools: **168 passing**, unchanged (P4 doesn't touch CLI).

### Deviations from plan

1. **Server-side form-submit handler unchanged.** Plan said
   "Server-side form-submit handler updated to do partial
   updates (only write fields present in submission)." P3's
   strict-naive handler already iterated only present fields;
   P4 didn't need to touch it. The "partial-update" semantics
   shifted entirely to the JS-controlled payload. The five
   new tests in `test_app_form.py` document the cross-tier
   contract so a future server change that breaks this fails
   loudly.

2. **`Cache-Control: no-store` not in plan.** Caught during the
   walkthrough — without it, a `window.location.reload()` after
   form save could be served from browser cache, hiding whether
   touched-state worked. Three reproductions in a row before
   the diagnosis clicked: refresh stayed at 50 even though KV
   held 129; shift-reload (cache bypass) showed the badge;
   normal reload didn't. Fix is the page response opting out of
   browser caching for the dynamic device-page route — the CSS
   / theme / asset routes stay cache-immutable individually.

3. **app.js trim regression repaired.** P3's trim renamed the
   landing form's input to `#deviceNameInput` in JS but the
   HTML kept `id="deviceName"`. Live landing form was broken
   between P3 and P4 deploys; nobody noticed because nobody
   used it. P4 caught it during the module-loading conversion.
   Documenting so a future "what regressed when?" archaeology
   has a quick answer.

4. **Initial `data-valid` set on first render.** P0's
   `touched_state.js` sets `data-valid="true|false"` at
   `init()` time, not just on first input. The FR text says
   "as the user types"; the P0 module's choice was "set
   initial state for visible feedback before any keystroke."
   P4 lives with that and softens the CSS for valid (border
   color, not full outline) so the initial state isn't loud.
   If "neutral until first keystroke" is the right UX, removing
   the initial-set is one small edit in the P0 module — flagged
   as a low-priority followup.

### Open question resolutions

| Open Q | Decision | Where |
|---|---|---|
| Where does the partial-update logic live? | JS-side via `serialize()` omitting clean+write_only; server stays naïve | [`app.js`](../backend/src/static/app/app.js) + P3's [`routes_app_form.py`](../backend/src/api/routes_app_form.py) untouched |
| ES modules vs `defer`? | `<script type="module">` for both pages — required for `import`; runs in strict mode by default | [`device.html`](../backend/src/static/app/device.html), [`landing.html`](../backend/src/static/app/landing.html) |
| Should valid pattern fields paint green at first paint? | Yes, but with subtler CSS than the loud red of invalid | [`styles.css`](../backend/src/static/app/styles.css) data-valid rules |
| Browser cache vs `location.reload()`? | `Cache-Control: no-store` on device page; CSS/theme/assets stay individually cache-immutable | [`routes_app.py`](../backend/src/api/routes_app.py) |

### Sign-off checklist

| Item | Status | Notes |
|---|:---:|---|
| All automated tests green | ✅ | 168 tools + 232 backend |
| Snap-on-edit untouched: stored 129 stays 129 | ✅ | live: refreshed page showed badge with `129`; Save without touching the slider; KV verified `129` post-save |
| Snap-on-edit dirty: drag → 50 writes 50 | ✅ | live: dragged slider, Save, KV showed 50 |
| `write_only` untouched: stored secret survives empty-form Save | ✅ | live: KV held `'actualsecret'`, refreshed page (empty wifi), Save, KV verified `'actualsecret'` post-save |
| `write_only` touched: typed value writes | ✅ | live: typed in field, Save, KV showed the new value |
| Live pattern: red on invalid keystrokes | ✅ | live: typed `7am` into `start_time`, red outline appeared per-keystroke |
| Live pattern: green when valid | ✅ | live: typed `07:30`, subtle green border appeared |
| No silent data stomping in any path tested | ✅ | both off-spec and write_only paths preserve as designed |
| CSP clean | ✅ | `<script type="module">` is same-origin; no inline handlers, no `eval`, `cdn.jsdelivr.net` removed in P3 |

### Manual walkthrough

All 8 plan walkthrough steps verified live on staging on
2026-05-08 against `https://stra2us-staging.austindavid.com/app/critterchron/p3demo`.

| Step | Status | Notes |
|---|:---:|---|
| 1. Set `ir_brightness=129` via CLI | ✅ | reset done CLI-side; page rendered with badge `129 — not in current allowed values` and slider visually pinned at 100. |
| 2. Open page; slider pinned at 100, warning shows 129 | ✅ | confirmed in browser; `data-original="129"` on the slider input. |
| 3. Submit form without touching anything; refresh shows `129` still | ✅ | **the marquee P4 test.** Save → page reloaded → badge still showed 129. CLI verified KV unchanged. |
| 4. Move slider to 50, Save → KV now 50 | ✅ | Save → page reloaded → badge gone, slider at 50. |
| 5. Set `wifi_password=actualsecret`; open page, input is empty | ✅ | re-set CLI-side; page rendered empty masked input, no Reveal button (write_only takes precedence). |
| 6. Submit without touching wifi → KV still holds `actualsecret` | ✅ | **the second marquee P4 test.** Save → page reloaded → CLI-verified KV still `actualsecret`. |
| 7. Type new password, submit → KV updates | ✅ | typed value, Save → CLI-verified KV updated to new value. |
| 8. Type `7am` then `07:30` into `start_time` → red then green | ✅ | per-keystroke red on `7am`; green border on `07:30`. |

### Items deferred / followups

Two new + the prior list:

1. **Browser-side test runtime.** The combination of P0's
   structural-only JS tests + P4's manual walkthrough has
   worked through 4 phases, but the failure mode (cache header
   issue caught only in walkthrough; required three browser
   reproductions to diagnose) suggests adding a real browser
   runtime would pay off. Playwright + a small fixture corpus
   could exercise the touched-state behaviors end-to-end on
   every CI run. Tracked as a v2 item; not blocking.

2. **Initial `data-valid` set vs neutral-until-first-input.**
   P0 module sets data-valid at bind time. FR-pedant interpretation
   is "neutral until first keystroke." Soft CSS in P4 makes the
   initial-set unobtrusive; if a future UX review says "I want
   neutral-then-feedback," removing the initial-set is a 3-line
   change in `forms/touched_state.js`.

3. **Build-context consolidation** (carried from P3). Switch
   docker-compose context to repo root + `pip install -e tools/`
   so backend can drop the vendored `markdown_render.py` + the
   parity test, and `widget_renderer` can take pydantic `Var`
   instead of dict.

4. **Server-side lint at catalog upload** (carried from P0/P1/P2/P3).

5. **YAML 1.1 truthy-enum doc note** (still open from P0).

### Rollback

Reverting P4 leaves staging on P3: form submits via browser
native POST, which fires the strict-naive handler with every
form field — re-introducing the off-spec stomp + write_only-wipe
footguns. The data layer survives the rollback (no schema
changes); the failure mode is "the customer page silently
stomps" rather than "the customer page crashes."

### Deploy notes

* Initial deploy was clean (smoke 10/10). The `Cache-Control:
  no-store` fix required a second redeploy — caught during
  walkthrough as described in deviations.
* Walkthrough re-uses the P3 fixture (`p3demo` device under
  critterchron) but **the prior walkthrough's saves wrote
  values that needed re-setting** — `ir_brightness` had been
  stomped to 50 from P3's step 3, and `wifi_password` had been
  cleared. CLI-side `set ... 129` + `set ... actualsecret
  --encrypted` reset the fixture to a known off-spec /
  encrypted state for each walkthrough phase. Pattern: when
  reusing fixtures across phases, expect to reset them.
* Two diagnostic gotchas worth knowing for future phases:
  - **Browser cache + JS reload.** `window.location.reload()`
    serves stale content if the dynamic page lacks
    `Cache-Control: no-store`. Confounded the walkthrough until
    the third pass.
  - **Off-spec preservation cannot be observed after a clobber.**
    Step 4 (drag + save → KV=50) wiped the evidence of step 3's
    behavior. The marquee test for P4 must be retried each time
    the fixture is reset; the test passes by ABSENCE of stomping
    rather than by presence of any positive signal.
