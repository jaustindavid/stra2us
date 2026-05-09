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

The encrypted-flag sidecar (`kv:<key>:enc`) is **set from the
catalog** as of v1.6.5, not from prior state. If the catalog
declares `encrypted: true`, every form-submit lands the value
encrypted (sets `:enc=1`); if the catalog declares (or implies)
not-encrypted, every form-submit clears the flag. Pre-v1.6.5
this path "preserved" whatever sidecar was there, which broke
the case where the operator deleted both the value and the
sidecar — the next form-submit landed plaintext despite the
catalog declaring `encrypted: true`. Catalog-as-authoritative
matches the broader principle filed in TODO.md ("`stra2us set`
should honor the catalog's `encrypted:` field").

Fields NOT present in the catalog (a stale POST after a
republish that removed the field) fall back to the pre-v1.6.5
"preserve `:enc`" behavior — a vanishing field shouldn't
silently strip encryption from data that's still legitimately
stored.
"""

from __future__ import annotations

import json
from typing import Any

import msgpack
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.dependencies import check_acl, get_admin_context
from api.routes_app_theme import load_catalog_dict
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

    # v1.6.5: load the catalog so the encryption decision can honor
    # the catalog's `encrypted: true` declaration. Pre-v1.6.5 this
    # path "preserved whatever was there" — which broke when the
    # operator deleted the sidecar (or set the value for the first
    # time): the new write landed plaintext despite the catalog
    # explicitly declaring it encrypted. The catalog is now
    # authoritative for the catalog-aware write path; matches the
    # principle in TODO.md ("`stra2us set` should honor the
    # catalog's `encrypted:` field").
    catalog = await load_catalog_dict(app)
    catalog_vars = (catalog or {}).get("vars") or {}

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

        # Decide encryption from the catalog, not from prior state.
        # If the catalog declares the field `encrypted: true`,
        # we set `:enc=1` regardless of whether the sidecar was
        # there before. If the catalog says (or implies) NOT
        # encrypted, we drop any stale `:enc` so the field's wire
        # behavior matches the current catalog. A field absent
        # from the catalog (e.g. a stale POST after a republish
        # that removed the field) falls back to the pre-v1.6.5
        # "preserve" semantics — we don't want a disappearing
        # field to silently strip encryption from data that's
        # still legitimately stored.
        var = catalog_vars.get(name) if isinstance(catalog_vars, dict) else None
        await redis.set(kv_key, packed)
        if isinstance(var, dict):
            if var.get("encrypted"):
                await redis.set(f"{kv_key}:enc", b"1")
            else:
                await redis.delete(f"{kv_key}:enc")
        else:
            # Field not in catalog: preserve prior `:enc` (the
            # pre-v1.6.5 behavior). Read-then-(maybe)-set so a
            # stale form doesn't clobber a legitimate flag.
            was_encrypted = await redis.get(f"{kv_key}:enc")
            if was_encrypted:
                await redis.set(f"{kv_key}:enc", b"1")

    # POST-redirect-GET so a refresh doesn't re-submit. 303
    # explicitly forces the GET method; 302 leaves it
    # implementation-defined and some browsers preserve POST.
    return RedirectResponse(url=f"/app/{app}/{device}", status_code=303)
