# Implementation plan: Catalog-driven app page UI

*Drafted 2026-05-07. Companion to
[`fr_catalog_app_ui.md`](fr_catalog_app_ui.md). The FR is the
design of record; this doc is the work-breakdown the implementing
team follows.*

## How to use this doc

Six phases, each shippable and testable on its own. The team
implements one phase, stages it, and we walk through the
**Sign-off checklist** together before they pick up the next.
P0 unblocks everything else; P5 (CSP enforcement) runs as its
own track in parallel from P0 onward.

Each phase has:

- **Goal** — one sentence, so we don't lose the thread.
- **In scope / Out of scope** — explicit boundaries to prevent drift.
- **Deliverables** — concrete artifacts (files, routes, libraries).
- **Dependencies** — what must land first.
- **Automated tests** — the team writes these; CI green is the
  gate to call the phase ready for review.
- **Manual walkthrough** — the steps we run together on staging.
- **Sign-off checklist** — clean yes/no items; all green = phase done.
- **Rollback** — how to back out if staging walkthrough fails.

## Phase dependency graph

```
P0 (Foundations)
 ├─→ P1 (Asset pipeline)
 │    └─→ P2 (Theme stylesheet)
 │         └─→ P3 (Renderer dispatch)
 │              └─→ P4 (JS form behavior)
 └─→ P5 (CSP enforcement)  [parallel track]
```

P0 fans out widely — most of its sub-tasks can run concurrently
within the phase. P1 → P2 → P3 → P4 is a hard chain. P5 starts at
P0 (Report-Only ships in P0) and finishes after the audit completes.

## Branching, staging, telemetry

- One feature branch per phase, merged to `main` only after
  walkthrough sign-off.
- Each phase ships behind a config flag (`STRA2US_CATALOG_UI_V2`
  or per-feature flags called out below) so we can stage without
  exposing customers mid-flight.
- Staging deploys after every phase; production rollout is a
  separate decision once all phases are green.
- CSP report endpoint must exist (or be explicitly stubbed to
  the server log) before P0 ships. Without a sink, Report-Only
  is decorative.

---

## P0 — Foundations

**Goal:** ship every pure-function building block (parser, lint,
sanitizers, JS module, CSP middleware in Report-Only) with no
customer-visible behavior change.

### In scope

- Catalog YAML parser extension: recognize `theme:`, `ui:`,
  field-level `enum`/`min`/`max`/`step`/`widget`/`multiline`/
  `max_length`/`pattern`/`help`/`help_markdown`/`write_only`.
- Shared lint module — one Python module, two callers (CLI at
  publish, server at upload). Per-key validation per the FR.
- Markdown sanitizer (markdown-it-py + bleach) wired with the
  FR's allowlist; HTML caching keyed by
  `(app, publish_hash, block_id)`.
- SVG sanitizer using `defusedxml.ElementTree` + hand-rolled
  allowlist walker. Output is the re-serialized clean tree, not
  the original bytes.
- CSP middleware emits `Content-Security-Policy-Report-Only`
  with the full FR policy on every response. Reports go to
  a logged endpoint (path TBD by team; document in PR).
- Touched-state JS module (`data-original` + dirty flag +
  partial submit). Standalone module with DOM unit tests; not
  wired into any page yet.

### Out of scope

- Any user-visible UI change. No new routes serve content. No
  page renders differently.

### Deliverables

- `tools/stra2us_cli/catalog_lint.py` (shared module)
- `tools/stra2us_cli/sanitizers/markdown.py`
- `tools/stra2us_cli/sanitizers/svg.py`
- `backend/src/middleware/csp.py`
- `frontend/src/forms/touched_state.js` (or wherever JS lives)
- Test corpora committed alongside each sanitizer.

### Dependencies

None. P0 is the foundation.

### Automated tests

- **Parser:** fixture catalogs covering every new key, including
  the malformed cases lint must catch. Assert parsed structure.
- **Lint:** every error case from the FR's lint table. Plus the
  bonus warnings (unused asset, enum-with-min/max, slider
  without min/max).
- **Markdown sanitizer:** OWASP XSS cheatsheet vectors,
  `javascript:` / `data:` / `vbscript:` URLs in `href` and `src`,
  `on*=` event attrs, `<style>`/`<script>`/`<iframe>`/`<form>`,
  malformed nesting, mojibake/zero-width tricks.
