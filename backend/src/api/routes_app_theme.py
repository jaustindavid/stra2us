# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Per-app theme stylesheet route.

`GET /app/<app>/_theme.css?v=<hash>` — public, year-immutable
cache headers, served as `text/css`. Body is a single
`[data-app="<app>"] { … }` rule built by the parameterized
serializer in `services/theme_serializer.py`.

Why this is a route rather than a static file: the body comes
from the catalog YAML stashed at `_catalog/<app>` (P1's storage
layout). Each catalog republish bumps the `?v=<hash>` query
parameter that the page wrapper emits, busting browser caches
without the server having to mint anything per-request.

Why this is public: the body is a CSS rule with hex colors and
allowlisted font names. Nothing sensitive. Same posture as
`/app/<app>/_assets/<filename>`.
"""

from __future__ import annotations

import msgpack
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core.redis_client import get_redis_client
from services.theme_serializer import serialize_theme_css, theme_hash


router = APIRouter()

# Same cache shape as the asset route (per FR §"Assets" and the
# CSS section). Browsers + intermediates can hold the bytes for a
# year; cache-bust happens via the `?v=<hash>` query parameter the
# page wrapper emits.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


async def load_theme(app: str) -> tuple[dict | None, str | None]:
    """Read the catalog YAML for `app` from KV, return
    `(theme_dict_or_None, hash)`. Returns `(None, None)` when no
    catalog is published — caller decides whether to 404 or emit
    an empty rule.

    The YAML is the source of truth — we re-parse on each request
    rather than caching, because the catalog can be republished
    out-of-band (the CLI writes directly to KV) and we'd otherwise
    serve stale theme CSS until the next process restart. The
    parse is fast (~1ms for typical catalog sizes); the
    `Cache-Control: immutable` response header means real-world
    requests don't even hit this code path on most page loads.
    """
    redis = get_redis_client()
    raw = await redis.get(f"kv:_catalog/{app}")
    if raw is None:
        return None, None
    try:
        unpacked = msgpack.unpackb(raw, raw=False)
    except Exception:
        return None, None
    if not isinstance(unpacked, str):
        return None, None
    try:
        doc = yaml.safe_load(unpacked)
    except yaml.YAMLError:
        return None, None
    if not isinstance(doc, dict):
        return None, None
    theme = doc.get("theme")
    if theme is not None and not isinstance(theme, dict):
        # Mis-shaped theme block — treat as no theme. Lint would
        # have caught this on publish; defense in depth.
        theme = None
    return theme, theme_hash(theme)


@router.get("/app/{app}/_theme.css", include_in_schema=False)
async def serve_theme(app: str):
    """Serve the per-app theme CSS rule.

    Returns 404 only when no catalog YAML is published for `app`
    (the customer page wouldn't reach this either; the route is a
    safety net). A catalog with no `theme:` block returns a valid
    empty-body rule — the customer page falls back to the
    stra2us-default values via `var(--app-x, <default>)` in the
    base stylesheet.
    """
    theme, h = await load_theme(app)
    if theme is None and h is None:
        # Catalog wasn't found at all. 404 lets the browser-side
        # console show a clear error rather than a "succeeded with
        # no content" mystery.
        raise HTTPException(status_code=404, detail="catalog not published")

    body = serialize_theme_css(app, theme)
    headers = {"Cache-Control": _CACHE_CONTROL}
    if h:
        # Strong ETag — same theme dict always hashes to the same
        # `?v=` and the same ETag. `If-None-Match` works without
        # surprises if a browser revalidates after `?v=` rotation.
        headers["ETag"] = f'"{h}"'

    return Response(
        content=body,
        media_type="text/css; charset=utf-8",
        headers=headers,
    )
