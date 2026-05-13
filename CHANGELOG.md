# Changelog

Notable changes per release. The per-release detail lives in:

* [`docs/fr_catalog_app_ui_progress.md`](docs/fr_catalog_app_ui_progress.md)
  — every catalog-FR-related release through v1.6.9 written up in
  full, including verification paths and lessons-learned.
* `git log v<X.Y.Z>` — the commit history per tag.

This file is the at-a-glance summary. Boundary markers between
versions go here; deep dives go in the linked docs.

---

## v1.7.0 — 2026-05-13

The v1.6.x cycle wrapped. Eleven point releases evolved the
v1.6.0 catalog-app-ui FR baseline into a markedly different
codebase; v1.7.0 is the boundary marker. No new functionality
ships at v1.7.0 itself — it's the cumulative state of v1.6.0
through v1.6.9 stamped as the new stable shape.

### Architectural shifts since v1.6.0

* **Encrypted-field customer-page rendering: stripped + simplified.**
  v1.6.0 through v1.6.7 had a dedicated encrypted-Reveal branch
  in the customer-page renderer — empty `<input>` + Reveal
  button + server-fetch via `/peek/kv/` on click. v1.6.5 patched
  the type-flip; v1.6.7 added peek-while-typing; v1.6.8's first
  iteration tried to fix the resulting data-loss footgun
  (`load + Save = wipe stored value`); none fully worked.
  **v1.6.8 commit 1 stripped the encrypted-Reveal branch
  entirely.** Encrypted fields now render through the same
  widget-renderer path as any other field, with their plaintext
  populated in `value=` and `data-original=`. A `widget: secret`
  field gets `type="password"` (browser-side visual masking) +
  a Show/Hide button (pure client-side type-flip). The data-loss
  bug closes architecturally. Trade-off accepted: plaintext is
  in the rendered HTML (DevTools-readable); HTTPS + at-rest
  encryption + device-side wire encryption all unchanged.

* **Catalog as authoritative contract.** v1.6.5 and v1.6.7 made
  the catalog's `encrypted:` field load-bearing on both the
  server-side form-submit (v1.6.5: `routes_app_form.py` sets the
  `:enc` sidecar from the catalog, not from prior state) and
  the CLI (v1.6.7: `stra2us set` ignores the `--encrypted` flag
  for catalog-declared keys and uses the catalog's declaration
  instead). Pre-v1.6.5 the catalog's `encrypted:` was
  documentation-only; the operator's flags drove behavior. Now
  the catalog drives.

* **Form-submit touched-state framework gained two omit branches.**
  v1.6.7 added `data-from-default` plumbing: clean fields whose
  value came from the catalog default get omitted from the
  POST, preventing the form-submit from materializing per-device
  overrides for fields the operator never touched. (The v1.6.8
  encrypted-skip branch was added then later removed when the
  populated-value approach made it unnecessary.)

### New features

* **`catalog lint` subcommand** (v1.6.4) — runs the publish-time
  lint without uploading. Same gate as `publish` (exit 5 on
  errors, 0 with warnings), no network call. Useful for catalog
  authors iterating on YAML before having server creds, and for
  verifying lint-rule changes against a local file.

* **Catalog-lint secret-pairing warnings** (v1.6.4, rewritten in
  v1.6.8) — soft warnings when `widget: secret`, `encrypted:`,
  and `write_only:` are partially combined. Rewritten in v1.6.8
  to reflect the post-encrypted-Reveal-strip semantics
  (rationale shifted from "data-loss footgun" to "plaintext-in-
  HTML exposure"; the third warning now distinguishes low-value
  secrets from "set but never read" higher-value secrets).

