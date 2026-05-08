# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Public asset-serve route for the customer-facing app page.

Serves files published by `stra2us catalog publish` (see
`tools/stra2us_cli/catalog_publish.py`) under the per-app reserved
KV namespace. Implements `GET /app/<app>/_assets/<filename>` per
`docs/fr_catalog_app_ui.md` "Assets (self-hosted images)".

Why this route exists separately from `/app/_static/`:

* `/app/_static/` is the *application*'s shipped frontend (JS, CSS,
  HTML for the customer page itself). Static, baked into the
  container image.
* `/app/<app>/_assets/<file>` is a *vendor*'s catalog-attached
  bundle (logos, inline markdown images). Per-app, content comes
  from KV, lifecycle tied to `stra2us catalog publish`.

The route is intentionally public — auth is the wrong gate for
brand assets that the customer page references in `<img>` tags.
The asset URL is unguessable in any practical sense (anyone who
knows the app slug knows the asset is published) and the bytes
are trusted-by-construction (sanitized at publish for SVGs;
content-type pinned in `.meta`; payload size capped by lint at
publish time).
"""

from __future__ import annotations

import re

import msgpack
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core.redis_client import get_redis_client


router = APIRouter()

# Same constraints lint enforces at publish time. Duplicated here
# (rather than imported from `stra2us_cli`) to keep the backend's
# import surface clean and the validation independent — a buggy
# CLI shouldn't be able to publish a path-traversal filename and
# have the server happily fetch it just because the publish-time
# check passed.
_FILENAME_RE = re.compile(r"^(?!\.)[a-z0-9._-]{1,64}$")
# `.meta` is a sibling key, not a separately-fetchable asset.
_RESERVED_SUFFIXES = (".meta",)


# Cache headers per the FR. Browsers + intermediates can hold the
# bytes for a year; cache-bust happens via the `?v=<hash>` query
# parameter that the renderer (P3) emits when it constructs URLs.
# `immutable` tells modern browsers not to revalidate even on
# explicit reload — safe because every byte change in the payload
# changes the `?v=` and thus the URL.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _kv_key(app: str, filename: str, *, meta: bool = False) -> str:
    suffix = ".meta" if meta else ""
    return f"_catalog/{app}/_assets/{filename}{suffix}"


@router.get("/app/{app}/_assets/{filename}", include_in_schema=False)
async def serve_asset(app: str, filename: str):
    """Serve a published catalog asset.

    Reads the bytes + meta from KV, returns the bytes with the
    declared `Content-Type` and a year-immutable cache header.
    Returns 404 when either the bytes or the meta are missing
    (treats partial state as absent — defends against the
    publish-died-mid-flight window from
    `docs/fr_catalog_app_ui.md` §5a).
    """
    if not _FILENAME_RE.match(filename):
        # Both a sanity check on URL shape and a guard against
        # path-traversal. The route's `{filename}` capture is
        # already a single segment (no slashes), but a `..` or
        # leading dot would still slip through without this.
        raise HTTPException(status_code=404, detail="bad asset name")
    if filename.endswith(_RESERVED_SUFFIXES):
        raise HTTPException(status_code=404, detail="reserved suffix")

    redis = get_redis_client()
    bytes_raw = await redis.get(f"kv:{_kv_key(app, filename)}")
    meta_raw = await redis.get(f"kv:{_kv_key(app, filename, meta=True)}")
    if bytes_raw is None or meta_raw is None:
        raise HTTPException(status_code=404, detail="asset not found")

    # KV values are msgpack-packed (the CLI uploads via
    # `client.put(key, value)` which msgpack-packs first). Unpack
    # to recover the original bytes / dict.
    try:
        payload = msgpack.unpackb(bytes_raw, raw=True)
        meta = msgpack.unpackb(meta_raw, raw=False)
    except Exception:
        # Corrupted KV records — treat as 404 rather than 500. The
        # operator can re-publish; the customer doesn't need a
        # detailed error.
        raise HTTPException(status_code=404, detail="asset corrupted")

    if not isinstance(payload, (bytes, bytearray)):
        raise HTTPException(status_code=404, detail="asset payload not bytes")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail="asset meta malformed")

    content_type = meta.get("content_type", "application/octet-stream")
    headers = {"Cache-Control": _CACHE_CONTROL}
    sha256 = meta.get("sha256")
    if isinstance(sha256, str) and sha256:
        # Strong ETag — same bytes always produce the same hash, so
        # `If-None-Match` works without surprises. The renderer's
        # `?v=<hash>` short-circuits revalidation by changing the
        # URL on every republish; ETag is for the "browser cleared
        # the cache, then re-requested" case.
        headers["ETag"] = f'"{sha256}"'

    return Response(
        content=bytes(payload),
        media_type=content_type,
        headers=headers,
    )