- **SVG sanitizer:** `<script>`, `<foreignObject>`,
  `xlink:href="javascript:..."`, external `<image href>`,
  external DTD, `<!ENTITY>` billion-laughs, `<use href>` with
  external target.
- **CSP middleware:** every directive present in the header;
  Report-Only flag on; report-uri/report-to populated.
- **Touched-state JS:** initial render captures `data-original`;
  dirty flag flips on `input`/`change`; serialize-on-submit
  emits live value if dirty, original verbatim if not, omitted
  entirely for `write_only` untouched.

### Manual walkthrough

1. Publish a fixture catalog with valid `theme:` + `ui:` blocks
   via the CLI. Confirm publish succeeds and stash byte-equals
   on read.
2. Publish three deliberately broken catalogs (bad hex color,
   enum + min/max combo, oversized markdown). Confirm each fails
   with field-pointing error messages.
3. Hit any backend endpoint with curl. Confirm
   `Content-Security-Policy-Report-Only` header present with the
   full policy.
4. Open the JS module's test harness page (test-only); type into
   a fixture form; confirm dirty/touched behavior in devtools.

### Sign-off checklist

- [ ] All automated tests green in CI.
- [ ] Lint catches every documented error case.
- [ ] Sanitizer test corpora pass with zero leaks.
- [ ] CSP Report-Only header present on every route.
- [ ] CSP report sink is documented (where do reports go, who
      watches them).
- [ ] No customer-facing behavior change observable in staging.

### Rollback

P0 is invisible to customers. Roll back by reverting the merge
commit; no data migration involved.

---

## P1 — Asset pipeline

**Goal:** vendors can ship logos and other images alongside their
catalog; stra2us serves them same-origin with cache-immutable URLs.

### In scope

- CLI `catalog publish` reads sibling `_assets/` directory and
  uploads each file (bytes + `.meta`) to KV per the FR's
  publish order (assets first, catalog YAML last, GC dropped
  files last).
- SVGs run through the P0 SVG sanitizer at publish time;
  rejected SVGs fail the publish.
- `GET /app/<app>/_assets/<filename>` route reads from KV,
  returns bytes with stored `content_type` and
  `Cache-Control: public, max-age=31536000, immutable`.
- Cache-bust hash plumbing: `?v=<sha256-prefix>` derived from
  the asset's stored `.meta`. Convention reused by P2.

### Out of scope

- Any rendering of assets in pages (P3 wires logos into chrome).
- Theme stylesheet route (P2).

### Deliverables

- `tools/stra2us_cli/catalog_publish.py` extended for assets.
- `backend/src/api/routes_app_assets.py` (or extension of
  existing routes_app).
- Asset-listing helper for GC at publish time.

### Dependencies

- P0 (SVG sanitizer, lint).

### Automated tests

- Publish a catalog bundle with PNG, JPEG, WebP, and SVG; assert
  all reachable at the served URL with correct content-type.
- Republish with one asset removed; confirm GC deletes the
  dropped asset only after the catalog YAML lands.
- Publish an oversized asset; confirm publish fails with the
  size-limit error before any KV writes occur.
- Publish a `.gif` (not in allowlist); confirm rejection.
- Publish an SVG with `<script>` inside; confirm sanitizer
  rejects (or strips, depending on FR final wording — match
  the FR).
- Hit asset URL after publish; confirm `Cache-Control: immutable`
  and matching ETag/`?v=` hash.
- Mid-publish kill (test harness simulates process death between
  asset upload and YAML upload); confirm prior catalog still
  serves prior assets consistently.

### Manual walkthrough

1. Publish the critterchron fixture catalog with a real logo.svg
   in `_assets/`.
2. Hit `https://staging/app/critterchron/_assets/logo.svg?v=…`
   in a browser. Confirm image renders, headers correct.
3. Republish with logo.svg replaced by a new file. Confirm new
   `?v=…` URL serves new bytes; old URL returns the previous
   bytes (KV history) or 404 (post-GC) depending on order —
   confirm matches FR semantics.
4. Try to publish a 5 MiB PNG. Confirm CLI rejects at lint.
5. Try to publish an SVG with `<script>alert(1)</script>`.
   Confirm CLI rejects.

### Sign-off checklist

