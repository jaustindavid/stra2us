# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Cached markdown→HTML rendering for catalog blocks
(P3 of `docs/fr_catalog_app_ui_plan.md`).

The FR's spec:
> HTML caching keyed by `(app, publish_hash, block_id)`.

Each catalog publish bumps the publish_hash (it's the same
SHA-256 prefix used for theme cache-bust). Cache entries from
prior publishes are unreachable but stay in memory until process
restart — fine for typical catalog sizes; if it ever becomes
relevant, swap the dict for an LRU.

Block IDs in use:
* `header` — `ui.header_markdown`
* `footer` — `ui.footer_markdown`
* `help.<varname>` — per-field `help_markdown`

Caller passes the rendered-HTML cache key components; this module
doesn't know about catalog shapes, only that the same `(app,
publish_hash, block_id, source)` tuple should produce the same
output and avoid re-running the sanitizer when re-rendered.
"""

from __future__ import annotations

import threading

from .markdown_render import sanitize_markdown


# `(app, publish_hash, block_id) → rendered_html`. Process-local
# cache; not shared across workers but each FastAPI worker is
# small and the cost of a cold sanitize is sub-millisecond, so a
# warm-up across N workers takes seconds.
_cache: dict[tuple[str, str, str], str] = {}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0}


def render_block(*, app: str, publish_hash: str, block_id: str,
                 source: str) -> str:
    """Return sanitized HTML for the given markdown source.

    Cache key is `(app, publish_hash, block_id)`. The `source`
    argument is *not* part of the key — the assumption is that a
    given (app, publish_hash, block_id) tuple always carries the
    same source bytes (the publish hash uniquely identifies the
    catalog state). If the source changes without the hash
    changing (which would be a publish-pipeline bug), we'd serve
    stale; the test corpus exercises this expected invariant.
    """
    key = (app, publish_hash, block_id)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _stats["hits"] += 1
            return cached
    # Compute outside the lock so a slow sanitize doesn't serialize
    # all readers. A second writer for the same key races to set
    # the same value; idempotent.
    rendered = sanitize_markdown(source, app=app)
    with _lock:
        _cache[key] = rendered
        _stats["misses"] += 1
    return rendered


def clear() -> None:
    """Test helper / operator hook. Called by `pytest` fixtures so
    one test's cache state doesn't leak into the next. Not exposed
    on any HTTP route."""
    with _lock:
        _cache.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


def stats() -> dict:
    """Read-only snapshot of cache stats. Useful for tests
    asserting "the sanitizer was called once per unique block."""
    with _lock:
        return dict(_stats)
