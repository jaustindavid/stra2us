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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core.redis_client import get_redis_client
from api.dependencies import get_admin_context, check_acl
from api.routes_app_theme import load_catalog_dict, load_theme
from services.page_renderer import render_page
from services.value_resolver import resolve_value


router = APIRouter()

# Static files for the customer-facing /app/ surface. Lives alongside
# the admin UI's static files but in its own subdirectory so the two
# don't accidentally share assets / inherit each other's styling.
STATIC_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "static", "app")
)


# Cached on first read so we don't re-touch disk on every device-page
# render. The customer page is small (~1.5 KB) and changes only when
# a new container image ships; in-process caching is correct + cheap.
_DEVICE_TEMPLATE: str | None = None


def _device_template() -> str:
    """Read `device.html` once per process. Returns the template
    string (with `{{APP}}` / `{{THEME_HASH}}` placeholders intact)
    for `_render_device_page` to substitute."""
    global _DEVICE_TEMPLATE
    if _DEVICE_TEMPLATE is None:
        with open(os.path.join(STATIC_DIR, "device.html")) as fh:
            _DEVICE_TEMPLATE = fh.read()
    return _DEVICE_TEMPLATE


async def _render_device_page(app: str, device: str) -> HTMLResponse:
    """Substitute placeholders in `device.html` and return as HTML.

    Renders the customer-facing form server-side (P3): catalog +
    per-device current values flow through `page_renderer` to
    produce the inline form HTML, which replaces the
    `{{SETTINGS_SECTION}}` placeholder in the template.

    `{{APP}}` is the catalog slug (already constrained to
    `^[a-z][a-z0-9_]*$` by the catalog schema; selector-safe with
    no escaping). `{{THEME_HASH}}` comes from the catalog's theme
    block via `load_theme`, so the page's `<link>` URL and the
    route the browser fetches share a hash by construction. Empty
    hash + empty settings section when no catalog is published —
    the page degrades to "you're logged in but the catalog isn't
    here yet" rather than 500'ing.

    `{{TELEMETRY_TOPIC}}` + `{{HEARTBEAT_SECONDS}}` are surfaced
    as `data-*` attributes on `<body>` so app.js can drive the
    status badge + activity tail without re-fetching the catalog
    client-side. P3 retired the client-side catalog parse; these
    two values are the only catalog-derived config the JS still
    needs.
    """
    template = _device_template()
    catalog = await load_catalog_dict(app)
    _, theme_hash = await load_theme(app)
    settings_html = await _render_settings_section(app, device, catalog)
    telemetry_topic = _resolve_telemetry_topic(app, device, catalog)
    heartbeat_seconds = _resolve_heartbeat_seconds(catalog)
    favicon_href = _resolve_favicon_href(app, catalog)
    rendered = (
        template
        .replace("{{APP}}", app)
        .replace("{{DEVICE}}", device)
        .replace("{{THEME_HASH}}", theme_hash or "")
        .replace("{{SETTINGS_SECTION}}", settings_html)
        .replace("{{TELEMETRY_TOPIC}}", telemetry_topic)
        .replace("{{HEARTBEAT_SECONDS}}", str(heartbeat_seconds))
        .replace("{{FAVICON_HREF}}", favicon_href)
    )
    # `Cache-Control: no-store` so a `window.location.reload()` after
    # form save always re-renders against fresh KV. Without this,
    # browsers may serve the previous render (before the save), which
    # hides the very state change the customer just made — and worse,
    # masks whether P4's touched-state serialize behaved correctly
    # (the post-save page would look pre-save). Caught during P4
    # walkthrough on staging. The CSS / theme.css / asset routes
    # are still cache-immutable individually; only the dynamic page
    # itself opts out.
    return HTMLResponse(
        content=rendered,
        headers={"Cache-Control": "no-store"},
    )


def _resolve_telemetry_topic(app: str, device: str,
                             catalog: dict | None) -> str:
    """Catalog-declared `telemetry_topic` with `{app}` / `{device}`
    placeholder substitution; falls back to the FR's convention
    `<app>/public/heartbeep` when the catalog is missing or
    doesn't declare one. Mirrors the pre-P3 client-side resolver
    in app.js (`resolveTelemetryTopic`)."""
    declared = (catalog or {}).get("telemetry_topic") or "{app}/public/heartbeep"
    return (
        declared
        .replace("{app}", app)
        .replace("{device}", device)
    )


def _resolve_heartbeat_seconds(catalog: dict | None) -> int:
    """Catalog-declared cadence with the customer-page's 60s
    fallback. Drives the status-badge thresholds in app.js."""
    declared = (catalog or {}).get("heartbeat_interval_seconds")
    if isinstance(declared, int) and declared > 0:
        return declared
    return 60


_DEFAULT_FAVICON_HREF = "/app/_static/favicon.png"


