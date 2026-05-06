# Stra2us Catalog — feature request / proposal

**Status:** proposal, from the CritterChron team. Accepted; M1
implementation landed in `tools/` + `docs/catalog_spec.md`.
**Audience:** Stra2us maintainers, historical reference.
**One-line:** adopt the per-app YAML "catalog" pattern CritterChron has
prototyped, and grow a web UI on top of it so non-CLI users can see and
edit the tunables their IoT clients expose.

---

## Stra2us team notes (added during M1 implementation)

This FR is preserved verbatim below as CritterChron submitted it.
During implementation we made a handful of corrections and intentional
deviations. Recorded here so we don't keep rediscovering them.

1. **GET on a missing KV key returns 200, not 404.** The FR describes
   the UI logic as "GET, get 404 or empty, render unset." The stra2us
   server today (see `backend/src/api/routes_device.py:179-192`)
   returns HTTP 200 with a msgpack-encoded `{"status": "not_found"}`
   body when the key is absent in Redis. The reference CLI in `tools/`
   treats both responses — a real 404 and the 200+not_found envelope —
   as "unset", so any future server change to a proper 404 is
   transparent to the CLI. The UI (M3) must do the same. Normalizing
   this to a real 404 is a candidate cleanup for M2. Inline note below
   where the FR makes the claim.

