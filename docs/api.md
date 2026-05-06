# Stra2Us — API Reference

All device endpoints require HMAC-SHA256 request signing. Admin endpoints require HTTP Basic Auth (or a valid session cookie issued after login).

---

## Authentication

### Device Requests (HMAC)

Every device request must include the following headers:

| Header | Value |
|---|---|
| `X-Client-ID` | The registered client ID |
| `X-Timestamp` | Current Unix epoch time (seconds) |
| `X-Signature` | HMAC-SHA256 hex digest (see below) |
| `Content-Type` | `application/x-msgpack` or `text/plain` |

**Signature calculation:**

```
HMAC-SHA256(secret_bytes, URI + body_bytes + timestamp_string)
```

- `URI` is the path including query string (e.g. `/q/sensor?ttl=3600`)
- Requests with no body use an empty byte string for the body component
- Timestamp must be within ±300 seconds of the server clock

### Admin Requests

Admin endpoints (`/api/admin/*` and `/admin/*`) require HTTP Basic Auth on the first request. The server issues a session cookie (`admin_session`) that is accepted on subsequent requests.

Beyond authentication, admin routes enforce a per-caller ACL — see
[acl_model.md](acl_model.md) for the schema and matching rules. Most
admin routes below note their required access level; the short version:

- Peek/mutate routes (`peek/kv/…`, `peek/q/…`, `POST/DELETE /kv/…`,
  `DELETE /q/…`) require read or write access on the specific
  resource.
- Listing routes (`/stats`, `/logs`, `/kv_scan`) return only the
  entries the caller can read.
- Credential-management routes (`/keys/…`, `/admin_users/…`,
  `/keys/backup`, `/keys/restore`) require a superuser ACL (`*:rw`).

---

## Unauthenticated Endpoints

### `GET /health`

Liveness check. No authentication required. Safe to call before SNTP sync.

**Response `200 OK`:**
```json
{"status": "ok"}
```

---

## Device Endpoints

### `POST /q/{topic}` — Publish to Queue

Publish a message to a named topic queue.

