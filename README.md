# Stra2Us — IoT Telemetry Service

Stra2Us is a high-performance, stateless IoT messaging and configuration relay designed for resource-constrained devices (ESP32, Particle Photon 2, and similar). It features an async Python/Redis backend, a zero-malloc C++ client SDK, and a browser-based admin dashboard.

## Design Architecture

- **Stateless Backend:** Zero in-process state — everything lives in Redis. Scales horizontally out of the box.
- **HMAC-SHA256 Signatures:** Devices sign requests with a shared secret + Unix timestamp. The server enforces a ±300 second replay window.
- **Broadcast Streams:** Queues use Redis Streams (`XADD`/`XREAD`). Each subscriber maintains its own cursor, so multiple devices can read independently without consuming each other's messages.
- **Micro-serialization:** Payloads use [MessagePack](https://msgpack.org/) by default, cutting wire overhead vs. JSON. Plain-text (`text/plain`) is also accepted and automatically wrapped server-side.

## Technical Stack
- **Backend:** Python 3.9+, FastAPI, Uvicorn, Redis Streams.
- **Client SDK:** C++ (Arduino/ESP-IDF), zero-malloc, mbedTLS HMAC.
- **Dashboard:** Vanilla HTML/JS, no build step, protected by Basic Auth + session cookies.

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
   working through every change. Anything touching `/q/`, `/kv/`, or
   `/firmware/` request handling gets explicit smoke-test coverage.

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

## Installation

### 1. Requirements
- Python 3.9 or higher
- Redis Server (`sudo apt install redis-server` on Debian/Ubuntu)

### 2. Start the Backend

```bash
cd backend

# Local development
./start.sh

# For external network access (e.g. Raspberry Pi)
./start.sh --host 0.0.0.0

# Custom port
./start.sh --host 0.0.0.0 --port 9000
```

> The script checks that Redis is running before starting. If it's not, it will print the exact command to start it.

### 3. Create the Admin User

```bash
cd backend
source venv/bin/activate
python create_admin.py your_username your_password
```

Then open `http://<your-ip>:8000/admin` in a browser.

### 4. Running the live test suite locally

See [`docs/staging.md`](docs/staging.md) for the host-side bring-up
(skipping the docker-assumed paths), provisioning a test client
straight in Redis, and running `tools/tests/test_*_live.py` against it.

### 5. Raspberry Pi Deployment Tips
- **Auto-start Redis:** `sudo systemctl enable redis-server`
- **Firewall:** `sudo ufw allow 8000`
- **Run in background:** `nohup ./start.sh --host 0.0.0.0 &`
- **Check connectivity:** `curl http://<rpi-ip>:8000/health`

---

## API Reference

Full API documentation is in [`docs/api.md`](docs/api.md).

Apps built on Stra2us can describe their KV variables with a
per-app YAML *catalog* (`<app>.s2s.yaml`), consumed by the
[reference CLI in `tools/`](tools/README.md). See
[`docs/catalog_spec.md`](docs/catalog_spec.md) for the schema.

### Quick Reference

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | None | Liveness check — safe to call before SNTP sync |
| `POST /q/{topic}` | HMAC | Publish a message to a queue |
| `GET /q/{topic}` | HMAC | Consume the next message from a queue |
| `POST /kv/{key}` | HMAC | Write a persistent KV value |
| `GET /kv/{key}` | HMAC | Read a persistent KV value |
| `GET /api/admin/keys/backup` | Admin | Download all client credentials as JSON |
| `POST /api/admin/keys/restore` | Admin | Restore credentials from a backup file |
| `POST /api/admin/kv/{key}` | Admin | Create or modify a persistent KV value (Frontend Endpoint) |

Both `/q/` and `/kv/` accept `Content-Type: application/x-msgpack` (default) or `Content-Type: text/plain` (server wraps the string in MessagePack automatically).

---

## C++ Client SDK (v2.0.0)

> **Breaking change from v1.x:** All methods now return `int` (HTTP status code) instead of `bool`. Check `result == 200` instead of `if (result)`.

### Include

```cpp
#include "IoTClient.h"

WiFiClient wifiClient;
IoTClient iotClient(wifiClient, "192.168.1.100", 8000, "my-device", "hex-secret");
iotClient.setTimeFunction([]() { return (uint32_t)time(nullptr); });
```

### Publish (MessagePack)

```cpp
uint8_t buf[64];
// ... pack data into buf using cmp ...
int status = iotClient.publishQueue("sensors/temp", buf, sizeof(buf));
if (status == 200) Serial.println("OK");
```

### Publish (Raw String — no MessagePack library needed)

```cpp
// Server wraps it in MessagePack automatically (FR-1 + FR-4)
int status = iotClient.publishQueue("device/status", "heartbeat");
if (status == 200) Serial.println("Heartbeat sent");
```

### Consume

```cpp
uint8_t rxBuf[256];
size_t rxLen = 0;
int status = iotClient.consumeQueue("commands", rxBuf, sizeof(rxBuf), &rxLen);
if (status == 200) {
    // rxBuf contains a valid MessagePack message
} else if (status == 204) {
    // Queue is empty — nothing to do
} else if (status == 401) {
    Serial.println("Auth failure — check secret");
} else if (status == -1) {
    Serial.println("TCP connection failed");
}
```

### KV Read/Write

```cpp
int status = iotClient.writeKV("config", buf, len);
int status = iotClient.readKV("config", rxBuf, sizeof(rxBuf), &rxLen);
```

---

## CLI Test Client

```bash
cd backend
source venv/bin/activate

# Publish (JSON or plain string)
python test_client.py --client-id xxx --secret xxx publish sensor_data '{"temp": 22.4}'

# Follow a queue (polls until Ctrl-C)
python test_client.py --client-id xxx --secret xxx follow sensor_data --delay 1.0

# KV read/write
python test_client.py --client-id xxx --secret xxx set device-config '{"interval": 60}'
python test_client.py --client-id xxx --secret xxx get device-config

# Point at a remote server
python test_client.py --url http://192.168.1.50:8000 --client-id xxx --secret xxx publish heartbeat ok
```

---

## Backup & Restore

Client credentials (IDs, HMAC secrets, ACLs) can be exported and re-imported via the Admin Dashboard under **Backup / Restore**, or directly via the API:

```bash
# Download backup
curl -u admin:password http://localhost:8000/api/admin/keys/backup -o backup.json

# Restore (skips existing clients)
curl -u admin:password -X POST http://localhost:8000/api/admin/keys/restore \
  -H 'Content-Type: application/json' -d @backup.json

# Restore and overwrite existing clients
curl -u admin:password -X POST "http://localhost:8000/api/admin/keys/restore?force=true" \
  -H 'Content-Type: application/json' -d @backup.json
```

> ⚠️ Backup files contain raw HMAC secrets. Treat them like a password manager export — never commit to version control.

---

## Changelog

### 2026-04-13 — Admin UI cleanup + Activity Log overhaul

**UI hardening & cleanup**

- Fixed missing CSS `@keyframes pulse` — the Topic Monitor "Live" indicator now animates as intended.
- Added missing `.text-muted` CSS class — empty-state messages ("No active queues", etc.) now render in muted gray instead of bright white.
- Removed unused `.logo` CSS rule (dead code; sidebar uses `.sidebar-logo`).
- Extracted ~25 inline `style=` attributes from `index.html` into named CSS classes (`form-label`, `form-hint`, `modal-actions`, `monitor-controls`, `card-toolbar`, `btn-ghost`, `filter-chip`, etc.) for maintainability.
- Added `escapeHtml()` sanitization to all dynamic content injected via JS template literals — topic names, client IDs, KV keys, log fields, and monitor data. Prevents XSS if any of these values contain HTML special characters.
- Fixed null-guard bug in the modal close-button handler (`app.js`) that could throw a TypeError if `.closest('.modal')` returned null.

**Activity Log: storage + retention**

- Migrated `system:activity_log` from a Redis LIST (capped at 1,000 entries with no time awareness) to a Redis STREAM with dual-constraint retention:
  - **Time-based:** entries older than 24 hours are trimmed via `XTRIM MINID`.
  - **Count-based safety cap:** `MAXLEN ~ 150000` (~11 MB) prevents unbounded growth from unusually chatty clients.
- Rationale: the previous 1,000-entry cap provided only minutes of history at moderate traffic. The new policy retains a full 24 hours for normal workloads while bounding worst-case storage.
- **Migration note:** `system:activity_log` changed from LIST to STREAM type. Before deploying, delete the old key: `docker exec stra2us-iot redis-cli DEL system:activity_log`. Client credentials and queue data are not affected.

**Activity Log: per-client filtering**

- Added `client_id` query parameter to `GET /api/admin/logs` — accepts one or more client IDs for server-side filtering. Default (omitted) returns all clients.
- Default limit increased from 50 to 200 to take advantage of deeper retention.
- Admin UI now shows toggle-able filter chips above the log table, one per registered client. Default is "show all"; clicking a chip filters to that client. Multiple chips can be active simultaneously.
- Client chip list refreshes each time the Activity Logs tab is opened (picks up newly registered clients).

**Validation performed:**

- Pulse animation confirmed working on Topic Monitor live indicator.
- All modals (Peek, KV Editor, ACL Editor) open and close correctly after close-button handler fix.
- Dashboard, Key Management, ACL editing, Topic Monitor, Backup/Restore all verified functional after CSS refactor — no visual regressions.
- Log filter chips render for all registered clients; toggling filters correctly; deselecting all returns to full view; 5-second auto-refresh respects active filter.
- Peek, Delete, Edit operations on queues and KV pairs confirmed working after `routes_admin.py` edits.
- Backup download confirmed producing valid JSON after file edits.