- [ ] All automated tests green.
- [ ] Walkthrough steps 1–5 behave as described.
- [ ] Asset URL response time on staging is acceptable
      (<100ms p95 for cached, <500ms cold).
- [ ] No CSP Report-Only violations triggered by asset serving.

### Rollback

Disable the asset serve route via config flag; CLI can still
publish but renderer doesn't reference assets yet (still on
prior chrome). No data loss — assets remain in KV.

---

## P2 — Theme stylesheet

**Goal:** vendor's brand colors, fonts, logo, and product name
apply to their app's customer-facing page section, scoped so
they can't bleed into stra2us chrome.

### In scope

- `GET /app/<app>/_theme.css?v=<hash>` route. Hash derived from
  serialized theme block, bumps on republish.
- Parameterized template helper for CSS generation. Catalog
  values pass through validated formatters (color, font name,
  length-cap), never string-concatenated raw.
- Page wrapper emits `<section data-app="…">` and
  `<link rel="stylesheet" href="…/_theme.css?v=…">` in `<head>`.
- Base stylesheet refactor: every themable rule uses
  `var(--app-foo, <stra2us-default>)`. (Audit which rules; this
  is bigger than it sounds.)
- Adversarial test: feed serializer values that should have
  failed lint but pretend they didn't. Assert no escape.

### Out of scope

- Form widgets and field-level rendering (P3).
- Markdown blocks on the page (P3).

### Deliverables

- `backend/src/api/routes_app_theme.py`
- `backend/src/services/theme_serializer.py` with parameterized
  template helper.
- Refactored base stylesheet with CSS custom property fallbacks.
- Adversarial-input test suite for the serializer.

### Dependencies

- P0 (lint), P1 (cache-bust hash convention, asset URLs for logo).

### Automated tests

- Publish theme with valid values; fetch `_theme.css`; assert
  expected `--app-*` vars present.
- Publish theme missing `font_family`; fetch CSS; assert
  fallback rule kicks in.
- Adversarial inputs: `"#fff; } body { background: red"`,
  `"#5b3fb8) expression(alert(1))"`, `"system-ui, url(evil.com)"`.
  Assert serializer escapes/rejects, no second CSS rule emitted.
- Republish; assert new `?v=…` hash; old URL still 200 (cache)
  but new URL serves updated CSS.
- CSP Report-Only doesn't fire on the new stylesheet (it's
  same-origin under `style-src 'self'`).

### Manual walkthrough

1. Publish critterchron's theme with brand colors and logo.
2. Open the customer-facing page in staging. Confirm:
   - Section background, primary, accent colors match catalog.
   - `product_name` and `logo` appear in the page chrome (where
     P3 will refine — for P2, basic placement is sufficient).
   - Stra2us admin chrome (header, side nav) is **not** themed.
3. View page source; confirm `<link rel="stylesheet"
   href="/app/critterchron/_theme.css?v=…">` is present.
4. Open devtools → Network; confirm `_theme.css` loads with
   correct cache headers.
5. Republish theme with different colors. Confirm cache-bust
   URL changes; refresh shows new colors immediately.

### Sign-off checklist

- [ ] All automated tests green, including adversarial.
- [ ] Theme applies to vendor section only.
- [ ] Default fallbacks kick in for missing keys.
- [ ] No CSP violations.
- [ ] Walkthrough 1–5 behave as described.

### Rollback

Config flag disables emission of the `<link>` tag in the page
wrapper. Theme route can stay live (no harm); page falls back
to stra2us defaults.

---

## P3 — Renderer dispatch (server-side, no JS)

**Goal:** customer-facing form picks the right widget for every
field per the FR's dispatch table; markdown blocks render at
header/footer/help-markdown positions.

### In scope

- Widget HTML for every row in the FR's renderer dispatch table.
  Default browser semantics only — no live feedback yet, no
  snap-on-edit, no `write_only`. (P4 adds those.)
- Header/footer markdown blocks rendered through P0 sanitizer
  (cached output reused across page renders).
- `help` (plain text tooltip) and `help_markdown` (sanitized
  inline block) per field.
- `product_name` and `logo` placement in the section chrome
  using P1 asset URLs.
- Off-spec stored values display verbatim with warning markup
  ("not in current allowed values" badge). Markup-only — no JS
  required for the warning to render.

### Out of scope

