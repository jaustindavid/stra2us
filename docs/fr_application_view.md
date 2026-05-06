# FR: Application View — customer-facing per-device UI

*Drafted 2026-05-03 — design for review, not yet implemented.*

## Problem

Today's `/admin` UI is doing two unrelated jobs:

1. **Administering stra2us** — managing HMAC clients, viewing
   fleet-wide activity logs, backup/restore, troubleshooting why a
   device isn't checking in, peeking at queues. Audience: internal
   ops.
2. **Operating one device** — "I'm the owner of this critterchron, I
   want to update the wifi password and check that it's online."
   Audience: end customer.

These are different jobs done by different people with different
mental models, and the only reason they share a UI is that there's
only one. As we add real customer-facing flows (procyon rescue, OTA
firmware push, support tickets), the cost of pretending the customer
is an admin grows: the language is wrong ("Catalog vars"), the layout
is wrong (nav full of irrelevant tabs), and the surface area for
accidental damage is wrong (every admin route is reachable, gated only
by 403s on click).

## Design property: app-agnostic by construction

The app view is **one generic view** that adapts to any app via what
the catalog declares and what's in KV. There is no per-app code in
stra2us; adding a new app means writing its `<app>.s2s.yaml` with
`label:` on customer-facing vars and provisioning scoped admin users.

What's catalog-driven (no code per app):
- Which variables show up (presence of `label:`).
- Each var's title (`label`), description (`help`), type (drives the
  input control), encryption state (drives masking + Reveal),
  permitted range (drives client-side validation).
- The catalog YAML is the single source of truth; the page is a
  thin renderer over it.

What's convention-driven but applies uniformly across apps:
- Telemetry topic naming (one global rule, applied per-app via the
  URL — see open question 1).
- Status-badge freshness thresholds (one global constant, optionally
  derived from a per-catalog `expected_heartbeat_seconds` field if
  some apps have very different cadence).

What's *not* in scope for v1 — would break the app-agnostic property
if added carelessly:
- Per-var bespoke editors (e.g. a graphical time-picker for
  `brightness_schedule`). Path forward when it becomes real: a
  catalog field like `render: graphical_schedule` that dispatches to
  a registered renderer in the JS — keeps the dispatch generic, even
  if individual renderers are app-specific. Filed under "Not
  proposing" below.

This property is load-bearing: it's why the FR's server work is so
small (one new endpoint + one new route handler), and why scoping a
new product onto stra2us is a YAML edit rather than a feature
release.

## Two surfaces, shared auth

| | Admin (`/admin`) | App (`/app/<app>/<device>`) |
|---|---|---|
| **Audience** | internal ops, "stra2us superadmins" | end customer who owns one device |
| **Scope** | fleet-wide, multi-app | one device |
| **Surface** | full nav, all the existing tabs | single page, no nav |
| **Status content** | activity logs, perf logs, raw queues | "last seen", recent telemetry tail |
| **Edit ergonomics** | inline catalog editor (today) | read-mostly + per-variable edit modal |
| **Auth** | htpasswd + `admin_acls:<user>` | **same** |
| **Server-side enforcement** | per-route `require_admin_*` + `check_acl` | **same** |
| **Branding** | technical, "stra2us-admin" | customer-friendly, app-themed |

The key insight: **auth and the ACL primitives don't change at all.**
The server already enforces correctly per route. We're just adding a
second presentation surface that consumes the same APIs through a
different lens.

## Persona for the application view

A critterchron owner. Internal staff today; conceivably the
end-customer in the future as we ship product. Operationally:

- Bought a critterchron, was given a username + password by whoever
  provisioned the device.
- Bookmarks `/app/critterchron/ricky` (or whatever the device id is —
  they probably don't care about the URL details, but it's stable
  and shareable for support).
- Logs in once, the browser session keeps them in.
- Comes back occasionally to check that it's online or to update a
  setting (wifi changed, brightness schedule needs tweaking).
- Does not know what a "Redis stream" is. Should never see one.

## Application view — what it shows

A single page. No nav.

**Header.**
- Device name (`ricky` — whatever the catalog/operator named it).
- Status badge: **Online** (green) if the most recent telemetry
  message is < 60s old, **Recently active** (yellow) if < 1 hour,
  **Offline** (gray) otherwise. Computed from the telemetry tail
  (see below) — no separate ping endpoint needed.
- "Last seen: 2 minutes ago" computed from the latest telemetry
  `received_at`.

**Settings (the catalog vars).** A list of cards, one per
customer-facing catalog var, showing:
- The var's `label` as the card title (a few human-friendly words).
- The var's `help` text as the sub-line (description / tooltip-y).
- The current effective value with **its source surfaced explicitly**:
  - Device-scope override set → "60"
  - App-scope value set, no device override → "Your device is using
    the app default: 60"
  - Neither set → "Using the catalog default: 30"

  Surfacing the source matters: the customer should know why their
  value is what it is, including whether changing the device-scope
  setting would override an operator's app-wide intent. (More
  informative than "60 (default)" or just "60", and removes the
  "did the operator do something to my device?" surprise.)
- An "Edit" button that opens a modal — reuses the existing catalog
  editor's prefill + masked-reveal + Encrypted-checkbox UX. Locked
  to device scope (operator can't write app-scope).

**Per-var visibility.** A var is shown in the app view iff it has a
`label` field in the catalog. No label → hidden, admin-only.
Operator-jargon stuff (`debug_flag_experimental`, perf knobs, etc.)
just doesn't get a label and stays in the admin UI only. *Catalog
field addition needed — see below.*

**Recent activity.** Last ~10 telemetry messages from the device's
queue topic, showing timestamp + a brief one-line summary. The
"summary" rendering depends on what the catalog declares about the
telemetry shape — for now, just dump the decoded msgpack as compact
JSON. A future iteration could let the catalog declare a "render
hint" per topic. Out of scope here.

**Things explicitly NOT on the app view:**
- No Activity Logs tab (admin-side concept).
- No Topic Monitor (raw queue inspection is a debugging tool).
- No Catalogs (operator never needs to publish or edit a schema).
- No Admin Users / Backup / Restore (provisioning operations).
- No fleet view, no other devices.

## URL + auth flow

**URL shapes.**
- `/app/<app>/<device>` — the canonical bookmarkable URL for one
  device. The customer's permanent address.
- `/app/` — friendly landing page for customers who don't know (or
  forgot) their full URL. See "Bare-URL landing form" below.

Multi-device owners just have multiple bookmarks; we don't need a
"pick your device" landing page in v1.

