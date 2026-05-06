# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Customer-facing /app/ surface (see docs/fr_application_view.md).

Three routes:
  GET /app/                       — public landing form (`landing.html`)
  GET /app/{app}/{device}         — auth-gated, ACL-checks, serves
                                    the per-device customer page
                                    (`device.html`)
  GET /api/app/lookup_device      — public name → app lookup, used by
                                    the bare-URL form to 302 customers
                                    to their canonical device URL

Auth gating for `/app/{app}/{device}` is handled by
`admin_auth_middleware` (main.py), which sets `request.state.admin_user`
before this route handler runs. The route handler then enforces ACL
via the standard `check_acl` machinery — same enforcement as the
admin endpoints.

The bare landing form and the lookup endpoint are public on purpose:
a customer who's lost their bookmark needs to be able to find their
device URL before being asked to log in. Cloudflare Turnstile (or
equivalent) gates the lookup endpoint at the edge in production to
prevent device-name enumeration; not enforced at this layer.
"""

import os
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from core.redis_client import get_redis_client
from api.dependencies import get_admin_context, check_acl


router = APIRouter()

# Static files for the customer-facing /app/ surface. Lives alongside
# the admin UI's static files but in its own subdirectory so the two
# don't accidentally share assets / inherit each other's styling.
STATIC_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "static", "app")
)


@router.get("/app", include_in_schema=False)
@router.get("/app/", include_in_schema=False)
async def landing():
    """Bare-URL landing form. Public — no auth required.

    A customer who's lost their bookmark hits this page, types in
    their device name, and is 302'd to `/app/<app>/<device>` once the
    lookup endpoint resolves which app the device lives under.
    """
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


@router.get("/app/{app}/{device}", include_in_schema=False)
@router.get("/app/{app}/{device}/", include_in_schema=False)
async def device_page(app: str, device: str, request: Request):
    """Per-device customer page. Auth-gated by middleware; we then
    ACL-check that the caller has rw on `<app>/<device>` before serving.

    A user who's authenticated but doesn't own this device gets a 404
    that redirects to the bare landing form — same shape as the
    "device not found" failure mode, so the UX doesn't distinguish
    "wrong device for me" from "no such device." Avoids leaking
    "this device exists but you can't see it" via auth-success-but-
    page-load-failure.
    """
    admin_ctx = await get_admin_context(request)
    try:
        await check_acl(admin_ctx, f"kv/{app}/{device}", mode="write")
    except HTTPException:
        # Not the right user for this device — soft 404 to the landing
        # form, same as the genuine "no such device" case. The page
        # itself can render an inline message.
        return FileResponse(
            os.path.join(STATIC_DIR, "landing.html"),
            status_code=404,
        )
    return FileResponse(os.path.join(STATIC_DIR, "device.html"))


@router.get("/api/app/lookup_device", include_in_schema=False)
async def lookup_device(name: str):
    """Resolve a device name → its app via a Redis SCAN of `kv:*/<name>/*`.
    Returns `{app: "<app>"}` or 404. Public — no auth required.

    Captcha-gated at the edge in production (Cloudflare Turnstile or
    equivalent) to prevent device-name enumeration. See
    docs/fr_application_view.md > "Anti-enumeration".

    Lookup mechanism: scan-on-demand. Linear in fleet size; documented
    as a known issue with a `device_to_app:<name>` reverse-index fix
    if perf ever matters at scale.

    Constraint: device names are unique across apps. (See FR.)
    """
    if not name or "/" in name:
        # Trivially-invalid names, refuse without scanning. "/" would
        # let a probe target arbitrary path shapes.
        raise HTTPException(status_code=404, detail="No device by that name")

    redis = get_redis_client()
    pattern = f"kv:*/{name}/*"
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=200)
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            # Key shape: kv:<app>/<name>/<rest>. Extract <app>.
            parts = key.split(":", 1)[1].split("/")
            if len(parts) >= 3 and parts[1] == name:
                return {"app": parts[0]}
        if cursor == 0:
            break

    raise HTTPException(status_code=404, detail="No device by that name")
