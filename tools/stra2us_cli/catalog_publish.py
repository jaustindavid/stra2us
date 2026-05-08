# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Asset bundle handling for `stra2us catalog publish`
(P1 of `docs/fr_catalog_app_ui_plan.md`,
`docs/fr_catalog_app_ui.md` "Assets (self-hosted images)").

Responsibilities:

* Discover the `_assets/` directory next to the catalog YAML.
* Sniff each asset's content type (filename suffix; allowlist
  enforced by lint).
* Compute sha256 + size for the `.meta` sidecar.
* Run SVGs through the P0 sanitizer; rejected SVGs fail the publish.
* Push the bundle to the server in the FR's order:
    1. PUT each asset's bytes + .meta (re-read each to verify).
    2. PUT catalog YAML (the commit point).
    3. PUT updated `_assets_index` listing.
    4. DELETE files dropped from the bundle since the previous
       publish.

A publish that dies between (1) and (2) leaves the prior catalog
pointing at the prior assets — consistent. A publish that dies
during (4) leaves stale assets, which the next publish cleans up
via the index diff. The index is the cheap mechanism that lets
the CLI know "what was published last time" without a server-side
list endpoint.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .catalog_lint import (
    ASSET_FILENAME_RE,
    Asset,
    LintIssue,
    errors as lint_errors,
    lint_asset_bundle,
    warnings as lint_warnings,
)
from .client import Stra2usClient, Stra2usError
from .sanitizers import SvgSanitizeError, sanitize_svg


# Filename suffix → content type. The mapping is intentionally tiny:
# the FR's allowlist is svg / png / jpeg / webp; lint rejects anything
# else by content-type. Keep this in lockstep with
# `STRA2US_ASSET_CONTENT_TYPES` in catalog_lint.py.
_SUFFIX_TO_CONTENT_TYPE = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


# Server-side KV layout for the bundle. See `docs/fr_catalog_app_ui.md`
# "Storage layout" — keys are siblings of the bare `_catalog/<app>`
# catalog YAML key, under a reserved `_assets/` segment + `.meta`
# / `_assets_index` siblings for metadata + GC bookkeeping.
def _bytes_key(app: str, filename: str) -> str:
    return f"_catalog/{app}/_assets/{filename}"


def _meta_key(app: str, filename: str) -> str:
    return f"_catalog/{app}/_assets/{filename}.meta"


def _index_key(app: str) -> str:
    return f"_catalog/{app}/_assets_index"


@dataclass
class LoadedAsset:
    """An asset on disk, prepared for upload. `bytes_payload` may
    differ from the file's bytes when the asset is an SVG that the
    sanitizer rewrote (re-serialized clean tree)."""
    filename: str
    content_type: str
    bytes_payload: bytes
    sha256_hex: str
    size_bytes: int


class PublishError(RuntimeError):
    """Asset-pipeline failure — sanitizer rejection, network error
    during the staged upload, or a verification mismatch on re-read.
    Carries a human-readable message; the CLI prints it and exits
    nonzero."""


