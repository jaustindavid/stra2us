# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Live roundtrip tests for per-key encrypted KV values.

Skipped unless a local stra2us is reachable — same env contract as
test_publish_live.py:

    STRA2US_HOST          e.g. http://127.0.0.1:8153
    STRA2US_CLIENT_ID     a client with rw on `_test/*`
    STRA2US_SECRET_HEX    matching secret

The test client needs rw on `_test/*` (or whatever prefix you change
TEST_PREFIX to). CI leaves the env unset, so the tests no-op there.
"""

from __future__ import annotations

import os
import uuid

import msgpack
import pytest
import requests

from stra2us_cli.client import (
    KVENC_EXT_TYPE,
    Stra2usClient,
)


TEST_PREFIX = "_test"


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


def _unique_key() -> str:
    return f"{TEST_PREFIX}/enc_{uuid.uuid4().hex[:8]}"


def test_encrypted_roundtrip_string():
    """A string written with encrypted=True comes back as plaintext via
    the standard get() — the CLI auto-decrypts ext type 0x21."""
    client = _client()
    key = _unique_key()
    plaintext = "hunter2-very-confidential"
    try:
        client.put(key, plaintext, encrypted=True)
        assert client.get(key) == plaintext
    finally:
        client.delete(key)


def test_encrypted_long_value_crosses_block_boundary():
    """WPA2 passwords go up to 63 chars; make sure multi-block keystream works."""
    client = _client()
    key = _unique_key()
    plaintext = "x" * 63
    try:
        client.put(key, plaintext, encrypted=True)
        assert client.get(key) == plaintext
    finally:
        client.delete(key)


def test_plaintext_unaffected_by_encryption_path():
    """Records written without encrypted=True must NOT come back as ext 0x21,
    even after another key in the same namespace is encrypted. Pins that
    the sidecar is per-key, not global."""
    client = _client()
    enc_key = _unique_key()
    plain_key = _unique_key()
    try:
        client.put(enc_key, "secret", encrypted=True)
        client.put(plain_key, "not-a-secret")

        # Round-trip via the high-level get (would auto-decrypt either way)
        assert client.get(plain_key) == "not-a-secret"

        # And confirm the wire form for the plaintext key isn't an ext type:
        # do a raw HTTP fetch and inspect the msgpack header byte.
        body = _raw_get(client, plain_key)
        decoded = msgpack.unpackb(body, raw=False)
        assert not isinstance(decoded, msgpack.ExtType)
        assert decoded == "not-a-secret"
    finally:
        client.delete(enc_key)
        client.delete(plain_key)


def test_demote_to_plaintext_by_bare_set():
    """Per FR: re-setting an encrypted key without --encrypted demotes it
    to plaintext (the 'I changed my mind' semantic)."""
    client = _client()
    key = _unique_key()
    try:
        client.put(key, "secret-v1", encrypted=True)
        # Sanity: comes back via decrypt path
        assert client.get(key) == "secret-v1"

        # Bare set → sidecar should be cleared
        client.put(key, "now-public-v2")
        body = _raw_get(client, key)
        decoded = msgpack.unpackb(body, raw=False)
        assert not isinstance(decoded, msgpack.ExtType)
        assert decoded == "now-public-v2"
    finally:
        client.delete(key)


def test_encrypted_wire_form_is_ext_0x21():
    """Server-side contract: GET of an encrypted record returns msgpack
    ext type 0x21 with raw ciphertext bytes. Verifies the wire format
    pinned in docs/fr_encrypted_values.md without going through the
    CLI's auto-decrypt."""
    client = _client()
    key = _unique_key()
    plaintext = "wifi-password-here"
    try:
        client.put(key, plaintext, encrypted=True)
        body = _raw_get(client, key)
        decoded = msgpack.unpackb(body, raw=False)
        assert isinstance(decoded, msgpack.ExtType)
        assert decoded.code == KVENC_EXT_TYPE
        assert len(decoded.data) == len(plaintext)  # XOR preserves length
        assert decoded.data != plaintext.encode("utf-8")  # ciphertext ≠ plaintext
    finally:
        client.delete(key)


def _raw_get(client: Stra2usClient, key: str) -> bytes:
    """HMAC-signed GET that returns the raw response body without going
    through the CLI's msgpack auto-decode. Used to inspect the wire form."""
    import hashlib
    import hmac
    import time

    ts = int(time.time())
    uri = client._kv_uri(key)
    secret = client._secret_bytes()
    payload = uri.encode("utf-8") + b"" + str(ts).encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    r = requests.get(
        client.base_url + uri,
        headers={
            "X-Client-ID": client.client_id,
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        },
        timeout=client.timeout,
    )
    r.raise_for_status()
    return r.content
