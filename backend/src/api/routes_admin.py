# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone
import json
import msgpack
import time
from core.redis_client import get_redis_client
from core.security import generate_secret
from api.dependencies import (
    ADMIN_ACL_KEY_FMT,
    get_admin_context,
    require_admin_kv,
    require_admin_queue,
    require_admin_superuser,
    check_acl,
    _prefix_matches,
)
from api.routes_app import _resolve_telemetry_topic
from api.routes_app_theme import load_catalog_dict
from core.admin_auth import HTPASSWD_FILE, is_rescue_on_default
from core.perf_log import PerfPhases, PERF_LOG_STREAM
from core.version import get_release_version
import os

router = APIRouter()


# v1.7.0: surface the running release tag to the admin UI. The
# admin's sidebar footer fetches this on page load and displays
# the result alongside the Sign Out link. Public to any authed
# admin — the version string is not sensitive. Source of truth
# is `backend/VERSION` (a one-line file bumped per release; see
# `core/version.py`).
@router.get("/release")
async def get_release(_: dict = Depends(get_admin_context)):
    return {"version": get_release_version()}

# Client IDs we refuse to mint, because they collide with sub-namespaces
# under each `<app>/`. See "Reserved-name enforcement" in
# docs/fr_application_view.md. A device named `public` would have its
# per-device data at `<app>/public/...`, which is the shared-namespace
# convention — a customer's narrow `<app>/public:r` grant would suddenly
# include the rogue device's private data, and the rogue device's writes
# would land in the shared namespace.
#
# Match is case-sensitive and exact: `public` is blocked, `Public`,
# `_public`, `publik`, etc. are not. Fuzzy/case-folded matching invites
# its own edge-case bugs; the convention is just "don't pick the literal
# word `public`."
RESERVED_CLIENT_IDS = {"public"}


def _reject_if_reserved(client_id: str) -> None:
    if client_id in RESERVED_CLIENT_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Client id {client_id!r} is reserved as a namespace under "
                f"each app. Reserved: {sorted(RESERVED_CLIENT_IDS)}."
            ),
        )


class ClientCreate(BaseModel):
    client_id: str


class DeviceProvision(BaseModel):
    """Combined create-client-and-grant-app-access payload. Mirrors the
    customer-shaped device ACL from fr_application_view.md: rw on the
    device's own per-device namespace + rw on the app's shared public/
    namespace (so the device can publish telemetry there)."""
    client_id: str
    app: str

class KVPayload(BaseModel):
    value: str
    encrypted: bool = False

class AclPermission(BaseModel):
    prefix: str
    access: str  # "r" or "rw"

class AclUpdate(BaseModel):
    permissions: List[AclPermission]

class ClientBackupEntry(BaseModel):
    client_id: str
    secret: str
    acl: dict

class BackupPayload(BaseModel):
    exported_at: int
    clients: List[ClientBackupEntry]

# v1.7.1 Sprint 4: scope-aware companion to /keys. Returns just the
# client_ids the caller has ACL coverage for (no secrets, no ACL
# bodies). The Activity Logs view in the admin UI uses this to
# populate filter chips for scoped (non-superuser) admins — pre-
# v1.7.1 it called /keys, which is superuser-only, so a scoped
# admin opening the Activity Logs page got 403 → blank view +
# no chips. The scope-aware shape lets every admin see the chips
# for clients they can actually filter activity on.
#
# `/keys` stays superuser-locked since it returns secrets + ACL
# bodies — that surface is tied to the client-management UI that
# scoped admins shouldn't reach.
@router.get("/visible_clients")
async def list_visible_clients(ctx: dict = Depends(get_admin_context)):
    redis = get_redis_client()
    caller_perms = ctx["acl"].get("permissions", [])
    if not caller_perms:
        # No permissions at all (e.g. a misconfigured admin row) →
        # see nothing. Empty list, not 403 — the page renders
        # without filter chips, same as the network-failure path.
        return []

    keys = await redis.keys("client:*:secret")
    visible: list[str] = []
    for k in keys:
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        client_id = k.split(":")[1]
        acl_raw = await redis.get(f"client:{client_id}:acl")
        if not acl_raw:
            continue
        try:
            client_acl = json.loads(acl_raw)
        except Exception:
            continue
        # Device-shaped clients: visible if any caller perm covers
        # `<app>/<client_id>`. Internal probes / custom-ACL clients
        # without the device shape are visible only to wildcards.
        app = _device_app_for_client(client_acl, client_id)
        if app is not None:
            path = f"{app}/{client_id}"
            if any(_prefix_matches(p.get("prefix", ""), path)
                   for p in caller_perms):
                visible.append(client_id)
        else:
            # Non-device-shaped client: only wildcard ACLs see them.
            # The `_prefix_matches` predicate naturally handles `*`,
            # so this is just "does the caller have anything that
            # matches the bare client_id." In practice this catches
            # internal probes (smoke-test client, etc.) for superuser
            # callers without leaking them to scoped admins.
            if any(_prefix_matches(p.get("prefix", ""), client_id)
                   for p in caller_perms):
                visible.append(client_id)
    return sorted(visible)


@router.get("/keys")
async def list_keys(_: dict = Depends(require_admin_superuser)):
    redis = get_redis_client()
    keys = await redis.keys("client:*:secret")
    result = []
    for k in keys:
        if isinstance(k, bytes):
            k = k.decode('utf-8')
        client_id = k.split(":")[1]
        acl_json = await redis.get(f"client:{client_id}:acl")
        acl = json.loads(acl_json) if acl_json else {}
        result.append({
            "client_id": client_id,
            "acl": acl
        })
    return result

def _device_app_for_client(client_acl: dict, client_id: str) -> Optional[str]:
    """Given a client's ACL, derive the app it belongs to. The
    `provision_device` endpoint shapes the per-device ACL with a
    `<app>/<client_id>` prefix; we recover `<app>` by finding that
    prefix. Returns None if the client doesn't have a device-shaped
    ACL (e.g., it's an internal probe with custom permissions)."""
    for perm in client_acl.get("permissions", []):
        prefix = perm.get("prefix", "")
        if "/" in prefix:
            head, tail = prefix.split("/", 1)
            if tail == client_id:
                return head
    return None