**Stale or wrong URL → soft 404.** A request for
`/app/<app>/<unknown_device>` (no such device exists) renders the
bare-URL landing form with an inline message ("That device wasn't
found. Try entering its name below."). Doesn't hard 404 the
browser. Same shape if the URL pattern itself ever shifts in v2 —
old bookmarks degrade to "enter your device name" rather than
breaking outright. Cheap insurance that makes the URL shape less of
a one-way door.

**Auth.** Same htpasswd + cookies + `admin_acls:<user>` machinery.
The server route handler:

1. Resolves the admin context via the existing middleware (no code
   change there).
2. Calls `check_acl(ctx, f"kv/{app}/{device}", mode="write")` — if
   it fails, redirect to a friendly "you don't have access to this
   device" page (or just 403, depending on appetite).
3. If ok, serves the static `/app/index.html`. Page-side JS then
   takes over, reading `window.location.pathname` to discover its
   own (app, device) and fetching from the existing admin APIs.

**Bare-URL landing form.** *Deprecated by v1.5 — see
[fr_v15_auth.md](fr_v15_auth.md). Kept here for historical
reference; the `landing.html` page, the form's JS handler, and
the `/api/app/lookup_device` endpoint are scheduled for removal
in v1.5 Phase 7.* Original v1 design: a customer who lost their
bookmark or got their device name written on a sticker but
doesn't know the URL pattern hits `/app/` and sees a single-
input form: "Enter your device name." Submit → the server looks
up which app contains that device → 302 to `/app/<app>/<device>/`.
Auth happens at the destination via the same flow as a direct
visit (basic auth prompt or existing session cookie).

**Why v1.5 removes it:** Google sign-in dissolves the chicken-
and-egg the form was solving. With universal sign-in, a customer
who hits `/app/` (no session) gets redirected to Google, signs
in, and lands on a device-list page derived from their ACL — no
need to "look up" their device by name. The lookup form's
enumeration risk (which Turnstile was added to mitigate) goes
away as a bonus.

Lookup mechanism for v1: scan `kv:*/<device>/*` on each form submit
to find the app. Single Redis call, no schema work. The form is
interactive — even a few hundred ms per lookup is acceptable.

**Known issue (deferred):** the scan grows linearly with fleet size.
At small scale (hundreds of devices) it's invisible. Past some
threshold (probably ~10K KV keys, depending on Redis config) it'll
get slow enough to be noticeable. The fix is a `device_to_app:<device>`
reverse index written at device-provisioning time — O(1) lookup, a
few lines of bookkeeping in whatever provisions a device. File
separately when actual perf complaints surface; not worth pre-
optimizing for fleet sizes we don't have.

**Constraint relied on:** device names are unique across apps. (True
today; would need a per-device-name disambiguation step if that
breaks.)

**Failure mode (deliberately bare).** Device not found → "No device
by that name was found. Check the spelling or contact your
administrator." We do **not** suggest similar names ("did you mean
'rico'?"). The reason is non-obvious enough to call out: if a
customer types "ricky" thinking that's their device but the actual
name is "rico", a Levenshtein-style suggestion could redirect them
to `/app/<app>/rico` — where their credentials don't work. They get
"login failed" with no signal about what's wrong, instead of "the
name you typed isn't a device." False positive in lookup turns into
a false negative at auth, which is worse than the bare miss. Tell
the customer their name didn't match, let them re-check the sticker
/ contract / wherever they got it.

(Same logic blocks ambiguity disambiguation, "we sent you an email"
recovery, etc. — all defer to operator support for v1.)

The form itself doesn't need auth — it's just a name lookup. The
302 destination is what triggers auth (existing flow). A user
without permission for the resolved device hits the standard
"you don't have access" path described above.

**Anti-enumeration: Cloudflare Turnstile (or equivalent) gates the
form submit.** The lookup endpoint is unauthenticated by design (a
customer can't be expected to log in *before* they know their
device URL), so anyone on the internet could otherwise probe
`/api/app/lookup_device?name=...` to enumerate device names.
Captcha-gating the form submit blocks programmatic scraping at the
edge with no auth burden on legitimate customers. Treats device
names as "not-deeply-secret but not-trivially-enumerable" identifiers
— acceptable since they're intended to be the kind of friendly
serial-number-shaped string a customer sees on their hardware.

**Implication for ACL provisioning.** A customer's ACL looks like:
```json
{"permissions": [
  {"prefix": "critterchron/ricky",    "access": "rw"},
  {"prefix": "critterchron/public",   "access": "r"},
  {"prefix": "_catalog/critterchron", "access": "r"}
]}
```

Three purpose-specific grants, each doing exactly one job:

1. **Device-scope read+write** (`critterchron/ricky:rw`). The
   customer's own KVs and per-device queue (if any).
2. **Public-namespace read** (`critterchron/public:r`). The shared
   telemetry stream (`q:critterchron/public/...`), shared scripts/
   firmware blobs, any other cross-device-visible data. See the
   [Namespace convention](#namespace-convention-apppublic) section
   for the convention.
3. **Catalog read** (`_catalog/critterchron:r`). The published
   catalog YAML.

No broader read needed; **cross-device read is denied by default**.
A customer-for-ricky cannot read `kv:critterchron/timmy/...` or
`q:critterchron/timmy`, because none of their three perms match
those paths. Multi-tenant isolation falls out of the convention
naturally.

## Server-side additions

Mostly nothing! The app view consumes existing endpoints:

- `GET /api/admin/me` — **new**. Returns `{username, acl,
  is_superuser, scope_kind}` so the page can confirm identity and
  refuse to render if something's off.
- `GET /api/admin/peek/kv/<app>/<device>/<keyName>` — already exists.
  Per-var read.
- `GET /api/admin/peek/kv/<app>/<keyName>` — already exists. App-scope
  fallback for "current effective value" rendering.
- `GET /api/admin/peek/kv/_catalog/<app>` — already exists. Catalog
  YAML.
- `POST /api/admin/kv/<app>/<device>/<keyName>` — already exists with
  `{value, encrypted}` payload (added in `fr_encrypted_values.md`).
  Per-var write.
- `GET /api/admin/stream/q/<topic>` — already exists. Returns the last
  N messages with `received_at` derived from stream IDs. Used for the
  telemetry tail and the "last seen" computation. The `topic` for a
  device follows the catalog/app convention (for critterchron it's
  the device's heartbeep topic — needs confirmation).
- New thin route `GET /app/{app}/{device}` — **new**. Auth-gates
  (per the flow above) then serves the static `app/index.html`.
- New thin route `GET /app/` — **new**. Serves the bare-URL landing
  form (static page).
- New endpoint `POST /api/admin/provision_device` — **new** (landed
  2026-05-04). Idempotent one-shot device provisioning,
  superuser-gated. Body `{client_id, app}`. Two response shapes
  depending on whether the client already existed:
  `{client_id, secret: "<hex>", acl, created: true}` for new clients
  (secret shown once, save now); `{client_id, secret: null, acl,
  created: false}` for existing clients (secret left untouched —
  don't re-leak via provision; ACL replaced with the device-on-app
  shape). Idempotent re-runs are safe; useful for retrofitting the
  device-on-app ACL onto clients minted before this endpoint existed.
  Reserved-name guard (`RESERVED_CLIENT_IDS`) applies.
  Replaces the manual sequence of POST /keys → PUT /keys/{id}/acl
  with the formulaic ACL `[<app>/<device>:rw, <app>/public:rw]` per
  the namespace convention. Surfaced in the admin UI's Key Management
  tab as the primary "Provision Device for App" form; the legacy bare
  "Register New Client" form stays for non-app cases (admin-management
  clients, multi-app devices, custom ACL shapes). 8 live tests in
  [`tools/tests/test_provision_device_live.py`](../tools/tests/test_provision_device_live.py)
  cover success, refuse-overwrite, reserved-name, empty-field,
  slash-in-id, and superuser-required. **CLI command deferred** — the
  current `stra2us` CLI is HMAC-only and has no admin-auth path; the
  admin UI is sufficient at current provisioning cadence, and bulk
  cases can use a 15-line `requests`-based script if needed.

- New endpoint `GET /api/app/lookup_device?name=<device>` — **new**.
  Scans `kv:*/<device>/*` and returns `{app: "critterchron"}` (or
  404). No auth required — pure name → app lookup, no data exposed
  beyond what the form already implies. The form's submit handler
  calls this and then `window.location.assign(...)` to the resolved
  URL, which triggers auth at the destination.

So the server work is one new API endpoint (`/api/admin/me`), one
lookup endpoint (`/api/app/lookup_device`), and two route handlers
(`/app/{app}/{device}` and `/app/`). Everything else is consuming
what's already there.

## Namespace convention: `<app>/public/`

The ACL shape above relies on a path-naming convention. Stra2us is
intentionally path-opaque — it just does prefix matching on whatever
paths come in — so the *meaning* of paths is realized entirely by
the ACL grants operators issue. The convention:

- **`<app>/public/*`** — anything cross-device-visible. Shared
  queues (telemetry streams, time-sync, coordination), formerly
  app-scope KVs that operators want as overrideable defaults,
  shared scripts/firmware blobs.
- **`<app>/<device>/*`** — per-device private namespace.
- **`_catalog/<app>`** — schema (already a separate namespace).
- *Implicit:* anything that isn't `public` (or other future reserved
  sub-namespace under `<app>/`) IS a device. No explicit `devices/`
  segment needed; "everything else is a device" keeps paths short
  and avoids a firmware migration.

**Reserved sub-namespace list: just `{"public"}`.** Future shared
sub-categorizations (`metrics`, `health`, etc.) live UNDER
`public/`, not as new top-level reserved names — keeps the reserved
list a single word permanently.

### Why this works (and why the cross-device leak is gone)

The prefix matcher (`_prefix_matches`) treats `<app>/public` and
`<app>/<device>` as sibling namespaces — neither encompasses the
other, because the matcher requires either an exact match or a
`prefix + "/"` segment boundary. So a permission with prefix
`<app>/public` matches `<app>/public/anything` but **not**
`<app>/anything_else`. The cross-device leak that the broader
`<app>:r` perm caused (the previous design) is structurally
impossible under the new convention.

### Migration

Existing data needs to move into `public/`:

- **App-scope KVs** (~4 live entries in critterchron:
  `critterchron/{cloud_heartbeep, heartbeep, ir_poll_interval,
  timezone_offset}`) → `<app>/public/<varname>`. One-shot Redis
  `RENAME` per key, doable by hand. *Sweep simultaneously:* the
  superseded `coaticlock/*` POC data still in Redis (5 app-scope
  KVs + the `bb32` device's data) — can be deleted outright, no
  longer used.
- **`<app>/scripts/...` and `<app>/fw/...`** (existing shared
  blobs, currently siblings to devices at the device-level slot) →
  `<app>/public/scripts/...` and `<app>/public/fw/...`. Slightly
  bigger because the consumers (devices that fetch shared scripts
  or firmware) need to know the new path. Coordinated firmware roll
  required. Not blocking the FR; can land incrementally.
- **Shared queue topics** (e.g., critterchron's current shared
  `q:critterchron` telemetry stream) → `q:<app>/public/...`.
  Firmware update to publish to the new path. Confirmed
  in-progress with critterchron firmware roll.

**Per-device data is unchanged.** Devices keep using
`/kv/<app>/<device>/<varname>` and `/q/<app>/<device>` (or whatever
per-device topics they use). No firmware change for the per-device
path.

**Code changes in stra2us itself: minimal.** Two one-liners for path
construction:
- `static/app.js: _kvPath(app, keyName, null)` →
  `${app}/public/${keyName}` instead of `${app}/${keyName}`.
- `tools/stra2us_cli/catalog.py: kv_path(app, keyName, None)` →
  same change.

The catalog editor's "app-scope" pane stays exactly as it is —
operators write to the same logical "app default for this var" slot
they always have; only the underlying path moves. Which means
existing workflows for setting an app-wide default keep working.

## Reserved-name enforcement

The convention is footgun-free as long as no one creates an HMAC
client whose `client_id` is `public`. A rogue `public` client would:
- Have its per-device data live at `<app>/public/...`, colliding
  with the shared namespace.
- Cause a customer's narrow `<app>/public:r` grant to suddenly
  include the rogue device's private data.
- Cause the rogue device's writes to land in the shared namespace.

**Single enforcement point:**

`POST /api/admin/clients` (`create_client` in
[routes_admin.py](../backend/src/api/routes_admin.py)) refuses
client IDs in the reserved list. Both the admin UI's Key Management
tab and any direct API call hit this route, so single-point
enforcement covers everything.

```python
RESERVED_CLIENT_IDS = {"public"}

@router.post("/clients", ...)
async def create_client(client: ClientCreate, ...):
    if client.client_id in RESERVED_CLIENT_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Client id {client.client_id!r} is reserved as a "
                f"namespace under each app. Reserved: "
                f"{sorted(RESERVED_CLIENT_IDS)}."
            ),
        )
    # ... existing create logic ...
```

**Match is case-sensitive and exact.** Only `public` is blocked;
`Public`, `PUBLIC`, `_public`, `publik`, `pub` all pass. This is
deliberate — case-folding and fuzzy matching invite their own
edge-case bugs, and the convention is just "don't pick the literal
word `public`." See [Known issues / explicit caveats](#known-issues--explicit-caveats)
below.

**Defense in depth (small):**
- `restore_keys` (the backup-restore endpoint) iterates client IDs
  from a backup file. Mirror the check there so a backup containing
  `client:public:secret` can't silently un-reserve it. ~3 LOC.
- Any future CLI like `stra2us provision-client` should mirror the
  check for friendlier errors before the round-trip.

**Pre-ship drift check:** before deploying the validator, scan for
existing collisions to avoid blocking legitimate restarts/rotations
against a pre-existing rogue client:

```bash
redis-cli --scan --pattern 'client:public:*'
```

Expected: zero hits given current fleet sizes. If non-zero, decide
case-by-case (rename, revoke, etc.) before turning the check on.

**What stra2us-server explicitly does *not* enforce** (intentionally,
because they're not actually footguns):
- Writing KVs at `kv:<app>/public/...` — the *intended* use.
- Granting ACL perms with prefix `<app>/public` — how customers get
  their read access.
- Catalog YAML naming devices — catalogs don't enumerate devices.

## Critterchron firmware-team brief

*This section is the self-contained "what the critterchron firmware
team needs to do" derived from the namespace decision above. Kept
inline with the FR rather than a separate doc so the rationale stays
attached to the action items — future readers can see why the
specific path changes were chosen.*

### Why this is happening

Stra2us is gaining an "application view" at `/app/<app>/<device>/`
intended for end customers (the people who own a critterchron) to
log in, see their device's status, and adjust settings. To make
that login useful — i.e. to keep customer A from accidentally
reading customer B's secrets — we need each customer's ACL to be
narrowly scoped to their own device's namespace, plus a *public*
namespace for shared data.

Stra2us's ACLs are flat-prefix matchers: a permission with prefix
`critterchron` matches `critterchron/anything`, including any other
device. Today critterchron's shared queue lives at exactly
`q:critterchron`, which means customers need a `critterchron:r`
grant just to see telemetry — and that grant *also* gives them read
on `kv:critterchron/<other_device>/<anything>`. Cross-device leak.

Fix: by convention, **all cross-device-visible data moves under
`<app>/public/...`**. Customer ACLs become `<app>/<device>:rw` +
`<app>/public:r`, neither of which matches other devices' paths.
The leak is gone structurally — no stra2us code change needed for
the ACL piece, just a path-naming migration.

This applies to critterchron because it currently uses the
shared-topic pattern. Future apps that ship per-device topics from
day one wouldn't need this.

### What firmware changes

Three path changes. All are publish-side or read-side; the request
signing protocol, HMAC client_id, and per-device KV paths are
unchanged.

| What | Old path | New path |
|---|---|---|
| Telemetry publish | `q:critterchron` | `q:critterchron/public/heartbeep` |
| Shared script fetch | `kv:critterchron/scripts/<name>` | `kv:critterchron/public/scripts/<name>` |
| Shared firmware blob fetch | `kv:critterchron/fw/<name>` | `kv:critterchron/public/fw/<name>` |

That's it. (If critterchron also publishes other shared streams
beyond `heartbeep`, those move under `q:critterchron/public/<name>`
too — same convention.)

### Catalog YAML changes (shipped in the same PR as firmware)

The firmware and the published catalog YAML
(`<app>.s2s.yaml`) are coupled — both consume the same path
conventions, and the customer-facing `/app/<app>/<device>` UI
reads the catalog to know which topic to tail. Update them in
the same PR. Two new top-level fields per
[`catalog_spec.md`](catalog_spec.md):

```yaml
app: critterchron
telemetry_topic: "{app}/public/heartbeep"   # NEW — post-migration topic
heartbeat_interval_seconds: 30              # NEW — drives /app status
                                            # thresholds; set to your
                                            # actual cadence
vars:
  # existing var declarations unchanged
```

`{app}` and `{device}` are placeholders the app view substitutes
at runtime — set `telemetry_topic` to whatever literal path the
firmware actually publishes to. If the firmware publishes
per-device (e.g. `q:critterchron/public/<device>/heartbeep`),
declare `"{app}/public/{device}/heartbeep"`.

`heartbeat_interval_seconds` should match the cadence the
firmware actually uses (the device's `heartbeep` setting, in
seconds). Default 60s. Used by the app view to decide
"Online / Recently active / Offline" status — `<2× interval` is
Online, `<20× interval` is Recently active, otherwise Offline.

While you're in the catalog, this is also a good moment to add
`label:` to any var that should be visible in the customer-facing
app view (a few human-friendly words; presence is the visibility
gate). See `catalog_spec.md` §2 for the full list of var-level
fields, including `encrypted: true` for secret material.

### What firmware does NOT change

To make scope crisp:

- **Per-device KV paths.** Devices keep reading and writing
  `kv:critterchron/<device>/<varname>` exactly as today. No change.
- **HMAC signing protocol.** Request signing, response verification,
  timestamp drift window — all identical.
- **Client_id.** A device's id stays its id (which is what the URL
  bookmark uses, what the ACL grants reference, what `client.put`
  signs against).
- **Catalog YAML reads.** Catalog still lives at `_catalog/critterchron`.
- **Per-device queue topics**, if any (`q:critterchron/<device>` —
  not currently used in critterchron, but if it ever is, the path
  is unchanged).

### Device ACL update

Each critterchron device's HMAC client ACL today (likely something
like `critterchron/<device>:rw` + `critterchron:rw` for shared
publish) needs the shared grant narrowed:

```diff
 {"permissions": [
   {"prefix": "critterchron/<device>", "access": "rw"},
-  {"prefix": "critterchron",          "access": "rw"}
+  {"prefix": "critterchron/public",   "access": "rw"}
 ]}
```

Required for devices to keep being able to publish to the new
shared topic and fetch from the new shared paths. Stra2us-side
operator action; not a firmware change but tightly coupled to the
firmware roll.

### Suggested cutover sequence

The transition has three moving parts (firmware paths, stra2us KV
data location, device ACLs). Order matters less than usual because
the paths involved are mostly separate — the worst case at any
intermediate step is "telemetry isn't being captured for a few
minutes" or "shared script fetch returns 404." Both recoverable.
Recommended order:

1. **Stra2us data move.** Operator runs:
   ```bash
   redis-cli rename kv:critterchron/scripts/<each>   kv:critterchron/public/scripts/<each>
   redis-cli rename kv:critterchron/fw/<each>        kv:critterchron/public/fw/<each>
   redis-cli rename kv:critterchron/heartbeep        kv:critterchron/public/heartbeep
   redis-cli rename kv:critterchron/cloud_heartbeep  kv:critterchron/public/cloud_heartbeep
   redis-cli rename kv:critterchron/ir_poll_interval kv:critterchron/public/ir_poll_interval
   redis-cli rename kv:critterchron/timezone_offset  kv:critterchron/public/timezone_offset
   ```
   (And the queue topic stream itself if you want history preserved
   — `RENAME q:critterchron q:critterchron/public/heartbeep`.)

2. **Add `critterchron/public:rw` to every device ACL** *before*
   removing the broader `critterchron:rw`. Devices keep working
   under both grants during the transition (the prefix matcher
   accepts the first match, so order in the JSON list matters —
   put the more specific `critterchron/public` perm first if you
   want it preferred).

3. **Push firmware update.** New firmware publishes telemetry to
   `q:critterchron/public/heartbeep` and fetches scripts/fw from
   the new paths. Devices that haven't updated yet keep publishing
   to the old topic (which now has nobody listening for the
   purposes of the app view, but no harm — it's just a stream
   getting writes).

4. **Once all devices are on new firmware**, drop the broader
   `critterchron:rw` from each device's ACL — only the narrower
   `critterchron/public:rw` remains. Sweep any leftover data at
   the legacy paths.

If a device is offline / unreachable for the firmware roll, it
keeps publishing to the old topic and showing offline in the app
view until it comes back online and gets the update. Acceptable
since the customer-facing app view is new — no existing customer
expectation to break.

### Compatibility window

Roughly: **between steps 1 and 3, devices on old firmware will
appear offline in the app view.** They're still functional; their
telemetry just goes to a topic the app view isn't tailing. The
stra2us push has stated this is "minutes" of work for critterchron;
the customer-facing app view isn't in production yet, so no
customer-visible outage during the window.

### Drift-test recommendations (operational, post-cutover)

Worth automating as part of critterchron's CI or deployment health
checks once the new convention is live:

- **No KV path under `<app>/public/<known_device_id>/...`.** Catches
  the operator footgun of putting per-device data inside the public
  namespace (would expose it cross-tenant). Lint walks the device
  list and the KV scan output.
- **No queue path under `q:<app>` (bare, no sub-prefix).** Catches
  any straggler that didn't migrate.
- **Every device's ACL has exactly `<app>/<device>:rw` +
  `<app>/public:rw`** (and nothing broader). One-liner sweep.

These are the same family of drift tests as the encrypted-record
name lints (`docs/fr_encrypted_values.md`).

### What's not asked of the firmware team

To be clear about scope:
- Not changing how devices authenticate (HMAC stays the same).
- Not changing how the catalog is consumed.
- Not changing per-device behaviour (settings reads, OTA, etc.).
- Not adopting any new dependencies, libraries, or protocols.

Pure path renames. Three of them, in three obvious places in the
codebase.

## Catalog enhancements (small, optional)

**App-level: telemetry topic declaration.**
```yaml
app: critterchron
telemetry_topic: "critterchron/public/heartbeep"   # critterchron post-migration
# or "critterchron/public/{device}/heartbeep" for per-device-tagged
# or omitted → default convention "{app}/public/{device}"
vars:
  ...
```

Stra2us doesn't need to read this; it's consumed by the app view's
JS to know which topic to tail for "last seen" + recent activity.
Shipping the field with a default convention keeps the topic-naming
decision *per-app*, not stra2us-wide — each app picks the topic
shape that matches its actual firmware.

After the namespace migration described above, all telemetry topics
live under `<app>/public/` regardless of whether they're shared or
per-device — the only choice is whether the topic name includes the
device id. Customer's `<app>/public:r` grant covers either shape, so
this decision is no longer ACL-coupled (which is the whole point of
the public/ convention).

**Per-var: `label` for app-view visibility.**
One addition to `<app>.s2s.yaml` per var:
```yaml
vars:
  wifi_password:
    type: string
    scope: [app, device]
    help: WPA2 PSK             # description shown as a tooltip / sub-line
    encrypted: true            # already specified in fr_encrypted_values.md
    label: WiFi password       # NEW — human-friendly title for the app view
  brightness_schedule:
    type: string
    scope: [app, device]
    help: "comma-separated time:level pairs, e.g. 06:00:0.2,18:00:0.8"
    label: Brightness schedule
  debug_flag_experimental:
    type: bool
    scope: [app]
    help: dev-only
    # no label → hidden from app view, admin-only
```

**`label` is the single opt-in signal for the app view.** A var with
a `label` is customer-facing; absence means "hidden, admin-only."
Collapsing this into one field (rather than separate `label` and
`internal: bool`) forces an explicit "have I thought about how this
looks to a customer?" decision at catalog-write time. The operator
can't accidentally expose `debug_flag_experimental` just by
forgetting an `internal: true` — they'd have to deliberately write
a label.

`label` is distinct from `help`: `label` is the title (a few words),
`help` is the description / tooltip (a sentence). The catalog editor
in `/admin` shows both today. The app view in `/app` shows the label
as the card title and the help text as the sub-line.

Field is advisory, consumer-side only — stra2us doesn't need to know
about it; the catalog YAML loader reads it. Drift-test pattern:
anything matching `^(debug_|perf_|.*_experimental$)` should NOT have
a label. Same lint family as the encrypted-name patterns, just
inverted.

## UI sketch

```
┌──────────────────────────────────────────────────────┐
│  ricky                            [● Online]         │
│  Last seen: 12 seconds ago                           │
│  ─────────────────────────────────────────────────    │
│                                                       │
│  Settings                                            │
│                                                       │
│  ┌─────────────────────────────────────────────┐    │
│  │ WiFi password                       [Edit]  │    │
│  │ ●●●●●●●●●●●  [Reveal]                       │    │
│  │ WPA2 PSK                                     │    │
│  └─────────────────────────────────────────────┘    │
│                                                       │
│  ┌─────────────────────────────────────────────┐    │
│  │ Brightness schedule                  [Edit] │    │
│  │ 06:00:0.1, 18:00:0.8, 22:00:0.05            │    │
│  │ comma-separated time:level pairs             │    │
│  └─────────────────────────────────────────────┘    │
│                                                       │
│  ┌─────────────────────────────────────────────┐    │
│  │ Heartbeep interval                   [Edit] │    │
│  │ 30 seconds                                   │    │
│  │ telemetry beat interval                      │    │
│  └─────────────────────────────────────────────┘    │
│                                                       │
│  ─────────────────────────────────────────────────    │
│                                                       │
│  Recent activity                                     │
│  12s ago    {"battery": 0.84, "uptime_h": 42}        │
│  42s ago    {"battery": 0.84, "uptime_h": 42}        │
│  1m 12s ago {"battery": 0.85, "uptime_h": 42}        │
│  ...                                                  │
└──────────────────────────────────────────────────────┘
```

Edit modal reuses the catalog editor's primitives:
prefill-from-current, textarea for strings, mask + Reveal for
encrypted, Encrypted checkbox (probably hidden by default in the
app view since the operator shouldn't be flipping that flag — gated
on `me.is_superuser`).

## Optional admin-UI nav gating (low priority)

If a scoped customer ever lands on `/admin` (old bookmark, support
link), they'll see superadmin-only nav entries (Admin Users,
Backup/Restore) that 403 on click. Hiding those entries is cosmetic
polish — gate on `me.is_superuser` once `/api/admin/me` exists. Not
blocking; file when someone notices.

