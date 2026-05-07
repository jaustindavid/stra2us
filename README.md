# Stra2Us — IoT Telemetry Service

Stra2Us is a high-performance, stateless IoT messaging and configuration
relay designed for resource-constrained devices (ESP32, Particle Photon
2, and similar). It features an async Python/Redis backend, a
zero-malloc C++ client SDK, and a browser-based admin dashboard.

> **License:** [PolyForm Noncommercial 1.0.0](LICENSE) — source-available
> for noncommercial use; reselling or commercial use requires explicit
> permission. Personal projects, research, education, hobby use → fine.

> **Status:** Production-shipped (v1.5.x). The deploy pipeline is dev →
> staging → prod via `tools/stage`; see [Deploying](#deploying) below.
> The discipline that produced this iteration is captured in
> [Rules of Operation](#rules-of-operation) and
> [`docs/fr_v15_incremental.md`](docs/fr_v15_incremental.md).

## Design Architecture

- **Stateless Backend:** Zero in-process state — everything lives in
  Redis. Scales horizontally out of the box.
- **HMAC-SHA256 Signatures:** Devices sign requests with a shared
  secret + Unix timestamp. The server enforces a ±300 second replay
  window.
- **Broadcast Streams:** Queues use Redis Streams (`XADD`/`XREAD`).
  Each subscriber maintains its own cursor, so multiple devices can
  read independently without consuming each other's messages.
- **Micro-serialization:** Payloads use [MessagePack](https://msgpack.org/)
  by default, cutting wire overhead vs. JSON. Plain-text
  (`text/plain`) is also accepted and automatically wrapped
  server-side.

## Technical Stack

- **Backend:** Python 3.9+, FastAPI, Uvicorn, Redis Streams.
- **Client SDK:** C++ (Arduino/ESP-IDF), zero-malloc, mbedTLS HMAC.
- **Dashboard:** Vanilla HTML/JS, no build step, protected by Basic
  Auth + session cookies.

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
   of the original failure, and is worse than no action. The first
   v1.5 attempt's "uvicorn rolled back 18 months" was this kind of
   guess.

8. **Pin direct deps; lock transitive ones.** `requirements.txt` is
   the human-readable list of direct deps, pinned with `==`.
   `requirements.lock.txt` (when it lands) captures the full
   transitive resolution against a known-good container. The two
   move together, never independently.

9. **Don't skip checkpoints.** A change is done when its checkpoint
   passes — which means the smoke test is green AND, for any change
   that touches human-facing UI, a deliberate end-to-end walk-through
   in a real browser (sign in, navigate to the affected pages,
   exercise the changed flow). Automated tests can't see layout,
   shape, or "does this still feel right" — eyeballs do. "It probably
   works" and "I glanced at it" are not checkpoints.

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

### Bringing up staging

```bash
# On host, in $STAGING_DIR:
tools/stage up
tools/stage wait-tunnel
tools/stage seed-users      # idempotent
tools/stage smoke           # 9/9 expected once a device is heartbeating
```

### Promoting to prod

Tag a staging-verified commit, then re-point prod's checkout at
the tag:

```bash
# On dev:
git tag -a v1.X.Y <sha-verified-on-staging> -m "what changed"
git push origin v1.X.Y

# On host, in $PROD_DIR:
git fetch --tags
git checkout -B deploy v1.X.Y
docker compose build stra2us-iot
docker compose up -d
( set -a && source .env && tools/smoke_test.sh )    # 9/9 expected
```

A `tools/stage promote <tag>` wrapper for the prod side is on the
TODO list.

### The `rescue` user

Stra2Us ships with a `rescue` htpasswd entry provisioned by
`bootstrap-host.sh` from `backend/admin.htpasswd.default`. The
default password is **intentionally undocumented** — it's a
placeholder so a fresh-bootstrap host has *some* working htpasswd
login while the operator gets oriented, and the soft warning (server
log) plus UI banner fire until it's overridden.

**Override on every fresh installation.** From the host:

```bash
cd $PROD_DIR/backend
python3 create_admin.py rescue '<your-chosen-password>'
docker compose --env-file ../.env -f ../docker-compose.yaml \
    -p stra2us-prod restart stra2us-iot
```

(Same dance in `$STAGING_DIR` for staging, finishing with
`tools/stage deploy` to rebuild + restart.)

The `rescue` user has implicit wildcard ACL via the `RESCUE_USERS`
list in `backend/src/api/dependencies.py`, so it works as a true
break-glass account regardless of Redis state — even on a fresh
bootstrap before `tools/stage seed-users` has run.

### Operator sign-in: OAuth, not htpasswd

Post-v1.5, the operator's primary admin path is **OAuth (Google) on
the browser hostname** (`stra2us.austindavid.com`). Their identity
lives as `admin_acls:<google-email>` in Redis, edited via the Admin
Users page. They do **not** have an htpasswd entry.

`backend/admin.htpasswd` is expected to contain only:
- `rescue` — break-glass via the device hostname's Basic Auth path
  (RESCUE_USERS-covered, always wildcard).
- `smoke` — used by `tools/smoke_test.sh` for the activity-log
  heartbeat check; provisioned via `tools/stage seed-users`.

If you find operator-named entries (e.g. an old username you used to
type into the Basic Auth dialog), they're an artifact of pre-v1.5
setup and can be removed: delete the htpasswd line, then delete the
corresponding `admin_acls:<name>` Redis row (visible in the Admin
Users page as `acl-only` source post-Phase-5; click Delete).

**Footgun worth knowing:** the "is on default" check compares the
live htpasswd's `rescue` line to `admin.htpasswd.default`
byte-for-byte. If you ever change rescue's password to something
else and then deliberately re-set it to the documented default via
`create_admin.py rescue '<default-pass>'`, a fresh salt is generated
and the lines diverge — the banner *stays silent* even though the
password is back to the default plaintext. This is by design: we
treat "operator ran `create_admin.py`" as "operator made an active
choice." If you want the warning to fire again, do a literal line
copy from `admin.htpasswd.default` into `admin.htpasswd` instead.

### Local development (no docker)

For running tests against a host-side backend (no docker), see
[`docs/local_dev.md`](docs/local_dev.md) — covers the bring-up
dance for `tools/tests/test_*_live.py`.

---

## API Reference

Full API documentation is in [`docs/api.md`](docs/api.md).

Apps built on Stra2us can describe their KV variables with a per-app
YAML *catalog* (`<app>.s2s.yaml`), consumed by the [reference CLI in
`tools/`](tools/README.md). See
[`docs/catalog_spec.md`](docs/catalog_spec.md) for the schema.

### Quick Reference

| Endpoint                       | Auth  | Description                |
|--------------------------------|-------|----------------------------|
| `GET /health`                  | None  | Liveness check             |
| `POST /q/{topic}`              | HMAC  | Publish to a queue         |
| `GET /q/{topic}`               | HMAC  | Consume from a queue       |
| `POST /kv/{key}`               | HMAC  | Write a persistent KV      |
| `GET /kv/{key}`                | HMAC  | Read a persistent KV       |
| `GET /api/admin/keys/backup`   | Admin | Download credentials JSON  |
| `POST /api/admin/keys/restore` | Admin | Restore from backup file   |
| `POST /api/admin/kv/{key}`     | Admin | Create/modify a KV (UI)    |

Both `/q/` and `/kv/` accept `Content-Type: application/x-msgpack`
(default) or `Content-Type: text/plain` (server wraps the string in
MessagePack automatically).

---

## Clients

### C++ SDK (devices)

Zero-malloc C++ client for ESP32 / Particle Photon 2 / Arduino. All
methods return `int` (HTTP status code; check `result == 200`). One-line
publish for raw strings; the server wraps them in MessagePack
automatically.

```cpp
IoTClient iot(wifiClient, "host", 8153, "client-id", "hex-secret");
int status = iot.publishQueue("device/status", "heartbeat");
```

Full API + the wire-format details every client must implement (HMAC
signing, response verification, msgpack value shapes, encrypted-value
ext family, etc.) are in
[**`docs/client_spec.md`**](docs/client_spec.md) — required reading
before writing a fourth client.

### Python CLI (`stra2us`)

`tools/stra2us_cli` — a Python client for testing, scripting, and
catalog publishing. Lives at `tools/` and self-installs via
`pip install -e tools/`. Supports publish/consume, KV read/write,
catalog publish, and ad-hoc HMAC-signed requests against any stra2us
instance.

```sh
stra2us --url https://stra2us.austindavid.com --client-id <id> \
    --secret <hex> publish sensors/temp '{"c": 22.4}'
```

See [**`tools/README.md`**](tools/README.md) for full subcommand
reference and examples.

---

## Backup & Restore

Client credentials (IDs, HMAC secrets, ACLs) can be exported and
re-imported via the Admin Dashboard under **Backup / Restore**, or
directly via the API:

```bash
# Download backup
curl -u admin:password http://localhost:8000/api/admin/keys/backup \
    -o backup.json

# Restore (skips existing clients)
curl -u admin:password -X POST \
    http://localhost:8000/api/admin/keys/restore \
    -H 'Content-Type: application/json' -d @backup.json

# Restore and overwrite existing clients
curl -u admin:password -X POST \
    "http://localhost:8000/api/admin/keys/restore?force=true" \
    -H 'Content-Type: application/json' -d @backup.json
```

> ⚠️ Backup files contain raw HMAC secrets. Treat them like a
> password manager export — never commit to version control.

---

## Changelog

### 2026-05-06 — v1.5: OAuth, hostname-aware auth, staging environment

OAuth (Google) auth on the browser hostname; htpasswd retained as
the rescue path on the device hostname. New `tools/stage` helper
wraps the dev → staging → prod flow with smoke gates at every
checkpoint. Bootstrap-default `rescue` user with soft warning + UI
banner until the operator overrides. Implementation history,
phase-by-phase rationale, and the rules of operation that produced
this iteration: [`docs/fr_v15_incremental.md`](docs/fr_v15_incremental.md).

### 2026-04-13 — Admin UI cleanup + Activity Log overhaul

UI hardening (CSS fixes, XSS-safe rendering of dynamic content),
modal close-button null-guard. Activity log migrated from Redis
LIST → STREAM with 24h time-based retention + 150k count-based
safety cap. Per-client filter chips above the log table.

For older entries, `git log --oneline` and the FR docs in `docs/`.