@router.get("/keys/{client_id}/admins")
async def list_admins_for_device(client_id: str, _: dict = Depends(require_admin_superuser)):
    """Return admin users whose ACL covers this device's
    `<app>/<client_id>` resource path. Powers the "Admin users with
    access" section in the device ACL editor — read-only relationship
    view; mutation happens through the per-user ACL editor.

    Computation: iterate `admin_acls:*`, for each user check whether
    any prefix in their ACL matches `<app>/<client_id>` per the same
    semantics `check_acl` uses (`*`, exact match, or parent-prefix).
    Returns the most permissive matching access level per user (`rw`
    wins over `r`).
    """
    redis = get_redis_client()
    raw = await redis.get(f"client:{client_id}:secret")
    if not raw:
        raise HTTPException(status_code=404, detail="Client not found")
    raw_acl = await redis.get(f"client:{client_id}:acl")
    try:
        client_acl = json.loads(raw_acl) if raw_acl else {"permissions": []}
    except ValueError:
        client_acl = {"permissions": []}

    app = _device_app_for_client(client_acl, client_id)
    if app is None:
        # Not a device-shaped client (e.g. an internal probe with
        # custom ACL). No app context, so we can't compute admins.
        return {"client_id": client_id, "app": None, "admins": []}

    resource_path = f"{app}/{client_id}"

    acl_keys = await redis.keys("admin_acls:*")
    admins = []
    for k in acl_keys:
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        username = k[len("admin_acls:"):]
        raw_user_acl = await redis.get(k)
        if not raw_user_acl:
            continue
        try:
            user_acl = json.loads(raw_user_acl)
        except ValueError:
            continue
        max_access = None
        for perm in user_acl.get("permissions", []):
            prefix = perm.get("prefix", "")
            if _prefix_matches(prefix, resource_path):
                access = perm.get("access", "r")
                if access == "rw":
                    max_access = "rw"
                    break  # already at max
                if max_access is None:
                    max_access = access
        if max_access:
            admins.append({"username": username, "access": max_access})

    admins.sort(key=lambda a: a["username"])
    return {"client_id": client_id, "app": app, "admins": admins}


@router.post("/keys")
async def create_client(client: ClientCreate, _: dict = Depends(require_admin_superuser)):
    _reject_if_reserved(client.client_id)
    redis = get_redis_client()
    secret = generate_secret()
    await redis.set(f"client:{client.client_id}:secret", secret)
    # New clients start with no permissions (deny-all); edit ACL separately.
    acl = {"permissions": []}
    await redis.set(f"client:{client.client_id}:acl", json.dumps(acl))
    return {
        "client_id": client.client_id,
        "secret": secret,
        "acl": acl
    }

@router.post("/provision_device")
async def provision_device(payload: DeviceProvision, _: dict = Depends(require_admin_superuser)):
    """Idempotent one-shot device provisioning: ensure an HMAC client
    exists with the customer-shaped ACL for `<app>`.

    Resulting ACL (per fr_application_view.md namespace convention):
      [
        {"prefix": "<app>/<client_id>", "access": "rw"},  # device's own ns
        {"prefix": "<app>/public",      "access": "rw"},  # shared topic
      ]

    Two behaviors depending on whether the client already exists:

    - **New client.** Mint a secret, set the ACL, return both. Response
      `created: true`, `secret: "<hex>"`. This is the standard "register
      a new device" flow — operator must save the secret immediately.
    - **Existing client.** *Leave the secret alone* (don't regenerate —
      that'd break already-deployed devices using the existing secret),
      replace the ACL with the device-on-app shape. Response
      `created: false`, `secret: null`. Useful for retrofitting the
      device-on-app ACL onto clients minted before this endpoint
      existed, and for re-running provisioning scripts safely.

    *ACL replacement is wholesale.* If the existing client had a
    custom ACL (e.g. multi-app perms), that's clobbered. The common
    case is a one-app device, so this is right; for the rare
    multi-app device, use the lower-level `PUT /keys/{id}/acl`
    instead.

    Reserved-name guard (`RESERVED_CLIENT_IDS`) applies; `app` is
    similarly validated so an empty/whitespace app doesn't produce a
    nonsense ACL.
    """
    _reject_if_reserved(payload.client_id)
    if not payload.client_id.strip():
        raise HTTPException(status_code=400, detail="client_id is required")
    if not payload.app.strip():
        raise HTTPException(status_code=400, detail="app is required")
    if "/" in payload.app or "/" in payload.client_id:
        # `/` would corrupt the prefix shape — caller likely passed a
        # path instead of an identifier.
        raise HTTPException(
            status_code=400,
            detail="app and client_id must not contain '/'",
        )

    redis = get_redis_client()
    existing_secret = await redis.get(f"client:{payload.client_id}:secret")
    created = existing_secret is None

    acl = {
        "permissions": [
            {"prefix": f"{payload.app}/{payload.client_id}", "access": "rw"},
            {"prefix": f"{payload.app}/public",              "access": "rw"},
        ]
    }

    if created:
        secret = generate_secret()
        await redis.set(f"client:{payload.client_id}:secret", secret)
    else:
        # Don't return the existing secret — we already promised "shown
        # once at creation"; re-leaking via provision would undermine
        # that. If the operator's lost the secret, they need to
        # revoke + re-create.
        secret = None

    await redis.set(f"client:{payload.client_id}:acl", json.dumps(acl))
    # v1.6.7: reverse-index `<device_name> → <app>` so the customer
    # landing form's `lookup_device` finds freshly-provisioned
    # devices BEFORE they've done their first KV write. Pre-v1.6.7
    # the lookup scanned `kv:*/<name>/*` and returned 404 for any
    # device that hadn't heartbeated yet — forcing the operator
    # workflow into "provision → flash → device heartbeats →
    # configure" instead of the natural "provision → configure →
    # flash". The reverse index closes that gap and incidentally
    # turns the lookup from O(N) scan into O(1) get. See
    # `routes_app.py:lookup_device` for the read side + the
    # backfill-on-scan-hit fallback for legacy devices.
    await redis.set(f"device_to_app:{payload.client_id}", payload.app)

    return {
        "client_id": payload.client_id,
        "secret": secret,        # hex string if created, null if existing
        "acl": acl,
        "created": created,
    }