The catalog editor's existing `lockedDevice` mode is what the app
view's edit modal will reuse — same primitive, both surfaces.

## Implementation phases

**Phase 0 — Provision a customer for real.** No code. Provision a
scoped user with the recommended ACL shape against a real device,
log into `/admin` as them today, write down what hurts. Purpose:
confirm the ACL story works end-to-end against the existing
infrastructure before building UI on top.

Two sub-steps to make this useful in the post-namespace-decision
world:

- **Phase 0a — try the *current* setup.** Provision a user with
  the *legacy* broad ACL (`<app>/<device>:rw` + `<app>:r`) against
  a real device. Tests the existing scoped-admin enforcement on
  unmigrated data. Catches any per-route enforcement gaps under
  the current path conventions.
- **Phase 0b — migrate, then try the *target* setup.** Run the
  namespace migration (operator-side: relocate the ~4 critterchron
  app-scope KVs + scripts/fw + the shared queue topic, in
  coordination with the firmware roll). Then provision a fresh
  user with the *target* ACL (`<app>/<device>:rw` +
  `<app>/public:r` + `_catalog/<app>:r`) and exercise the same
  flow. Confirms the new convention works end-to-end before any UI
  code lands.

The "what hurts" feedback from 0b is the most valuable, since it
shapes Phases 1–4. If 0b reveals that the broader admin UI needs
gating to be usable for a customer (likely), that work moves into
Phase 1 alongside `/api/admin/me`.

