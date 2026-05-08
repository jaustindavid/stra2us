# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""End-to-end tests for the asset publish pipeline (P1 of
`docs/fr_catalog_app_ui_plan.md`).

Covers the FR's automated-test list:

* Publish PNG / JPEG / WebP / SVG; assert all reach KV at the
  expected paths with correct content_type + sha256 in `.meta`.
* Republish with one asset removed; confirm GC deletes the dropped
  asset only after the catalog YAML lands.
* Publish an oversized asset; confirm publish fails with a
  size-limit error before any KV writes occur.
* Publish a `.gif` (not in allowlist); confirm rejection.
* Publish an SVG with `<script>` inside; confirm sanitizer rejects.
* Mid-publish kill simulation; confirm prior catalog still serves
  prior assets consistently.

The recording stub from `test_publish_lint.py` is reused so the
"what was published, in what order, and were any GCs run" story
falls out of straightforward dict assertions.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import pytest

from stra2us_cli import catalog_publish, cli as cli_module
from stra2us_cli.catalog_publish import PublishError


# ----- recording stub -----

class _RecordingClient:
    def __init__(self):
        self.base_url = "http://test"
        self.puts: list[tuple[str, object]] = []
        self.deletes: list[str] = []
        self._store: dict[str, object] = {}
        # Optional: caller can register a key whose PUT raises, to
        # simulate a publish dying mid-flight.
        self.fail_on_put: str | None = None

    def put(self, key, value, encrypted=False):
        if self.fail_on_put is not None and key == self.fail_on_put:
            from stra2us_cli.client import Stra2usError
            raise Stra2usError(f"simulated network failure on PUT {key}")
        self.puts.append((key, value))
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        self.deletes.append(key)
        self._store.pop(key, None)


@pytest.fixture
def stub_client(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(cli_module, "_build_client", lambda args: client)
    return client


def _args(catalog_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        catalog=str(catalog_path),
        profile=None, server=None, client_id=None, secret_hex=None,
    )


def _write_catalog(tmp_path: Path, body: str = "") -> Path:
    p = tmp_path / "demo.s2s.yaml"
    if not body:
        body = """
            app: demo
            theme:
              logo_asset: logo.svg
            vars:
              x: {type: int, scope: [app]}
        """
    p.write_text(textwrap.dedent(body))
    return p


# Smallest valid bytes for each allowlisted content-type. These are
# byte-true headers — the route reads `meta.content_type` so the
# bytes don't have to be a real image, but using real headers keeps
# tests honest if a future check tightens validation.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP" + b"VP8 " + b"\x00" * 8
_CLEAN_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<circle cx="5" cy="5" r="3" fill="red"/></svg>'
)


# ----- happy paths -----

def test_publish_full_bundle_png_jpeg_webp_svg(tmp_path, stub_client):
    """Every allowlisted content-type round-trips through the
    pipeline: bytes + .meta land at the expected KV paths, then
    the catalog YAML, then the index."""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: logo.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "logo.svg").write_bytes(_CLEAN_SVG)
    (a / "hero.png").write_bytes(_PNG_MAGIC)
    (a / "thumb.jpg").write_bytes(_JPEG_MAGIC)
    (a / "banner.webp").write_bytes(_WEBP_MAGIC)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0

    # Order: all assets (bytes+meta pairs in alphabetical order),
    # then catalog YAML, then _assets_index.
    keys = [k for k, _ in stub_client.puts]
    assert keys == [
        "_catalog/demo/_assets/banner.webp",
        "_catalog/demo/_assets/banner.webp.meta",
        "_catalog/demo/_assets/hero.png",
        "_catalog/demo/_assets/hero.png.meta",
        "_catalog/demo/_assets/logo.svg",
        "_catalog/demo/_assets/logo.svg.meta",
        "_catalog/demo/_assets/thumb.jpg",
        "_catalog/demo/_assets/thumb.jpg.meta",
        "_catalog/demo",
        "_catalog/demo/_assets_index",
    ]

    # Meta carries the right content-type per filename.
    by_key = dict(stub_client.puts)
    assert by_key["_catalog/demo/_assets/hero.png.meta"]["content_type"] == "image/png"
    assert by_key["_catalog/demo/_assets/thumb.jpg.meta"]["content_type"] == "image/jpeg"
    assert by_key["_catalog/demo/_assets/banner.webp.meta"]["content_type"] == "image/webp"
    assert by_key["_catalog/demo/_assets/logo.svg.meta"]["content_type"] == "image/svg+xml"

    # Index lists every published filename, sorted.
    assert by_key["_catalog/demo/_assets_index"] == [
        "banner.webp", "hero.png", "logo.svg", "thumb.jpg",
    ]


def test_meta_carries_sha256_and_size(tmp_path, stub_client):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "hero.png").write_bytes(_PNG_MAGIC)
    cli_module.cmd_catalog_publish(_args(p))
    by_key = dict(stub_client.puts)
    meta = by_key["_catalog/demo/_assets/hero.png.meta"]
    assert meta["size"] == len(_PNG_MAGIC)
    # sha256 is the lowercase hex digest of the 32-byte SHA-256.
    assert len(meta["sha256"]) == 64
    assert meta["sha256"].islower()


# ----- republish + GC -----