- Any client-side interactivity beyond browser-native form
  validation. Snap, live pattern feedback, write_only, dirty
  tracking — all P4.

### Deliverables

- `backend/src/api/routes_app.py` (or wherever) extended:
  - Widget dispatch by field type + hints.
  - Markdown block injection.
  - Off-spec warning markup.
- HTML snapshot tests per widget type.

### Dependencies

- P0, P1, P2.

### Automated tests

- Snapshot test per widget type: enum→`<select>`,
  enum+widget=radio→radio group, slider+min+max→`<input
  type=range>`, multiline→`<textarea>`, secret→`<input
  type=password>`, pattern→`<input pattern>`, etc.
- Off-spec value: stored brightness=129, catalog says max=100;
  assert HTML shows 129 with warning badge and slider markup
  caps at 100.
- Markdown blocks: known-good markdown renders to expected
  HTML; cached on second render (assert sanitizer not called
  twice).
- Forward compat: catalog with unknown widget hint renders as
  the type-default; unknown markdown tag stripped gracefully.

### Manual walkthrough

1. Open critterchron's customer page on staging. Confirm:
   - `display_mode` is a dropdown with the catalog's enum values.
   - `ir_brightness` is a slider 0–100.
   - `wifi_password` is a masked input.
   - `greeting` is a textarea.
   - `start_time` is a text input.
   - Header markdown renders above the form; footer below.
   - Logo and product name appear in section chrome.
2. Use admin raw KV editor to set `ir_brightness=129`. Refresh
   customer page. Confirm:
   - Slider visually pins at 100.
   - Warning badge shows "129 — not in current allowed values."
   - The number 129 is visibly the stored value, not 100.
3. Submit the form via browser-native submit (no JS yet).
   Confirm: in-range values save; out-of-range values blocked
   by browser's native validation (HTML5 `min`/`max`/`pattern`).
4. Test forward compat: hand-edit a catalog with `widget:
   future_widget_xyz`. Confirm renderer falls back to the
   type's default widget.

### Sign-off checklist

- [ ] All snapshot tests green.
- [ ] Off-spec values show warning + verbatim value.
- [ ] Markdown blocks render correctly with caching.
- [ ] Native browser validation blocks bad submits.
- [ ] No JS required for any P3 behavior.
- [ ] CSP clean.

### Rollback

Config flag (`STRA2US_CATALOG_UI_V2`) routes back to the
existing all-text-input renderer. No data implications.

---

## P4 — JS form behavior

**Goal:** the form actively keeps the customer between the lines
on every keystroke, and handles off-spec / write-only fields
without silently stomping data.

### In scope

- Touched-state JS (from P0 module) wired into every form field
  on the customer page.
- Live `pattern` feedback: input event handler toggles
  `data-valid="true|false"`; base stylesheet renders red/green.
- Snap-on-edit slider: out-of-range stored value displays
  pinned, but `data-original` retains the raw value; first
  interaction snaps; untouched submit serializes the original
  verbatim.
- `write_only` fields: input ships empty regardless of stored
  value. Untouched fields are **omitted** from the submitted
  form (server treats absence as "preserve current"). This is
  a new behavior of the form-submit path — call it out in PR.
- Form submit semantics: per-field, send live value if dirty,
  original verbatim if not, omit entirely if write_only and
  untouched.

### Out of scope

- Any server-side validation of submitted values. Storage
  remains unfiltered per FR.

### Deliverables

- `frontend/src/forms/customer_app_form.js` (or equivalent)
  wiring P0's touched-state module to the rendered form.
- Server-side form-submit handler updated to do partial updates
  (only write fields present in submission).
- End-to-end tests covering each behavior.

### Dependencies

- P0 (JS module), P3 (rendered forms to wire to).

### Automated tests

- **Snap-on-edit:** stored brightness=129, no user interaction,
  submit form. Assert KV value stays 129.
- **Snap-on-edit dirty:** stored brightness=129, user clicks
  slider. Assert displayed value snaps to 100, submit writes 100.
- **Live pattern:** type "7am" into start_time field. Assert
  `data-valid="false"` flips on first keystroke; "07:00" flips
  to true.
- **write_only untouched:** wifi_password stored "secret123",
  page renders empty input, submit form unchanged. Assert KV
  retains "secret123" (field omitted from PUT).
- **write_only touched:** type "newpass" into empty wifi field,
  submit. Assert KV updates to "newpass".
