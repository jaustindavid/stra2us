# Stra2us Catalog — Schema Spec

**Status:** draft (M1)
**Audience:** app developers adopting Stra2us, tooling authors, UI authors

A *catalog* is a hand-maintained YAML file per app that describes the KV
variables the app's devices read. It is a human-readable mirror of the
`get_int` / `get_float` / `get_string` call sites in the device firmware.

The catalog is **not** compiled, generated, or enforced by the stra2us
server. It is a contract between the app's firmware and the tooling
(CLI, future web UI) that operators use to poke at live values. The
device firmware remains the ultimate arbiter of what a key means and
what values it honors.

This spec defines:

1. The on-disk YAML schema.
2. The key resolution / fallback contract the catalog describes.
3. The invariants tooling must preserve.
4. How the catalog is published to the stra2us server for UI discovery.

Origin: this pattern was prototyped in the CritterChron project; see
the CritterChron FR for the motivation and design discussion.

---

## 1. File layout

One file per app, conventionally named `<app>.s2s.yaml`, checked in at
the app's repository root. Example:

```yaml
app: critterchron
telemetry_topic: "{app}/public/heartbeep"   # tail this for status / activity
heartbeat_interval_seconds: 300             # 5min cadence; thresholds scale

vars:
  heartbeep:
    type: int
    default: 300
    scope: [app, device]
    range: [10, 3600]
    label: Heartbeat interval                # customer-facing title; presence
                                             # is the app-view visibility gate
    help: |
      Stra2us heartbeat cadence in seconds.

      Device reads this once per loop iteration and adjusts in-place.
      Lower values give snappier UI feedback at the cost of bandwidth;
      300s is a reasonable default for always-online devices.

  wifi_password:
    type: string
    scope: [app, device]
    label: WiFi password
    encrypted: true                          # encrypted on the wire to devices
    help: WPA2 PSK for the device's home network.

  ir:
    type: string
    scope: [device]
    ops_only: true
    # No `label`: this is operator-only — the customer never sees it.
    help: |
      Script name this device should run. Empty string = keep current.
```

### Top-level fields

| Field  | Required | Type   | Purpose                                      |
|--------|----------|--------|----------------------------------------------|
| `app`  | yes      | string | App identifier. Matches the `<app>` segment in KV keys. Lowercase, alphanumeric + underscore, must not contain `/`. |
| `vars` | yes      | map    | Map from key name → variable descriptor (see §2). |
| `version` | no    | int    | Catalog revision counter. Bump on backward-incompatible changes (e.g. renames, type changes). Used by the UI to detect stale stashed copies. Defaults to `1`. |
| `telemetry_topic` | no | string | Topic the customer-facing app view tails for "is this device alive" + recent activity. Supports `{app}` and `{device}` placeholders. Default: `{app}/public/heartbeep`. Consumed by the `/app/<app>/<device>` UI per [`fr_application_view.md`](fr_application_view.md). Apps with a single shared telemetry topic per fleet (e.g. critterchron) declare it explicitly; apps with per-device topics use the default convention. |
| `heartbeat_interval_seconds` | no | int | App's expected telemetry cadence. Drives the app view's status-badge thresholds: a device is "Online" if its last message was `< 2 × interval` ago, "Recently active" if `< 20 × interval`, otherwise "Offline". Default: `60`. A 5-minute-cadence app should set this to `300` so a healthy device isn't called Offline at 4 minutes since last message. |

### Variable name rules

Variable names (keys under `vars:`) must:

- Be non-empty.
- Match `[a-z][a-z0-9_]*` — lowercase ASCII, digits, and underscore.
- Not contain `/` (slashes are KV path separators).
- Not begin with `_` (reserved for server-side metadata keys).

---

## 2. Variable descriptor

Each entry under `vars:` is a map with the following fields.

