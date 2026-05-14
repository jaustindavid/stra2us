# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Running-release version surface.

Source of truth: `backend/VERSION` — a one-line text file whose
contents are the release tag (e.g. `v1.7.0`). The file is
manually bumped per release for now; a future enhancement (filed
in TODO.md) automates the bump via `tools/stage promote`.

Read pattern: cached on first call. The file is bind-mounted into
the container via `./backend:/app`, so re-deploys + promotes pick
up the new value on the next container restart — no need to
poll. The cache exists so the `/api/admin/release` endpoint
doesn't hit the disk on every request.

Fallback: if the file is missing or unreadable, the running
release is reported as `"dev"`. Useful in test environments and
fresh checkouts that haven't been deployed yet."""

from __future__ import annotations

import os
from pathlib import Path

# `__file__` is backend/src/core/version.py. The VERSION file lives
# at backend/VERSION (alongside the requirements file, the
# Dockerfile, etc.). That's two dirs up from this file.
_VERSION_PATH = Path(__file__).resolve().parent.parent.parent / "VERSION"

_FALLBACK = "dev"

_cached: str | None = None


def get_release_version() -> str:
    """Return the running release tag (e.g. `"v1.7.0"`).

    Reads `backend/VERSION` on first call, caches the result for
    the lifetime of the process. Returns `"dev"` if the file is
    missing or empty — that's the right shape for local checkouts
    that haven't been deployed.
    """
    global _cached
    if _cached is not None:
        return _cached
    try:
        raw = _VERSION_PATH.read_text().strip()
    except (FileNotFoundError, OSError):
        _cached = _FALLBACK
        return _cached
    _cached = raw or _FALLBACK
    return _cached


def _reset_cache_for_tests() -> None:
    """Test-only: clear the cached value so a fixture can swap
    the VERSION file content between tests. Not part of the
    public surface — leading underscore is the convention."""
    global _cached
    _cached = None
