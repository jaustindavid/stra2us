# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the lint-into-publish wiring (P1 first commit, deferred
from P0 — `docs/fr_catalog_app_ui_progress.md` "Items deferred /
followups").

These don't talk to a live server. We monkeypatch `_build_client` to
return a recording stub, then assert that:

* a clean catalog publishes (the stub records a `put` call);
* a broken catalog fails with exit code 5 *before* any `put`;
* warnings print but pass through (publish succeeds);
* lint sees the local `_assets/` directory when one exists alongside
  the catalog file.

The "catalog publish" tests against a real server live in
`test_publish_live.py` (skipped unless a host is configured).
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import pytest

from stra2us_cli import cli as cli_module


# ----- recording stub -----

class _RecordingClient:
    """Minimal Stra2usClient stand-in. Honors read-after-write
    (publish_assets re-reads every PUT) and records puts/deletes
    so tests can assert "publish actually called the network" or
    "publish bailed before reaching the network." `base_url` exists
    because `cmd_catalog_publish` references it in the success
    message."""

    def __init__(self):
        self.base_url = "http://test"
        self.puts: list[tuple[str, object]] = []
        self.deletes: list[str] = []
        self._store: dict[str, object] = {}

    def put(self, key, value, encrypted=False):
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
    """Build the argparse.Namespace shape `cmd_catalog_publish` reads."""
    return argparse.Namespace(
        catalog=str(catalog_path),
        profile=None, server=None, client_id=None, secret_hex=None,
    )


def _write_catalog(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "demo.s2s.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# ----- happy path -----

def test_clean_catalog_publishes(tmp_path, stub_client):
    """Catalog without an `_assets/` directory takes the pre-P1
    publish path (just the catalog YAML, no index, no GC). This
    preserves backward compatibility for legacy catalogs and
    avoids silently nuking assets from a previous publish if a
    user republishes from a working tree that doesn't carry the
    `_assets/` dir."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app], default: 1}
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    assert [k for k, _ in stub_client.puts] == ["_catalog/demo"]
    assert "vars:" in stub_client._store["_catalog/demo"]


def test_empty_assets_dir_clears_bundle(tmp_path, stub_client):
    """An *empty* `_assets/` dir is the explicit signal for "no
    assets in this bundle" — runs the full pipeline including GC.
    Distinguishes from "no _assets/ dir at all" which means
    "assets unmanaged by this publish, leave prior bundle alone."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app], default: 1}
    """)
    (tmp_path / "_assets").mkdir()
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    assert [k for k, _ in stub_client.puts] == [
        "_catalog/demo",
        "_catalog/demo/_assets_index",
    ]
    assert stub_client._store["_catalog/demo/_assets_index"] == []


def test_clean_catalog_with_theme_and_ui(tmp_path, stub_client):
    """The full P0 surface — theme, ui, every UI hint — should pass
    publish-time lint when paired with a sibling `_assets/logo.svg`."""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          primary_color: "#5b3fb8"
          font_family: system-ui
          logo_asset: logo.svg
        ui:
          header_markdown: "## hi"
        vars:
          mode:
            type: string
            scope: [app]
            enum: [a, b, c]
    """)
    (tmp_path / "_assets").mkdir()
    (tmp_path / "_assets" / "logo.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<circle cx="5" cy="5" r="3"/></svg>'
    )
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    # Three writes: asset bytes, asset .meta, catalog YAML, index = 4.
    assert len(stub_client.puts) == 4
    keys = [k for k, _ in stub_client.puts]
    assert keys == [
        "_catalog/demo/_assets/logo.svg",
        "_catalog/demo/_assets/logo.svg.meta",
        "_catalog/demo",
        "_catalog/demo/_assets_index",
    ]


# ----- lint rejections -----

def test_broken_catalog_fails_before_publish(tmp_path, stub_client, capsys):
    """Numeric `enum` + `min`/`max` is mutually exclusive. Lint should
    catch it; publish should bail with exit code 5; client.put should
    never be called."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          n:
            type: int
            scope: [app]
            enum: [1, 2, 3]
            min: 0
            max: 10
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []  # no network call
    err = capsys.readouterr().err
    assert "catalog lint failed" in err
    assert "mutually exclusive" in err


def test_bad_theme_color_fails(tmp_path, stub_client, capsys):
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          primary_color: "purple"
        vars:
          x: {type: int, scope: [app]}
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []
    assert "theme.primary_color" in capsys.readouterr().err


def test_oversized_help_markdown_fails(tmp_path, stub_client, capsys):
    big = "x" * 5000  # exceeds STRA2US_MARKDOWN_MAX_BYTES (4096)
    p = _write_catalog(tmp_path, f"""
        app: demo
        vars:
          s:
            type: string
            scope: [app]
            help_markdown: "{big}"
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []


