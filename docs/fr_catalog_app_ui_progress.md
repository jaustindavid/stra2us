# Implementation progress: Catalog-driven app page UI

*Companion to [`fr_catalog_app_ui.md`](fr_catalog_app_ui.md) (design)
and [`fr_catalog_app_ui_plan.md`](fr_catalog_app_ui_plan.md)
(work-breakdown). One entry per phase, appended at sign-off. Records
what landed, deviations from plan, gotchas caught, and items
deferred to the next phase. Source of truth for "where are we" when
picking up the next phase.*

---

## P0 — Foundations *(in review — drafted 2026-05-07)*

**Status:** code complete, automated tests green, awaiting manual
walkthrough sign-off.

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
| CSP Report-Only header present on every route | ✅ | Asserted in middleware tests; wired in [main.py](../backend/src/main.py) |
| CSP report sink documented | ✅ | Module docstring + this entry; `/api/_csp_report` + `stra2us.csp` logger |
| No customer-facing behavior change in staging | ✅ | No routes serve content; module not wired into any page |

### Manual walkthrough

Plan's P0 walkthrough has 4 steps. Step 4 is browser-side and ready
to drive through the preview pane; steps 1–3 need decisions.

| Step | Status | Plan |
|---|:---:|---|
| 1. Publish a fixture catalog with valid `theme:` + `ui:` blocks | ⏳ | Existing `stra2us catalog publish` command works on the new schema (parser is additive). Needs a staging server and credentials to exercise; can also be unit-checked via the round-trip test pattern in `test_catalog_ui_fields.py::test_combined_critterchron_example_loads`. |
| 2. Publish three deliberately-broken catalogs | ⚠️ | **Lint not yet wired into `cmd_catalog_publish`.** Module is built and unit-tested; integration into the CLI's publish path is the question below. |
| 3. CSP Report-Only header on every backend response | ⏳ | Wired and unit-tested; `curl -I` against staging is the on-server check. |
| 4. JS module test harness in browser | ▶ | Reachable at `/app/_static/forms/_test_harness.html` once the dev server is running. |

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