def _resolve_favicon_href(app: str, catalog: dict | None) -> str:
    """v1.6.7 (TODO #7): per-app favicon URL with default fallback.

    Catalog `theme.favicon_asset` (if set) is rendered as
    `/app/<app>/_assets/<file>` — same shape as `logo_asset`. When
    unset (or the catalog isn't published yet), falls back to the
    default at `/app/_static/favicon.svg`. Cosmetic-but-noisy:
    pre-v1.6.7 the customer page had no `<link rel="icon">` so
    every page load triggered a speculative `/favicon.ico` request
    that 404'd, polluting the browser console.

    The catalog-side asset is validated by `catalog_lint`'s
    asset-listing rules (must exist in the bundle, filename shape).
    No additional escaping needed here — the catalog `app` slug is
    already constrained to `^[a-z][a-z0-9_]*$`, and the asset
    filename was sanity-checked at publish time.
    """
    if not isinstance(catalog, dict):
        return _DEFAULT_FAVICON_HREF
    theme = catalog.get("theme")
    if not isinstance(theme, dict):
        return _DEFAULT_FAVICON_HREF
    favicon_asset = theme.get("favicon_asset")
    if not isinstance(favicon_asset, str) or not favicon_asset:
        return _DEFAULT_FAVICON_HREF
    return f"/app/{app}/_assets/{favicon_asset}"


async def _render_settings_section(app: str, device: str,
                                   catalog: dict | None) -> str:
    """Build the inline form HTML by resolving each customer-facing
    var's current value (device → public → catalog default) and
    handing the result to `page_renderer.render_page`. Returns the
    full `<section class="catalog-app">…</section>` markup, or a
    polite "no catalog yet" message when no catalog is published
    for this app."""
    if catalog is None:
        return (
            '<section class="catalog-app">'
            '<p class="hint">No catalog published for this app yet — '
            'check back once your operator has run '
            '<code>stra2us catalog publish</code>.</p>'
            '</section>'
        )
    redis = get_redis_client()
    values: dict = {}
    for name, var in (catalog.get("vars") or {}).items():
        if not isinstance(var, dict) or not var.get("label"):
            # Operator-only var (no `label:` per fr_application_view).
            continue
        values[name] = await resolve_value(redis, app, device, name, var)
    return render_page(app=app, device=device, catalog=catalog, values=values)


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
    return await _render_device_page(app, device)


@router.get("/api/app/lookup_device", include_in_schema=False)
async def lookup_device(name: str):
    """Resolve a device name → its app. Returns `{app: "<app>"}` or 404.

    **Auth (v1.7.1+):** admin session required, same as the
    `/app/` landing form. The auth middleware in `main.py` enforces
    this — see `_path_requires_auth`. Pre-v1.7.1 this endpoint was
    intentionally public, which made it enumerable: an unauthed
    attacker could probe device names and learn (a) which exist,
    (b) which app they belong to. v1.7.1 Sprint 3 closed that by
    requiring an OAuth-allowlisted admin session before either
    `/app/` or this lookup endpoint is reachable.

    Lookup mechanism (v1.6.7+):

    1. **`device_to_app:<name>` reverse index** — written at provision
       time by `routes_admin.py:provision_device`. O(1) Redis GET.
       Resolves freshly-provisioned devices that haven't yet done
       their first KV write — pre-v1.6.7 those returned 404 because
       the only "exists" predicate was a `kv:*/<name>/*` SCAN hit,
       which forced the operator workflow into "provision → flash →
       device heartbeats → configure" instead of the natural
       "provision → configure → flash."

    2. **KV SCAN fallback** — for devices provisioned before v1.6.7
       (no reverse-index entry yet). Linear in fleet size, but
       only fires for the legacy population. On a hit, the
       reverse-index entry is backfilled as a side effect, so the
       legacy population self-heals on first lookup.

    Constraint: device names are unique across apps. (See FR.)
    """
    if not name or "/" in name:
        # Trivially-invalid names, refuse without scanning. "/" would
        # let a probe target arbitrary path shapes.
        raise HTTPException(status_code=404, detail="No device by that name")

    redis = get_redis_client()

    # 1. Fast path: reverse index.
    indexed = await redis.get(f"device_to_app:{name}")
    if indexed is not None:
        # Redis returns bytes by default; decode if needed. The
        # codebase elsewhere handles both shapes (str-mode and
        # bytes-mode clients) — match that defensiveness here.
        if isinstance(indexed, bytes):
            indexed = indexed.decode("utf-8")
        return {"app": indexed}

    # 2. Fallback: SCAN for legacy devices that pre-date the reverse
    #    index. Materialize the index on hit so the next lookup goes
    #    fast.
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
                app_name = parts[0]
                # Backfill the reverse index — turns a O(N) scan into
                # an O(1) get for the next lookup of this device.
                # Best-effort: failure here doesn't block the response,
                # the next lookup will scan again and try once more.
                try:
                    await redis.set(f"device_to_app:{name}", app_name)
                except Exception:
                    pass
                return {"app": app_name}
        if cursor == 0:
            break

    raise HTTPException(status_code=404, detail="No device by that name")