### Phase 0a findings (2026-05-03)

Drove the existing `/admin` as scoped user `austin`
(`critterchron/ricky_raccoon:rw` + `critterchron:r`, the legacy broad
shape). Two real findings, one architectural decision confirmed.

**Finding 1: Cross-device KV leak in dashboard list.** With the legacy
`<app>:r` perm, the dashboard renders every `kv:critterchron/<other_device>/*`
key — `check_acl` correctly passes them under the broader read.
**Resolved structurally by the namespace migration:** post-migration,
austin's ACL becomes `critterchron/ricky_raccoon:rw` +
`critterchron/public:r` + `_catalog/critterchron:r`, none of which
match cross-device paths. No additional UI work needed.

**Finding 2: Catalogs → Devices tab leaks all device names.** The
device-list endpoint enumerates every HMAC client whose ACL grants
access to the app — gated only at the app-read level, not per-device.
*Persists after migration*: even with the narrow ACL, austin still
has the broader `critterchron/public:r` (which the endpoint accepts as
"can see this app at all"). Fix is server-side: filter the device-list
endpoint to include only devices the caller has `rw` on. ~10 LOC,
useful regardless of A/B choice. Tracked as part of Phase 6 (admin-UI
nav gating) since it's a different pattern than the JS gating but
lives in the same family of "scope what scoped users see."

