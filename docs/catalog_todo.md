# Catalog — worklist & open questions

Running ledger for catalog work. Kept concise: items either have a
decision (date + note) or an owner + ask. When a section grows beyond
"active", split it into its own doc.

Pointer back: [catalog_spec.md](catalog_spec.md) is canonical; this
file is where we capture the in-flight thinking that shapes it.

---

## Decisions log

| Date       | Decision | Why |
|------------|----------|-----|
| 2026-04-22 | Accept FR, ship M1 as spec + reference CLI in `tools/`. | FR was thorough; pattern is proven in CritterChron; surface is small. |
| 2026-04-22 | `default: per-device` magic string → `default_per_device: true` flag. | Keeps `default:` strictly typed; lint/CLI don't need to special-case a sentinel string. |
| 2026-04-22 | Add `bool` and `enum` types (plus `format:` hint) in M1, not later. | FR open question #5; retrofitting after UI exists is painful. |
| 2026-04-23 | **Variant C: publish via existing `/kv/_catalog/{app}`, raw YAML text.** Replaces the earlier "bespoke `/catalog/{app}` endpoint + JSON projection" plan. | Zero new server code for M2; reuses HMAC + ACLs; YAML comments round-trip. |
| 2026-04-23 | Format on the wire is YAML text (not JSON). | Follows from Variant C; UI pays ~40 KB for js-yaml, acceptable for admin-only tool. |
| 2026-04-22 | M2 landed: `catalog publish` + `catalog fetch` via `/kv/_catalog/{app}`. | Smoke-tested round-trip byte-equal against local redis+uvicorn; 29 offline + 3 live tests green. |
| 2026-04-22 | `STRA2US_FIRMWARE_DIR` env override in backend (default still `/firmware`). | Hardcoded `/firmware` blocked non-Docker local dev; one-line, zero behavior change for the compose path. |
| 2026-04-23 | Static catalog-drift lint stays in critterchron for now; no upstream to `stra2us_cli`. | CritterChron is the only consumer today; its `test_s2s_catalog.py` (~210 lines) is the proven shape. Wait for a 2nd app before generalizing. |
| 2026-04-24 | Added `default_per_platform: true` as third mutually-exclusive sibling of `default` / `default_per_device`. | Critterchron's `light_exponent` needed it (Particle/CDS defaults to 2.5, ESP32/BH1750 to 0.5 because the upstream `n` normalization is inverted). `default_per_device` was lying about the source of truth. |
| 2026-04-25 | GET on missing KV stays at HTTP 200 with `{"status": "not_found"}`; not switching to 404. | Status envelope is already the contract on the device path; CLI and UI handle it; flipping to 404 would force every consumer to special-case error vs. unset and buys nothing. |
| 2026-04-25 | Per-key `enforce: true` confirmed working end-to-end. | Removed from parking lot after user-verified test. Server-side advisory-reject is live; closes FR open-question #2. |

## Active — M2 worklist

**Goal of M2:** app dev can publish their catalog to stra2us with a
single CLI command; the stashed copy is retrievable via the existing
`/kv/` endpoint so M3's UI has something to read.

M2 is **shipped** as of 2026-04-22. All residual follow-ups closed:

- [x] `_catalog` reservation test — `test_app_name_rejects_underscore_prefix`
      in `tools/tests/test_catalog.py` pins that apps can't start with `_`.
- [x] CritterChron hand-back — migrated catalog + adoption note live at
      `docs/handoff/critterchron.{s2s.yaml,md}`. Validates clean.

## Open questions (for when they come up, not now)

- **Versioning / history.** `POST /kv/` overwrites. If we decide we
  want "last N catalog revisions per app," cheapest path is a
  parallel publish to `/q/_catalog_history/{app}` (each publish is a
  queue message, TTL gives automatic retention). Defer until someone
  asks for it.
- **Discovery — "which apps have catalogs?"** Not answerable with
  existing server surface; needs a narrow prefix-scan endpoint
  (`GET /kv/?prefix=_catalog/`, admin-auth) **or** the UI carries a
  configured list of apps. Revisit when M3 lands; MVP-UI can hardcode.
- **Server-side schema validation.** Currently CLI-only. If we ever
  want advisory-reject on upload (invariant 2 + FR open-question-#2's
  "enforce" concept), the server would need pyyaml + pydantic — adds
  deps, worth a discussion. Low priority; CLI validation is already
  tight.
## Parking lot (not for M2, not forgotten)

- Web UI (M3) is **shipped** — the FR's four views (App index,
  Variables table, Key detail / device view, Device index) all live
  in the admin UI today, plus the FR's "raw key inspector"
  debugging tab. Remaining polish items below (read_cadence, last
  updated timestamp, diff viewer) and the FR's "nice to have" list
  (write audit log surfaced as a per-key history view, batch edits,
  live-preview push channel) aren't blocking — they earn their keep
  on demand.
- `read_cadence` UI rendering — schema field exists in M1; actual UI
  hint consumption lands in M3.
- Catalog diff viewer / history stream — predicated on versioning
  decision above.
- **Show "last updated" timestamp in Catalogs list.** On the
  admin-UI Catalogs / Published Apps view, display when each
  `_catalog/<app>` was last published. Redis strings don't carry a
  native mtime, so pick one of: (a) store a sidecar
  `_catalog_meta/<app>` `{updated_at}` written on publish, (b) scan
  the activity log for the latest `POST /kv/_catalog/<app>` entry
  (cheap, log-retention bound — currently 24 h), or (c) embed a
  `published_at` key into the catalog YAML itself at publish time
  (round-trips through fetch, but mutates user-authored content).
  Leaning (a) — cleanest, no schema change. Filed 2026-04-23.
- **Catalog drift lint as `stra2us_cli lint` subcommand.** Today
  critterchron ships its own `test_s2s_catalog.py` that greps
  `get_int("k", default)` / `get_float(...)` across HAL C++, resolves
  `#define SYMBOL N` defaults, and cross-checks against the YAML
  catalog (forward: call sites without entries; reverse: entries with
  no reader unless `ops_only`). When a 2nd app appears, upstream into
  `stra2us_cli lint`. Open design points at that time:
    - Stra2us should define a **canonical catalog-read API** (e.g.
      `Stra2usConfig::get_int(key, default)` on the official client).
      Apps that bypass it (raw HTTP, wrapper libraries, etc.) are out
      of scope for lint — that's an acknowledged gap, not a bug.
    - Language adapter shape: at minimum C++ (critterchron) and
      Python (for stra2us_cli + app scripts). JS/TS later if needed.
    - `#define`-resolution is C++-specific; the Python adapter will
      need its own "default literal" discovery (module constants).
  Filed 2026-04-23.