def _content_type_for(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    return _SUFFIX_TO_CONTENT_TYPE.get(suffix)


def discover_assets(asset_dir: Path) -> list[LoadedAsset]:
    """Walk `_assets/` and return one LoadedAsset per file.

    SVGs go through the P0 sanitizer in this pass; rejected SVGs
    raise `PublishError` here, before any network activity.

    Files without a recognized suffix are still loaded with
    `content_type=None`; lint catches them downstream and produces
    a clear "content type … not in allowlist" message.
    """
    out: list[LoadedAsset] = []
    for path in sorted(asset_dir.iterdir()):
        if not path.is_file():
            # Skip subdirectories. `_assets/` is intentionally flat —
            # the URL space stays sane only if filenames don't carry
            # additional path segments. Lint enforces the same shape
            # via ASSET_FILENAME_RE on each entry.
            continue
        filename = path.name
        raw = path.read_bytes()
        ctype = _content_type_for(filename)
        if ctype == "image/svg+xml":
            try:
                # The sanitizer's output is the *re-serialized clean
                # tree*, not the original bytes — a vendor who ships
                # a benign SVG with novel features will see those
                # features dropped on the way through. That's the
                # P0 contract; reuse it verbatim.
                cleaned = sanitize_svg(raw)
            except SvgSanitizeError as e:
                raise PublishError(
                    f"_assets/{filename}: SVG rejected by sanitizer: {e}"
                ) from e
            payload = cleaned
        else:
            payload = raw
        # Hash the *payload* (post-sanitization for SVGs) so the
        # cache-bust ?v= matches the bytes the server actually
        # returns.
        sha256_hex = hashlib.sha256(payload).hexdigest()
        out.append(LoadedAsset(
            filename=filename,
            content_type=ctype or "application/octet-stream",
            bytes_payload=payload,
            sha256_hex=sha256_hex,
            size_bytes=len(payload),
        ))
    return out


def lint_loaded(assets: list[LoadedAsset]) -> list[LintIssue]:
    """Adapt `LoadedAsset` → the lint module's `Asset` record and
    run `lint_asset_bundle`. Returns the lint issues for the caller
    to print + decide whether to fail."""
    return lint_asset_bundle([
        Asset(filename=a.filename,
              content_type=a.content_type,
              size_bytes=a.size_bytes)
        for a in assets
    ])


def _existing_index(client: Stra2usClient, app: str) -> list[str]:
    """Read the prior `_assets_index` listing. Returns [] if the key
    is absent (first publish) or the value isn't shaped like a
    listing (defensive: avoid a bad sidecar from blocking a publish).
    """
    try:
        value = client.get(_index_key(app))
    except Stra2usError:
        # If the read itself fails (network error, signature mismatch),
        # treat it as "we don't know the prior listing" rather than
        # blocking the publish. GC won't run; orphans accumulate
        # until the next clean publish. Documented tradeoff.
        return []
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return value
    return []


def _put_and_verify(client: Stra2usClient, key: str, value: object,
                    *, label: str) -> None:
    """PUT then re-read; raise if the re-read doesn't match. The FR
    relies on the single-node Redis read-after-write guarantee
    (`docs/fr_catalog_app_ui.md` "Read-after-write assumption"). If
    the deployment shape ever changes this routine is the place to
    pin the re-read to the write target."""
    try:
        client.put(key, value)
    except Stra2usError as e:
        raise PublishError(f"{label}: PUT {key} failed: {e}") from e
    try:
        observed = client.get(key)
    except Stra2usError as e:
        raise PublishError(f"{label}: re-read {key} failed: {e}") from e
    if observed != value:
        raise PublishError(
            f"{label}: re-read mismatch on {key} (wrote {type(value).__name__}, "
            f"got {type(observed).__name__})"
        )


def publish_assets(
    client: Stra2usClient,
    app: str,
    assets: list[LoadedAsset],
    yaml_text: str,
) -> dict:
    """Run the FR's publish order. Returns a small dict summarizing
    what happened — the CLI uses it to print the success message.

    Steps:
      1. PUT each asset bytes + .meta. Verify by re-read.
      2. PUT catalog YAML (the commit point). Verify.
      3. PUT updated `_assets_index`. Verify.
      4. DELETE files in (old_index - new_index).

    Raises `PublishError` on any failure. A failure between steps 1
    and 2 leaves prior catalog + prior assets consistent. A failure
    after step 2 leaves the new catalog live; subsequent publishes
    reconcile via the index diff.
    """
    new_listing = sorted(a.filename for a in assets)
    old_listing = _existing_index(client, app)

    # --- step 1: assets ---
    for a in assets:
        meta = {
            "content_type": a.content_type,
            "sha256": a.sha256_hex,
            "size": a.size_bytes,
        }
        # PUT bytes first, then meta — readers either see both
        # (consistent) or only-bytes (acceptable: serve route falls
        # back to `application/octet-stream`).
        _put_and_verify(client, _bytes_key(app, a.filename),
                        a.bytes_payload, label=f"_assets/{a.filename}")
        _put_and_verify(client, _meta_key(app, a.filename),
                        meta, label=f"_assets/{a.filename}.meta")

    # --- step 2: catalog YAML (commit point) ---
    _put_and_verify(client, f"_catalog/{app}", yaml_text, label="catalog YAML")

    # --- step 3: index ---
    _put_and_verify(client, _index_key(app), new_listing,
                    label="_assets_index")

    # --- step 4: GC ---
    dropped = sorted(set(old_listing) - set(new_listing))
    for filename in dropped:
        try:
            client.delete(_bytes_key(app, filename))
            client.delete(_meta_key(app, filename))
        except Stra2usError as e:
            # Don't fail the publish on GC errors — the catalog is
            # already live with the new listing. Surface the issue
            # so the operator knows orphans exist.
            raise PublishError(
                f"GC: failed to delete dropped asset {filename}: {e}. "
                "Catalog is published; orphans remain in KV."
            ) from e

    return {
        "assets_uploaded": len(assets),
        "assets_dropped": len(dropped),
        "dropped_filenames": dropped,
    }
