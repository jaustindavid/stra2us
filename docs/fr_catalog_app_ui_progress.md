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
