# CSP enforcement — admin / api audit inventory

*Drafted 2026-05-08. Companion to
[`fr_catalog_app_ui.md`](fr_catalog_app_ui.md) ("CSP rollout
against an app that has no CSP today") and
[`fr_catalog_app_ui_plan.md`](fr_catalog_app_ui_plan.md) P5.
This is the **inventory** of CSP violations the admin/api surface
contains as of P5 sign-off — *not* a record of fixes (none of the
admin violations were addressed during P5; the customer-facing
`/app/*` route flipped to enforcing without touching admin).*

## Status

The catalog-app-ui FR's P5 took the **two-track flip** described
in the FR's "CSP rollout" section:

* **`/app/*` enforcing** — customer-facing surface, built
  CSP-clean from P0 onward; no violations.
* **`/admin/*` and `/api/*` Report-Only** — pre-existing surface
  that predates CSP. Cleaning it up is a real refactor; doing
  it inside the catalog-app-ui FR would have ballooned scope
  past the FR's actual goals. Deferred to a separate effort,
  tracked here.

Until the admin cleanup lands, the existing CSP middleware keeps
emitting `Content-Security-Policy-Report-Only` on admin/api
responses, with reports still flowing to `/api/_csp_report` and
the `stra2us.csp` logger. Any new admin change that introduces
a *new* violation surfaces in telemetry before it reaches enforcing.

## Inventory (as of 2026-05-08)

Counts from a codebase grep on the `catalog-app-ui` branch.
Numbers are approximate — within a few of the actual count, not
exact.

### Inline event handlers (`on*=`)

* **`backend/src/static/index.html`**: ~30 occurrences.
  Examples: `onclick="openKvModal()"`, `onclick="closeKvModal()"`,
  `onclick="saveKv()"`, `onclick="aclAddRule()"`,
  `onclick="switchCatalogTab('variables')"`,
  `onclick="downloadBackup()"`, `onclick="monitorStart()"`,
  `onclick="monitorStop()"`, …
* **`backend/src/static/app.js`**: ~25 occurrences inside
  template literals that are `innerHTML`-injected at runtime.
  Examples: peek/edit/delete buttons, ACL rule rows, catalog
  navigation rows, scope-save buttons. These are dynamically
  rendered, so a CSP flip would block them at *interaction
  time* (no console error until the user clicks something).

### Inline `style=` attributes

* **`backend/src/static/index.html`**: ~15 occurrences (e.g.
  `style="margin-top: 16px;"`, `style="display:none;"`,
  `style="flex:1;"`, `style="background: var(--accent-danger);"`).
  Typically one-off layout tweaks that should be CSS classes.

### External CDN resources

* **`backend/src/static/index.html` line 8** —
  `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">`
* **`backend/src/static/index.html` line 470** —
  `<script src="https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js" integrity="..." crossorigin="anonymous">`

### Inline `<script>...</script>` blocks

None found — admin's JS lives entirely in
`backend/src/static/app.js`. ✅

### `eval` / `new Function` / string-arg `setTimeout` / `setInterval`

Not yet greppedconfirm during the admin cleanup; common in
older admin UIs but the cursory `grep -E "\beval\b|\bnew Function"` on
`backend/src/static/app.js` came up clean during the P5 audit
(spot check, not exhaustive).

### `javascript:` URLs in `href`

Not seen in the spot-check; `href="#"` plus an `onclick=` is
the dominant pattern (still a CSP violation via the inline
handler, separate from the URL scheme).

## Fix patterns

When the cleanup happens, the standard transformations are:

| Violation | Standard fix |
|---|---|
| `<button onclick="foo()">` in static HTML | Add `id="..."` or `data-action="foo"`, then `document.getElementById('...').addEventListener('click', foo)` (or a delegated listener on a parent that dispatches by `data-action`) in app.js. |
| `onclick="..."` in template-literal-rendered HTML | Replace with `data-action="..."` in the template; rely on a single delegated listener on the parent container that reads `data-action` and dispatches. Avoids re-binding listeners on every re-render. |
| `<div style="display:none;">` | Move to a class like `.hidden { display: none; }` (already exists in `styles.css`). |
| `<div style="margin-top: 16px;">` | Add a utility class (`.mt-2` or named `.kv-input-row`); avoid expanding the inline-style allowlist. |
| `<link href="https://fonts.googleapis.com/...">` | Self-host — download the Inter font subset, serve from `/admin/_static/fonts/`. Or accept the `font-src 'self' https://fonts.gstatic.com` (and `style-src ... https://fonts.googleapis.com`) allowlist additions and document the trust decision. |
| `<script src="https://cdn.jsdelivr.net/.../js-yaml.min.js">` | Self-host — same approach P3 took for the customer page. The admin currently parses catalog YAML client-side via this; either keep js-yaml self-hosted, or switch to fetching parsed catalogs from a new admin endpoint that does the parse server-side. |

## Estimated effort

Rough sizing — not a commitment, just a sniff test:

| Slice | Est. work |
|---|---|
| Admin onclick → addEventListener (delegated, ~20 unique action names after dedup) | ~4h |
| `index.html` inline styles → CSS classes | ~1h |
| Self-host js-yaml | ~30 min |
| Self-host Inter font (subset selection, vendor a 2-weight WOFF2) | ~1h |
| End-to-end retest of admin UI | ~2h |
| Flip middleware to enforcing (single line) + smoke pass | 30 min |

Total: roughly one focused day. The end-to-end retest is the
unbounded part — every admin click flow needs at least a touch.

## Sequencing recommendation

When the cleanup is picked up:

1. Self-host the two CDNs first (smallest change, biggest CSP
   surface reduction). Run staging in Report-Only for ≥1 release;
   confirm the only remaining violations are the inline handlers
   + styles you expect.
2. Convert inline styles. Easy mechanical work; no behavior risk.
3. Convert inline handlers in batches, by feature area (KV
   modal first, then ACL editor, then catalog detail, etc).
   Each batch can land + soak in Report-Only before the next.
4. Once Report-Only telemetry is quiet for a release, flip
   `enforce_path_prefixes` to include `/admin/` and `/api/`
   (or pass `enforce_default=True` and use
   `report_only_path_prefixes` for any remaining holdouts).
5. Final smoke pass. Done.

## Test infrastructure

The CSP middleware tests in
[`backend/tests/test_csp_middleware.py`](../backend/tests/test_csp_middleware.py)
already cover the flip-knob mechanics (`enforce_path_prefixes`,
`enforce_default`, `report_only_path_prefixes`). When the admin
cleanup lands, add a smoke test that hits each major admin
endpoint and asserts the response carries enforcing CSP — same
shape as the existing `test_main_app_enforces_csp_on_customer_route`.

## Why this didn't ship in P5

P5's plan scope was "audit + flip." Audit produced this
inventory; flip happened for the CSP-clean half (`/app/*`). The
admin cleanup needs:

- Real-traffic Report-Only telemetry over ≥1 release cycle to
  confirm no violations beyond the static inventory exist.
- A focused day of refactor work that's mechanical but tedious.
- Coordination with whoever else uses the admin UI to retest
  flows that aren't covered by automated tests.

None of that is hard. None of it belongs inside the
catalog-app-ui FR. Spinning it off as a separate effort with
this inventory as a starting point is the cleanest move.
