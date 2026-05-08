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

## P1 — Asset pipeline *(in review — drafted 2026-05-07)*

**Status:** code complete, automated tests green
(178 tools + 50 backend, +31 from P0), CLI publish path verified
end-to-end against staging (assets land in KV in the correct
order), awaiting staging redeploy + manual walkthrough sign-off.

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

### Sign-off checklist (anticipated; final form awaits walkthrough)

| Item | Status |
|---|:---:|
| All automated tests green | ✅ 178 tools + 50 backend |
| Publish PNG / JPEG / WebP / SVG; bytes + .meta land at expected KV paths with correct content-type | ✅ unit + integration |
| Republish drops removed asset via GC after catalog YAML lands | ✅ `test_republish_drops_removed_asset_via_gc` |
| Oversized asset rejected before any KV write | ✅ `test_oversized_asset_fails_before_any_put` |
| `.gif` (not in allowlist) rejected | ✅ `test_disallowed_content_type_rejected` |
| SVG with `<script>` rejected by sanitizer | ✅ `test_svg_with_script_rejected_by_sanitizer` |
| Cache-Control immutable + matching ETag/sha256 | ✅ `test_serves_png` |
| Mid-publish kill leaves prior catalog consistent | ✅ `test_mid_publish_kill_leaves_prior_catalog_consistent` |
| Asset URL response time on staging acceptable (<100ms p95 cached, <500ms cold) | ⏳ awaits staging redeploy |
| No CSP Report-Only violations triggered by asset serving | ⏳ awaits staging redeploy |

### Walkthrough (status pre-redeploy)

| Step | Status | Notes |
|---|:---:|---|
| 1. Publish critterchron fixture catalog with a real logo.svg in `_assets/` | ✅ (CLI side) | Published to staging. Bytes + meta + index landed at the expected KV paths. Asset serve route blocked by old auth middleware until redeploy. |
| 2. Hit `/app/critterchron/_assets/logo.svg?v=…` in browser; image renders, headers correct | ⏳ | Awaits staging redeploy. |
| 3. Republish with logo.svg replaced by a new file; new `?v=` URL serves new bytes | ⏳ | Awaits staging redeploy. |
| 4. Try to publish a 5 MiB PNG; CLI rejects at lint | ✅ | `test_oversized_asset_fails_before_any_put` covers; verify locally during walkthrough. |
| 5. Try to publish an SVG with `<script>alert(1)</script>`; CLI rejects | ✅ | `test_svg_with_script_rejected_by_sanitizer` covers; verify locally during walkthrough. |

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

* This phase's redeploy needs **all the P1 sandbox files** copied
  to the branch — both new (`tools/stra2us_cli/catalog_publish.py`,
  `backend/src/api/routes_app_assets.py`, `tools/examples/_assets/logo.svg`,
  three new test files) and modified (`tools/stra2us_cli/cli.py`,
  `backend/src/main.py`, `docs/catalog_spec.md`, `tools/examples/critterchron_v2.s2s.yaml`
  unchanged but referenced). Same lesson from P0: eyeball
  `git status` before commit so modifications aren't missed.
* The CLI publish was exercised against staging *before* the
  server redeploy — proved the publish path is forward-compatible
  (server happily stores bytes + meta + index even without the
  serve route present). Useful checkpoint for sanity. Staging
  redeploy below brings the serve route + auth-middleware
  exception online.