* **Device-name reverse-index lookup** (v1.6.7) —
  `device_to_app:<client_id>` written at provision time +
  cleared at revoke. `lookup_device` consults it first (O(1))
  before falling back to SCAN for legacy devices. Closes a
  workflow gap: pre-v1.6.7 a provisioned-but-unwritten device
  was invisible to the customer landing form's lookup, forcing
  "provision → flash → device heartbeats → configure" as the
  forced order. Post-v1.6.7 the natural order ("provision →
  configure → flash") works.

* **Per-app favicon** (v1.6.7) — `theme.favicon_asset` field on
  the catalog Theme model. Customer page emits the catalog's
  favicon if set, defaults to a built-in 256×256 PNG (derived
  from the admin's source) otherwise. Closes the `/favicon.ico`
  404 noise.

* **Activity Logs action filter** (v1.6.9) — client-side
  substring filter box above the table; filters in-memory
  against the fetched batch.

### Observability + ops hardening

* **Structured error logging on `/kv/` and `/q/` 500s** (v1.6.6) —
  `stra2us.errors` logger captures unhandled exceptions in the
  device-API middleware with request context (path, method,
  client_id). Activity log entries get `Error (500) [ExceptionClass]`
  tags so the distribution of failure modes is visible at a
  glance via `XREVRANGE`.

* **`ClientDisconnect` mapped to HTTP 499** (v1.6.6) — flaky
  device connections that drop mid-request no longer log as
  500s with full tracebacks. Activity log shows
  `Client disconnect (499)`, no error log emitted. Clears
  most of the "infrequent 500s on staging" noise.

* **Pre-commit cache-bust hook** (v1.6.9) — `.githooks/pre-commit`
  catches missed `?v=N` bumps when static JS/CSS files change
  without updating the referrer HTML's cache-bust query string.
  Closes the footgun that ate two hotfix cycles. Install via
  `git config core.hooksPath .githooks` (one-time per clone).

### Bug fixes worth naming

* Monitor tab cursor regression (v1.6.2)
* `formatAge` "just now ago" string bug (v1.6.3)
* Device picker modal viewport overflow (v1.6.3)
* Device picker doesn't recognize wildcard ACL coverage (v1.6.3)
* Catalogs admin view listing asset keys as catalogs (v1.6.5)
* Customer-page form-submit clobbering on stale render (v1.6.5
  / v1.6.7 / v1.6.8 — the long saga, finally resolved
  architecturally)
* Admin shell unusable on mobile (v1.6.5)

### Deploy / runbook

* **`docs/release_cycle.md`** — operator runbook capturing
  the eight-phase cycle, naming the common footguns
  (cache-bust, stale local main, tag-vs-staging mismatch,
  provisioned-but-unwritten devices). Written during v1.6.x
  as the cycle stabilized.

* **`tools/examples/lint_smoke/*`** (v1.6.4) — checked-in
  fixtures for verifying the `catalog lint` subcommand
  end-to-end (clean / warns / errors cases).

### Closed TODOs through v1.7.0

The TODO list shrank from 13 to 11 net over v1.6.x (8 closures,
6 additions, net -2). Items closed:

* `widget:secret` cursor + Monitor Clear regression
* Catalogs admin view shows asset keys
* Admin sidebar mobile usability
* `lookup_device` doesn't find provisioned-but-unwritten devices
* `stra2us set` honors catalog `encrypted:`
* Form-submit stuffs catalog defaults
* Customer favicon 404
* `[HIGH]` Cache-bust automation
* Activity Logs action filter
* `write_only` multi-writer-race documentation (closed obsolete)

### Remaining open at v1.7.0

* Backup/restore (whole-instance + per-app)
* Automate pre-build / external-file staging
* Thorough responsive pass (admin + customer)
* Basic Auth brute-force lockout
* First-class "global admin" recognition
* Gate `/app/` landing behind OAuth
* Scoped admins can't see Activity Logs
* `tools/stage nuke` *(deferred)*
* Synthetic device-traffic CLI
* Generalize `widget: radio` to any enum-backed field
* Surface running release version in admin UI
* Extend v1.6.6 instrumentation to catch `HTTPException(500)`

---

## v1.6.0 — 2026-04-XX

Original catalog-app-ui FR ship. See
[`docs/fr_catalog_app_ui_progress.md`](docs/fr_catalog_app_ui_progress.md)
for the per-phase write-up.

---

*(Pre-v1.6.0 history lives in git log + the v1.5.x FR docs
under `docs/fr_v15_*.md`.)*