@router.put("/keys/{client_id}/acl")
async def update_acl(client_id: str, acl_update: AclUpdate, _: dict = Depends(require_admin_superuser)):
    redis = get_redis_client()
    existing = await redis.get(f"client:{client_id}:secret")
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")
    acl = {"permissions": [p.dict() for p in acl_update.permissions]}
    await redis.set(f"client:{client_id}:acl", json.dumps(acl))
    return {"status": "ok", "client_id": client_id, "acl": acl}

@router.delete("/keys/{client_id}")
async def revoke_client(client_id: str, _: dict = Depends(require_admin_superuser)):
    redis = get_redis_client()
    await redis.delete(f"client:{client_id}:secret")
    await redis.delete(f"client:{client_id}:acl")
    # v1.6.7: also clear the reverse-index entry so a deleted
    # device doesn't continue to resolve through the customer
    # landing form's lookup. Without this, the lookup would
    # cheerfully return the (now-defunct) app name for a
    # device whose secret + ACL are gone — confusing UX.
    await redis.delete(f"device_to_app:{client_id}")
    return {"status": "ok"}

# --- Admin users ---
#
# Admin accounts live in htpasswd (auth) + Redis (ACL). The UI can read
# the union and update ACLs; create/delete/password-reset stay CLI-only
# to avoid putting credential management in the browser session.