**Architectural decision: Option B (separate `/app` view) confirmed.**
Phase 0a partially falsified the original assumption that Option A
(ACL-down the existing admin UI) would be unacceptable — functionally
the admin UI is mostly usable for a scoped user once gated. But the
customer-facing-polish argument still wins: admin UI is operator-shaped
(vocabulary, layout, no per-device bookmark URLs, no telemetry-derived
status), and re-skinning to feel customer-friendly would touch enough
surfaces that a separate `/app` view is comparable effort with cleaner
separation. The Option A nav-gating + endpoint-filter work still
happens (Phase 6) because admins still use `/admin`, but as supporting
polish, not as the primary customer surface.

**Phase 1 — `/api/admin/me`.** *Landed 2026-05-03.* New endpoint
in [`routes_admin.py`](../backend/src/api/routes_admin.py): returns
`{username, acl, is_superuser, scope_kind, scope_app, scope_device}`.
`scope_kind` derivation ignores read-only perms (which are scaffolding
for catalog/public access, not identity-defining) and looks at the rw
perms only. Five persona shapes covered + verified by 8 live tests in
[`tools/tests/test_me_live.py`](../tools/tests/test_me_live.py):
superadmin, device-narrow (post-migration target), device-broad
(pre-migration legacy — important for Phase 0a→0b transition),
app-scoped, multi-device (falls to `custom`), and the
provisioned-without-Redis-ACL deny-all case.

