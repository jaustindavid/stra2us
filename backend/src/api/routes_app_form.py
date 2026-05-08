# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Customer-facing form-submit handler (P3 of
`docs/fr_catalog_app_ui_plan.md`).

`POST /app/<app>/<device>` — receives form-urlencoded data from the
inline customer page form, writes each field to its
`<app>/<device>/<key>` KV path, redirects back to the GET page.

P3 ships the **strict-naive** version: every field present in the
POST body is written verbatim. Off-spec stomping and the
write-only-empty-wipes-stored-value cases are documented pre-P4
behavior — the FR explicitly defers both to P4's touched-state JS,
which omits unchanged fields client-side.

Wire-shape: catalog values are msgpack-packed in KV. Browser-form
values arrive as strings. We mirror the existing admin endpoint
(`backend/src/api/routes_admin.py:set_kv`)'s pattern: try
`json.loads` to recover types (so `"129"` stores as int 129 and
`"true"` as bool); fall back to string when the JSON parse fails.
The encrypted-flag sidecar (`kv:<key>:enc`) is preserved across
the write so an encrypted record stays encrypted after a form
save.
"""

from __future__ import annotations

import json
from typing import Any

import msgpack
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.dependencies import check_acl, get_admin_context
from core.redis_client import get_redis_client


router = APIRouter()


def _decode_form_value(raw: str) -> Any:
    """Mirror `routes_admin.set_kv`'s coercion: JSON-parse if
    possible (recovering int/float/bool/null), otherwise treat as
    string. Catches `"129"` → `129`, `"true"` → `True`, `"3.14"` →
    `3.14`, `"hello"` → `"hello"` (JSON parse fails, falls back).
    Empty string stays as empty string."""
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


@router.post("/app/{app}/{device}", include_in_schema=False)
async def submit_device_form(app: str, device: str, request: Request):
    """Strict-naive form-submit handler. See module docstring for
    the pre-P4 caveats.

    Auth + ACL match the GET path: admin context required, ACL
    `kv/<app>/<device>` write. A user authenticated but not ACL'd
    for this device gets a soft 404 to landing (same shape as the
    GET — avoids "this device exists but you can't see it" leak).
    """
    admin_ctx = await get_admin_context(request)
    try:
        await check_acl(admin_ctx, f"kv/{app}/{device}", mode="write")
    except HTTPException:
        # Same soft-404 shape as `device_page` — don't distinguish
        # "no such device" from "wrong user for this device."
        return RedirectResponse(url="/app/", status_code=303)

    form = await request.form()
    redis = get_redis_client()

    for name, raw_value in form.multi_items():
        # multi_items() yields all (k, v) pairs; we don't expect
        # multi-valued fields in the customer form (radio groups
        # post a single value, checkboxes aren't in P3's dispatch
        # table). If a future widget needs multi-value semantics
        # this'll need revisiting.
        if not name or "/" in name:
            # Defense: a form field whose name contains `/` would
            # let a crafted page write outside the device's KV
            # namespace. The renderer never emits these; reject
            # rather than build the path.
            continue
        decoded = _decode_form_value(str(raw_value))
        packed = msgpack.packb(decoded, use_bin_type=True)
        kv_key = f"kv:{app}/{device}/{name}"
        # Preserve the encrypted-flag sidecar across the write.
        # Writes through the device-side `/kv/` path use
        # `X-Encrypted: 1` to set the flag; the form-submit
        # path doesn't have a header to consult, so we just keep
        # whatever was there. Lint marks the var as `encrypted:
        # true`; the existing record's flag is the authoritative
        # state per fr_encrypted_values.md ("server-side flag is
        # what governs wire behavior").
        was_encrypted = await redis.get(f"{kv_key}:enc")
        await redis.set(kv_key, packed)
        if was_encrypted:
            await redis.set(f"{kv_key}:enc", b"1")

    # POST-redirect-GET so a refresh doesn't re-submit. 303
    # explicitly forces the GET method; 302 leaves it
    # implementation-defined and some browsers preserve POST.
    return RedirectResponse(url=f"/app/{app}/{device}", status_code=303)
