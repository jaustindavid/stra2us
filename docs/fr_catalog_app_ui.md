# FR: Catalog-driven app page UI — widgets + theming

*Drafted 2026-05-07 — design for review, not yet implemented.
Extends the existing catalog mechanism in
[`docs/catalog_spec.md`](catalog_spec.md) and the customer-facing
app page in [`fr_application_view.md`](fr_application_view.md).*

## Status

**Pending — not yet implemented.** Filed in response to the
observation that today's customer-facing app page (`/app/<app>/<device>/...`)
treats every value as a free-text input with no app-specific
branding, while the underlying intent is much more constrained
(many strings are enums; many ints have ranges; vendors want
their colors and a welcome message).

The two halves — widget hints for input fields and theme/markdown
for presentation — are addressed together in this FR because
they share the catalog surface, the publish-time validation
infrastructure, and the same product goal: make the page feel
like a first-party app for the vendor without letting the vendor
ship code that runs on our server.

## Problem

The catalog (`<app>.s2s.yaml`) declares fields with types,
defaults, and descriptions. The server renders a customer-facing
page from that catalog. Today the rendering is generic:

- Every `str` is a free-text input. Many are really enums
  (`display_mode` ∈ `{clock, weather, photo, off}`); customer
  types `Clock` instead of `clock` and silently breaks their
  device.
- Every `int` is a number input. Many have ranges (`brightness`
  is `0..100`); no UI affordance, no server-side enforcement.
- The page chrome is stra2us-themed. A critterchron customer
  sees no critterchron branding, no welcome copy, no link to the
  vendor's docs. The "first-party app" feel is broken at the
  surface.

Two extreme answers are bad:
- **Arbitrary CSS/HTML/JS from clients.** Security disaster (XSS,
  CSS-based exfiltration, layout sabotage). Hard sandbox to
  build, hard to audit, breaks accessibility.
- **No customization at all.** Vendors who want a branded
  experience accept the mismatch or build an external UI
  (the "nuclear option" — duplicates the auth and hosting
  story, breaks first-party login).

Sweet spot: a closed, declarative extension to the catalog —
metadata (widget hints) and a tight allowlist of theming knobs
(colors, fonts, logo, sanitized markdown blocks) — that the
server's renderer consumes. Vendor stays as ignorant as today;
all customization lives where the catalog lives.

## Goals

1. **Catalog-author can express common UX intents in a line of
   YAML.** "This is an enum." "This int is 0–100." "Use these
   brand colors." "Show this welcome blurb above the form."
   No code, no markup, no separate schema files.
2. **Server renders appropriate widgets and applies theming**
   without per-app code.
3. **The customer UI tries to keep the customer between the
   lines.** The form makes a non-enum value or out-of-range int
   awkward to enter (dropdowns, native HTML5 attributes, plus a
   small JS handler for per-keystroke pattern feedback — see
   "Validation"). It does *not* gate writes server-side.
   The catalog is **guidelines, not rules** — the server
   stores anything any authorized writer sends, including the
   customer-UI write path; the UI is the only surface that
   tries to constrain values, and only as a UX nicety.
4. **Constant security surface.** Whatever the vendor ships,
   they can't run arbitrary code, can't load arbitrary
   resources, can't reach outside their page section.
5. **Forward-compatible.** Unknown widgets / theme keys / markdown
   tags fall back to defaults. Old catalogs work on new servers;
   new catalogs degrade gracefully on old servers.
6. **Pareto-efficient.** ~90% of the customization a typical
   app-vendor wants, with ~10% of the implementation effort of a
   full UI-customization framework.

## Non-goals

- **Arbitrary CSS, HTML, or JavaScript from clients.** Hard no.
  Even sanitized CSS is rich enough to do tracking and layout
  sabotage; HTML/JS introduces a code-execution surface.
- **Custom widgets per app.** Widget vocabulary stays centrally
  curated. App-vendors pick from what we offer.
- **Layout / grouping / ordering** of fields beyond catalog
  order. Possibly a v2; not now.
- **Conditional fields** (show X only if Y == foo). Useful and
  complex; defer.
- **Per-customer / per-device theming.** Theme is app-level only.
- **i18n / localized markdown.** One markdown blob per block;
  no per-locale variants.
- **Replacing the catalog format.** Strictly additive — old
  catalogs remain valid.

## Design — Part 1: Widget hints (input layer)

### Extended field schema

A catalog field today (illustrative; adjust to actual shape
in `catalog_spec.md`):

```yaml
fields:
  display_mode:
    type: str
    default: clock
    description: "What the display shows when idle"
```

With widget hints:

```yaml
fields:
  display_mode:
    type: str
    default: clock
    description: "What the display shows when idle"
    enum: [clock, weather, photo, "off"]
```