**Phase 2 — App view skeleton + bare-URL form.** *Landed
2026-05-03.* New surface at [`static/app/`](../backend/src/static/app):
`landing.html` (bare-URL form), `device.html` (per-device customer
page), `styles.css` (minimal, brand-neutral), `app.js` (single file
that bootstraps either page based on body class). New routes in
[`routes_app.py`](../backend/src/api/routes_app.py): `GET /app/`
serves landing (public), `GET /app/{app}/{device}` auth-gates +
ACL-checks then serves device.html (soft-404s to landing on missing
device or wrong owner), `GET /api/app/lookup_device` does a Redis
SCAN of `kv:*/<name>/*` for the form's name→app resolution (public).
Auth middleware gains `_path_needs_admin_auth()` helper that knows
the four /app/ public-vs-gated path classes. Static assets live at
`/app/_static/` (underscore-prefixed reserved namespace, mounted
before the dynamic routes so it claims the prefix). Verified
end-to-end through the browser: landing form submit → lookup →
redirect → cookie auth → device page renders 3 cards (encrypted
WiFi password masked, heartbeep value populated, brightness_schedule
"(not set)" fallback). Edit buttons stub to `/admin#kv-edit-...`
links pending Phase 3's shared-edit-modal extraction.

**Phase 3 — Edit modal in app view.** *Landed 2026-05-04, with a
deferred dedupe.* New modal in [`device.html`](../backend/src/static/app/device.html) +
[`app.js`](../backend/src/static/app/app.js): single-input form, locked
to device scope, no encrypted-checkbox UI (catalog-driven per FR's
"explicit caveats" section). Save POSTs `{value, encrypted: <current
state>}` to `/api/admin/kv/<path>` so the FR's "demote on bare set"
guard doesn't drop the flag. Per-card Reveal button on encrypted
records (mask is shoulder-surf protection only; value is in DOM).
Verified end-to-end through the browser: open Edit on heartbeep
(int) → modal prefilled with `45` → change to `60` → save → card
updates to `60` via re-fetch. Encrypted-record flow: open Edit on
wifi_password → input prefilled with plaintext but masked → Reveal
toggle works → save with new value → server `peek_kv` confirms
`encrypted: true` survived the round-trip.

