# Stra2Us — IoT Telemetry Service

Stra2Us is a high-performance, stateless IoT messaging and configuration
relay designed for resource-constrained devices (ESP32, Particle Photon
2, and similar). It features an async Python/Redis backend, a
zero-malloc C++ client SDK, a Python reference CLI, and a browser-based
admin dashboard with per-app, catalog-driven device configuration.

> **License:** [PolyForm Noncommercial 1.0.0](LICENSE) — source-available
> for noncommercial use; reselling or commercial use requires explicit
> permission. Personal projects, research, education, hobby use → fine.

> **Status:** Production-shipped, **v1.8.1**. The deploy pipeline is
> dev → staging → prod via `tools/stage`; see [Deploying](#deploying).
> Admin auth is **Google OAuth** on the browser hostname, with an
> htpasswd `rescue` break-glass path on the device hostname. The
> discipline that produced this is captured in
> [Rules of Operation](#rules-of-operation) and
> [`docs/fr_v15_incremental.md`](docs/fr_v15_incremental.md).

## Design Architecture

- **Stateless Backend:** Zero in-process state — everything lives in
  Redis. Scales horizontally out of the box.
- **HMAC-SHA256 Signatures:** Devices sign requests with a shared
  secret + Unix timestamp (`X-Client-ID` / `X-Timestamp` /
  `X-Signature`). The server enforces a ±300 second replay window, and
  signs its responses back so a device can verify them with the same
  key.
- **Broadcast Streams:** Queues use Redis Streams (`XADD`/`XREAD`).
  Each subscriber maintains its own cursor, so multiple devices can
  read independently without consuming each other's messages.
- **Micro-serialization:** Payloads use [MessagePack](https://msgpack.org/)
  by default, cutting wire overhead vs. JSON. Plain-text
  (`text/plain`) is also accepted and automatically wrapped
  server-side.
- **Per-app catalog UI:** Apps describe their KV variables in a per-app
  YAML *catalog* (`<app>.s2s.yaml`); the admin dashboard renders a
  typed, validated configuration page per device from it. Values
  resolve `<app>/<device>` → `<app>/public` → catalog default.
- **Encrypted values:** A field can be marked `encrypted: true`; the
  server encrypts it on the device GET response (HMAC-keystream cipher,
  msgpack ext type `0x21`) so a wire observer doesn't see the
  plaintext. (Wire-confidentiality on fetch — values are stored in
  cleartext at rest; see [Security](#security).)
- **Backup / restore:** Whole-instance and per-app dump/restore of all
  load-bearing state (clients, ACLs, KV, catalogs, queues), with a
  versioned envelope format.

## Technical Stack

- **Backend:** Python **3.10+**, FastAPI, Uvicorn (multi-worker),
  Redis Streams.
- **Client SDK:** C++ (Arduino/ESP-IDF), zero-malloc, mbedTLS HMAC.
- **Reference CLI:** `stra2us` (Python) — publish/consume, KV
  read/write, catalog publish, synthetic-traffic generation.
- **Dashboard:** Vanilla HTML/JS, no build step. **Google OAuth** on
  the browser hostname; htpasswd Basic Auth `rescue` path on the
  device hostname; HMAC-signed session cookies (`Secure` +
  `SameSite=Lax`).

---

## Rules of Operation

These distill the failure modes that have hurt us before. They apply
to any change that touches the running service — dep bumps, code
edits, topology changes, recovery work.

1. **Verify before reacting.** When something breaks, diagnose with
   tests and logs before changing anything. Don't act on intuition.

2. **Tests are the truth.** A claim ("OAuth works", "the rebuild is
   safe", "deps are stable") needs receipts: a green
   [`tools/smoke_test.sh`](tools/smoke_test.sh) run or a reproducible
   repro. No receipts, no claim, no forward motion.

3. **Invariants pass, or fail in a predicted way.** Every smoke-test
   check has an expected pass-state and a known set of failure modes.
   A surprising failure mode means the test is wrong before the
   system is — fix the test first.

4. **Reproduce before fixing.** When a real failure surfaces, expand
   the smoke test to reproduce it *first*, then patch the underlying
   issue. This makes the regression catchable next time.

5. **Devices are sacred.** The IoT path
   (`iot.stra2us.austindavid.com:8153`, HTTP, HMAC-signed) must keep
   working through every change. Anything touching `/q/` or `/kv/`
   request handling gets explicit smoke-test coverage.

6. **One variable per phase.** Code, dependencies, and network
   topology change in separate steps. If a change wants all three,
   it's three steps. Entanglement is what made the first v1.5
   attempt unrecoverable.

7. **Rollbacks go to a verified-working target — guesses don't
   count.** "Verified working" means a smoke-test pass at that
   version, or an image tag from a deploy that ran successfully.
   Picking arbitrary old version numbers without that evidence is
   *not* a rollback — it's a guess that adds new variables on top
   of the original failure, and is worse than no action.

8. **Pin direct deps; lock transitive ones.** `requirements.txt` is
   the human-readable list of direct deps, pinned with `==`.
   `requirements.lock.txt` (when it lands) captures the full
   transitive resolution against a known-good container. The two
   move together, never independently.

9. **Don't skip checkpoints.** A change is done when its checkpoint
   passes — which means the smoke test is green AND, for any change
   that touches human-facing UI, a deliberate end-to-end walk-through
   in a real browser. Automated tests can't see layout, shape, or
   "does this still feel right" — eyeballs do. "It probably works"
   and "I glanced at it" are not checkpoints.

See [`docs/fr_v15_incremental.md`](docs/fr_v15_incremental.md) for
the v1.5 rollout that made these rules concrete.

---

## Deploying

The deploy host runs **two independent stacks** on the same docker
host: `prod` (live traffic) and `staging` (validation before
prod). Code reaches the host via git; secrets reach it via
`tools/sync-secrets.sh`. See
[`docs/staging_environment.md`](docs/staging_environment.md) for
the architecture and rationale.

### One-time host bootstrap

```bash
# On dev — fill in tools/.deploy-config (see .deploy-config.example)
# Then push the host-bound .env files:
tools/sync-secrets.sh

# On host — clone both directories + create volume dirs:
./tools/bootstrap-host.sh
```

> ⚠️ **Bootstrap finishes with a mandatory step: rotate the `rescue`
> password.** See [The `rescue` user](#the-rescue-user) — the shipped
> default is intentionally unusable, and `rescue` maps to wildcard
> superuser, so a fresh host MUST have a strong rescue password set
> before it is reachable.

### Bringing up staging

```bash
# On host, in $STAGING_DIR:
tools/stage up
tools/stage wait-tunnel
tools/stage seed-users          # idempotent
tools/stage seed-smoke-device   # one-time, for the device-flow smoke
tools/stage smoke               # hostname + device-flow checks; all green
```

`tools/stage smoke` runs the public-surface checks **and** the
HMAC-signed device-flow smoke (`POST /q/`, `POST/GET /kv/` with
response-signature verification). Run just the latter with
`tools/stage smoke-device`.

### Promoting to prod

Tag a staging-verified commit, then promote:

```bash
# On dev:
git tag -a v1.X.Y <sha-verified-on-staging> -m "what changed"
git push origin v1.X.Y

# On host:
tools/stage promote v1.X.Y      # checks out the tag in $PROD_DIR,
                                # rebuilds, restarts, waits for the tunnel
tools/stage smoke-prod          # verify
```

`tools/stage promote` writes the running tag into `backend/VERSION`,
which the admin sidebar surfaces as a version badge (`GET
/api/admin/release`) — so "did my deploy land?" is answerable from the
browser.

### The `rescue` user

Stra2Us ships a `rescue` htpasswd entry (seeded by `bootstrap-host.sh`
from [`backend/admin.htpasswd.default`](backend/admin.htpasswd.default))
that maps to an implicit **wildcard superuser** ACL via `RESCUE_USERS`
in `backend/src/api/dependencies.py`. It's the break-glass account for
when OAuth is unavailable.

**The shipped default is a hash of a random password nobody knows — it
is intentionally *unusable* as a login** (publishing the hash is
therefore safe). That is fail-safe, not a convenience: you must set a
real password before the rescue path works.

**Rotate it to a strong random value on every fresh host, before
exposure:**

```bash
cd $PROD_DIR/backend
python3 create_admin.py rescue "$(openssl rand -base64 24)"   # save it
```

(or delete the `rescue` line entirely and rely solely on OAuth). There
is **no Basic-Auth brute-force lockout yet**, so a *weak* rescue
password is online-guessable and grants full compromise — use a long
random one. `is_rescue_on_default()` raises a startup warning and a
dashboard banner until the entry diverges from the shipped default.
Full rationale: the header of `admin.htpasswd.default` and
[`docs/security_audit_2026-05.md`](docs/security_audit_2026-05.md).

### Operator sign-in: OAuth, not htpasswd

The operator's primary admin path is **OAuth (Google) on the browser
hostname** (`stra2us.austindavid.com`). Their identity lives as
`admin_acls:<google-email>` in Redis, edited via the Admin Users page.
They do **not** have an htpasswd entry. `backend/admin.htpasswd` is
expected to contain only `rescue` (break-glass) and `smoke` (used by
`tools/smoke_test.sh`).

### Local development (no docker)

For running tests against a host-side backend (no docker), see
[`docs/local_dev.md`](docs/local_dev.md).

---

## API Reference

Full API documentation is in [`docs/api.md`](docs/api.md). Apps
describe their KV variables with a per-app YAML *catalog*
(`<app>.s2s.yaml`), consumed by the [reference CLI](tools/README.md);
schema in [`docs/catalog_spec.md`](docs/catalog_spec.md).

### Quick Reference

| Endpoint                              | Auth       | Description                                  |
|---------------------------------------|------------|----------------------------------------------|
| `GET /health`                         | None       | Liveness check                               |
| `POST /q/{topic}`                     | HMAC       | Publish to a queue                           |
| `GET /q/{topic}`                      | HMAC       | Consume from a queue (per-consumer cursor)   |
| `POST /kv/{key}`                      | HMAC       | Write a KV value (`X-Encrypted: 1` flags it) |
| `GET /kv/{key}`                       | HMAC       | Read a KV value (encrypted → signed ext-0x21)|
| `DELETE /kv/{key}`                    | HMAC       | Delete a KV value                            |
| `GET /app/{app}/{device}`             | OAuth      | Customer device-config page                  |
| `/oauth/google/{login,callback}`      | —          | Google OAuth flow                            |
| `GET /api/admin/release`              | Admin      | Running release tag (sidebar badge)          |
| `GET /api/admin/backup`               | Superuser  | Whole-instance dump                          |
| `GET /api/admin/backup/app/{app}`     | Superuser  | Per-app dump                                 |
| `POST /api/admin/restore`             | Superuser  | Restore a dump (`?force_overwrite=1`)        |
| `POST /api/admin/restore/app/{app}`   | Superuser  | Per-app restore (URL app is authoritative)   |
| `GET /api/admin/keys/backup`          | Superuser  | *Legacy* — client-credentials-only dump      |

Both `/q/` and `/kv/` accept `Content-Type: application/x-msgpack`
(default) or `Content-Type: text/plain` (server wraps the string in
MessagePack automatically).

---

## Clients

### C++ SDK (devices)

Zero-malloc C++ client for ESP32 / Particle Photon 2 / Arduino. All
methods return `int` (HTTP status code; check `result == 200`).

```cpp
IoTClient iot(wifiClient, "host", 8153, "client-id", "hex-secret");
int status = iot.publishQueue("device/status", "heartbeat");
```

The wire-format details every client must implement (HMAC signing,
response verification, msgpack value shapes, the encrypted-value
ext family) are in [**`docs/client_spec.md`**](docs/client_spec.md) —
required reading before writing another client.

### Python CLI (`stra2us`)

`tools/stra2us_cli` — a Python client (3.10+) for testing, scripting,
and catalog publishing. Self-installs via `pip install -e tools/`.
Verbs include `get` / `put` / `set` / `del` (KV), `show`, `catalog`
(list/lint/publish/fetch), and `synth-traffic` (synthetic device load
for warming up staging). Credentials come from `--server` /
`--client-id` / `--secret`, a `--profile` in `.stra2us`, or the
`STRA2US_*` env vars.

```sh
stra2us --server https://iot.stra2us.austindavid.com:8153 \
    --client-id <id> --secret <hex> get sensors/temp
```

See [**`tools/README.md`**](tools/README.md) for the full subcommand
reference.

---

## Backup & Restore

Whole-instance and per-app dump/restore of all load-bearing state —
clients + ACLs, admin users, KV (including catalogs + assets), queues,
and the device→app reverse index. Available from the admin dashboard
under **Backup / Restore**, or via the API (superuser-gated):

```bash
# Whole-instance dump (add ?include_logs=1 to include the activity log)
curl -H "Cookie: admin_session=..." \
    https://stra2us.austindavid.com/api/admin/backup -o dump.json

# Per-app dump
curl -H "Cookie: admin_session=..." \
    https://stra2us.austindavid.com/api/admin/backup/app/<app> -o app.json

# Restore (skip-existing by default; ?force_overwrite=1 to replace)
curl -X POST -H 'Content-Type: application/json' \
    https://stra2us.austindavid.com/api/admin/restore -d @dump.json
```

The dump is a versioned JSON envelope (base64-msgpack for binary
values); per-app restore is sandboxed to its app namespace as
defense-in-depth. Full schema:
[`docs/fr_backup_envelope_v1.md`](docs/fr_backup_envelope_v1.md). The
legacy `GET/POST /api/admin/keys/{backup,restore}` (client credentials
only) remain for scripts that target them.

> ⚠️ Dumps contain raw HMAC secrets and admin ACLs. Treat them like a
> password-manager export — never commit to version control. Responses
> carry `X-Stra2us-Sensitive: true` + `Cache-Control: no-store`.

---

## Security

Auth is HMAC-SHA256 (devices) and Google OAuth / htpasswd-rescue
(admins), with a Redis-backed prefix ACL model. The browser surface is
hardened with an enforcing CSP, output escaping, `Secure` +
`SameSite=Lax` session cookies, and a CSRF Origin guard on
state-changing admin requests. A 2026-05 security review and its
remediations (rescue-credential hardening, cookie/CSRF, the
encrypted-values nonce fix, CORS pinning) are recorded in
[**`docs/security_audit_2026-05.md`**](docs/security_audit_2026-05.md),
which also lists the affirmatively-sound surfaces and the open/
deferred items. Application traffic is not encrypted at the app layer
— confidentiality relies on HTTPS / the Cloudflare tunnel.

---

## Changelog

### 2026-05-30 — Security hardening pass

Four findings from a structured review, remediated: rescue-credential
documentation, admin cookie flags + CSRF Origin guard, a per-client
nonce for the encrypted-values cipher (closes a same-second
two-time-pad leak; server-only, no client changes), and CORS origin
pinning. Details + the "verified sound" list:
[`docs/security_audit_2026-05.md`](docs/security_audit_2026-05.md).

### 2026-05-14 — v1.8.1: Backup / restore (whole-instance + per-app)

Versioned dump/restore of all load-bearing state, whole-instance and
per-app, with an admin-UI download/restore surface and a documented
envelope format. Per-app restore is namespace-sandboxed.
[`docs/fr_backup_envelope_v1.md`](docs/fr_backup_envelope_v1.md).

### 2026-05-14 — v1.7.2: Device-flow tooling

`stra2us synth-traffic` synthetic-load CLI, and a beefier smoke test
that exercises the HMAC-signed device protocol end-to-end
(`tools/smoke_test_device.sh`, `tools/stage smoke-device`).

### 2026-05-13 — v1.7.1: Polish + plumbing

Generalized `widget: radio` to any enum-backed field; auto-write
`backend/VERSION` from `tools/stage`; gated the `/app/` landing form
behind OAuth; scoped admins can see Activity Logs for clients in their
ACL.

### 2026-05-13 — v1.7.0: Cycle boundary + version badge

Boundary marker wrapping the v1.6.x catalog-app-ui cycle. New:
running-release tag in the admin sidebar (`GET /api/admin/release`,
sourced from `backend/VERSION`). Architectural shifts since v1.6.0
(encrypted-field render simplification, catalog-as-contract, cache-bust
automation) are summarized in [`CHANGELOG.md`](CHANGELOG.md).

### 2026-05-06 — v1.5: OAuth, hostname-aware auth, staging environment

OAuth (Google) auth on the browser hostname; htpasswd retained as the
rescue path on the device hostname. New `tools/stage` helper wraps the
dev → staging → prod flow with smoke gates at every checkpoint.
[`docs/fr_v15_incremental.md`](docs/fr_v15_incremental.md).

### 2026-04-13 — Admin UI cleanup + Activity Log overhaul

UI hardening (CSS fixes, XSS-safe rendering), modal close-button
null-guard. Activity log migrated from Redis LIST → STREAM with 24h
retention + 150k count cap. Per-client filter chips above the log
table.

For the full per-release detail, see [`CHANGELOG.md`](CHANGELOG.md),
the `docs/` FR write-ups, and `git log v<X.Y.Z>`.