**Query Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ttl` | int | 3600 | Message TTL in seconds (max 604800 = 7 days) |

**Content Types:**

- `application/x-msgpack` — Body must be a valid MessagePack-encoded value. Stored as-is.
- `text/plain` — Body is a raw UTF-8 string. Server wraps it in MessagePack before storage. Subscribers receive a properly-wrapped msgpack string.

The server stores the authenticated `X-Client-ID` alongside every message in the stream, enabling attribution metadata in consumer responses (see below).

**Response `200 OK`:**
```json
{"status": "ok"}
```

**Error Responses:**
- `400` — Invalid MessagePack payload or non-UTF-8 text body
- `401` — Missing or invalid HMAC signature / timestamp out of window
- `403` — Client ACL does not permit writes to this topic

---

### `GET /q/{topic}` — Consume from Queue

Read the next available message for this client. Each client maintains its own independent read cursor, so multiple clients can consume from the same topic independently.

**Query Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `envelope` | bool | `false` | When `true`, wraps the payload in a metadata envelope |

**Response `200 OK` (default — raw payload):**  
Body is raw MessagePack bytes (`Content-Type: application/x-msgpack`).

**Response `200 OK` (`?envelope=true`):**  
Body is a MessagePack-encoded dict:
```json
{
  "data": "heartbeat",
  "client_id": "bb32",
  "received_at": 1712412399
}
```
- `data` — The payload exactly as published (string, map, or other msgpack value).
- `client_id` — The authenticated publisher's `X-Client-ID`; cannot be forged by the client.
- `received_at` — Unix seconds derived from the Redis Stream entry ID — authoritative server-side receive time, independent of any client clock.

> **Backward compatibility:** The default response format (raw payload bytes) is unchanged. Existing consumers are not affected. Opt in per-request with `?envelope=true`.

**Response `204 No Content`:**  
Queue is empty or all available messages have been consumed.

**Error Responses:**
- `401` — Authentication failure
- `403` — Client ACL does not permit reads from this topic

---

### `POST /kv/{key}` — Write KV Value

Write a persistent key-value entry.

**Content Types:** Same as `/q/{topic}` — accepts `application/x-msgpack` or `text/plain`.

**Response `200 OK`:**
```json
{"status": "ok"}
```

---

### `DELETE /kv/{key}` — Delete KV Value

Idempotent — succeeds whether or not the key existed. The HMAC client
needs write access on `kv/<key>`. Mirrors `DELETE /api/admin/kv/{key}`
on the admin route, but reachable from device credentials so an HMAC
caller (e.g. a publish/retract tool) doesn't need to hold an admin
session to clear keys it wrote.

**Response `200 OK`:** signed envelope wrapping `{"status": "ok"}`.

---

### `GET /kv/{key}` — Read KV Value

Read a persistent key-value entry.

**Response `200 OK`:**  
Body is raw MessagePack bytes.

**Response `200 OK` (key not found):**
```json
{"status": "not_found"}
```

> Note: Returns `200` with a `not_found` body rather than `404` to simplify embedded client logic.

---

## Admin Endpoints

All admin endpoints require Basic Auth or a valid `admin_session` cookie.

### `GET /api/admin/whoami`

Returns the logged-in admin's identity and ACL. Intended for UI code
that wants to hide entries the caller can't use — the routes
themselves still enforce.

**Response `200 OK`:**
```json
{
  "username": "scoped",
  "acl": {"permissions": [{"prefix": "critterchron", "access": "rw"}]},
  "is_superuser": false
}
```

`is_superuser` is `true` iff the ACL contains a `{"prefix": "*", "access": "rw"}` entry.

---

### `GET /api/admin/stats`

Returns queues and KV entries the **caller** can read. Entries outside
the caller's ACL are silently omitted.

---

### `GET /api/admin/keys`

> Requires superuser ACL (`*:rw`).

Lists all registered client IDs and their ACLs. Does **not** return secrets.

---

### `POST /api/admin/keys`

> Requires superuser ACL (`*:rw`).

Register a new client and generate a secret. New clients start with
**no permissions** — set the ACL via `PUT /api/admin/keys/{id}/acl`
before the device will be allowed to do anything.

**Request body (JSON):**
```json
{
  "client_id": "ESP32-Weather-01"
}
```

**Response:**
```json
{
  "client_id": "ESP32-Weather-01",
  "secret": "aabbcc...(64 hex chars)...",
  "acl": {"permissions": []}
}
```

> ⚠️ The secret is only returned once. Store it immediately.

---

### `PUT /api/admin/keys/{client_id}/acl`

> Requires superuser ACL (`*:rw`).

Replace a client's ACL. See [acl_model.md](acl_model.md) for the schema.

**Request body (JSON):**
```json
{"permissions": [{"prefix": "critterchron", "access": "rw"}]}
```

---

### `DELETE /api/admin/keys/{client_id}`

> Requires superuser ACL (`*:rw`).

Revoke a client. The device will immediately receive `401` on all future requests.

---

### `GET /api/admin/keys/backup`

> Requires superuser ACL (`*:rw`). The backup is functionally a total
> read of every credential in the system.

Export all client credentials (IDs, secrets, ACLs) as a JSON file.

**Response:** JSON file download (`Content-Disposition: attachment`).

```json
{
  "exported_at": 1712345678,
  "clients": [
    {
      "client_id": "ESP32-Weather-01",
      "secret": "aabbcc...",
      "acl": {"permissions": [{"prefix": "*", "access": "rw"}]}
    }
  ]
}
```

> ⚠️ **Security:** This response contains raw HMAC secrets. Treat the file like a password manager export. Never commit to version control.

---

### `POST /api/admin/keys/restore`

> Requires superuser ACL (`*:rw`).

Restore credentials from a backup JSON body.

**Query Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `force` | `false` | If `true`, overwrites existing clients. Default skips them. |

**Request body:** A backup JSON blob (same format as the backup response above).

**Response:**
```json
{
  "restored": ["new-device-1"],
  "skipped": ["existing-device"],
  "overwritten": []
}
```

---

### `GET /api/admin/peek/q/{topic}`

Peek at the oldest message in a queue without consuming it.

---

### `GET /api/admin/stream/q/{topic}`

Read-only tail of a topic's stream — does **not** advance any
subscriber cursor, so safe to call on a live queue. Uses `XREVRANGE`
under the hood; expired messages (past their `exp` field) are filtered
server-side.

**Query Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `limit` | 50 | Max number of recent messages to return |
| `client_id` | *(none)* | Repeatable; filter to messages whose stored publisher matches one of the listed client IDs |

**Response `200 OK`:**
```json
[
  {"id": "1712345678901-0", "received_at": 1712345678, "client_id": "ricky", "data": {"hb": 60}}
]
```

Requires queue read access on `<topic>`.

---

### `GET /api/admin/peek/kv/{key}`

Peek at the value of a KV key, decoded from MessagePack.

---

### `GET /api/admin/kv_scan`

List KV keys matching a literal prefix, filtered to those the caller
can read. Intended for UI discovery — e.g. `prefix=_catalog/` to list
published catalogs.

**Query Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `prefix` | *(required)* | Literal prefix to match (no globbing — `*` is appended server-side) |
| `limit` | 500 | Cap on returned items after ACL filtering |

**Response `200 OK`:**
```json
{"prefix": "_catalog/", "count": 2, "truncated": false, "items": [{"key": "_catalog/critterchron", "bytes": 5400}]}
```

`truncated` reflects whether the caller's *visible* result set was
capped, not the raw redis scan size.

---

### `GET /api/admin/catalog/{app}/devices`

List HMAC clients that can read or write under `<app>` — i.e. the
"devices" the catalog UI's Devices tab shows. A client qualifies if
its ACL has a permission whose prefix is `*`, exactly `<app>`, or
starts with `<app>/`. Wildcard clients show up under every app's list,
which is intentional. Device IDs are returned in client-ID form;
they're also the convention for the second path segment in
`<app>/<device>/<key>` overrides, which is what the per-device
effective-value view depends on.

**Response `200 OK`:**
```json
{"app": "critterchron", "devices": ["nova", "ricky"]}
```

Requires read access on `kv/<app>`.

---

### `POST /api/admin/kv/{key}`

Create or modify a KV value from the admin UI / CLI. The body is a
JSON envelope with a `value` string; the server tries `json.loads()`
on `value` first (so `"60"` round-trips as the integer 60, `"true"`
as boolean true, etc.) and falls back to the raw string on parse
error. Whatever it ends up as is then re-encoded as MessagePack and
written to `kv:<key>`, matching the wire shape device clients see.

**Request body:**
```json
{"value": "60"}
```

**Response `200 OK`:**
```json
{"status": "ok"}
```

Requires write access on `kv/<key>`.

---

### `DELETE /api/admin/q/{topic}`

Delete an entire queue and all its messages.

---

### `DELETE /api/admin/kv/{key}`

Delete a KV entry.

---

### `GET /api/admin/logs`

Returns recent activity log entries the **caller** can read, newest
first. Entries for resources outside the caller's ACL are silently
omitted; firmware fetches and other non-app-scoped actions pass
through regardless. Logs are stored in a Redis Stream with dual
retention: entries older than 24 hours are trimmed, with a safety cap
of 150,000 entries (~11 MB) to bound storage from unusually chatty
clients.

**Query Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `limit` | 200 | Max number of log entries to return |
| `client_id` | *(none)* | Filter by one or more client IDs. Repeat the parameter to select multiple clients (e.g. `?client_id=bb32&client_id=coaticlock`). When omitted, all clients are returned. |

**Response `200 OK`:**
```json
[
  {
    "timestamp": 1712345678,
    "client_id": "bb32",
    "action": "POST /q/coaticlock",
    "status": "Success (200)"
  }
]
```

For wildcard-ACL admins (the common ops case) the server skips the
internal over-fetch multiplier — request/response cost scales with
`limit`, not `limit*10`.

---

### `GET /api/admin/perf_log`

Tail the `system:perf_log` stream — one entry per request whose total
wall time exceeded `STRA2US_PERF_LOG_THRESHOLD_MS` (default 100ms).
Outlier-only by design; healthy traffic produces no entries. Capped
at 5,000 entries with a 24-hour age trim.

**Query Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `limit` | 200 | Max number of entries to return |
| `path_prefix` | *(none)* | Filter to paths starting with this string (e.g. `/api/admin/kv_scan`) |
| `min_ms` | 0 | Filter to entries with `total_ms` at or above this floor |

**Response `200 OK`:**
```json
[
  {
    "timestamp": 1712345678,
    "method": "GET",
    "path": "/api/admin/logs",
    "total_ms": 26.64,
    "status": 200,
    "client_id": "austin",
    "phases": {"xrevrange": 17.86, "filter_loop": 1.58}
  }
]
```

`phases` is present only when the route is annotated with
`PerfPhases`; check [core/perf_log.py](../backend/src/core/perf_log.py)
for the helper. Requires superuser ACL — perf data isn't a
per-resource concern, but it surfaces internal paths an ops view
should see and app-scoped admins shouldn't need.

---

### Admin user management

> All routes in this section require superuser ACL (`*:rw`).

Admin accounts live in `admin.htpasswd` (authentication) plus a Redis
row (authorization). The UI reads the union and edits the ACL; the
htpasswd side is managed out-of-band via CLI.

#### `GET /api/admin/admin_users`

Returns every admin in htpasswd with its ACL record if one exists.
Users without a Redis row are surfaced as `provisioned: false` so the
UI can flag "needs provisioning".

**Response `200 OK`:**
```json
[
  {"username": "alice", "acl": {"permissions": [{"prefix": "*", "access": "rw"}]}, "provisioned": true},
  {"username": "bob",   "acl": {"permissions": []}, "provisioned": false}
]
```

#### `PUT /api/admin/admin_users/{username}/acl`

Replace an admin user's ACL. Returns `404` if the username isn't in
htpasswd — the UI can't grant permissions to an account that can't
authenticate.

**Request body:** same shape as `PUT /api/admin/keys/{id}/acl`.

---

## Redis Key Schema

| Pattern | Type | Description |
|---|---|---|
| `client:{id}:secret` | String | Client HMAC secret (hex) |
| `client:{id}:acl` | String (JSON) | Client ACL — `{"permissions": [...]}` |
| `admin_acls:{user}` | String (JSON) | Admin user ACL — same shape as client ACL |
| `q:{topic}` | Stream | Message queue |
| `kv:{key}` | String | Persistent KV value (raw msgpack) |
| `cursor:{client_id}:q:{topic}` | String | Per-client read cursor for a queue |
| `system:activity_log` | Stream | Activity log — 24h retention with 150K entry safety cap |