*Deferred:* the FR called for lifting the edit primitives into a
shared module so admin and app stay in sync. To minimize risk to
admin's working catalog editor, I copied the primitives into app's
`app.js` instead and marked them with a `TODO(dedupe)` comment.
The follow-on dedupe pass (mount `/_shared/edit_primitives.js`,
have both surfaces import) is mechanical once both have stabilized.
Until then, fixes to either copy must be mirrored in the other.

**Phase 4 — Telemetry tail + status badge.** *Landed 2026-05-04.*
Topic resolution driven by catalog's `telemetry_topic` field with
`{app}` / `{device}` placeholder substitution; default convention
`{app}/public/heartbeep`.

**Status thresholds are catalog-driven, not hardcoded.** Catalog
declares `heartbeat_interval_seconds` (default 60s if absent); status
buckets derive as multiples of it: `< 2× interval` → Online (one
missed beat tolerated as jitter), `< 20× interval` → Recently active,
otherwise Offline. So a 5-minute-cadence device is healthy at 4
minutes since last message (online), still recent at 30 minutes
(recent threshold = 100min), and offline only past ~1.5h. Same code
correctly handles a 30s-cadence device with thresholds in the seconds
range. App-agnostic — each catalog tunes its own staleness model.

Recent activity tail renders the last 10 messages (count-bounded,
not time-bounded — staleness is communicated per-row by the relative
timestamp on each entry). Decoded payload as JSON. Filtered by
`client_id` so cross-device messages on a shared topic don't leak
into the customer's view. Refreshes every 30s + on tab visibility
change. Server-side: `/api/admin/stream/q/{topic:path}` (was
`{topic}` — needed `:path` to support multi-segment topic names per
the new convention).

Verified end-to-end against mock telemetry with a 5-minute-cadence
catalog: 4-min message → Online with "Last seen 4m ago"; 30-min
message → Recently active; 3-hour message → Offline. Cross-device
filter confirmed (rachel_raccoon messages on the shared topic do
not appear in ricky_raccoon's view).

**Phase 5 — Catalog field formalization.** *Landed 2026-05-04 as
spec doc updates.* The actual code consumption of the new fields
landed during earlier phases (Phase 2 honors `label`, Phase 4
honors `telemetry_topic` and `heartbeat_interval_seconds`,
encryption FR honors `encrypted`). Phase 5 was the documentation
cleanup pass: [`catalog_spec.md`](catalog_spec.md) now formally
documents all four fields — `telemetry_topic` and
`heartbeat_interval_seconds` in the top-level fields table; `label`
and `encrypted` in the variable descriptor table. The example was
updated to show them in context. A new §5.1 in the catalog spec
captures the recommended drift-lint patterns: "sensitive vars MUST
be encrypted" (mirror of fr_encrypted_values.md), "operator-only
vars MUST NOT have a `label`" (inverse of the visibility
convention), and "no reserved sub-namespace names as device
identifiers." Originally framed as a separate `internal: bool`
field; collapsed during design discussion to "no label = hidden,"
which keeps the schema smaller and forces the operator to think
about customer-facing copy when adding a var.

**Phase 6 — Admin UI nav gating + device-list filter.** *Landed
2026-05-04, smaller than estimated.* Two pieces:

- **Nav gating:** turned out to already exist in admin's
  [`app.js:applyWhoami()`](../backend/src/static/app.js) hiding
  `.nav-superuser` entries (Key Management, Admin Users,
  Backup/Restore) for non-superusers. Was calling a parallel
  `/api/admin/whoami` endpoint; consolidated to `/me` (Phase 1's
  unified identity endpoint, which is a strict superset). Deleted
  `/whoami` so we don't carry duplicate routes. Verified: scoped
  admin sees 4 nav entries instead of 7.

- **Device-list filter** (the Phase 0a finding): the
  `/api/admin/catalog/{app}/devices` endpoint enumerated every HMAC
  client in the fleet. Now filters by the caller's per-device rw
  ACL — superadmin sees the whole fleet, app-scoped admin sees
  every device under their app, scoped customer sees only their own
  device. Outer gate moved from `kv/<app>:r` (legacy broad shape) to
  `kv/_catalog/<app>:r` (matches the recommended scoped ACL).
  Verified: superadmin sees 5 devices, scoped sees just
  ricky_raccoon, fully-unprovisioned admin gets 403.

**Phase 7 (optional) — Customer onboarding ergonomics.** Friendly
error pages (no-access, device-not-found), self-service password
reset, "share this link with support" affordance. Defer until we
have real customers to study.

## Open questions

1. ~~**Telemetry topic naming.**~~ **Resolved.** Catalog-declared
   `telemetry_topic` field at the app level, default convention
   `{app}/public/{device}`. Critterchron post-migration will declare
   `"critterchron/public/heartbeep"` (shared topic) or similar. The
   public/ namespace convention makes the topic-shape decision
   ACL-independent — see "Namespace convention" above.

2. **Multi-device URL navigation.** A customer with two devices has
   two bookmarks. Is that fine, or do we want a `/app/<app>` landing
   that lists their devices? The latter requires the page to know
   which devices the customer has access to (deriveable from their
   ACL — every `<app>/<device>:rw` perm). Punt to v1.5 unless real
   complaints.

3. **Customer auth UX.** htpasswd is fine for internal staff. For
   real end-customers, we'd want a real login page (not browser
   basic auth dialog), self-service password reset, possibly
   federated auth. Out of scope here — v1 ships with the existing
   auth, just under a different URL.

4. ~~**Styling.**~~ **Resolved (lightweight).** v1 ships visually
   similar to `/admin` but cleaner — same shared CSS palette, no
   per-app brand effort. Catalog YAML can grow optional fields
   later (`brand_color`, `logo_url`, etc.) for apps that want
   light brand customization without forking the page. Not worth
   meaningful effort now; the door's left open in the structure
   (per-app catalog declarations driving render) but no
   implementation work is planned.

5. ~~**Read-only with edit-modal vs. inline editing.**~~ **Resolved**
   per user preference: read-mostly, click "Edit" to open a modal.
   This is what's sketched throughout the FR. Worth re-evaluating
   after Phase 3 ships and operators have used it for a week, but
   the v1 decision is settled.