- **Mixed form:** dirty one field, leave another off-spec
  alone. Assert dirty field updates, off-spec field preserves.

### Manual walkthrough

1. On staging, set `ir_brightness=129` via admin raw KV editor.
2. Open customer page; confirm slider pinned at 100, warning
   shows 129.
3. Submit form without touching anything. Refresh; confirm
   `ir_brightness` still 129.
4. Move the slider to 50. Submit. Confirm `ir_brightness` now 50.
5. Set `wifi_password=actualsecret`. Open page; confirm input
   is empty (not pre-filled with the secret).
6. Submit form without touching wifi field. Confirm KV still
   holds "actualsecret".
7. Type a new password, submit. Confirm KV updated.
8. Type "13" into `start_time`; confirm field flips red. Type
   ":30"; confirm flips green.

### Sign-off checklist

- [ ] All automated tests green.
- [ ] Walkthrough 1–8 behave as described.
- [ ] No silent data stomping in any path tested.
- [ ] CSP clean (JS loads as same-origin under `script-src
      'self'`).
- [ ] Performance: first-interaction-to-feedback under 50ms on
      typical hardware.

### Rollback

Config flag falls back to P3 rendering with native browser
behavior only. Off-spec values still display with warning, just
without the snap/dirty mechanism.

---

## P5 — CSP enforcement (parallel track)

**Goal:** flip CSP from Report-Only to enforcing across all
stra2us routes, without breaking existing admin/api pages.

### In scope

- Audit existing admin/api routes for inline `<script>`,
  `on*=` handlers, inline `style=`, `javascript:` URLs,
  external CDN assets, `eval`/`Function` usage.
- Fix violations as they appear in Report-Only telemetry.
- Flip middleware from `Content-Security-Policy-Report-Only`
  to `Content-Security-Policy` on all routes.

### Out of scope

- The customer-facing `/app/<app>/...` route ships strict from
  P2 onward (new template territory, no legacy to clean up).

### Deliverables

- Audit report (markdown doc) listing every violation found
  and how it was fixed.
- Code changes per violation.
- Middleware flip commit.

### Dependencies

- P0 (Report-Only deployed).
- At least one full release cycle of Report-Only telemetry.

### Automated tests

- After flip: every route returns `Content-Security-Policy`
  header (not `-Report-Only`).
- Smoke tests across admin and api routes confirm no console
  errors / no broken pages under enforcing CSP.

### Manual walkthrough

1. Review the audit report end-to-end before the flip.
2. Stage the enforcing CSP change. Click through every admin
   surface (login, dashboard, catalog editor, raw KV editor,
   user management). Confirm no console errors, no broken UI.
3. Hit api routes via the docs/spec; confirm headers correct.
4. Confirm Report-Only telemetry has been quiet for ≥1 release.

### Sign-off checklist

- [ ] Audit report committed.
- [ ] All known violations fixed.
- [ ] Report-Only telemetry quiet for ≥1 release.
- [ ] Enforcing CSP doesn't break any admin/api flow tested.
- [ ] Header present on every route.

### Rollback

Revert middleware to Report-Only. Audit fixes stay (they're
improvements regardless).

---

## Cross-cutting: when something breaks during walkthrough

If any sign-off item fails on staging:

1. Team files the gap as a sub-issue against the phase.
2. We don't proceed to the next phase until it's resolved.
3. If the gap reveals a design problem (not just an
   implementation bug), update the FR and this plan before
   re-attempting.

The point of phase gates is to catch design drift early. A
walkthrough that finds something genuinely wrong is doing its
job; that's not a failure of the team or the plan.

## Open questions for the team to resolve in implementation

These are small enough that the FR doesn't have to settle them,
but the team should pick before coding the relevant phase:

- **CSP report sink** — server log or dedicated endpoint?
  (P0)
- **Lint module packaging** — sub-package of `stra2us_cli` or
  separate shared package? (P0)
- **`product_name` / `logo_alt` exact placement** in section
  chrome — design choice, doesn't change behavior. (P3)
- **Mixed simple+object enum lists** allowed? Default to
  "no, lint rejects" unless someone has a reason. (P0)
- **App slug character set for `data-app`** — confirm existing
  slug constraints make CSS-selector escaping unnecessary. (P2)

None block starting; flag in PR description for the relevant
phase.
