# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for v1.6.7's `stra2us set` catalog-honors-encrypted change.

Pre-v1.6.7 the operator's `--encrypted` flag drove encryption on
every `stra2us set` call. The catalog's `encrypted: true` field was
documentation-only — Stra2us itself didn't act on it.

v1.6.7 makes the catalog authoritative for the catalog-aware write
path: `stra2us set` reads `var.encrypted` from the catalog and
ignores the `--encrypted` flag. Mismatches surface as a stderr
warning (catalog says no, operator passed flag) or info (catalog
says yes, operator didn't bother with the flag — it's auto). The
raw-KV `stra2us put` path is unchanged: no catalog consulted,
operator-controlled `--encrypted`.

Tests pin all four scenarios.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import pytest

from stra2us_cli import cli as cli_module


# ----- recording stub that tracks the encrypted flag --------------

class _RecordingClient:
    """Minimal Stra2usClient stand-in tracking (key, value, encrypted)
    per put. The existing `_RecordingClient` in test_publish_lint.py
    drops the encrypted bit on the floor — fine for publish tests
    but useless for ours. Local copy with the extra column."""

    def __init__(self):
        self.base_url = "http://test"
        self.puts: list[tuple[str, object, bool]] = []

    def put(self, key, value, encrypted=False):
        self.puts.append((key, value, encrypted))

    def get(self, key):
        return None

    def delete(self, key):
        pass


@pytest.fixture
def stub_client(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(cli_module, "_build_client", lambda args: client)
    return client


def _args(catalog_path: Path, *, target: str, key: str, value: str | None,
          encrypted: bool = False, unset: bool = False) -> argparse.Namespace:
    """Build the argparse.Namespace shape `cmd_set` reads."""
    return argparse.Namespace(
        catalog=str(catalog_path),
        target=target, key=key, value=value,
        encrypted=encrypted, unset=unset,
        profile=None, server=None, client_id=None, secret_hex=None,
    )


def _write_catalog(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "demo.s2s.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# ----- the four scenarios ------------------------------------------

def test_catalog_encrypted_true_no_flag_auto_encrypts(
    tmp_path, stub_client, capsys
):
    """Catalog declares `encrypted: true` and the operator omits
    `--encrypted`. Result: `put(encrypted=True)` is called (catalog
    drives), and an info-level stderr message tells the operator
    they didn't need the flag for catalog keys."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          wifi_password:
            type: string
            scope: [app, device]
            encrypted: true
    """)
    rc = cli_module.cmd_set(_args(
        p, target="dev1", key="wifi_password",
        value="hunter2", encrypted=False,
    ))
    assert rc == 0
    assert len(stub_client.puts) == 1
    key, value, encrypted = stub_client.puts[0]
    assert encrypted is True, "catalog drives — should encrypt regardless of flag"
    assert value == "hunter2"

    err = capsys.readouterr().err
    assert "info:" in err
    assert "encrypted" in err.lower()


def test_catalog_encrypted_true_with_flag_silent_agreement(
    tmp_path, stub_client, capsys
):
    """Catalog says encrypted, operator also passed `--encrypted`.
    Both agree — no info, no warning, just encrypt."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          wifi_password:
            type: string
            scope: [app, device]
            encrypted: true
    """)
    rc = cli_module.cmd_set(_args(
        p, target="dev1", key="wifi_password",
        value="hunter2", encrypted=True,
    ))
    assert rc == 0
    assert stub_client.puts[0][2] is True

    err = capsys.readouterr().err
    # No noise when operator and catalog align.
    assert "warning:" not in err
    assert "info:" not in err


def test_catalog_encrypted_false_with_flag_warns_and_ignores(
    tmp_path, stub_client, capsys
):
    """Catalog says NOT encrypted, operator passed `--encrypted`.
    The flag is ignored (catalog drives) and a stderr warning
    surfaces the policy: catalog wins, use `stra2us put` for
    ad-hoc raw KV writes."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          greeting:
            type: string
            scope: [app, device]
    """)
    rc = cli_module.cmd_set(_args(
        p, target="dev1", key="greeting",
        value="hello", encrypted=True,
    ))
    assert rc == 0
    assert stub_client.puts[0][2] is False, (
        "catalog says not-encrypted; --encrypted flag must be ignored"
    )

    err = capsys.readouterr().err
    assert "warning:" in err
    assert "stra2us put" in err  # mentions the escape hatch
    assert "ignored" in err.lower()


def test_catalog_encrypted_false_no_flag_quiet(
    tmp_path, stub_client, capsys
):
    """Catalog says not-encrypted, operator omitted the flag. The
    common case for non-secret fields. No noise, just plaintext."""
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          greeting:
            type: string
            scope: [app, device]
    """)
    rc = cli_module.cmd_set(_args(
        p, target="dev1", key="greeting",
        value="hello", encrypted=False,
    ))
    assert rc == 0
    assert stub_client.puts[0][2] is False

    err = capsys.readouterr().err
    assert "warning:" not in err
    assert "info:" not in err


# ----- raw-KV path (stra2us put) is unchanged ----------------------

def test_cmd_put_still_uses_flag_directly(tmp_path, stub_client):
    """`stra2us put` is the raw-KV path — no catalog consulted,
    `--encrypted` operator-controlled. v1.6.7's catalog-honors
    change applies ONLY to `stra2us set` (the catalog-aware path).
    `put` behavior unchanged so non-catalog data + clients
    implementing their own encryption discipline still work."""
    # `put` doesn't need a catalog, but the CLI still loads it to
    # report the app. We give a minimal one.
    p = _write_catalog(tmp_path, """
        app: demo
        vars:
          x: {type: int, scope: [app], default: 1}
    """)
    args = argparse.Namespace(
        catalog=str(p),
        key="any/raw/path", value="raw-value", file=None,
        encrypted=True,
        profile=None, server=None, client_id=None, secret_hex=None,
    )
    rc = cli_module.cmd_put(args)
    assert rc == 0
    # The flag flows directly through to client.put, no catalog
    # consultation.
    assert stub_client.puts[0][2] is True