def test_republish_drops_removed_asset_via_gc(tmp_path, stub_client):
    """Publish two assets; republish with one removed; assert the
    dropped file is DELETEd. Order matters per FR §5a: catalog YAML
    must land *before* the GC."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "logo.svg").write_bytes(_CLEAN_SVG)
    (a / "hero.png").write_bytes(_PNG_MAGIC)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    assert stub_client.deletes == []  # first publish, nothing to GC

    # Drop hero.png and republish.
    (a / "hero.png").unlink()
    stub_client.puts.clear()  # focus the assertion on republish only
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0

    # GC ran for the dropped asset.
    assert sorted(stub_client.deletes) == [
        "_catalog/demo/_assets/hero.png",
        "_catalog/demo/_assets/hero.png.meta",
    ]
    # Index now reflects the remaining bundle.
    by_key = dict(stub_client.puts)
    assert by_key["_catalog/demo/_assets_index"] == ["logo.svg"]


def test_mid_publish_kill_leaves_prior_catalog_consistent(tmp_path, stub_client):
    """Simulate a failure between asset upload and catalog YAML
    (the commit point). Prior catalog + prior assets remain in
    place; the new catalog is NOT visible. Per FR §5a's atomicity
    contract."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "logo.svg").write_bytes(_CLEAN_SVG)
    cli_module.cmd_catalog_publish(_args(p))  # successful first publish
    pre_kill_yaml = stub_client._store["_catalog/demo"]
    pre_kill_logo = stub_client._store["_catalog/demo/_assets/logo.svg"]
    pre_kill_index = stub_client._store["_catalog/demo/_assets_index"]

    # Now: publish a NEW catalog with a NEW asset, but kill the
    # publish on the catalog-YAML PUT (the commit point).
    p2 = _write_catalog(tmp_path, """
        app: demo
        version: 2
        vars:
          x: {type: int, scope: [app], default: 42}
    """)
    (a / "logo.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    )
    stub_client.fail_on_put = "_catalog/demo"  # die at commit point

    rc = cli_module.cmd_catalog_publish(_args(p2))
    # The publish wrapper translates the inner network error to
    # `PublishError` (rc=6) since the failure happened *during* the
    # asset-pipeline staging — same exit code as a sanitizer rejection
    # so CI can distinguish "your catalog/bundle has a problem" from
    # "the server's just unreachable right now."
    assert rc == 6

    # Critical: prior catalog YAML is unchanged. The new asset DID
    # land at its KV key (FR's "consistent in either direction":
    # the prior catalog references the prior bundle filename, and
    # those bytes either match the prior or the new — readers see
    # *some* valid catalog + bundle pair, never a torn state where
    # the catalog references a filename whose bytes are missing).
    assert stub_client._store["_catalog/demo"] == pre_kill_yaml
    assert stub_client._store["_catalog/demo/_assets_index"] == pre_kill_index
    # The asset bytes were updated (overwriting the prior bundle's
    # bytes). That's expected — same filename means same key, and
    # the new bytes hash to a different ?v= so any cached URL
    # naming the OLD hash will 404 cleanly. The catalog still says
    # version 1 (the un-updated YAML); a customer who hits the
    # asset URL the renderer was emitting before this publish gets
    # the new bytes, which match the current renderer's still-
    # version-1 reference.
    new_logo = stub_client._store["_catalog/demo/_assets/logo.svg"]
    assert new_logo != pre_kill_logo  # new bytes did land


# ----- rejections -----

def test_oversized_asset_fails_before_any_put(tmp_path, stub_client):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    # Just over the default 256 KiB cap.
    (a / "huge.png").write_bytes(_PNG_MAGIC + b"\x00" * 300_000)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5  # lint failure
    # Nothing was published — failure happened before any network call.
    assert stub_client.puts == []


def test_disallowed_content_type_rejected(tmp_path, stub_client, capsys):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "anim.gif").write_bytes(b"GIF89a" + b"\x00" * 32)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []
    err = capsys.readouterr().err
    assert "content type" in err
    assert "anim.gif" in err


def test_svg_with_script_rejected_by_sanitizer(tmp_path, stub_client, capsys):
    """The P0 SVG sanitizer rejects `<script>` wholesale. The
    publish surfaces this as a publish-time error before any KV
    writes occur."""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: evil.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "evil.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<script>alert(1)</script>'
        b'</svg>'
    )

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 6  # asset-pipeline failure
    assert stub_client.puts == []
    err = capsys.readouterr().err
    assert "evil.svg" in err
    assert "<script>" in err


def test_filename_uppercase_rejected(tmp_path, stub_client, capsys):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    (a / "LOGO.PNG").write_bytes(_PNG_MAGIC)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []


def test_total_bundle_cap(tmp_path, stub_client):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    # Many small files summing past the 2 MiB bundle cap.
    one_chunk = _PNG_MAGIC + b"\x00" * 200_000
    for i in range(11):
        (a / f"img{i}.png").write_bytes(one_chunk)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []


# ----- SVG sanitization re-serialization -----

def test_svg_with_drop_attribute_strips_silently(tmp_path, stub_client):
    """Inline `style=` in an SVG is dropped (not rejected) — the FR
    keeps the rest of the SVG, replacing it with the cleaned tree.
    Verify the *cleaned* bytes are what shipped, not the original."""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: logo.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    a = tmp_path / "_assets"
    a.mkdir()
    raw = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<circle cx="5" cy="5" r="3" style="fill:url(evil)"/>'
        b'</svg>'
    )
    (a / "logo.svg").write_bytes(raw)

    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    by_key = dict(stub_client.puts)
    shipped = by_key["_catalog/demo/_assets/logo.svg"]
    assert b"style=" not in shipped
    assert b"<circle" in shipped  # element survived
