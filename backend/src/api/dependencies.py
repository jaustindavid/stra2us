from fastapi import Request, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader
from core.redis_client import get_redis_client
from core.security import verify_signature, verify_timestamp
import json

client_id_header = APIKeyHeader(name="X-Client-ID")
timestamp_header = APIKeyHeader(name="X-Timestamp")
signature_header = APIKeyHeader(name="X-Signature")

async def verify_device_request(
    request: Request,
    client_id: str = Depends(client_id_header),
    timestamp_str: str = Depends(timestamp_header),
    signature: str = Depends(signature_header)
):
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid X-Timestamp format")

    if not verify_timestamp(timestamp):
        raise HTTPException(status_code=401, detail="Request expired or replay detected")

    redis = get_redis_client()
    
    # Fetch client secret
    secret_hex_bytes = await redis.get(f"client:{client_id}:secret")
    if not secret_hex_bytes:
        raise HTTPException(status_code=401, detail="Invalid Client ID")
    secret_hex = secret_hex_bytes.decode('utf-8')
        
    # Read the raw body
    body = await request.body()
    uri = str(request.url.path)

    if not verify_signature(secret_hex, uri, body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Signature")

    # Fetch ACLs
    acl_json = await redis.get(f"client:{client_id}:acl")
    acl = json.loads(acl_json) if acl_json else {"read_write": "*"}

    return {
        "client_id": client_id,
        "secret_hex": secret_hex,
        "acl": acl
    }

CATALOG_STASH_PREFIX = "_catalog/"

# Redis key prefix for admin ACL records. See docs/admin_acls.md.
ADMIN_ACL_KEY_FMT = "admin_acls:{user}"


async def load_admin_acl(username: str) -> dict:
    """Load an admin user's ACL from Redis. Returns an empty permissions
    envelope if the record is missing — i.e. strict deny-all until the
    migration tool (or the UI) has provisioned a row for this user.
    """
    redis = get_redis_client()
    raw = await redis.get(ADMIN_ACL_KEY_FMT.format(user=username))
    if not raw:
        return {"permissions": []}
    try:
        return json.loads(raw)
    except ValueError:
        return {"permissions": []}


async def get_admin_context(request: Request) -> dict:
    """FastAPI dependency — yields a client_context-shaped dict for an
    authenticated admin (username stashed by admin_auth_middleware).
    Raises 401 if for some reason the middleware didn't set it (e.g.
    route misconfigured to skip admin gating)."""
    username = getattr(request.state, "admin_user", None)
    if not username:
        raise HTTPException(status_code=401, detail="Admin session required")
    acl = await load_admin_acl(username)
    return {"client_id": username, "acl": acl, "is_admin": True}


def require_admin_kv(mode: str):
    """Dependency factory — gate an admin KV route on the logged-in
    admin's ACL. `mode` is 'read' or 'write'. Returns the admin context
    so handlers can log/identify the caller if they need to.
    """
    async def _dep(request: Request, key: str) -> dict:
        ctx = await get_admin_context(request)
        await check_acl(ctx, f"kv/{key}", mode=mode)
        return ctx
    return _dep


def require_admin_queue(mode: str):
    async def _dep(request: Request, topic: str) -> dict:
        ctx = await get_admin_context(request)
        await check_acl(ctx, f"q/{topic}", mode=mode)
        return ctx
    return _dep


async def require_admin_superuser(request: Request) -> dict:
    """Gate routes that manage credentials or admin identity — HMAC client
    CRUD (incl. backup, which dumps all secrets) and admin-user ACL editing.
    Requires a wildcard '*:rw' permission in the caller's ACL. A scoped
    admin (e.g. 'critterchron:rw') is not a provisioning operator.
    """
    ctx = await get_admin_context(request)
    for perm in ctx["acl"].get("permissions", []):
        if perm.get("prefix") == "*" and perm.get("access") == "rw":
            return ctx
    raise HTTPException(
        status_code=403,
        detail="Forbidden: requires superuser ACL (*:rw)",
    )


def _acl_candidate_paths(resource_path: str) -> list[str]:
    """ACL candidate paths for an (already type-stripped) key.

    A catalog stash at `_catalog/<app>[/...]` is governed by the same ACL
    rules as `<app>[/...]` — an admin with `critterchron:rw` can publish
    `_catalog/critterchron`, and `*` covers every stash. Returns both the
    original path and the dealiased one (if applicable) so the matcher
    can satisfy any rule that covers either.
    """
    paths = [resource_path]
    if resource_path.startswith(CATALOG_STASH_PREFIX):
        aliased = resource_path[len(CATALOG_STASH_PREFIX):]
        if aliased:
            paths.append(aliased)
    return paths


def _prefix_matches(prefix: str, path: str) -> bool:
    return prefix == "*" or path == prefix or path.startswith(prefix + "/")


async def check_acl(client_context: dict, requested_resource: str, mode: str = "read"):
    acl = client_context["acl"]

    # New ACL schema: {"permissions": [{"prefix": "...", "access": "r|rw"}, ...]}
    # Strip the resource-type segment (q/ or kv/) — permissions are namespace-only.
    resource_path = requested_resource
    for type_prefix in ("q/", "kv/"):
        if resource_path.startswith(type_prefix):
            resource_path = resource_path[len(type_prefix):]
            break

    candidates = _acl_candidate_paths(resource_path)

    for perm in acl.get("permissions", []):
        prefix = perm.get("prefix", "")
        access = perm.get("access", "r")
        if any(_prefix_matches(prefix, p) for p in candidates):
            if mode == "write" and access != "rw":
                raise HTTPException(status_code=403, detail="Forbidden: Write access denied")
            return True

    raise HTTPException(status_code=403, detail=f"Forbidden: No permission for '{requested_resource}'")