2. **`default: per-device` magic string replaced by explicit flag.**
   The FR (and CritterChron's live catalog) uses the literal string
   `per-device` as a sentinel in the numeric `default:` field. The M1
   schema requires `default_per_device: true` as a separate boolean
   field, so `default:` stays strictly typed. See
   `docs/catalog_spec.md` §2. CritterChron's existing catalog will
   need a small migration — we'll ship one when we hand integration
   back.

3. **Added types up front: `bool`, `enum`.** The FR's §6 "non-trivial
   types" open question — we chose to include them in M1 rather than
   retrofit after the UI existed. `format:` is also in M1 as a
   UI-rendering hint.

4. **Publishing uses the existing `/kv/` endpoint, not a bespoke
   catalog endpoint. Wire format is raw YAML text.** The FR's §6
   open-question-#1 leaned JSON-at-publish-time; a later design
   discussion shifted to "the catalog is just a KV value under a
   reserved `_catalog/{app}` path." This buys zero new server code
   for M2 (reuses HMAC, ACLs, the existing POST/GET handlers) and
   preserves YAML comments round-trip. The UI (M3) pays ~40 KB for
   a YAML parser. See `docs/catalog_spec.md` §6 for the full shape.
   Publishing itself is M2, not M1.

5. **Out of scope for the stra2us repo (aligns with FR).** Per-app
   catalogs, drift lints, and any codegen stay app-side. The FR is
   explicit about this; noted here so we don't backslide.

Implementation landing sites:
- Schema spec: [`docs/catalog_spec.md`](catalog_spec.md)
- Reference CLI: [`tools/`](../tools/README.md)

---


## TL;DR

Stra2us does KV well. It does not yet answer two adjacent questions the
apps built on it keep asking:

1. *"What are the knobs this app exposes, and what are they for?"* —
   for operators, for new developers joining the project, for the CLI
   tooling that validates writes before they land.
2. *"How do I change one of those knobs without remembering the exact
   key syntax, type, and allowed range?"* — especially for devices
   that are headless, unreachable, or belong to someone who isn't
   going to run a Python CLI.

CritterChron has prototyped a small answer: a hand-maintained YAML
catalog per app, a Python CLI that reads the catalog to drive
`list / show / set`, and a drift lint that ties the catalog to the
device's C++ source. It works well enough that we'd like to see the
pattern pulled into Stra2us proper as a first-class concept, and a web
UI built against it.

The device firmware remains the ultimate arbiter of what a key means
and what values it honors. The catalog is a *contract*, not a
*gatekeeper*. That property is load-bearing — see "Invariants" below —
and any web UI must preserve it.

## Context: what CritterChron built

CritterChron is a fleet of Particle-class microcontrollers running a
little agent-VM over LED matrices. We drive behavior through Stra2us
KV — heartbeat cadence, brightness thresholds, IR script pointer,
animation tuning — using the already-established resolution order:

```
<app>/<device>/<key>   →  <app>/<key>  →  compiled-in default
```

As the knob count grew, three problems showed up in order:

1. **Nobody remembered what the knobs were.** Keys accreted over
   months. Reading the C++ `get_int("min_brightness", …)` call sites
   is not how an operator finds out what tunables exist.
2. **Setting knobs was typo-prone.** `STRA2US_HOST` vs `STRATUS_HOST`;
   `night_enter_brightness` vs `night_entry_brightness`; integer ranges
   you remembered differently than the device did. Silent
   misconfigurations because Stra2us happily stores whatever string
   you hand it.
3. **No visibility into the fallback chain.** Given a device showing
   bad behavior, we had to manually probe three keys (device scope →
   app scope → default) to figure out which layer was controlling.

The catalog + CLI answer all three. The drift lint (item 3 under
"Pieces" below) is the forcing function that keeps it honest.

## The pieces

### 1. `<app>.s2s.yaml` — the catalog

One file per app, checked in at the app's repo root. Describes the KV
variables the device firmware reads. Hand-maintained on purpose —
source-of-truth is the C++ `get_int/get_float` call sites, the catalog
is a human-readable mirror.

```yaml
app: critterchron

vars:
  heartbeep:
    type: int
    default: 300
    scope: [app, device]
    range: [10, 3600]
    help: |
      Stra2us heartbeat cadence in seconds. Device reads this once per
      loop iteration and adjusts in-place.

  ir:
    type: string
    scope: [device]
    help: |
      Script name this device should run. Empty string = "keep current,
      boot into compiled-in default on next reset."
    ops_only: true    # read by Stra2usClient internals, not the app

  min_brightness:
    type: int
    default: per-device
    scope: [app, device]
    range: [1, 255]
    help: |
      Floor on the 0-255 sink brightness. Compiled-in default is set
      per device header (rachel=1, ricky=3, rico=32).
```

Fields:

| Field       | Purpose                                                    |
|-------------|------------------------------------------------------------|
| `type`      | `int`, `float`, or `string`. Governs parsing + validation. |
| `default`   | Compiled-in fallback, or the literal `per-device` when the default lives in per-device headers. Used by the drift lint. |
| `scope`     | List of `app` and/or `device`. Which KV levels this key is valid at. |
| `range`     | `[lo, hi]` for numeric types. **Tool-side** validation only — device firmware is not required to honor it. |
| `help`      | Free-form prose. Surfaced in CLI listings, hover text in the UI, etc. |
| `ops_only`  | Opt out of the "must have a C++ reader" lint. Used for keys read by Stra2us client libraries themselves. |

The full critterchron catalog is in `critterchron.s2s.yaml` in this
repo — 13 entries today, covering telemetry cadence, OTA pointer,
animation tuning, light-sensor/brightness, and night-palette
thresholds.

### 2. `tools/s2s.py` — the CLI

Three verbs, all driven by the catalog:

```
s2s.py catalog                            # pretty-print the variable table
s2s.py show <device> [<key>]              # resolution chain for one or all keys
s2s.py show --app [<key>]                 # just the app-scope row(s)
s2s.py set <device> <key> <value>         # write with type + scope + range validation
s2s.py set --app <key> <value>
s2s.py set ... --unset                    # write empty string (no DELETE in the server)
```

Validation is advisory: the CLI rejects writes outside the catalog's
declared range or at an invalid scope, but `curl` with a valid HMAC
still gets through. That's fine — the CLI is the friendly surface, not
the enforcement boundary.

Credentials use the existing env-var shape
(`STRA2US_HOST / _CLIENT_ID / _SECRET_HEX`) or explicit flags.

### 3. `test_s2s_catalog.py` — the drift lint

Static analysis that catches the "catalog and code disagree" class of
bug. Two directions, both implemented:

- **Forward (code → catalog).** Every `get_int("k", …)` /
  `get_float("k", …)` call site in the HAL source must have a catalog
  entry with a matching `type`. Literal `default` expressions must
  equal the catalog's `default`. Defaults that resolve through
  `#define SYMBOL N` are followed, with unambiguous matches checked
  and ambiguous/unresolved sites reported (and skipped).

- **Reverse (catalog → code).** Every catalog entry must be read
  somewhere in the HAL source, unless marked `ops_only: true`.

Runs in CI. Failing lint is the forcing function that keeps the
catalog in sync — a new knob added to the device without a catalog
entry fails the test, which is exactly the point. 19 call sites, 13
catalog entries, clean today.

## Usage model

The catalog has three distinct audiences, each reached through a
different surface:

- **Device firmware** reads values via `get_int/get_float`. Catalog is
  invisible here — the device sees ordinary KV. The only constraint
  the catalog imposes on firmware is via the drift lint, which fails
  the build if a new `get_*` call has no entry.
- **App developers** read the YAML directly, and use the CLI to poke
  live values during development and debugging. The drift lint
  enforces their habits.
- **Operators** (the people with "please turn down the brightness on
  the raccoon" requests) use the CLI today, and ideally a web UI
  tomorrow. They do not read C++. They should not need to remember
  that `heartbeep` has two E's.

The loop in practice looks like this:

```
dev adds get_int("new_knob", DEFAULT)  →  test_s2s_catalog.py fails CI
  →  dev adds a new_knob: entry to critterchron.s2s.yaml  →  CI green
  →  operator runs `s2s.py show ricky new_knob`
  →  operator runs `s2s.py set ricky new_knob 42`, CLI validates
  →  device picks up next loop iteration, no reflash
```

Every step above works today in CritterChron. The two missing steps
are the ones this FR asks for.

## Invariants (read before designing anything against this)

These are non-negotiable properties the pattern depends on. The
primary ask from the Stra2us side is that the web UI respect them.

### 1. Never materialize placeholder entries at lookup keys

The fallback chain is `<app>/<device>/<key>` → `<app>/<key>` →
compiled-in default. The *absence* of a key is how the chain
advances. An empty-string placeholder written at device scope "to
pre-register the key" **wins the probe at device scope** and
short-circuits the fallback to app scope. It is a correctness bug
disguised as a usability feature.

Corollary: a web UI listing keys must not "create" a key as a
side-effect of viewing or navigating to it. The list of known keys
comes from the catalog YAML, not from the server's key inventory.
Unset is a legitimate state; rendering it as an empty row is fine,
writing `""` to the server to make it visible is not.

### 2. Device firmware is the arbiter

The catalog declares `range: [1, 255]`. The device firmware may or
may not honor that — in fact, today, CritterChron's HAL clamps
`min_brightness < 0 → 0`, slightly looser than the catalog says.
That's intentional: the catalog is a contract with the CLI and the
UI, not with the device. Tightening the catalog is a tooling policy
change; tightening the device requires a firmware release.

Any UI must not imply "this range is enforced." The right UX word is
"recommended" or "valid"; never "required" or "enforced."

### 3. Catalog is hand-maintained; no code generation

No YAML → C header compilation. No generated Python stubs. The
catalog is a descriptor, not an input to the build. The drift lint is
the forcing function; regeneration would hide precisely the drift we
want the test to catch.

Corollary for Stra2us: if this gets pulled in, the server shouldn't
generate the catalog either. The server consumes it; apps write it.

## The asks

### Ask 1: Pull the catalog pattern into the Stra2us repo

Concrete scope:

- **A schema spec** (`docs/CATALOG_SPEC.md` in the stra2us repo, or
  similar) describing the YAML fields above, the invariants, and the
  fallback-order contract. CritterChron's `critterchron.s2s.yaml` is a
  working reference; the spec should generalize it without
  critterchron-specific assumptions.
- **A reference CLI implementation**, equivalent to CritterChron's
  `tools/s2s.py`, shipped alongside the server. An app adopting
  Stra2us gets `stra2us catalog|show|set` for free; the only
  per-app file they write is `<app>.s2s.yaml`.
- **A server-side "catalog stash" key**, e.g. `<app>/_catalog`, where
  the app uploads its YAML at deploy time. This is the discovery
  mechanism for the web UI — the UI reads the stashed YAML to know
  what keys to offer. *Not* a source of truth; the file in the app's
  repo is source of truth, the stash is a published copy.

Why in Stra2us rather than as an external tool:

- Discoverability. "Use Stra2us" and "what knobs does this app
  expose" become the same onboarding path.
- Versioning. The CLI and the server protocol evolve together; coupling
  them in one repo means one version number to reason about.
- It's small. The reference CLI is ~300 lines of Python; the schema
  spec is a markdown file; the stash endpoint is one KV key. This
  doesn't bloat Stra2us.

What stays out of scope for the stra2us repo:

- **Per-app catalogs.** Those live with the app.
- **Drift lint.** That's an app-side test because it walks app-side
  source. A template / example is fine; the actual test lives where
  the code does.
- **Catalog → schema codegen.** See invariant 3.

### Ask 2: A web UI on top of the catalog

This is where the user-experience payoff lives. Today an operator
who wants to dim the raccoon runs:

```
source session.sh
python tools/s2s.py set ricky_raccoon max_brightness 96
```

That's three pieces of context (venv, script path, exact key) plus a
secret in their shell. It works for developers, not for the
"I'd like the clock a little dimmer" stakeholder.

**Minimum viable UI.** Four views:

1. **App index.** List of apps with catalogs stashed at
   `<app>/_catalog`. One row per app; click through.
2. **Key table for an app.** The catalog, rendered. Each row: key
   name, type, scope, default, range, help (tooltip or expandable).
   This is `s2s.py catalog` as a web page.
3. **Key detail / device view.** For one `<app, device, key>` triple:
   the resolution chain (device → app → default → effective) as three
   rows, plus an edit control that produces a POST to the appropriate
   scope with type + range validation drawn from the YAML. This is
   `s2s.py show` + `s2s.py set` in one pane.
4. **Device index for an app.** List of device IDs that have written
   to `<app>/<device>/*` or are otherwise known to the server. Click a
   device to see its effective config (one column per key, values
   resolved through the chain). This assumes Stra2us already tracks
   which client_ids have been active per app; if not, it's a
   separate request.

**Auth.** The UI is a privileged surface — writes to KV are writes to
a live fleet. Start with the existing HMAC client model (the UI holds
a privileged client_id + secret; log in once, write freely) and
revisit if multi-user edit audit-trails become a requirement. The
catalog doesn't imply any new auth model.

**Real-time-ness.** Devices poll; writes land whenever the next
heartbeat or poll interval fires. The UI should say so — show the
key's cadence ("this knob is read once per loop, ≤5s") next to the
edit control so the user isn't confused when the raccoon doesn't
immediately change. The catalog could gain an optional
`read_cadence: "loop" | "poll_<key>" | "boot"` field to power this;
default "unknown" is fine.

**What the UI must not do** (restating invariants with UI teeth):

- Must not write any key on navigation. Viewing `max_brightness` on a
  device that hasn't set it must not POST an empty string to
  `<app>/<device>/max_brightness`. It must do `GET`, get 404 or empty,
  and render "unset" from that.
  > **Note (stra2us M1):** as of this writing the server returns 200
  > with a msgpack `{"status": "not_found"}` body for missing keys,
  > not a 404. The principle holds; the sentinel shape is different.
  > See team note #1 at the top of this doc.
- Must not claim a range is enforced on-device. "Range" or
  "recommended"; never "limit."
- Must not present itself as the list of known keys in a way that
  hides keys the device reads but the catalog omits. A "raw key
  inspector" tab that hits `<app>/<device>/*` with no catalog filter
  is useful for the exact debugging case where the catalog is wrong.

**What's nice to have, not required for v1:**

- Write audit log ("austin set heartbeep=15 on ricky at 12:04").
- Batch edits (set an app-scope value across a key set).
- Catalog diff viewer when an app's stashed catalog changes version.
- Live preview via a push channel — Stra2us already has a pub/sub
  side, and the UI could subscribe to the app stream to show the
  device's next heartbeat inline. Nice eventually; polling works for
  MVP.

## Open design questions for the Stra2us team

These are real choices, not rhetorical:

1. **Catalog stash key shape.** We've sketched `<app>/_catalog` with
   the raw YAML as the value. Alternatives: a structured JSON rendering
   instead of raw YAML (easier for a JS UI to consume; requires a
   server-side parse step or a pre-publish conversion). Mild
   preference for JSON-rendered-at-publish-time — the server stays
   YAML-agnostic and the UI doesn't need a YAML parser.
2. **Who validates writes: server or CLI?** Today CritterChron's CLI
   validates and the server is permissive. Easy win: have the server
   read the catalog and advisory-reject out-of-range writes with a
   `409` that clients can surface. Stronger: server *enforces*, UI
   stops worrying. We lean advisory — lines up with invariant 2
   (device is still the arbiter; server enforcement could confuse
   the contract) — but a toggle per-key (`enforce: true`) would let
   apps opt in to strict mode for the knobs where it's safe.
3. **Catalog versioning.** If an app ships a new catalog with a
   renamed key, the UI needs to know which stashed version it's
   looking at. A `version:` header in the YAML + server retention of
   the last N would handle this cleanly; how much history is the
   question.
4. **Device enumeration.** The UI needs to know which device IDs
   exist per app. The catalog can't carry that (it's a template, not
   a registry); the server can (it sees writes). Does Stra2us already
   have a "list client_ids that have written under `<app>/`" view?
   If not, it's a separate request but a small one.
5. **Non-trivial types.** Today we have int / float / string. Some
   candidate extensions: bool (today modeled as int 0/1), enum
   (today modeled as string + help-text listing valid values),
   duration (today modeled as int-seconds). These could all be sugar
   over the existing types with an optional `format:` hint the UI
   renders specially (checkbox, dropdown, time picker). No firmware
   impact — the device still reads an int. Worth getting right early
   because it shapes the UI.
6. **Multi-app UIs.** CritterChron is one app. The rest of the fleet
   is other apps. Should the UI be per-app (one deployment per app)
   or multi-app (index page lists all apps the server knows about)?
   We'd argue multi-app from day one — the cost is trivial, and the
   catalog stash pattern scales naturally.

## Non-goals

Being explicit so these don't come up as "didn't we also agree to…":

- **Code generation from the YAML.** See invariant 3.
- **YAML as the schema for anything beyond KV catalog.** Not a
  universal-config-language play. Scoped to the "describe the tunable
  knobs this app exposes" use case, with a deliberately small
  vocabulary.
- **Replacing the CLI with the UI.** The CLI is the developer surface
  and stays. The UI is the operator surface and adds a second path,
  not a replacement.
- **Secret management.** Catalog values are assumed non-secret. If an
  app wants to stash a secret in KV, the catalog is the wrong place
  to document it. Stra2us's existing HMAC setup handles auth; that's
  separate.
- **Per-device firmware version awareness.** The device might have
  shipped before a new catalog key existed, in which case it simply
  doesn't read the key and the UI edit is a no-op. The UI should
  probably surface "last heartbeat firmware version" next to the
  device in the detail view so the operator can reason about it; it
  isn't the catalog's job to model this.

## Prior art

Not pretending this is novel. Similar patterns elsewhere:

- Particle Cloud variables/functions — typed, documented, surfaced in
  their console. We're less coupled to the device runtime (no RPC,
  just KV) but the UX target is similar.
- AWS IoT Device Shadow "desired state" documents — structured JSON
  with a declared schema. We avoid the state-machine baggage of
  shadow reconciliation; the catalog is just description.
- Home Assistant's `configuration.yaml` + UI editor — closest in
  spirit: hand-written YAML plus a UI that reads it to present
  friendly controls. Same split of authority (file is truth, UI is
  an editor).

## Implementation artifacts in this repo

For reference when reviewing:

- `critterchron.s2s.yaml` — the worked example catalog (13 entries).
- `tools/s2s.py` — the three-verb CLI.
- `tools/s2s_client.py` — the HMAC + KV client the CLI wraps
  (mirrors the device-side signing protocol in
  `hal/particle/src/Stra2usClient.cpp`).
- `test_s2s_catalog.py` — the drift lint.

We're happy to hand over the catalog YAML, CLI, and lint as a
starting point for the Stra2us-side reference implementation. None of
it is CritterChron-specific by design; the only app-specific input is
the YAML.
