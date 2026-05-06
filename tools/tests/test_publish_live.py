"""Live round-trip test for `catalog publish` + `catalog fetch`.

Skipped unless a local stra2us is reachable. Expects env:

    STRA2US_HOST          e.g. http://127.0.0.1:8153
    STRA2US_CLIENT_ID     a client with rw on `_catalog/*`
    STRA2US_SECRET_HEX    matching secret

Run manually:
    STRA2US_HOST=... STRA2US_CLIENT_ID=... STRA2US_SECRET_HEX=... \\
        pytest tests/test_publish_live.py -v

CI leaves these unset, so the test no-ops there.
"""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path

import pytest
import requests

from stra2us_cli.catalog import load_catalog
from stra2us_cli.client import Stra2usClient


def _env_ready() -> bool:
    return all(
        os.environ.get(k)
        for k in ("STRA2US_HOST", "STRA2US_CLIENT_ID", "STRA2US_SECRET_HEX")
    )


pytestmark = pytest.mark.skipif(
    not _env_ready(),
    reason="needs STRA2US_HOST/CLIENT_ID/SECRET_HEX pointing at a live server",
)


def _client() -> Stra2usClient:
    host = os.environ["STRA2US_HOST"].rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return Stra2usClient(
        base_url=host,
        client_id=os.environ["STRA2US_CLIENT_ID"],
        secret_hex=os.environ["STRA2US_SECRET_HEX"],
    )


def test_publish_fetch_roundtrip(tmp_path: Path) -> None:
    # Unique per-run app name so parallel runs don't collide. Still under
    # the `_catalog/*` ACL prefix — make sure the test client has rw there.
    app = f"pytest_{uuid.uuid4().hex[:8]}"
    yaml_body = textwrap.dedent(f"""\
        app: {app}

        vars:
          sample_interval_sec:
            type: int
            default: 30
            scope: [app, device]
            range: [5, 600]
            help: roundtrip test key
    """)
    path = tmp_path / f"{app}.s2s.yaml"
    path.write_text(yaml_body)

    # Validate locally (what `catalog publish` does before upload).
    cat = load_catalog(path)
    assert cat.app == app

    client = _client()
    stash_key = f"_catalog/{app}"
    client.put(stash_key, yaml_body)

    fetched = client.get(stash_key)
    assert fetched == yaml_body, "published YAML must round-trip byte-for-byte"


def test_fetch_missing_returns_none() -> None:
    client = _client()
    missing = f"_catalog/pytest_missing_{uuid.uuid4().hex[:8]}"
    assert client.get(missing) is None


def test_publish_rejects_malformed(tmp_path: Path) -> None:
    """Malformed catalog must fail validation *before* any network call."""
    path = tmp_path / "bad.s2s.yaml"
    path.write_text("app: bad\nvars:\n  k:\n    type: int\n    default: 'not-an-int'\n")
    with pytest.raises(Exception):
        load_catalog(path)