6. ~~**Encrypted-checkbox visibility for non-superusers.**~~
   **Resolved (firmer than the original sketch).** The encryption
   decision belongs to the *catalog* — the app developer declares
   `encrypted: true` on a var, and that's the source of truth. The
   app view does **not** expose the Encrypted checkbox at all; users
   never get a choice. App-view saves include `encrypted: <current
   state>` in the POST so the FR's "demote on bare set" semantic
   doesn't accidentally drop the flag. The admin UI keeps the
   checkbox for operator/developer use.

## Longer-term: tightening `peek_kv` for encrypted records

The `<app>/public/` namespace convention (above) handles cross-
device read isolation structurally. The remaining gap is at-rest
plaintext: admin-side `peek_kv` returns *plaintext* for encrypted
records, even when the caller has only `r` on the path. The
encryption FR ([`fr_encrypted_values.md`](fr_encrypted_values.md))
protects values on the wire to devices, not at rest on the server.

Today this is fine — under the new convention, a customer's narrow
ACL (`<app>/<device>:rw` + `<app>/public:r`) doesn't include any
cross-device paths to peek into the first place. But if we ever want
defense in depth — or if a future ACL shape grants broader read for
some other reason — it'd be cleaner if `peek_kv` didn't hand out
plaintext for encrypted records to read-only callers.

**Proposed hardening (deferred):** make `peek_kv` return ciphertext
(or a redacted marker) for `encrypted: true` records when the caller
has only `r` on the path, not `rw`. Customer-for-ricky keeps seeing
plaintext for ricky's own records (they have `rw`), but gets opaque
bytes for any other-device record they could ever see. Smallest
change — pure server addition, no schema work. Estimate: ~30 LOC in
`routes_admin.py:peek_kv`.

Not blocking the FR. File when there's a concrete reason to harden
beyond what the namespace convention already gives us.

## Known issues / explicit caveats

Things accepted as-is, captured so future contributors don't think
they're bugs:

- **Reserved-name match is exact + case-sensitive.** `public` is
  blocked; `Public`, `PUBLIC`, `_public`, `publik`, etc. are not.
  Fuzzy/case-folded matching invites its own edge cases; the
  convention is just "don't pick the literal word `public`."
  Operator-side discipline.

- **Devices have stable identifiers; rename is not a flow we
  support.** Think of a device id like a friendly serial number —
  fixed at provisioning, used for the URL bookmark, never changed.
  If a rename flow ever becomes necessary, expect it to touch the
  bookmark URL, the device's HMAC client_id, every KV path under
  `<app>/<old_id>/`, the per-device queue topic name (if any), and
  the customer's ACL. Not impossible, but big enough that we'd want
  to design it as its own FR. URL-mismatch fallback today: stale
  `/app/<app>/<unknown_device>` → 404 → bare-URL form.

- **App-view edit modal does NOT expose the encryption flag.**
  Encryption is a developer/catalog decision (`encrypted: true` on
  the var declaration), not a customer choice. Saves from the app
  view include `encrypted: <current state>` to preserve the flag
  through the FR's "demote on bare set" semantic. Admin UI keeps
  the checkbox.

- **Mask + Reveal in the app view is theatrical.** Shoulder-surf
  protection only — the value is in the DOM, the Reveal toggle just
  flips a CSS mask. Not a real secret-hiding mechanism (devtools or
  a screen capture mid-reveal both expose it). Acceptable since the
  customer is, by definition, allowed to see their own device's
  values.

- **Admin user provisioning is currently manual.** `create_admin.py`
  + `redis-cli SET 'admin_acls:<user>' ...` is the path. A first-class
  "Add a Customer" UI flow is contemplated but not yet designed —
  the right CUJ is open and not blocking the rest of the FR.

  **Easy footgun while it's manual:** the customer-shape ACL has
  *three* perms, not two. Forgetting the third (`_catalog/<app>:r`)
  produces a confusing failure: the device page loads, then 403s on
  the catalog YAML fetch and shows "Couldn't load your device's
  settings: catalog &lt;app&gt; returned 403". The `provision_device`
  endpoint sets the device-shape ACL (two perms, no catalog grant)
  for HMAC clients, which has primed people's intuition for
  "two-perm device-on-app." The customer-shape is different. The
  three perms required:

  ```json
  {"permissions": [
    {"prefix": "<app>/<device>",   "access": "rw"},
    {"prefix": "<app>/public",     "access": "r"},
    {"prefix": "_catalog/<app>",   "access": "r"}
  ]}
  ```

  When the user-provisioning UI lands, it should set this shape
  automatically, mirroring how `provision_device` does for HMAC
  clients. Until then, document it loudly anywhere a customer's
  ACL gets manually edited.

- **Post-migration cleanup pass.** After relocating
  `<app>/scripts/...`, `<app>/fw/...`, and the app-scope KVs into
  `<app>/public/...`, expect a sweep for any "public-shaped" data
  that wasn't named accordingly today. Some shared data may have
  been written under per-device-looking paths that the convention
  now formalizes as belonging under `public/`. Worth budgeting the
  time for one operator-driven scan post-cutover.

- **The bare-URL lookup endpoint is unauthenticated** (gated only by
  Cloudflare Turnstile / equivalent at the edge). Device names are
  treated as "not deeply secret but not trivially enumerable"
  identifiers. If device names ever come to encode sensitive info
  (customer name, location, etc.), revisit the policy.

- **Mobile is desktop-CSS-inherited for v1.** The app view's shape
  (single column, stacked cards, no nav) is naturally mobile-friendly,
  but tap targets and modal sizing inherit `/admin`'s desktop-first
  styles. Will work on a phone in the technical sense, won't be
  *delightful*. Responsive breakpoints + touch-target sizing is a
  v2 polish pass — modest CSS effort, no structural redesign.

## What this FR is *not* proposing

- **A separate auth system.** Same htpasswd + ACLs. Customers are
  just admin users with a tightly-scoped ACL.
- **A separate stra2us-server-with-different-data.** Same backend,
  same Redis, same routes. The app view is presentation only.
- **Real-time push** (server-sent events, websockets). Telemetry
  tail is poll-based. Refresh on page focus. If we need real-time
  later, separate FR.
- **Multi-tenant data isolation as a security boundary.** The
  namespace convention plus ACL plumbing gives meaningful UX
  isolation between customers, but a server compromise still
  exposes everything. This FR is about UX, not adversarial
  isolation.
- **App-specific business logic.** The view is generic — driven by
  the catalog YAML and a topic-name convention. Per-app
  customization (e.g. "critterchron's brightness schedule needs a
  graphical editor") is out of scope for v1; could grow into a
  per-app render-hint catalog field later.