def _read_htpasswd_users() -> List[str]:
    if not os.path.exists(HTPASSWD_FILE):
        return []
    users = []
    with open(HTPASSWD_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            users.append(line.split(":", 1)[0])
    return users


@router.get("/admin_users")
async def list_admin_users(_: dict = Depends(require_admin_superuser)):
    """Return every known admin identity:

    - **htpasswd users** (e.g. `rescue`, `smoke`) — authenticate via
      Basic Auth on the device hostname. May or may not have an ACL
      row in Redis (rescue gets a hardcoded wildcard via RESCUE_USERS
      regardless).
    - **OAuth identities** (e.g. `austindavid@gmail.com`) — authenticate
      via Google on the browser hostname. Live only as
      `admin_acls:<email>` Redis rows; not in htpasswd.

    Each entry tagged with `source` so the UI can distinguish.
    Pre-v1.5 versions of this endpoint enumerated only htpasswd —
    OAuth identities were invisible. Phase 5 fixed that.
    """
    redis = get_redis_client()
    htpasswd_users = set(_read_htpasswd_users())

    # Find every admin_acls:* row. KEYS is fine here — the namespace
    # is small (handful of admin identities) and this endpoint is
    # rare (admin UI page load).
    acl_keys = await redis.keys("admin_acls:*")
    acl_users = set()
    for k in acl_keys:
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        # Strip the "admin_acls:" prefix.
        acl_users.add(k[len("admin_acls:"):])

    out = []
    for user in sorted(htpasswd_users | acl_users):
        in_htpasswd = user in htpasswd_users
        raw = await redis.get(ADMIN_ACL_KEY_FMT.format(user=user))
        if raw:
            try:
                acl = json.loads(raw)
                provisioned = True
            except ValueError:
                acl = {"permissions": []}
                provisioned = True  # row exists but is corrupt
        else:
            acl = {"permissions": []}
            provisioned = False

        # Source heuristic: presence of `@` in the username distinguishes
        # OAuth (Google email) identities from htpasswd-style usernames.
        # Imperfect (a non-OAuth account could in principle contain @),
        # but matches reality for our deployments.
        if in_htpasswd:
            source = "htpasswd"
        elif "@" in user:
            source = "oauth"
        else:
            source = "acl-only"  # orphaned ACL row, no auth path

        out.append({
            "username": user,
            "source": source,
            "acl": acl,
            "provisioned": provisioned,
        })
    return out


@router.put("/admin_users/{username}/acl")
async def update_admin_user_acl(username: str, acl_update: AclUpdate, _: dict = Depends(require_admin_superuser)):
    """Create or replace the Redis ACL row for an admin user.

    Username can be an htpasswd entry (rescue, smoke, ...) OR an
    OAuth email — the ACL row is just a Redis key, no underlying
    account record required. Pre-v1.5 this endpoint 404'd unless the
    user existed in htpasswd; that gate excluded OAuth identities
    from being granted permissions through the UI. Phase 5 dropped
    the gate. Spelling errors are now the operator's responsibility
    (see DELETE below for cleaning up orphan rows).
    """
    if not username.strip():
        raise HTTPException(status_code=400, detail="username is required")
    redis = get_redis_client()
    acl = {"permissions": [p.dict() for p in acl_update.permissions]}
    await redis.set(ADMIN_ACL_KEY_FMT.format(user=username), json.dumps(acl))
    return {"status": "ok", "username": username, "acl": acl}


@router.delete("/admin_users/{username}/acl")
async def delete_admin_user_acl(username: str, _: dict = Depends(require_admin_superuser)):
    """Remove an admin user's ACL row. Useful for revoking an OAuth
    identity (deletes their permissions immediately; their next
    request gets the empty-permissions deny-all path) and for
    cleaning up orphaned admin_acls rows from typos.

    Does NOT touch htpasswd — operator manages that via
    `create_admin.py` out-of-band. After deleting an OAuth user's
    ACL row, the user can still complete the OAuth flow but lands
    on the unauthorized landing page."""
    redis = get_redis_client()
    deleted = await redis.delete(ADMIN_ACL_KEY_FMT.format(user=username))
    return {"status": "ok", "username": username, "deleted": bool(deleted)}


@router.get("/security_warnings")
async def security_warnings(_: dict = Depends(get_admin_context)):
    """Surface non-blocking security concerns the admin should
    address. Frontend (`app.js`) fetches this on dashboard load and
    renders a banner per warning. Severities: `warning` (yellow),
    `error` (red).

    Currently checked:
      - rescue user is on the bootstrap-default password.

    Future candidates: TLS cert expiry, default OAuth secret in use,
    htpasswd file has weak hashes, etc.
    """
    warnings = []
    if is_rescue_on_default():
        warnings.append({
            "id": "rescue-default-password",
            "severity": "warning",
            "message": (
                "Rescue user is on the bootstrap-default password. "
                "Change it before exposing the device hostname to "
                "anything sensitive."
            ),
            "action": (
                "On the host: cd backend && "
                "python3 create_admin.py rescue '<new-password>'"
            ),
        })
    return {"warnings": warnings}


@router.get("/me")
async def get_me(admin_ctx: dict = Depends(get_admin_context)):
    """Return the caller's identity, ACL, and a derived scope hint.

    Used by both `/admin` and `/app/<app>/<device>` JS to drive UI
    gating without each gate re-deriving "what kind of user is this?"
    in three places. See docs/fr_application_view.md.

    `scope_kind` derivation:
      - any `*:rw` perm           → "superadmin"
      - exactly one `rw` perm with prefix `<app>` (one segment)
                                  → "app", with `scope_app` populated
      - exactly one `rw` perm with prefix `<app>/<device>` (two
        segments)               → "device", with `scope_app` and
                                  `scope_device` populated
      - anything else            → "custom" (multi-device operators,
                                  read-only personas, weird shapes
                                  — UI treats as "show everything,
                                  rely on per-route ACL enforcement")

    Read-only perms (the customer's `<app>/public:r` + `_catalog:r`
    grants) are *ignored* when deriving scope — they're scaffolding
    for the device-scoped read paths the customer needs, not what
    defines who they are.
    """
    acl = admin_ctx["acl"]
    perms = acl.get("permissions", [])

    is_superuser = any(
        p.get("prefix") == "*" and p.get("access") == "rw"
        for p in perms
    )

    scope_kind = "superadmin" if is_superuser else "custom"
    scope_app = None
    scope_device = None

    if not is_superuser:
        rw_prefixes = [
            p.get("prefix", "") for p in perms
            if p.get("access") == "rw"
        ]
        if len(rw_prefixes) == 1:
            parts = rw_prefixes[0].split("/")
            if len(parts) == 1 and parts[0]:
                scope_kind = "app"
                scope_app = parts[0]
            elif len(parts) == 2 and all(parts):
                scope_kind = "device"
                scope_app = parts[0]
                scope_device = parts[1]
            # anything else (3+ segments, empty parts) stays "custom"

    return {
        "username": admin_ctx["client_id"],
        "acl": acl,
        "is_superuser": is_superuser,
        "scope_kind": scope_kind,
        "scope_app": scope_app,
        "scope_device": scope_device,
    }


@router.get("/stats")
async def get_stats(admin_ctx: dict = Depends(get_admin_context)):
    redis = get_redis_client()
    q_keys = await redis.keys("q:*")
    kv_keys = await redis.keys("kv:*")

    queues = []
    for qk in q_keys:
        if isinstance(qk, bytes): qk = qk.decode('utf-8')
        topic = qk.split(":", 1)[1]
        try:
            await check_acl(admin_ctx, f"q/{topic}", mode="read")
        except HTTPException:
            continue
        count = await redis.xlen(qk)
        queues.append({"topic": topic, "count": count})

    kvs = []
    for kvk in kv_keys:
        if isinstance(kvk, bytes): kvk = kvk.decode('utf-8')
        name = kvk.split(":", 1)[1]
        # Skip the encrypted-flag sidecars (`kv:foo:enc`) — they're metadata
        # for `kv:foo`, not standalone records, and would otherwise show up
        # as ghost rows in the admin list.
        if name.endswith(":enc"):
            continue
        try:
            await check_acl(admin_ctx, f"kv/{name}", mode="read")
        except HTTPException:
            continue
        encrypted = bool(await redis.get(f"kv:{name}:enc"))
        kvs.append({"key": name, "encrypted": encrypted})

    return {
        "queues": queues,
        "kvs": kvs
    }

@router.get("/peek/q/{topic:path}")
async def peek_queue(topic: str, _: dict = Depends(require_admin_queue("read"))):
    redis = get_redis_client()
    # Peek at oldest message using xrange
    messages = await redis.xrange(f"q:{topic}", min="-", max="+", count=1)
    if not messages:
        return {"status": "empty", "message": None}
        
    try:
        msg_id, fields = messages[0]
        payload = fields[b"payload"]
        decoded = msgpack.unpackb(payload)
        return {"status": "ok", "message": decoded, "hex": payload.hex()}
    except Exception:
        return {"status": "ok", "message": "unparseable_msgpack", "hex": payload.hex()}

@router.get("/kv_scan")
async def scan_kv(
    request: Request,
    prefix: str = Query(..., min_length=1),
    limit: int = 500,
    admin_ctx: dict = Depends(get_admin_context),
):
    """List KV keys matching a literal prefix, filtered to those the
    logged-in admin can read. Intended for UI discovery (e.g.
    `prefix=_catalog/` to list published catalogs). Returns the raw key
    names with their stored byte size; callers fetch values via /peek/kv/*.
    """
    redis = get_redis_client()
    phases = PerfPhases(request)
    # redis keys are stored under the `kv:` namespace; match that.
    pattern = f"kv:{prefix}*"
    with phases.phase("redis_keys"):
        raw_keys = await redis.keys(pattern)

    # Filter by the caller's ACL — check_acl raises on deny, so catch it
    # per-key rather than letting a single unreadable entry fail the scan.
    items = []
    for k in raw_keys:
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        name = k.split(":", 1)[1]
        with phases.phase("acl_filter"):
            try:
                await check_acl(admin_ctx, f"kv/{name}", mode="read")
            except HTTPException:
                continue
        with phases.phase("strlen_loop"):
            size = await redis.strlen(k)
        items.append({"key": name, "bytes": size})
        if len(items) >= limit:
            break
    items.sort(key=lambda it: it["key"])
    # `truncated` now means "the caller's visible result set was capped",
    # not the raw redis KEYS output — UI already treats it as a hint.
    return {"prefix": prefix, "count": len(items), "truncated": len(items) >= limit, "items": items}


@router.get("/catalog/{app}/devices")
async def list_catalog_devices(app: str, admin_ctx: dict = Depends(get_admin_context)):
    """HMAC clients with access to <app>'s namespace.

    A device, for catalog-UI purposes, is a client that can read or write
    under <app>. That covers the three real-world ACL shapes: an exact
    app-match (`<app>:rw`), a wildcard (`*:rw`), or a deeper sub-prefix
    (`<app>/<device>:rw`). Returned device IDs are client IDs — and by
    convention also the second path segment in <app>/<device>/<key> KV
    writes, which is what the per-device effective-value view depends on.

    The returned set is *filtered* by the caller's own ACL: a scoped
    admin (e.g. `<app>/ricky:rw`) only sees devices they have rw on,
    not the whole fleet. This stops device-name leakage to scoped
    customer-style admins (Phase 0a finding from
    fr_application_view.md). Superadmins (`*:rw`) and app-scoped
    admins (`<app>:rw`) still see every device under the app.

    The outer gate is `_catalog/<app>:r` — that's the natural
    prerequisite (you need the catalog to make sense of device data),
    and it's what the recommended scoped-admin ACL shape grants.
    The legacy `kv/<app>:r` gate was too restrictive under the new
    public/ namespace convention (scoped admins don't have it).
    """
    await check_acl(admin_ctx, f"kv/_catalog/{app}", mode="read")

    redis = get_redis_client()
    acl_keys = await redis.keys("client:*:acl")
    devices: set[str] = set()
    app_subprefix = f"{app}/"
    for k in acl_keys:
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        parts = k.split(":")
        if len(parts) < 3:
            continue
        client_id = parts[1]
        raw = await redis.get(k)
        if not raw:
            continue
        try:
            acl = json.loads(raw)
        except Exception:
            continue
        for perm in acl.get("permissions", []):
            prefix = perm.get("prefix", "")
            if prefix == "*" or prefix == app or prefix.startswith(app_subprefix):
                devices.add(client_id)
                break

    # Filter to devices the *caller* has rw on. Superadmins (`*:rw`) and
    # app-scoped admins (`<app>:rw`) pass everything; a scoped admin
    # (`<app>/ricky:rw`) only sees ricky. Phase 0a finding from
    # fr_application_view.md.
    visible: list[str] = []
    for device in sorted(devices):
        try:
            await check_acl(admin_ctx, f"kv/{app}/{device}", mode="write")
            visible.append(device)
        except HTTPException:
            pass

    return {"app": app, "devices": visible}


@router.get("/peek/kv/{key:path}")
async def peek_kv(request: Request, key: str, _: dict = Depends(require_admin_kv("read"))):
    redis = get_redis_client()
    phases = PerfPhases(request)
    with phases.phase("redis_get"):
        msg = await redis.get(f"kv:{key}")
    if not msg:
        return {"status": "empty", "message": None}

    encrypted = bool(await redis.get(f"kv:{key}:enc"))
    try:
        with phases.phase("msgpack_unpack"):
            decoded = msgpack.unpackb(msg)
        with phases.phase("hex_encode"):
            hexed = msg.hex()
        return {"status": "ok", "message": decoded, "hex": hexed, "encrypted": encrypted}
    except Exception:
        with phases.phase("hex_encode"):
            hexed = msg.hex()
        return {"status": "ok", "message": "unparseable_msgpack", "hex": hexed, "encrypted": encrypted}

@router.post("/kv/{key:path}")
async def set_kv(key: str, payload: KVPayload, _: dict = Depends(require_admin_kv("write"))):
    redis = get_redis_client()
    try:
        data = json.loads(payload.value)
    except ValueError:
        data = payload.value
    packed = msgpack.packb(data)
    await redis.set(f"kv:{key}", packed)
    if payload.encrypted:
        await redis.set(f"kv:{key}:enc", b"1")
    else:
        await redis.delete(f"kv:{key}:enc")
    return {"status": "ok"}

@router.delete("/kv/{key:path}")
async def delete_kv(key: str, _: dict = Depends(require_admin_kv("write"))):
    redis = get_redis_client()
    await redis.delete(f"kv:{key}", f"kv:{key}:enc")
    return {"status": "ok"}

@router.delete("/q/{topic:path}")
async def delete_queue(topic: str, _: dict = Depends(require_admin_queue("write"))):
    redis = get_redis_client()
    await redis.delete(f"q:{topic}")
    return {"status": "ok"}

def _log_resource_from_action(action: str) -> Optional[str]:
    """Parse 'METHOD /q/<topic>' or 'METHOD /kv/<key>' into an ACL
    check target like 'q/<topic>' or 'kv/<key>'. Returns None for
    actions that aren't ACL-scoped — including any historical
    `/firmware/` entries that might still be in the activity log
    stream from before the route was removed."""
    try:
        _, path = action.split(" ", 1)
    except ValueError:
        return None
    if path.startswith("/q/"):
        return "q/" + path[len("/q/"):]
    if path.startswith("/kv/"):
        return "kv/" + path[len("/kv/"):]
    return None


@router.get("/logs")
async def get_logs(
    request: Request,
    limit: int = 200,
    client_id: Optional[List[str]] = Query(None),
    admin_ctx: dict = Depends(get_admin_context),
):
    redis = get_redis_client()
    phases = PerfPhases(request)
    # Over-fetch insurance for scoped admins: their ACL filter may drop
    # most entries, so we pull more than asked to leave a full page after
    # filtering. Wildcard admins skip this — every entry passes their
    # filter, so the multiplier is pure deserialization tax (the dominant
    # cost of this endpoint at any meaningful stream size).
    acl_perms = admin_ctx.get("acl", {}).get("permissions", [])
    is_wildcard = any(p.get("prefix") == "*" for p in acl_perms)
    fetch_count = limit if is_wildcard else min(limit * 10, 5000)

    with phases.phase("xrevrange"):
        records = await redis.xrevrange("system:activity_log", max="+", min="-", count=fetch_count)

    logs = []
    with phases.phase("filter_loop"):
        for msg_id, fields in records:
            cid = fields.get(b"client_id", b"unknown")
            if isinstance(cid, bytes):
                cid = cid.decode("utf-8")

            if client_id and cid not in client_id:
                continue

            action = fields.get(b"action", b"")
            status = fields.get(b"status", b"")
            action_str = action.decode("utf-8") if isinstance(action, bytes) else action

            # ACL filter: only show log entries whose target the caller can read.
            # Firmware hits and other non-ACL-scoped actions pass through — they
            # aren't per-app resources.
            resource = _log_resource_from_action(action_str)
            if resource is not None:
                try:
                    await check_acl(admin_ctx, resource, mode="read")
                except HTTPException:
                    continue

            logs.append({
                "timestamp": int(fields.get(b"timestamp", b"0")),
                "client_id": cid,
                "action":    action_str,
                "status":    status.decode("utf-8") if isinstance(status, bytes) else status,
            })
            if len(logs) >= limit:
                break

    return logs


# --- Backup / Restore ---

@router.get("/keys/backup")
async def backup_keys(_: dict = Depends(require_admin_superuser)):
    """Export all client IDs, secrets, and ACLs as a JSON blob.
    WARNING: Response contains raw HMAC secrets. Treat like a password vault.
    """
    redis = get_redis_client()
    secret_keys = await redis.keys("client:*:secret")
    clients = []
    for k in secret_keys:
        if isinstance(k, bytes):
            k = k.decode('utf-8')
        client_id = k.split(":")[1]
        secret = await redis.get(f"client:{client_id}:secret")
        acl_json = await redis.get(f"client:{client_id}:acl")
        secret_str = secret.decode('utf-8') if isinstance(secret, bytes) else secret
        acl = json.loads(acl_json) if acl_json else {}
        clients.append({
            "client_id": client_id,
            "secret": secret_str,
            "acl": acl,
        })

    payload = {
        "exported_at": int(time.time()),
        "clients": clients,
    }
    return JSONResponse(content=payload, headers={
        "Content-Disposition": "attachment; filename=stra2us_backup.json"
    })


@router.post("/keys/restore")
async def restore_keys(payload: BackupPayload, force: bool = Query(False), _: dict = Depends(require_admin_superuser)):
    """Restore client credentials from a backup JSON blob.
    By default, skips clients that already exist.
    Pass ?force=true to overwrite existing entries.
    """
    redis = get_redis_client()
    results = {"restored": [], "skipped": [], "overwritten": []}

    for client in payload.clients:
        # Same reserved-name guard as `create_client` — prevents a
        # backup file from silently un-reserving `client:public:*`
        # entries that pre-date the convention. (See comment on
        # RESERVED_CLIENT_IDS.)
        _reject_if_reserved(client.client_id)

        existing = await redis.get(f"client:{client.client_id}:secret")
        if existing and not force:
            results["skipped"].append(client.client_id)
            continue

        await redis.set(f"client:{client.client_id}:secret", client.secret)
        await redis.set(f"client:{client.client_id}:acl", json.dumps(client.acl))

        if existing:
            results["overwritten"].append(client.client_id)
        else:
            results["restored"].append(client.client_id)

    return results


# --- Backup / Restore v2: whole-instance + per-app (v1.8.0 Sprint 7) ---
#
# Successor to `/keys/backup` (which only covers client credentials).
# Envelope format + Redis-side (de)serializers live in
# `services/backup_format.py` and `services/backup_io.py`; this
# section is the HTTP/auth glue.
#
# Sensitive-data warning: dumps contain HMAC secrets, OAuth-mapped
# admin ACL rows, and at-rest encrypted KV plaintext (the wire-
# encryption inverse). Treat dumps with password-manager sensitivity.
# `X-Stra2us-Sensitive: true` flagged on every dump response so
# downstream proxies + logging pipelines can scrub.


_DUMP_RESPONSE_HEADERS = {
    "X-Stra2us-Sensitive": "true",
    "Cache-Control": "no-store",   # don't let an intermediary cache a dump
}


def _dump_filename(env) -> str:
    """Friendly download filename — operator-readable, namespace-scoped."""
    if env.app:
        return f"stra2us_backup_{env.app}_{env.exported_at}.json"
    return f"stra2us_backup_whole_{env.exported_at}.json"


@router.get("/backup")
async def backup_whole(
    include_logs: bool = Query(False, description="Include system:activity_log entries (off by default — log dumps are typically large and rarely load-bearing for restores)."),
    _: dict = Depends(require_admin_superuser),
):
    """Whole-instance dump: clients, admin_acls, KV (incl. catalogs +
    assets), queues, device_to_app reverse index, optionally activity
    log. See `services/backup_format.py` for the envelope schema.

    Suitable for full-server migrations + periodic offline backups.
    For per-app exports use `GET /backup/app/<app>`.
    """
    from services.backup_io import collect_whole_envelope
    redis = get_redis_client()
    env = await collect_whole_envelope(redis, include_logs=include_logs)
    return JSONResponse(
        content=env.to_json(),
        headers={
            **_DUMP_RESPONSE_HEADERS,
            "Content-Disposition": f"attachment; filename={_dump_filename(env)}",
        },
    )


@router.get("/backup/app/{app}")
async def backup_per_app(
    app: str,
    include_logs: bool = Query(False),
    _: dict = Depends(require_admin_superuser),
):
    """Per-app dump: every load-bearing key whose namespace belongs
    to `<app>`. Clients, admin_acls, KV (incl. `_catalog/<app>` and
    `_catalog/<app>/_assets/...`), queues under `<app>/...`, the
    `device_to_app` rows pointing at `<app>`, and (with
    `?include_logs=1`) activity-log entries whose `client_id` is one
    of the included clients.

    Wildcard admins are NOT included — they're instance-scoped (not
    per-app); the destination instance should provision its own.

    Useful for cloning an app's state to a fresh stack, onboarding a
    new instance, or surgical restores.
    """
    if not app.strip() or "/" in app:
        raise HTTPException(status_code=400, detail="invalid app name")
    from services.backup_io import collect_per_app_envelope
    redis = get_redis_client()
    env = await collect_per_app_envelope(redis, app, include_logs=include_logs)
    return JSONResponse(
        content=env.to_json(),
        headers={
            **_DUMP_RESPONSE_HEADERS,
            "Content-Disposition": f"attachment; filename={_dump_filename(env)}",
        },
    )


@router.post("/restore")
async def restore_whole(
    request: Request,
    force_overwrite: bool = Query(False, description="Replace existing values. Default (false) = skip any key that already exists."),
    _: dict = Depends(require_admin_superuser),
):
    """Restore from a whole-instance dump. Default semantics: skip-
    existing, return a structured per-section list of what was
    restored / skipped / overwritten. Pass `?force_overwrite=1` to
    replace existing values.

    Refuses envelopes whose `dump_kind` is not `"whole"` — use the
    per-app endpoint for per-app envelopes. Refuses unknown
    `stra2us_backup_version` values; future v2 dumps need either a
    server that understands v2 or a manual migration.
    """
    from services.backup_format import BackupEnvelope, BackupFormatError
    from services.backup_io import apply_envelope
    body = await request.json()
    try:
        env = BackupEnvelope.from_json(body)
    except BackupFormatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if env.dump_kind != "whole":
        raise HTTPException(
            status_code=400,
            detail=(
                f"dump_kind={env.dump_kind!r}; this endpoint accepts "
                f"only whole-instance dumps. Use /restore/app/<app> for per-app."
            ),
        )
    redis = get_redis_client()
    result = await apply_envelope(redis, env, force_overwrite=force_overwrite)
    return result


@router.post("/restore/app/{app}")
async def restore_per_app(
    app: str,
    request: Request,
    force_overwrite: bool = Query(False),
    _: dict = Depends(require_admin_superuser),
):
    """Restore from a per-app dump, with the destination-app scope
    enforced regardless of what the envelope claims.

    Defense in depth: the URL path's `<app>` is the authoritative
    filter — keys that fall outside `<app>/...` / `_catalog/<app>` are
    rejected before any write, even if the envelope's `app` field
    says otherwise. The response includes any rejected keys under
    `rejected_outside_app_filter` so the operator can audit.

    Accepts either a per-app envelope (recommended) or a whole-
    instance envelope (in which case only the `<app>`-matching slice
    is imported). Refuses envelopes whose `app` field disagrees with
    the URL — that's an obvious operator mistake worth flagging.
    """
    if not app.strip() or "/" in app:
        raise HTTPException(status_code=400, detail="invalid app name")
    from services.backup_format import BackupEnvelope, BackupFormatError
    from services.backup_io import apply_envelope
    body = await request.json()
    try:
        env = BackupEnvelope.from_json(body)
    except BackupFormatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if env.app is not None and env.app != app:
        raise HTTPException(
            status_code=400,
            detail=(
                f"envelope app={env.app!r} does not match URL app={app!r}; "
                f"refusing cross-app restore"
            ),
        )
    redis = get_redis_client()
    result = await apply_envelope(
        redis, env, force_overwrite=force_overwrite, app_filter=app,
    )
    return result


# --- Performance log (over-threshold requests) ---

@router.get("/perf_log")
async def get_perf_log(
    limit: int = 200,
    path_prefix: Optional[str] = None,
    min_ms: float = 0.0,
    _: dict = Depends(require_admin_superuser),
):
    """Tail the system:perf_log stream. Superuser-only — perf data isn't
    a per-resource concern (no ACL filter applies) and reveals internal
    paths an ops view should see but app-scoped admins shouldn't need."""
    redis = get_redis_client()
    fetch_count = min(limit * 5, 5000)
    records = await redis.xrevrange(PERF_LOG_STREAM, max="+", min="-", count=fetch_count)

    out = []
    for msg_id, fields in records:
        path = fields.get(b"path", b"").decode("utf-8")
        if path_prefix and not path.startswith(path_prefix):
            continue
        total_ms = float(fields.get(b"total_ms", b"0"))
        if total_ms < min_ms:
            continue
        entry = {
            "timestamp": int(fields.get(b"timestamp", b"0")),
            "method":    fields.get(b"method", b"").decode("utf-8"),
            "path":      path,
            "total_ms":  total_ms,
            "status":    int(fields.get(b"status", b"0")),
            "client_id": fields.get(b"client_id", b"").decode("utf-8"),
        }
        if b"phase_breakdown" in fields:
            try:
                entry["phases"] = json.loads(fields[b"phase_breakdown"])
            except Exception:
                pass
        out.append(entry)
        if len(out) >= limit:
            break
    return out


# --- Topic Stream Monitor ---

# Paged-scan tunables for the client_id-filtered branch of
# stream_monitor. A shared-topic stream may interleave many
# publishers; a single XREVRANGE window of `limit` entries can miss
# a sparse publisher entirely. We walk backward in batches until we
# have `limit` matches, the stream's retained history is exhausted,
# or the safety cap fires (pathological "client never wrote here").
_STREAM_FILTER_BATCH = 100
_STREAM_FILTER_MAX_BATCHES = 10


@router.get("/stream/q/{topic:path}")
async def stream_monitor(
    topic: str,
    limit: int = 50,
    client_id: Optional[List[str]] = Query(None),
    _: dict = Depends(require_admin_queue("read")),
):
    """Read-only scan of the last N messages from a topic stream.
    Uses XREVRANGE — does not advance any subscriber cursor.

    `:path` on the topic param so multi-segment topic names like
    `<app>/public/heartbeep` (the post-namespace-migration shape per
    fr_application_view.md) match. Single-segment topics still match
    the same route.

    When `client_id` is set (one or more values), the scan walks
    backward in batches and accumulates only matching entries, so a
    sparse publisher on a shared topic is not crowded out by chatty
    neighbors. When `client_id` is unset the original single-shot
    XREVRANGE path is preserved.
    """
    redis = get_redis_client()
    key = f"q:{topic}"

    if client_id:
        records = await _xrevrange_filtered(redis, key, limit, client_id)
    else:
        records = await redis.xrevrange(key, max="+", min="-", count=limit)

    messages = []
    now = int(time.time())
    for msg_id, fields in records:
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode()

        # received_at derived from stream entry ID millisecond prefix (authoritative)
        ms_str = msg_id.split("-")[0]
        received_at = int(ms_str) // 1000

        # Skip expired messages
        exp = int(fields.get(b"exp", b"0"))
        if now > exp:
            continue

        cid = fields.get(b"client_id", b"unknown")
        if isinstance(cid, bytes):
            cid = cid.decode("utf-8")

        if client_id and cid not in client_id:
            continue

        raw_payload = fields.get(b"payload", b"")
        try:
            data = msgpack.unpackb(raw_payload, raw=False)
        except Exception:
            data = raw_payload.hex()

        messages.append({
            "id": msg_id,
            "received_at": received_at,
            "client_id": cid,
            "data": data,
        })

    return messages


async def _xrevrange_filtered(redis, key: str, limit: int, client_ids: List[str]):
    """Page backward through XREVRANGE accumulating entries whose
    `client_id` field is in `client_ids`. Returns up to `limit`
    matches in newest-first order. Stops at the oldest retained
    entry or at the `_STREAM_FILTER_MAX_BATCHES` safety cap, which
    bounds the worst-case scan when the filter matches nothing.

    Note: `limit` here is the cap on client_id-matches *before* the
    caller's per-entry `exp` filter runs. A caller that drops
    expired entries afterward may end up with fewer than `limit`
    rows. Intentional — matches the pre-existing unfiltered path's
    semantics, where `limit` likewise gates the raw XREVRANGE
    window, not the post-exp result.
    """
    wanted = {c.encode("utf-8") if isinstance(c, str) else c for c in client_ids}
    matches: list = []
    cursor = "+"
    for _batch in range(_STREAM_FILTER_MAX_BATCHES):
        batch = await redis.xrevrange(key, max=cursor, min="-", count=_STREAM_FILTER_BATCH)
        if not batch:
            break
        for msg_id, fields in batch:
            cid = fields.get(b"client_id", b"")
            if cid in wanted:
                matches.append((msg_id, fields))
                if len(matches) >= limit:
                    return matches
        last_id = batch[-1][0]
        if isinstance(last_id, bytes):
            last_id = last_id.decode()
        # Exclusive upper bound for the next page so we don't re-read
        # the boundary entry. Redis 6.2+ supports `(<id>` syntax.
        cursor = f"({last_id}"
        if len(batch) < _STREAM_FILTER_BATCH:
            # Stream exhausted — no point continuing.
            break
    return matches


# --- Heartbeat dump (operator debug) -----------------------------------
#
# `GET /api/admin/dump_heartbeats/<client_id>` — full-history dump of
# a single client's heartbeat publishes, as JSONL. Bounded by the
# stream's own 7-day EXPIRE (no per-entry exp filter applied here:
# operators debugging a stale device want the stale entries too).
#
# Topic is resolved the same way the customer-facing /app/ page
# resolves it (catalog `telemetry_topic` with `{app}`/`{device}`
# substitution; default `<app>/public/heartbeep`). The catalog read
# is *not* separately ACL-gated — the catalog is consulted only to
# learn which topic to gate on. The operative gate is the
# `q/<topic>` ACL check on the resolved topic.
_DUMP_HEARTBEATS_PAGE = 500


@router.get("/dump_heartbeats/{client_id}")
async def dump_heartbeats(client_id: str, request: Request):
    """Stream every heartbeat from `client_id` within the stream's
    retention window as JSONL.

    Auth: this handler can't use `require_admin_queue("read")` —
    the topic is *derived* in-handler (catalog lookup), not a path
    param the dependency could see. We re-implement the same check
    explicitly after resolving the topic.

    Response shape (newest-first):
        line 0 — `{"_meta": {...}}` metadata record
        line 1..N — one decoded entry per line
        empty body after `_meta` if no matches found

    Each data line carries both `data` (msgpack-decoded; null if
    decode fails) and `payload_hex` (raw bytes) for forensic
    completeness when a payload won't round-trip cleanly.
    """
    ctx = await get_admin_context(request)

    redis = get_redis_client()
    acl_raw = await redis.get(f"client:{client_id}:acl")
    if not acl_raw:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        client_acl = json.loads(acl_raw)
    except ValueError:
        client_acl = {"permissions": []}

    app = _device_app_for_client(client_acl, client_id)
    if app is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Client has no app affinity; cannot resolve a "
                "heartbeat topic. Use the redis-cli directly for "
                "clients without a catalog."
            ),
        )

    catalog = await load_catalog_dict(app)
    topic = _resolve_telemetry_topic(app, client_id, catalog)

    # Gate on the resolved topic. A scoped admin (e.g. someother_app:rw)
    # asking about a client whose telemetry lands under critterchron/
    # will 403 here, same as if they'd hit /api/admin/stream/q/<topic>.
    await check_acl(ctx, f"q/{topic}", mode="read")

    wanted_cid = client_id.encode("utf-8")
    key = f"q:{topic}"
    generated_at_iso = (
        datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
                                      .replace("+00:00", "Z")
    )
    utc_stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"heartbeats-{client_id}-{utc_stamp}.jsonl"

    async def _iter_jsonl():
        meta = {
            "_meta": {
                "topic": topic,
                "client_id": client_id,
                "generated_at_iso": generated_at_iso,
                "stream_max_age_days": 7,
            }
        }
        yield json.dumps(meta) + "\n"

        # Walk the stream forward in pages, collecting matches.
        # The brief calls for forward XRANGE traversal with an
        # exclusive `(<last_id>` lower bound between pages (mirror of
        # _xrevrange_filtered's backward idiom). We then emit the
        # collected matches in reverse so the JSONL body reads
        # newest-first, matching stream_monitor's convention. Memory
        # cost is bounded by the 7-day retention (~10MB worst case
        # per the brief's sizing).
        cursor = "-"
        matches: list = []
        while True:
            page = await redis.xrange(
                key, min=cursor, max="+", count=_DUMP_HEARTBEATS_PAGE
            )
            if not page:
                break
            for msg_id, fields in page:
                cid = fields.get(b"client_id", b"")
                if cid == wanted_cid:
                    matches.append((msg_id, fields))
            last_id = page[-1][0]
            if isinstance(last_id, bytes):
                last_id = last_id.decode()
            # Exclusive lower bound — Redis 6.2+ `(<id>` syntax,
            # same as _xrevrange_filtered's exclusive upper bound.
            cursor = f"({last_id}"
            if len(page) < _DUMP_HEARTBEATS_PAGE:
                # Stream exhausted.
                break

        # Newest-first emission. matches are oldest-first from XRANGE.
        for msg_id, fields in reversed(matches):
            if isinstance(msg_id, bytes):
                msg_id_s = msg_id.decode()
            else:
                msg_id_s = msg_id
            ms_str = msg_id_s.split("-", 1)[0]
            try:
                ts_ms = int(ms_str)
            except ValueError:
                ts_ms = 0
            received_at_iso = (
                datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
            )

            raw_payload = fields.get(b"payload", b"") or b""
            try:
                data = msgpack.unpackb(raw_payload, raw=False)
            except Exception:
                # Forensic mode: payload kept verbatim in payload_hex,
                # `data` stays semantically "decoded payload or null".
                # Diverges from stream_monitor's hex-into-data fallback
                # — see brief, "JSON encoding".
                data = None

            exp_raw = fields.get(b"exp", b"0")
            try:
                exp_val = int(exp_raw)
            except (TypeError, ValueError):
                exp_val = 0

            cid_raw = fields.get(b"client_id", b"")
            if isinstance(cid_raw, bytes):
                cid_str = cid_raw.decode("utf-8", errors="replace")
            else:
                cid_str = str(cid_raw)

            entry = {
                "ts_ms": ts_ms,
                "received_at_iso": received_at_iso,
                "client_id": cid_str,
                "exp": exp_val,
                "data": data,
                "payload_hex": raw_payload.hex(),
            }
            yield json.dumps(entry, default=str) + "\n"

    return StreamingResponse(
        _iter_jsonl(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
