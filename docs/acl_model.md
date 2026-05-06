# ACL model

How stra2us decides whether a request is allowed. Applies to both
device (HMAC) requests and admin (cookie/Basic) requests — same schema,
same matcher, different storage.

See also: [admin_auth_architecture.md](admin_auth_architecture.md) for
how admin identity is established before an ACL is consulted.

---

## Schema

An ACL record is a list of permissions:

```json
{"permissions": [{"prefix": "critterchron", "access": "rw"},
                 {"prefix": "_catalog/critterchron", "access": "r"}]}
```

- `prefix` — a path prefix. `*` matches everything.
- `access` — `"r"` (read) or `"rw"` (read + write).

A request is allowed if **any** permission entry covers the requested
resource at the required access level. No entries = deny-all.

### Matching rules

A prefix `P` covers a resource path `R` when:

- `P == "*"` (wildcard), **or**
- `R == P` (exact match), **or**
- `R.startswith(P + "/")` (prefix on a path segment boundary — so
  `critterchron` covers `critterchron/nova/heartbeep` but not
  `critterchronics`).

Resource paths are the key or topic with the `kv/` / `q/` type
stripped. Permissions don't distinguish reads of queues from reads of
KV — an entry either covers that namespace or it doesn't.

### `_catalog/<app>` alias

A permission that covers `<app>` also covers `_catalog/<app>` and
everything beneath it. The catalog stash lives in the `kv` namespace
under `_catalog/<app>`, but it's part of the same app — an operator
with `critterchron:rw` can publish to `_catalog/critterchron` without
a separate grant. See [catalog_spec.md](catalog_spec.md) for the stash
itself.

Wildcard (`*`) covers every `_catalog/*` entry for free, as expected.

---

## Two subjects

Both use the schema above.

### HMAC clients — `client:<id>:acl`

Devices authenticate with HMAC-SHA256 (see [api.md](api.md)). Their
ACL is stored at Redis key `client:<id>:acl`. Managed via
`POST/PUT/DELETE /api/admin/keys/...`.

### Admin users — `admin_acls:<user>`

Admin users authenticate via htpasswd + session cookie. Their ACL is
stored at Redis key `admin_acls:<user>`. Managed via
`PUT /api/admin/admin_users/{u}/acl` or seeded at install time with
[backend/migrate_admin_acls.py](../backend/migrate_admin_acls.py).

An admin user with no `admin_acls:<user>` row is deny-all by default
— the migration tool exists so htpasswd-only deployments don't
suddenly become inaccessible after upgrading.

---

## Admin route gating

Admin endpoints fall into three tiers:

### Per-resource — requires the matching prefix

These routes enforce the caller's ACL against the resource they're
operating on:

- `GET /api/admin/peek/kv/{key}` — read access on `<key>`
- `POST /api/admin/kv/{key}` — write access
- `DELETE /api/admin/kv/{key}` — write access
- `GET /api/admin/peek/q/{topic}` — read access on `<topic>`
- `DELETE /api/admin/q/{topic}` — write access
- `GET /api/admin/stream/q/{topic}` — read access
- `GET /api/admin/kv_scan?prefix=…` — scans all matching keys, drops
  any the caller can't read

### ACL-filtered — returns only what the caller can see

- `GET /api/admin/stats` — queue + KV listings, filtered per caller
- `GET /api/admin/logs` — activity log, filtered per caller. Firmware
  hits and other non-app-scoped actions pass through.

### Superuser-only — requires `*:rw`

Routes that manage credentials or admin identity. A scoped admin
(e.g. `critterchron:rw`) is intentionally **not** a provisioning
operator:

- `GET/POST/DELETE /api/admin/keys`, `/api/admin/keys/{id}`
- `PUT /api/admin/keys/{id}/acl`
- `GET /api/admin/keys/backup` — backup dumps **raw HMAC secrets**;
  functionally a huge read, and requires the same access as any other
  total read.
- `POST /api/admin/keys/restore`
- `GET /api/admin/admin_users`
- `PUT /api/admin/admin_users/{u}/acl`

### `GET /api/admin/whoami`

Any authenticated admin. Returns `{username, acl, is_superuser}` so
the UI can hide entries the caller can't use. Not a security boundary
— routes themselves still enforce.

---

## Operator recipes

### Full admin (provisioning operator)

```json
{"permissions": [{"prefix": "*", "access": "rw"}]}
```

Can create/revoke HMAC clients, manage admin users, backup/restore,
and touch every queue and KV. This is the role the migration tool
assigns by default (`--default '*:rw'`).

### App-scoped admin (tenant operator)

```json
{"permissions": [{"prefix": "critterchron", "access": "rw"}]}
```

Full read/write over `critterchron/*` and `_catalog/critterchron*`.
Cannot see, touch, or be told about other apps' data via `/stats`,
`/logs`, or `kv_scan`. Cannot access `/keys/*` or `/admin_users/*`.

### Read-only observer

```json
{"permissions": [{"prefix": "*", "access": "r"}]}
```

Can peek everything, write nothing.

### Mixed scopes

```json
{"permissions": [{"prefix": "critterchron", "access": "rw"},
                 {"prefix": "example", "access": "r"}]}
```

RW on critterchron, read-only on example.

---

## Non-goal: catalog is not a device rights mechanism

The catalog (`_catalog/<app>`) declares which keys are **ergonomic to
tune** — it is **not** a list of which keys a device may read or
write. A device writing `<app>/debug_flag_experimental` that isn't in
the catalog is legitimate usage, not drift. Device permissions live
here in the ACL layer, and only here.

This distinction is intentional. Stra2us is a message-passing system;
conflating "friendly controls" with "what's permitted" would be an
antipattern.