def test_logo_asset_missing_from_bundle_fails(tmp_path, stub_client, capsys):
    """When an `_assets/` directory exists alongside the catalog, lint
    runs with the asset listing and fails references that don't
    resolve. (When no `_assets/` directory exists, the listing is None
    and the check is skipped — different semantics, tested below.)"""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: logo.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    (tmp_path / "_assets").mkdir()  # exists, but empty
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 5
    assert stub_client.puts == []
    assert "not in bundle" in capsys.readouterr().err


def test_logo_asset_skips_existence_check_when_no_assets_dir(tmp_path, stub_client):
    """No `_assets/` directory → asset_listing is None → existence
    check is skipped (the syntactic shape is still validated). This
    is the "publish a catalog whose assets ship out-of-band" case;
    P1's asset pipeline tightens this when an `_assets/` directory is
    present alongside the catalog."""
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: logo.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    # No `_assets/` dir → just the catalog YAML, asset bundle untouched.
    assert [k for k, _ in stub_client.puts] == ["_catalog/demo"]


# ----- warnings -----

def test_warnings_print_but_publish_succeeds(tmp_path, stub_client, capsys):
    """`widget: slider` without min+max is a warning; publish should
    still succeed."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          n:
            type: int
            scope: [app]
            widget: slider
    """)
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    # Just catalog YAML — warnings don't change publish shape.
    assert [k for k, _ in stub_client.puts] == ["_catalog/demo"]
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "vars.n.widget" in err


def test_unused_asset_warns_does_not_fail(tmp_path, stub_client, capsys):
    p = _write_catalog(tmp_path, """
        app: demo
        theme:
          logo_asset: logo.svg
        vars:
          x: {type: int, scope: [app]}
    """)
    (tmp_path / "_assets").mkdir()
    (tmp_path / "_assets" / "logo.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"><circle cx="1" cy="1" r="1"/></svg>'
    )
    (tmp_path / "_assets" / "stray.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rc = cli_module.cmd_catalog_publish(_args(p))
    assert rc == 0
    err = capsys.readouterr().err
    assert "stray.png" in err
    assert "warning" in err


# ----- catalog lint subcommand (v1.6.4) -----------------------------
# `cmd_catalog_lint` runs the same lint passes as publish but stops
# before the network call. These tests pin the exit-code contract
# (0 clean, 5 errors, 6 asset-pipeline failure — same as publish)
# and confirm the dispatch is wired through `_dispatch_catalog`.
# No `stub_client` fixture: the lint command never builds a client,
# and the test should fail loudly if it ever does.

def test_lint_clean_catalog_returns_zero(tmp_path, capsys):
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app], default: 1}
    """)
    rc = cli_module.cmd_catalog_lint(_args(p))
    assert rc == 0
    out = capsys.readouterr().out
    # Confirmation line so an interactive operator gets a positive
    # signal instead of silence.
    assert "demo: lint OK" in out


def test_lint_broken_catalog_returns_5(tmp_path, capsys):
    """Same broken-catalog shape as `test_broken_catalog_fails_before_publish`
    above — `widget: secret` on a non-string field is a lint error."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          n: {type: int, scope: [app], widget: secret}
    """)
    rc = cli_module.cmd_catalog_lint(_args(p))
    assert rc == 5
    err = capsys.readouterr().err
    assert "catalog lint failed" in err
    assert "vars.n.widget" in err


def test_lint_warning_does_not_fail(tmp_path, capsys):
    """Warnings print to stderr but don't change the exit code —
    matches publish's "block on errors, pass through warnings" contract."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          wifi:
            type: string
            scope: [app]
            encrypted: true
    """)
    rc = cli_module.cmd_catalog_lint(_args(p))
    assert rc == 0
    captured = capsys.readouterr()
    assert "demo: lint OK (1 warning)" in captured.out
    assert "vars.wifi.encrypted" in captured.err
    assert "without `widget: secret`" in captured.err


def test_lint_dispatch_via_main(tmp_path, capsys):
    """End-to-end through `main(['catalog', 'lint', '--catalog', PATH])`,
    confirming the subparser + dispatch wiring (not just the cmd_*
    function being callable directly)."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app]}
    """)
    rc = cli_module.main(["--catalog", str(p), "catalog", "lint"])
    assert rc == 0
    assert "demo: lint OK" in capsys.readouterr().out