| Field                | Required | Type             | Purpose |
|----------------------|----------|------------------|---------|
| `type`               | yes      | string           | One of `int`, `float`, `string`, `bool`, `enum`. Governs parsing and validation. See §2.1. |
| `scope`              | yes      | list of string   | Non-empty subset of `["app", "device"]`. Which KV levels this key may be set at. See §3. |
| `default`            | no       | number / string  | Compiled-in fallback value. Informational — displayed in the UI, cross-checked by the drift lint. Mutually exclusive with `default_per_device` and `default_per_platform`. See §2.3. |
| `default_per_device` | no       | bool             | When `true`, the compiled-in default lives in per-device headers (one literal per unit) rather than app-wide code. The drift lint skips the default cross-check for keys with this flag. Mutually exclusive with `default` and `default_per_platform`. See §2.3. |
| `default_per_platform` | no     | bool             | When `true`, the compiled-in default lives in per-HAL source (one literal per platform — e.g. ESP32 vs Particle) rather than app-wide code. The drift lint resolves per-platform, looking in each HAL's source tree. Mutually exclusive with `default` and `default_per_device`. See §2.3. |
| `range`              | no       | `[lo, hi]`       | Numeric types only. **Recommended** bounds — advisory to tooling, not enforced on-device. See invariant 2 in §4. |
| `values`             | cond.    | list             | Required for `type: enum`. List of allowed string values. |
| `format`             | no       | string           | UI hint for how to render the control. See §2.2. Does not change validation. |
| `help`               | no       | string           | Free-form prose. Surfaced in CLI listings, tooltips in the UI. Use multi-line `help: \|` for anything longer than a phrase. **Convention for narrow surfaces (the customer-facing app view's edit modal):** put a short blurb on the first line, then a blank line, then long-form details. Narrow renderers clip at the first newline (or a ~20-word fallback if no newline) and append an ellipsis. See example below. |
| `ops_only`           | no       | bool             | Opt out of the "must have a firmware reader" drift-lint check. Used for keys consumed by Stra2us client libraries themselves (e.g. the OTA script pointer) rather than by `get_*` calls in app code. Defaults to `false`. |
| `read_cadence`       | no       | string           | Hint to the UI about how quickly a write takes effect on-device. One of `loop`, `poll`, `boot`, or a free-form string. Default: unspecified / unknown. |
| `enforce`            | no       | bool             | When `true`, the stra2us server (M2+) advisory-rejects writes that fall outside `range` with a 409. Defaults to `false`: server remains permissive, CLI/UI validate. |
| `label`              | no       | string           | Human-friendly title surfaced in the customer-facing app view (`/app/<app>/<device>`, see [fr_application_view.md](fr_application_view.md)). **Presence is the visibility gate**: a var with a `label` shows up in the customer's settings list, a var without one is hidden. Operator-jargon vars (`debug_flag_experimental`, perf knobs) just don't get a `label`. Distinct from `help` — `label` is a few words for the title, `help` is a sentence for the description. The admin UI shows both regardless. |
| `encrypted`          | no       | bool             | Declares that the value should be encrypted on the wire to devices (see [fr_encrypted_values.md](fr_encrypted_values.md)). Catalog declaration is **advisory** — the per-record server-side flag is what governs wire behavior — but operators should mark this `true` on any var that holds secret material (wifi passwords, API tokens). The drift lint can verify "every var marked `encrypted: true` is actually stored as encrypted on the server" and "vars whose names match `password|secret|key` are marked `encrypted: true`." |

### 2.1 Types

| Type     | On-wire form                                     | Validation |
|----------|--------------------------------------------------|------------|
| `int`    | msgpack int                                      | `range` applies if set. |
| `float`  | msgpack float (or int, coerced)                  | `range` applies if set. |
| `string` | msgpack str                                      | No length bound today. `range` not valid. |
| `bool`   | msgpack bool. Legacy firmware may read `0` / `1` as int — this is an app-side concern, not a schema concern. | CLI accepts `true`/`false`/`1`/`0`/`yes`/`no` case-insensitively and canonicalizes to msgpack bool. |
| `enum`   | msgpack str, constrained to `values`.            | Value must appear in `values`. |

### 2.2 `format` hints

Optional, advisory, UI-only. The CLI ignores `format` except to pass
it through in `catalog` listings. Recognized values:

| `format`     | Applies to       | UI rendering suggestion                  |
|--------------|------------------|------------------------------------------|
| `duration_s` | `int` / `float`  | Time input; label in seconds.            |
| `duration_ms`| `int`            | Time input; label in milliseconds.       |
| `percent`    | `int` / `float`  | Slider 0–100.                            |
| `brightness` | `int`            | Slider 0–255.                            |
| `hex_color`  | `string`         | Color picker.                            |
| `uri`        | `string`         | URL-ish text box with validation.        |

Apps may use any string here; unknown values fall back to the default
control for the underlying `type`.

### 2.3 Where the compiled-in default lives

`default`, `default_per_device`, and `default_per_platform` are the
three *mutually exclusive* ways a variable can declare where its
compiled-in fallback comes from. At most one may be set; zero is also
legal (e.g. `ops_only` keys like `ir` that have no fallback at all).

| Flag                     | Literal count | Location the drift lint looks in                  | Example |
|--------------------------|---------------|---------------------------------------------------|---------|
| `default: <value>`       | 1             | The catalog itself (and matching `#define` / const in firmware). | `heartbeep: 300` |
| `default_per_device: true` | N (one per unit) | Per-device headers, e.g. `hal/devices/<device>.h`. | A per-unit brightness floor that varies by physical build. |
| `default_per_platform: true` | N (one per platform) | Per-HAL source, e.g. `hal/<platform>/src/*.cpp`. | `light_exponent`: Particle/CDS driver defaults to 2.5, ESP32/BH1750 to 0.5 because the upstream normalization is inverted. |

The distinction matters for the drift lint, not for the server. The
server sees all three forms as "the catalog says nothing enforceable
about the default"; only tooling that walks firmware source cares
which tree to search.

Rationale for making `default_per_platform` a sibling flag rather than
folding it into `default_per_device`: the two are semantically
different. `default_per_device` means *every unit could in principle
be different and converge over the fleet's life*. `default_per_platform`
means *there are exactly N literals, one per HAL, and they will stay
different as long as the drivers differ*. Conflating them mis-routes
the drift lint to the wrong source tree.

---

## 3. Key resolution / fallback contract

A device reads a KV variable by probing up to three locations in order:

```
1. <app>/<device>/<key>    — per-device override
2. <app>/<key>             — app-wide default
3. compiled-in default     — from firmware source (mirrored by `default:`)
```

The *absence* of a key at a given scope is how the chain advances to
the next. A key that is not in Redis returns 404 from
`GET /kv/<key>`; the client treats 404 as "not set" and falls through.

### 3.1 Scope declaration

The `scope` field declares which levels a key is *valid* at:

- `scope: [app]` — only `<app>/<key>` is meaningful. The device does
  not read the per-device path.
- `scope: [device]` — only `<app>/<device>/<key>` is meaningful.
  Typically used for per-unit-only state (the OTA pointer, a device's
  role).
- `scope: [app, device]` — both paths are valid; per-device wins
  when both are set.

Tools must refuse writes to a scope the catalog doesn't list.

### 3.2 The placeholder trap

> **Invariant 1 (no placeholder writes).** A tool must *never* write
> an empty-string value to a key as a side-effect of listing,
> viewing, or navigating to it.

The fallback chain is driven by *absence*. Writing `""` to
`<app>/<device>/<key>` to "pre-register" the key causes subsequent
reads to return `""` at device scope, which wins the probe and
short-circuits the fallback to app-scope or compiled-in default. This
is a correctness bug disguised as a usability feature.

Corollary: the UI's list of known keys comes from the catalog, not
from the server's KV inventory. Rendering an unset key as an empty
row is fine. Writing `""` to make it appear in Redis is not.

### 3.3 Unset semantics

The stra2us server has no DELETE. "Unset" is expressed one of two ways:

- **Preferred:** the key does not exist in Redis (`GET` returns 404).
  Achieved by never having written it, or by out-of-band removal.
- **Legacy:** the key exists with value `""`. Per-type semantics:
  - `int` / `float`: device's `get_int` / `get_float` parser typically
    rejects the empty string and falls through as if unset.
  - `string` / `enum`: `""` is a *valid* value and does **not** fall
    through. Apps that want "unset = fall through" for strings must
    document a sentinel (e.g. the OTA pointer treats `""` as "keep
    current") in the key's `help`.

Tools should prefer mechanism 1 and treat `""` as an app-specific
legacy behavior. `s2s set --unset` writes `""` today as a pragmatic
workaround; if the server grows a DELETE verb, `--unset` will switch
to it transparently.

---

## 4. Invariants

These are non-negotiable properties the catalog pattern depends on.

### Invariant 1 — No placeholder writes

Already stated in §3.2. Repeated here so any reviewer of a tooling
change sees it.

### Invariant 2 — Device firmware is the arbiter

`range` and `values` are advisory to *tooling*. The device may or may
not enforce them. A UI must never present them as "limits" or
"enforced" — the right words are "range", "recommended", "valid".

Tightening a `range` in the catalog is a tooling-policy change.
Tightening what the device honors requires a firmware release.

The `enforce: true` opt-in (§2) is the one exception: an app can ask
the stra2us server to advisory-reject out-of-range writes. Even then,
a client with a valid HMAC signature and a direct `POST` request can
still reach Redis — `enforce` is a speed-bump, not a security
boundary.

### Invariant 3 — Catalog is hand-maintained; no codegen

The catalog is a descriptor, not a build input. There is no
YAML-to-C-header compilation, no generated Python stubs. The drift
lint (§5) is the forcing function that keeps catalog and code in
sync.

Corollary: the stra2us server consumes catalogs but does not generate
them. The file in the app's repo is the source of truth; the server's
stashed copy is a published projection.

---

## 5. The drift lint (app-side)

Every app adopting the catalog pattern should ship a CI check with
two directions:

- **Forward (code → catalog).** Every `get_int("k", …)` /
  `get_float("k", …)` call site in the firmware source must have a
  catalog entry with a matching `type`. When the literal `default`
  expression is resolvable (a literal number, or a symbol resolvable
  to a single `#define`), it must equal the catalog's `default`.
- **Reverse (catalog → code).** Every catalog entry must be read
  somewhere in the firmware, unless tagged `ops_only: true`.

The lint lives in the app's repo because it walks app-side source.
CritterChron's `test_s2s_catalog.py` is a working reference; the
stra2us repo may ship a templated version, but the canonical
implementation is per-app.

Failing lint on a new `get_*` call with no catalog entry is the
point — it forces the dev to either add a catalog entry or
deliberately mark the call as intentionally uncataloged.

### 5.1 Recommended name-pattern lints

These are catalog-only checks (no firmware walk required). Cheap to
wire up in the same CI job:

- **Encrypted secrets.** Vars whose names match `password|secret|key|token`
  (case-insensitive) should have `encrypted: true`. Catches the
  "forgot to mark sensitive" footgun (filed as a related concern in
  [`fr_encrypted_values.md`](fr_encrypted_values.md)).
- **No customer-facing label on operator-only vars.** Vars whose
  names match `debug_|perf_|.*_experimental$|_internal$` should NOT
  have a `label` field. Inverse of the visibility convention from
  [`fr_application_view.md`](fr_application_view.md): `label`-presence
  is what makes a var customer-facing, so the lint enforces "if it
  looks like an internal var, it shouldn't be."
- **No reserved sub-namespace names as device identifiers.** No
  device should be named `public` (or any other reserved
  sub-namespace under `<app>/`). Stra2us-server enforces this at
  HMAC-client provisioning time per
  [`fr_application_view.md`](fr_application_view.md), but the
  catalog-side lint catches accidental drift in app-controlled
  provisioning scripts.

These lint patterns are mirrored / inverted from each other —
"sensitive vars MUST be encrypted" vs "operator vars MUST NOT have
a label" — and use the same scaffolding. Worth picking up as a set.

---

## 6. Publishing to the stra2us server (M2)

The stra2us server stores a published copy of the catalog under a
reserved KV path so the web UI can discover apps and their knobs
without reaching into each app's source repository.

This section is **draft for M2**; the M1 CLI does not yet implement
publish. It is documented here so the schema author knows what the
server will receive.

### 6.1 Transport — reuse the existing `/kv/` surface

The catalog is published as an ordinary KV value at a reserved key:

```
<app>.s2s.yaml  ─┐
                 │  stra2us catalog publish
                 ▼
POST /kv/_catalog/{app}       body: raw YAML text
                              Content-Type: text/plain
                              X-Client-ID / X-Signature (HMAC as usual)
```

Reading is symmetric:

```
GET /kv/_catalog/{app}        → msgpack-encoded string (the YAML text)
```

No new server endpoints, no new auth model — a catalog is just a KV
entry under a reserved prefix. The `_` prefix is already reserved in
§1 for server-side / metadata keys, so `_catalog/{app}` slots in
without colliding with any app's variable namespace.

### 6.2 Wire format — raw YAML text

The CLI uploads the YAML file's bytes verbatim. The server does not
parse the payload; it is opaque to stra2us. The UI (M3) will parse
the YAML client-side to render forms.

Rationale:

- Minimum surface. Zero server work to land M2.
- YAML comments survive round-trip — useful when an operator
  `GET`s the stashed copy to inspect what a fleet was promised.
- The CLI already parses + schema-validates locally before publishing
  (same validation the M1 `catalog` verb uses), so malformed catalogs
  don't reach the server. Consistent with invariant 2: server stays
  permissive, tooling validates.

UI bundling cost: one YAML parser (~40 KB for js-yaml minified).
Acceptable for an admin-only tool.

### 6.3 Source of truth

The YAML in the app's repo is canonical. The stashed copy on the
server is a cache; it may be stale. When they disagree, the YAML
wins; a republish resolves the drift.

### 6.4 ACLs

Writes to `_catalog/{app}` should be restricted to privileged
client_ids (the app's deploy credential, or an ops CLI credential).
Reads can be broader — the UI needs them, and catalogs are not
secret. Both use the existing `check_acl` machinery in
`backend/src/api/routes_device.py`; no new access-control concepts.

### 6.5 What this defers

The "just a KV entry" approach trades simplicity for two things we may
want later. See `docs/catalog_todo.md` for the current worklist.

- **Versioning / history.** `POST /kv/` overwrites. If we want the
  last N catalog revisions per app, we grow a mechanism (a parallel
  `/q/_catalog_history/{app}` stream is the cheap option).
- **Discovery.** "Which apps have catalogs?" is a prefix-scan across
  KV. The server does not expose one today. Either add a narrow
  `GET /kv/?prefix=...` (admin-auth) or have the UI carry a
  configured list of apps.

---

## 7. Extension points (deliberately out of M1)

Listed so they don't surprise anyone who reads the schema and
wonders why we stopped short.

- **Derived or computed keys.** The catalog describes writable
  tunables, not computed state. If a key is "read-only from the
  operator's perspective", model it with `scope: [device]` and rely
  on the device to ignore writes, or exclude it from the catalog.
- **Per-device catalogs.** The catalog is per-*app*. Device-specific
  differences (brightness floor, grid geometry) are expressed by
  `scope: [device]` + `default_per_device: true` (per-unit) or
  `default_per_platform: true` (per-HAL), not by separate per-device
  files.
- **Secret values.** The catalog is for operator-tunable
  configuration. If an app stashes secrets in KV, the catalog is
  the wrong place to document them.
- **Catalog inheritance / imports.** Not supported. One file per app.

---

## 8. Reference implementations

- App-side YAML: `critterchron/critterchron.s2s.yaml`
- App-side drift lint: `critterchron/test_s2s_catalog.py`
- Reference CLI: `stra2us/tools/stra2us_cli/` (this repo, M1).