That's the entire ergonomic ask: one extra line per hint, no
schema changes for the underlying value (it's still `str`).
The server sees `enum: [...]` and renders a `<select>` instead
of a text input.

The full v1 vocabulary:

| Hint | Applies to | UI effect (best-effort) |
|---|---|---|
| `enum: [a, b, c]` *or* `enum: [{value, label}, ...]` | `str`, `int` | Dropdown / radio (label shown, value submitted); customer can't pick a value outside the set through the form |
| `min: N` | `int` | `<input type=number min>` — browser blocks submit |
| `max: N` | `int` | `<input type=number max>` — browser blocks submit |
| `step: N` | `int` | Slider / number-input step |
| `widget: slider` | `int` (with min+max) | `<input type=range>` instead of number |
| `widget: secret` | `str` | Password-style masked input. **Cosmetic only** — see note below; the value sits in plaintext in KV and is visible to anyone with read ACL. Pair with `write_only: true` if the goal is to never display the existing value. |
| `write_only: true` | `str` | Renderer ships an empty input regardless of the stored value. On submit, an empty input is treated as "no change" (the original value, tracked via the touched-state mechanism, is preserved); a non-empty input writes through. The customer can rotate the value but never sees the existing one through the form. |
| `widget: radio` | `str` (with enum) | Radio buttons instead of dropdown |
| `multiline: true` | `str` | `<textarea>` |
| `max_length: N` | `str` | `maxlength` attribute |
| `pattern: <regex>` | `str` | `pattern` attribute (browser blocks submit on mismatch) + per-keystroke red/green styling via a small `input`-event handler — see "Validation" |
| `help: "..."` | any | Tooltip / inline help below the field |

Field constraints listed above (`enum`, `min`, `max`, `pattern`,
`max_length`) shape the rendered widget and the form's HTML5
constraints. They are not enforced anywhere else: a write that
arrives over HTTP with a non-conforming value is stored verbatim.

**Numeric `enum` and `min`/`max` are mutually exclusive.** A
field declares one or the other, not both. Catalog publish lint
flags the combination.

**`widget: secret` is a UI affordance, not a security
boundary.** The masked input keeps the value off the customer's
screen during typing; it does *not* encrypt the value, prevent
it from being read back, or stop anyone with `kv:r` ACL on the
key from seeing it in the raw KV editor or via `/kv/<path>`.
The storage layer holds plaintext. If the intent is "the
customer can rotate this but should never see the current
value through the form," combine with `write_only: true` —
the renderer then ships an empty input regardless of the
stored value, and the touched-state mechanism (see
"Implications for displaying out-of-spec values" → submit
semantics) makes "no change" mean "preserve original." For
secrets that genuinely shouldn't be readable from KV at all,
this FR isn't enough — that's a "secret values" feature
whose home is the storage layer, not the catalog UI.

Each hint is **optional**; a field with no hints renders as today.

**Two enum forms, both supported:**

```yaml
# Simple — value used as both submitted value and displayed label
enum: [clock, weather, photo, off]

# Object form — pretty labels with raw values underneath
enum:
  - {value: clock, label: "Clock face"}
  - {value: weather, label: "Weather"}
  - {value: photo, label: "Photo carousel"}
  - {value: "off", label: "Off"}
```

The simple form is treated as the object form with `label == value`.
Server accepts either; renderer shows the label, submits the value.

**Validation note:** every hint above is a *UI affordance*. The
form makes the wrong value awkward to enter — dropdowns omit it,
sliders don't reach it, the pattern attribute paints it red — but
nothing on the server side rejects an HTTP write that bypasses the
UI. See the "Validation" section below for why.

### Renderer dispatch

```
type=int, has enum                    → <select>
type=int, has min+max+widget=slider   → <input type="range">
type=int, has min/max                 → <input type="number" min max>
type=int, otherwise                   → <input type="number">

type=str, has enum, widget=radio      → radio button group
type=str, has enum                    → <select>
type=str, multiline=true              → <textarea>
type=str, widget=secret               → <input type="password">
type=str, has pattern                 → <input type="text" pattern>
type=str, otherwise                   → <input type="text">
```

Unknown `widget:` values fall through to the type-default.
Unknown top-level hints are ignored.

### Validation: UI-only, best-effort

There is no server-side validation against the catalog on any
write path. From the storage layer's perspective, stra2us is a
dumb network-attached key-value store: an authorized write of
any byte sequence to any key the writer has ACL for is stored
verbatim and returned verbatim on read. The HMAC signing layer
and ACLs govern *who* can write *where*; the catalog never
narrows what *values* anyone may write.

**The catalog is guidelines, not rules.** It tells the renderer
how to draw the form and tells client developers what a
well-behaved client should write. It does not police writes.

The customer-facing form does best-effort prevention via the
widget choices above:

- `enum` renders as `<select>` / radio — the customer can only
  *choose* a value in the list.
- `min`/`max`/`step` use native `<input type=number>` constraints
  — the browser blocks form submit on out-of-range values.
- `pattern` uses the HTML5 `pattern` attribute — the browser
  blocks form submit on mismatch. HTML5 alone gives submit-time
  + `:invalid`-styled feedback, not per-keystroke. The renderer
  ships a small `input`-event handler (~10 lines, applies
  globally to any input with a `pattern` attribute) that
  toggles `data-valid="true|false"` on the input as the user
  types; the base stylesheet styles `[data-valid="false"]` red
  and `[data-valid="true"]` green. This is the "live red/green"
  experience — produced by the JS layer, not by HTML5.
- `max_length` uses `maxlength` — typing past the limit is just
  prevented.

Anything that bypasses the form (a curl by an authorized client,
a device write, an admin's raw KV editor) reaches storage
unfiltered. That is the intended contract.

**Why no server-side gate:**

- **Devices write whatever they need to.** The client SDK contract
  is "writes succeed unless auth fails or the server is down."
  Catalog-driven rejection would break devices in the field
  whenever a catalog is republished with tighter constraints — a
  UI policy decision the device wasn't part of.
- **The catalog evolves at the UI half's cadence.** A catalog
  republish that tightens an enum is a UI affordance, not a wire
  protocol change. Tying writes to it conflates the two.
- **It's the developer's job to behave.** A client that writes
  arbitrary strings to an enum is a buggy client; the server is
  not its conscience.
- **Validation in two places drifts.** A single source of
  prevention (the rendered form) avoids server/client logic
  divergence.

**The shape, stated plainly:**

| Surface | Auth | Server validates? | What happens |
|---|---|---|---|
| Device `/kv/<path>` (HMAC-signed) | Per-device ACL | No | Stored verbatim |
| Customer-facing `/app/<app>/<device>/...` form | Admin session / OAuth | No | Stored verbatim — but the form makes off-spec values awkward to even submit |
| Admin's raw `/api/admin/kv/<path>` editor | Superuser | No | Stored verbatim (operators are trusted) |

The contract is "the UI is opinionated; storage is not." Anything
can land in storage; the renderer copes (see "displaying
out-of-spec values" below).

### Implications for displaying out-of-spec values

The renderer regularly encounters values that don't conform to the
current catalog (a device wrote one, an admin raw-edited one, a
catalog tightened after the fact). The rule is: **show what's
there; don't correct it; let the user only pick values within the
catalog if they choose to change anything.**

Concretely:

- **Show the value as-is**, with a soft-warning indicator (red
  highlight, "out of current allowed values" tooltip; current
  allowed set / range shown for context). The renderer does not
  rewrite, snap, clamp, or default the displayed value — what the
  device wrote is what shows.
- **The widget continues to advertise only catalog-valid values.**
  An enum dropdown lists only the catalog's enum. A range slider
  spans only `min..max`. The customer cannot type, pick, or drag
  to the off-spec value; the only way to *preserve* it is to not
  change the field.
- **Saving changes to an off-spec field stomps the device's
  value.** That's expected and fine — the customer made an
  informed choice through a UI that warned them. There is no
  "save just this other field; preserve the off-spec one"
  affordance; saving the form writes the form's current state.
- **Snap-on-edit is OK, but only on edit.** A `[1..128]`
  brightness field showing `129` renders the slider pinned at
  `128` while the warning badge displays the actual value
  (`129`). If the customer drags the slider, the on-screen value
  moves into range and they cannot drag back up to `129`. If the
  customer doesn't touch it, the original `129` is what gets
  written on form submit — *not* the clamped `128` the slider is
  visually showing.

  This requires the form to **track touched state per field**, not
  just read `<input>.value` at submit. The renderer ships each
  field with its original stored value as a `data-original`
  attribute and a `dirty` flag flipped by the first `input` /
  `change` event on that field. Submit serializes:
  `dirty == true` → the live `<input>` value; `dirty == false` →
  the `data-original` value verbatim. This is the difference
  between "respects the device's reality" and "silently
  rewrites every off-spec field on every form submit." Test
  coverage for this case is mandatory — easy bug to ship, hard
  to spot in QA.

This is rare. It happens when the catalog is stale relative to
the devices, or a device is operating beyond the catalog's
declared bounds. **We trust the devices.** A customer who never
opens the UI never has their device's behavior constrained by
the catalog or its staleness; the catalog is a UI surface, not
a wire-protocol contract.

## Design — Part 2: Theme + markdown blocks (presentation layer)

### Theme variables (closed allowlist)

The catalog YAML can declare a `theme:` block. Each value is
validated against a per-key format. Unknown keys are ignored;
missing keys fall back to stra2us defaults.

```yaml
theme:
  primary_color: "#5b3fb8"     # buttons, accent borders
  accent_color:  "#ffb86c"     # highlights, links
  bg_color:      "#f7f3eb"     # page background
  text_color:    "#2a2a2a"     # body text
  font_family:   "system-ui"   # one of the allowlisted families
  logo_asset:    "logo.svg"    # references _assets/logo.svg in the catalog bundle
  logo_alt:      "Critterchron"
  product_name:  "Critterchron"
```

**Per-key validation (publish-time lint):**

| Key | Format | Constraint |
|---|---|---|
| `*_color` | `#RRGGBB` or `#RGB` | Hex only; no `rgb()`, no `var(...)`, no escapes |
| `font_family` | string | Allowlist: `system-ui`, `sans-serif`, `serif`, `monospace`. **No web fonts** (they exfiltrate via load events) |
| `logo_asset` | filename | Must reference a file present in the catalog's `_assets/` bundle (see "Assets" below). No external URLs. |
| `logo_alt`, `product_name` | string | Length-capped (100 / 60 chars); plain text only |

**How the server applies them:**

The per-app theme is served as a **same-origin external
stylesheet**, not as inline `<style>`:

```
GET /app/<app>/_theme.css?v=<catalog-hash>
  Content-Type: text/css
  Cache-Control: public, max-age=31536000, immutable
```

Body:

```css
[data-app="critterchron"] {
  --app-primary: #5b3fb8;
  --app-accent:  #ffb86c;
  --app-bg:      #f7f3eb;
  --app-text:    #2a2a2a;
  --app-font:    system-ui;
}
```

The page references it with `<link rel="stylesheet"
href="/app/<app>/_theme.css?v=<hash>">`. The base stylesheet
uses `var(--app-primary, <stra2us-default>)` etc. for elements
that should theme. Variables not set in the catalog fall back to
the stra2us defaults via the `var(name, fallback)` form.

This shape exists specifically so the CSP for the customer-facing
page can be `style-src 'self'` with no `'unsafe-inline'` and no
per-response nonce. See "Content Security Policy" under the
security model. (We considered inline-with-nonce; per-app
external CSS is the cleaner story — nothing inline to reason
about, naturally cacheable, cache-busts on republish via the
`?v=<hash>` parameter.)

**Why this is safe:**
- No `<style>` tags anywhere derived from catalog input. The
  server emits the rule into a static-shape CSS file; the
  catalog only provides values, not selectors or rules.
- Hex colors only — nothing exfiltration-capable.
- No web fonts — eliminates the load-timing exfil vector.
- Images self-hosted only (no external URLs in `logo_asset` or
  in markdown `<img>` — see "Assets" below). Eliminates the
  third-party tracking and cache-poisoning surface.
- All theming scoped to `[data-app="…"]` — can't escape into
  admin chrome, security banners, etc.

### Assets (self-hosted images)

Catalog images — logos, inline markdown `<img>` references — are
**always self-hosted by stra2us**. There is no external-URL
escape hatch; no CDN allowlist; no cross-origin image loads.

**Storage layout:**

The `tools/stra2us_cli/catalog publish` command treats a sibling
`_assets/` directory as part of the catalog bundle. Files in it
are uploaded into KV under the catalog's reserved namespace:

```
_catalog/<app>/_assets/<filename>     # bytes
_catalog/<app>/_assets/<filename>.meta # {content_type, sha256, size}
```

This sits alongside the existing `_catalog/<app>/...` stash —
same write path, same ACL story, same publish atomicity (assets
and YAML go up together; partial publishes are an existing
problem this FR does not introduce).

**Serve path:**

```
GET /app/<app>/_assets/<filename>
  → reads _catalog/<app>/_assets/<filename> from KV
  → returns bytes with stored content_type
  → cache headers: public, immutable (per-publish hash in URL
    handles invalidation)
```

The asset URL the renderer emits is
`/app/<app>/_assets/<filename>?v=<sha256-prefix>` — same-origin,
cache-busts on republish.

**Constraints (publish-time lint):**

| Constraint | Limit | Why |
|---|---|---|
| File size | 256 KiB per asset (configurable) | Catalog bundles aren't a file dump |
| Total bundle | 2 MiB per app (configurable) | Cap operator surprise |
| Content type | `image/svg+xml`, `image/png`, `image/jpeg`, `image/webp` | Closed allowlist; rejects `.ico`, `.gif`, etc. unless added |
| SVG sanitization | run through SVG sanitizer at publish, not render | SVGs are XML; same XSS hazards as HTML, plus external `<image href>` references |
| Filename | `[a-z0-9._-]+`, no leading dot, max 64 chars | Keeps URL space sane |

The reserved `_assets/` filename component means catalog fields
can't shadow it (`_assets` already starts with `_`, which is
reserved-namespace by existing convention).

**Why self-hosted:**
- One origin, one cache, one CSP rule (`img-src 'self'`).
- No third-party hosts to allowlist or audit; no operator-config
  required.
- Vendors get cache-immutable URLs without running any infra.
- The blast radius of a bad image is one app's page section.

### Markdown blocks (sanitized)

Catalog can declare a small set of markdown blobs at known
positions:

```yaml
ui:
  header_markdown: |
    ## Configure your Critterchron

    Settings sync to your clock within ~30 seconds. See
    [the docs](https://critterchron.example.com/docs) for help.

  footer_markdown: |
    Critterchron, Inc. · [Privacy](https://critterchron.example.com/privacy)
```

**Render path:**
1. Catalog publishes → server stores raw markdown alongside the
   catalog blob.
2. On page render, server runs each block through a hardened
   markdown→HTML sanitizer.
3. Sanitized HTML is inlined at the designated position
   (header above the form, footer below).

**Sanitization allowlist:**

| Allowed tags | Allowed attrs |
|---|---|
| `p`, `br`, `hr` | none |
| `strong`, `em`, `code`, `del` | none |
| `h2`, `h3`, `h4` (no `h1` — page already has one) | none |
| `ul`, `ol`, `li`, `blockquote`, `pre` | none |
| `a` | `href`: absolute `https://…` only. **Relative paths and same-origin absolute paths are stripped** (the link text is preserved, the `<a>` is unwrapped). Vendors who want to link to their own docs use full HTTPS URLs to their own domain; relative paths would resolve under stra2us, where the vendor doesn't own the namespace and the link would mislead. Auto-set `rel="noopener noreferrer"`, `target="_blank"` on surviving links. |
| `img` | `src` (must resolve to `/app/<app>/_assets/<file>` — see "Assets"), `alt` (length-capped) |

**Disallowed (silently stripped):**
- `<script>`, `<iframe>`, `<embed>`, `<object>`, `<style>`,
  `<link>`, `<meta>`, `<form>`, `<input>`
- `on*` event handlers
- `javascript:`, `data:`, `vbscript:` URLs anywhere
- Inline `style` attributes
- Anything the markdown library emits that we didn't allowlist

**Length limits:** `STRA2US_MARKDOWN_MAX_BYTES` per block
(default 4096). Catalog publish rejected if exceeded.

### Field-level markdown help (optional v1 add)

In addition to plain-text `help:` from Part 1, allow
`help_markdown:` per field with the same sanitizer treatment.
Renders as a small expanded-help block under the field input.
Useful for fields with non-trivial explanations (e.g. a
multi-condition pattern field).

## Combined example

A full critterchron catalog showing both layers in concert:

```yaml
# critterchron.s2s.yaml
theme:
  primary_color: "#5b3fb8"
  accent_color:  "#ffb86c"
  bg_color:      "#f7f3eb"
  text_color:    "#2a2a2a"
  font_family:   "system-ui"
  logo_asset:    "logo.svg"
  logo_alt:      "Critterchron"
  product_name:  "Critterchron"

ui:
  header_markdown: |
    ## Configure your Critterchron

    Settings sync within ~30 seconds. Need help? See
    [the docs](https://critterchron.example.com/docs).

  footer_markdown: |
    Critterchron, Inc.

fields:
  display_mode:
    type: str
    default: clock
    enum: [clock, weather, photo, "off"]
    help: "What the display shows when idle"

  ir_brightness:
    type: int
    default: 50
    min: 0
    max: 100
    widget: slider
    help: "0 = off, 100 = max"

  wifi_password:
    type: str
    default: ""
    widget: secret
    max_length: 63
    help: "WPA2/WPA3 passphrase"

  greeting:
    type: str
    default: "hi!"
    multiline: true
    max_length: 200
    help: "Shown on power-up. Newlines OK."

  start_time:
    type: str
    default: "07:00"
    pattern: "^([01][0-9]|2[0-3]):[0-5][0-9]$"
    help_markdown: |
      24-hour `HH:MM`. Examples: `07:00`, `13:30`, `23:59`.
```

Customer sees: a critterchron-branded page with the logo, a
welcome heading, a dropdown for display mode, a slider for
brightness (with min/max), a masked input for wifi, a textarea
with a character counter, a text input that goes red on each
keystroke until the value matches `HH:MM` (the JS `input`
handler described under "Validation"; submit is also blocked
by the browser's native `pattern` check), and an inline help
block
explaining the format. Same widget vocabulary, much better
fit, and a page that feels like critterchron's product.

## Catalog evolution

When a catalog is republished with tighter constraints (e.g.
adding an enum where there wasn't one, or adding a pattern):

- Already-stored values that don't match the new constraint
  remain stored — by definition, since nothing was ever rejected
  at write time. The renderer marks them ("not in current
  allowed values") so the operator/customer can fix if they
  want to.
- **Customer-UI** writes through the form can no longer
  *enter* an off-spec value via the widget (the dropdown won't
  show it, the slider won't reach it).
- **Device `/kv/` writes** are unaffected. Devices may keep
  writing values that the UI considers "out of spec"; the
  storage layer accepts them, and the UI shows them with the
  soft-warning indicator above. Catalog tightening is a UI
  policy change; it never reaches the wire protocol.

For theme/markdown/asset changes: applied immediately on next
render. No migration cost. Asset URLs cache-bust via the
publish-hash query parameter.

### Worked example — critterchron IR programs

Critterchron has a field that picks an "IR program" (an
agent-VM blob the clock executes). The catalog lists known
programs as an enum:

```yaml
fields:
  ir_program:
    type: str
    default: doozer
    enum: [doozer, scout, prancer]
    help: "Which IR pointer program runs at startup"
```

The clock's owner uploads a *new* IR program — `pixie` — by
writing the binary blob to KV and pointing the device at it.
The device now has `ir_program=pixie`, an enum value the
catalog doesn't yet know about. Three things happen, in order:

1. **Storage:** the device's write succeeds. Server stores
   `pixie` verbatim. The new IR runs on the clock immediately.
2. **UI:** when the customer next opens the app page, the
   dropdown shows `[doozer, scout, prancer]` — the catalog's
   current enum — with the *current* value rendered as a
   soft-warned label ("`pixie` (not in current allowed
   values)"). The customer can leave it (clock keeps running
   `pixie`) or pick something from the dropdown (which would
   replace `pixie` on save).
3. **Catalog update (when the app-vendor gets to it):** the
   vendor republishes the catalog with `pixie` added to the
   enum. UI catches up; warning disappears. No device
   intervention needed; the device never knew the UI was
   "behind."

This is the contract working as intended: **the device is the
source of operational truth**, the catalog is a UI hint that
trails the device's reality. A clock that adds a feature
faster than the catalog can describe it keeps working; the UI
catches up later.

## Implementation outline

1. **Catalog schema extension** — extend the parser
   (`tools/stra2us_cli/catalog.py`) to recognize `theme:`,
   `ui:`, and the new field-level hints.
2. **Catalog publish lint** — implemented as a single shared
   Python module (e.g. `stra2us_catalog/lint.py` in a small
   shared package, or vended via the existing `tools/stra2us_cli`
   package which the backend imports at server-start). The CLI
   calls it at `catalog publish` time; the server calls the same
   function when a catalog YAML is uploaded. **One implementation,
   two callers** — duplicating the rules in two places is the
   exact way they drift, and the duplication has bitten enough
   projects that it's worth the small upfront packaging work.
   The lint produces field-pointing errors:
   - `theme.primary_color: must be #RRGGBB hex, got "purple"`
   - `fields.brightness: enum and min/max are mutually exclusive`
   - `theme.logo_asset: references "logo.svg" but _assets/logo.svg
     not in bundle`
   - `ui.header_markdown: exceeds STRA2US_MARKDOWN_MAX_BYTES`
   - `_assets/banner.gif: content type image/gif not in allowlist`
   - Bonus lints (warnings, not errors): unused asset files,
     enum value collisions in the object form, fields with
     `widget: slider` but no `min`/`max`.
   The CLI fails the publish on errors; warnings are surfaced
   but pass-through. Server rejects upload of a YAML that fails
   lint (publish atomicity is enforced at the catalog blob
   level only — write paths into KV remain unvalidated).
3. **Markdown sanitizer** — pick a hardened library
   (`markdown-it-py` for parsing + `bleach` for allowlist
   sanitization is the pragmatic Python choice). Tests cover
   known XSS vectors: `<script>`, `javascript:` URLs, on-event
   attrs, `data:` URLs, malformed nesting, broken tags trying
   to escape the sanitizer. Run at render time.
4. **SVG sanitizer for `_assets/`** — at publish time, run any
   uploaded SVG through a sanitizer that strips `<script>`,
   `<foreignObject>`, external `href`/`xlink:href` references,
   on-event attrs.

   **Library choice.** There is no maintained Python equivalent of
   bleach for SVG (bleach itself is HTML-only — don't try to
   point it at SVG). The plan: parse with `defusedxml.ElementTree`
   (handles XML-bomb / external-entity attacks safely), walk the
   tree against a hand-rolled tag + attribute allowlist
   (~50 LoC), drop disallowed nodes/attrs, serialize back out.
   Allowlist tags: `svg`, `g`, `path`, `circle`, `ellipse`,
   `rect`, `line`, `polyline`, `polygon`, `text`, `tspan`,
   `defs`, `linearGradient`, `radialGradient`, `stop`, `use`
   (with same-document fragment refs only — no external
   `href`), `title`, `desc`. Allowlist attrs: geometric (`d`,
   `cx`, `cy`, `r`, `rx`, `ry`, `x`, `y`, `x1`, `y1`, `x2`,
   `y2`, `width`, `height`, `points`, `transform`,
   `viewBox`), presentation (`fill`, `stroke`,
   `stroke-width`, `opacity`, `fill-opacity`,
   `stroke-opacity`, `stroke-linecap`, `stroke-linejoin`,
   `stroke-dasharray`, `font-family`, `font-size`,
   `text-anchor`), structural (`id`, `class`). Reject any
   `style` attribute (kills inline CSS), any `on*` attr, any
   `href`/`xlink:href` not starting with `#`. Reject the SVG
   wholesale if it declares an external DTD or any
   `<!ENTITY>`. Tests cover the standard SVG-XSS corpus
   (`<script>`, `<foreignObject>` with HTML, `<use href>` to
   external doc, JS in `style`, `xlink:href="javascript:..."`).
5. **Asset serve route** — `GET /app/<app>/_assets/<filename>`
   reads from KV under `_catalog/<app>/_assets/<filename>`,
   returns bytes with stored `content_type`, `Cache-Control:
   public, max-age=31536000, immutable`.
   Sibling: `GET /app/<app>/_theme.css?v=<hash>` reads the
   catalog's `theme:` block and emits the scoped CSS rule for
   `[data-app="<app>"]` with the variables set; same cache
   headers. The CSS body is rendered from a fixed template via
   a parameterized serializer (placeholder substitution with
   already-lint-validated values), never string-concat of raw
   catalog input. This is what keeps inline `<style>` out of
   the page and lets the CSP stay strict.
5a. **Publish order** — the CLI pushes in this sequence:
    (1) for each file in `_assets/`, PUT bytes + meta to its KV
    location and re-read to verify; (2) PUT the catalog YAML
    (the commit point) and re-read to verify; (3) diff old vs
    new asset listing and DELETE files dropped from the bundle.
    A publish that dies between (1) and (2) leaves the prior
    catalog pointing at the prior assets — consistent. A
    publish that dies during (3) leaves stale assets, which
    the next publish cleans up.

    **Read-after-write assumption.** The "PUT then re-read"
    pattern assumes the read sees the write. stra2us's KV is
    backed by a single-node Redis instance; reads see writes
    immediately. If stra2us ever moves to replicated Redis or
    a multi-node KV, this publish flow needs to either pin the
    re-read to the write target or wait on replication
    acknowledgment before continuing. Calling that out here so
    a future deployment-shape change doesn't quietly break
    publish atomicity.
6. **Renderer dispatch** — modify the `/app/<app>/<device>/...`
   view (`backend/src/api/routes_app.py` or wherever) to:
   - Read theme + ui blocks; reference the external
     `/app/<app>/_theme.css?v=<hash>` via `<link>` (no inline
     `<style>`).
   - Wrap the section in `<section data-app="…">`.
   - Run header/footer markdown through sanitizer; inline.
   - Branch on field hints to pick widgets.
   - Emit `<img src="/app/<app>/_assets/...?v=<hash>">`.
   - Use `var(--app-…, fallback)` in the base stylesheet.
   - Ship the small global JS handler (~10 lines) that listens
     for `input` on any `[pattern]` field and toggles
     `data-valid="true|false"` based on the field's
     `validity.valid`. Base stylesheet styles
     `[data-valid="false"]` red and `[data-valid="true"]`
     green. This is the "live" feedback layer — explicit,
     auditable, and CSP-clean (it's a normal script served
     from `'self'`, not inline).
7. **CSP rollout** — see "CSP rollout against an app that has no
   CSP today" for the staged plan. In short:
   (a) audit existing surfaces (inline handlers, inline scripts,
   inline styles, external CDNs, `javascript:` URLs, `eval`-class
   patterns) and produce a fix list;
   (b) ship the customer-facing `/app/<app>/...` route under
   strict enforcing CSP from day one (it's new template
   territory; CSP-clean by construction);
   (c) ship admin/api routes under Report-Only with a violation
   collector for at least one release cycle;
   (d) fix what shows up;
   (e) flip admin/api to enforcing once Report-Only is quiet.
   Smoke-test the header on every release.
8. **Tests** — sanitizer XSS vectors; lint unit tests (every
   hint × value-shape combo); CSP header presence + shape on
   the customer route (enforcing, contains `script-src 'self'`,
   `style-src 'self'`, `default-src 'self'`, no `unsafe-*`);
   theme-CSS serializer fed adversarial values that *somehow*
   bypassed lint (e.g. `#ff0000; background: url(...)`) and
   asserts they don't escape into a second CSS rule — i.e. the
   parameterized serializer holds even when lint fails open;
   render integration tests on end-to-end scenarios (form
   renders correct widget, off-spec stored value soft-warns,
   asset URLs resolve, asset 404s are graceful, theme.css
   serves and applies, theme.css 404 falls back gracefully to
   stra2us defaults).
9. **Docs** — update `catalog_spec.md` with the new schema;
   add a "branding your app page" section to client_spec.md or
   its own short doc.

Estimated scope: ~1 focused day for catalog/widgets/markdown,
plus a half-day for the asset pipeline (publish bundle handling,
serve route, SVG sanitizer), plus the CSP rollout — which is
**not bounded by this FR's day-count**. Strict CSP for the new
customer-page route lands inside the half-day; the admin/api
Report-Only-then-enforce cycle is its own track that runs across
at least one release. Sanitizers, renderer dispatch, and the CSP
audit are the meaty parts; everything else is plumbing.

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `STRA2US_MARKDOWN_MAX_BYTES` | `4096` | Per-block limit on raw markdown size |
| `STRA2US_THEME_FONT_ALLOWLIST` | `"system-ui,sans-serif,serif,monospace"` | Permitted font families |
| `STRA2US_ASSET_MAX_BYTES` | `262144` (256 KiB) | Per-asset size limit at publish |
| `STRA2US_ASSET_BUNDLE_MAX_BYTES` | `2097152` (2 MiB) | Total `_assets/` bundle size per app |
| `STRA2US_ASSET_CONTENT_TYPES` | `"image/svg+xml,image/png,image/jpeg,image/webp"` | Allowed asset content types |

(There is intentionally no "validate writes" flag. The catalog
is guidelines, not rules; no write path on the server validates
against it. See "Validation" above.)

Operator-tunable per-environment.

## Forward compatibility

- New widget hints, theme keys, or markdown allowlist entries
  can be added later. Catalogs using newer hints render at
  reduced fidelity on older servers (default widget, default
  theme, stripped markdown tag); they don't crash.
- Old catalogs work unchanged on new servers.
- The base stylesheet's `var(name, fallback)` pattern means
  dropping a theme variable is graceful: page falls back to
  stra2us defaults.

## Security model (the load-bearing part)

This proposal lives or dies on the security stance. Specifically:

- **No client-supplied `<style>`, `<script>`, `<iframe>`, etc.**
  Catalog provides values; server provides selectors and tags.
- **Hex colors only, no expressions.** Eliminates CSS-injection
  via clever values.
- **No web fonts.** Eliminates load-timing exfiltration.
- **Images self-hosted only.** No external URL surface in
  catalog or markdown; same-origin asset serve from a reserved
  KV namespace. CSP `img-src 'self'` is sufficient.
- **SVG assets sanitized at publish time** — strip `<script>`,
  `<foreignObject>`, external href references, on-event attrs.
- **Markdown sanitized server-side at render time** with an
  explicit allowlist of tags + attributes, layered on a
  hardened library that handles malformed/adversarial input.
- **Catalog publish lint** rejects bad theme values, oversized
  assets, disallowed content types before the catalog is stored.
  Render-time is fast and trusted.
- **Page section scoped via `[data-app="…"]`.** Theme variables
  can't bleed into admin chrome or other apps' sections.

### Content Security Policy

stra2us today ships **no** Content-Security-Policy header. This
FR introduces one for the customer-facing page, because the
markdown-render and theme paths land in territory where CSP is
the difference between "safe by construction" and "safe pending
the next sanitizer CVE."

**Required header on `/app/<app>/<device>/...` — the complete
policy, not a summary:**

```
Content-Security-Policy:
  default-src 'self';
  script-src 'self';
  style-src 'self';
  img-src 'self';
  font-src 'self';
  connect-src 'self';
  frame-ancestors 'none';
  base-uri 'self';
  form-action 'self';
  object-src 'none';
```

Notes per directive:
- **`default-src 'self'`** — baseline so any directive we forget
  to enumerate inherits a restrictive default rather than
  browser-default "anywhere."
- **`script-src 'self'`** — the load-bearing one. Inline `<script>`
  and inline `on*=` handlers are blocked. No catalog-derived
  script of any kind.
- **`style-src 'self'`** — no `'unsafe-inline'`, no nonces. The
  per-app theme is an external stylesheet
  (`/app/<app>/_theme.css?v=<hash>`) precisely so this directive
  can stay this strict. If a future change wants inline `<style>`
  from theme data, it must introduce a per-response nonce and
  document the trade-off here, not slip it in.
- **`img-src 'self'`** — matches the self-hosted asset model.
  External images aren't allowed by the asset design; CSP is
  the second line of defense if a markdown-sanitizer bug lets
  one through.
- **`font-src 'self'`** — belt and suspenders for the "no web
  fonts" promise from the theme allowlist. Without it, fonts
  would inherit `default-src` (which is `'self'` here, so
  same effect) — listing it explicitly makes the promise
  audit-readable.
- **`connect-src 'self'`** — the customer page does fetch/XHR
  against stra2us itself; explicit listing avoids surprise if
  somebody later adds analytics or a third-party callout.
- **`frame-ancestors 'none'`** — prevents the customer page
  from being framed by a vendor's site (clickjacking surface).
- **`base-uri 'self'`** — without this, an injected `<base>`
  tag could hijack relative URLs even under otherwise-strict
  CSP.
- **`form-action 'self'`** — limits where forms on the page
  can submit.
- **`object-src 'none'`** — kills `<object>`/`<embed>` even if
  the markdown sanitizer ever lets one slip.

**Theme CSS serialization is data-not-string.** The
`/app/<app>/_theme.css` route does **not** build the CSS by
string-concatenating catalog values. It uses a fixed CSS
template with placeholders that the serializer fills with
already-lint-validated values via a parameterized helper
(comparable to a parameterized SQL query). Lint already
guarantees `#RRGGBB` shape and font-allowlist membership; the
serializer is the second line of defense. A value that somehow
passed lint must not be the only thing standing between catalog
input and a CSS-injection class of bug.

**Cache key.** The `?v=<hash>` parameter on
`/app/<app>/_theme.css` and `/app/<app>/_assets/<file>` comes
from the same place: a SHA-256 prefix of the catalog blob (for
`_theme.css`, hashed over the `theme:` section; for assets,
the file's own sha256). Both bump on republish, both are
emitted by the renderer when constructing `<link>` and `<img>`
URLs.

### CSP rollout against an app that has no CSP today

Going from no-CSP to strict in one ship is the trap the reviewer
called out. The plan:

1. **Audit the existing surfaces** before flipping anything.
   Grep for the patterns that strict CSP breaks:
   - inline `<script>...</script>` blocks
   - inline event handlers (`onclick=`, `onload=`, `onerror=`,
     `onsubmit=`, etc. — match `\son[a-z]+\s*=`)
   - inline `style="..."` attributes
   - inline `<style>...</style>` blocks
   - `javascript:` URLs in `href`
   - external asset hosts in `<script src>`, `<link href>`,
     `<img src>` on admin/api/static pages
   - `eval`, `new Function`, `setTimeout(string, …)`,
     `setInterval(string, …)` in the served JS bundle
   These are the things that will silently break under
   `script-src 'self'` / `style-src 'self'`. The audit produces
   a pre-flip checklist.
2. **Ship in Report-Only mode first.** Emit
   `Content-Security-Policy-Report-Only:` with the same
   directives, plus `report-to`/`report-uri` pointing at a
   small endpoint (or just structured logs). Run for at least
   one release cycle. Collect violations from real traffic —
   especially admin pages that the audit might miss.
3. **Fix what shows up.** Inline handlers move to addEventListener;
   inline styles move to classes; external CDN refs either get
   self-hosted or get an explicit allowlist entry (and a
   conversation about whether that's acceptable).
4. **Flip to enforcing** only after a quiet release in
   Report-Only. Keep the report endpoint live as a tripwire.

The customer-facing `/app/<app>/...` page is the easy case
because it's *new* template territory under this FR — write it
CSP-clean from day one, ship it directly in enforcing mode for
that route prefix only. Admin/api routes ship in Report-Only
until the audit completes.

**Verification:** smoke-test step that fetches the customer page
and asserts:
- The CSP header is present.
- It contains `script-src 'self'`, `style-src 'self'`,
  `default-src 'self'`.
- It contains no `'unsafe-inline'` or `'unsafe-eval'`.
- For the customer-page route specifically, it is *not* in
  Report-Only form (i.e. `Content-Security-Policy:`, not
  `Content-Security-Policy-Report-Only:`).
Catches both regressions and accidental relaxation.

The shape is *small* on purpose. Every additional vector
(allowing `<style>`, allowing `rgb()`, allowing data URLs,
allowing CSS-functions in colors) is a conversation worth
having explicitly rather than slipping in implicitly.

## Out of scope (deferred or won't-do)

- Custom widgets / components per app.
- Per-customer / per-device theming.
- Conditional fields ("show X only if Y == foo").
- i18n / localized markdown.
- Layout / ordering / grouping of fields beyond catalog order.
- Live-preview ("render this catalog as a customer would see
  it" in the admin UI). Nice-to-have for v2.
- Color contrast enforcement, image-quality checks, or any
  other "is this visual any good" gate. **Trust the vendor for
  visuals.** A vendor that ships an unreadable theme or a
  broken logo embarrasses themselves; we don't try to save
  them from it.
- Server-side validation of any write against the catalog. See
  "Validation" — this is a deliberate non-feature.
- External-URL images (CDN allowlist, third-party hosts).
  Self-hosted is the only path.
- JS hooks of any kind from the catalog. Period.

## Why this is the right shape

**Three principles drove every decision:**

1. **The catalog already exists, devices already ignore it.**
   Any solution requiring more from the device is wasted work.
   The catalog is the natural extension point.
2. **JSON Schema + UI Schema + sanitized markdown is the
   well-trodden path** for "describe a value + how to render it
   + how to surround it." We're nibbling a small subset that
   covers ~90% of what device catalogs need.
3. **Forward compatibility is free** if every hint is optional
   and unknown hints / tags / theme keys are ignored. Old/new
   server-catalog combinations always render *something*; never
   crash.

**Why not the alternatives:**

- **Arbitrary CSS sandbox:** vast attack surface (loadable
  fonts, position hacks, background-image exfiltration, CSS
  selector-based inference). Tight allowlist of variables
  avoids the surface entirely while covering ~90% of
  reasonable customization needs.
- **HTML/JS templates from catalog:** introduces a code-execution
  surface, needs sandboxing, fights accessibility. Markdown
  gets the same expressiveness for explanatory copy without
  the hazards.
- **External web app via HMAC client (the "nuclear option"):**
  vendors host their own infra, customers leave the
  first-party domain, login flows duplicate. Possible
  architecturally; supported by the existing HMAC API
  *today*; but a meaningfully worse customer experience than
  a curated first-party page with light theming.

## Open questions

(None blocking.)

**Resolved (recorded for the trail):**

- *Asset garbage collection* — implementation detail. Publish
  diffs old vs new `_assets/` listing and deletes the dropped
  files after the catalog YAML lands successfully.
- *Bundle uploads at publish time* — follow the existing
  critterchron pattern: push each blob (asset bytes + meta)
  to its predictable KV location first, then push the catalog
  YAML last, then re-read both to verify no tearing. No
  multipart, no tarball, no manifest — per-key PUTs in a
  defined order, catalog YAML is the commit point. If a
  publish dies mid-flight the next publish reconciles; if
  somebody reads in the gap they see the previous catalog
  pointing at the previous assets (still consistent).

- *Out-of-spec display behavior* — show the value as-is with a
  warning indicator; widget only offers catalog-valid choices;
  saving stomps; non-interaction preserves. Trust devices, don't
  correct them. See "Implications for displaying out-of-spec
  values".
- *Numeric `enum` vs `min`/`max`* — mutually exclusive; lint
  flags the combination. Resolved in the widget hints table.
- *Image hosting* — self-hosted only. No external URLs. See
  "Assets".
- *Color contrast / image quality* — trust the vendor. See
  "Out of scope".
- *Server-side validation timing* — there is none, by design.
  UI does best-effort prevention; storage is unfiltered. See
  "Validation".
- *Catalog-publish UX* — publish lint runs in the CLI and
  produces field-pointing errors. See implementation step 2.
