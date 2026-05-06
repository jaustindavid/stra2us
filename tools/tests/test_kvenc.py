# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Unit tests for the KV-value HMAC-keystream cipher.

No network, no server. Pins the wire format so server and CLI can't
silently drift apart (cross-checked: the CLI's ``_kvenc_xor`` and the
server's ``kvenc_xor`` must produce identical keystreams for the same
secret + nonce, since one encrypts and the other decrypts).
"""

from __future__ import annotations

import hmac
import hashlib

from stra2us_cli.client import _kvenc_xor, KVENC_LABEL, KVENC_EXT_TYPE


SECRET = bytes.fromhex("00" * 32)


def test_label_and_ext_type_pin():
    # If either of these changes, every deployed device decrypts garbage.
    assert KVENC_LABEL == b"stra2us-kvenc-v1"
    assert KVENC_EXT_TYPE == 0x21


def test_xor_is_symmetric():
    nonce = 1714608000  # arbitrary
    plaintext = b"super-secret-wifi-password-2024"
    ct = _kvenc_xor(SECRET, nonce, plaintext)
    assert ct != plaintext
    assert _kvenc_xor(SECRET, nonce, ct) == plaintext


def test_keystream_matches_spec():
    """First 32 bytes of keystream = HMAC(secret, label || nonce_be32 || 0x00).

    Pins the exact construction so a future refactor can't quietly swap
    counter endianness or label ordering.
    """
    nonce = 0x01020304
    plaintext = b"\x00" * 32
    ct = _kvenc_xor(SECRET, nonce, plaintext)
    expected = hmac.new(
        SECRET,
        KVENC_LABEL + nonce.to_bytes(4, "big") + b"\x00",
        hashlib.sha256,
    ).digest()
    assert ct == expected


def test_keystream_extends_past_one_block():
    """Plaintexts longer than 32 bytes (e.g. a 63-byte WPA2 password)
    must use counter=1 for the second block."""
    nonce = 42
    plaintext = b"\x00" * 64
    ct = _kvenc_xor(SECRET, nonce, plaintext)
    block0 = hmac.new(
        SECRET, KVENC_LABEL + nonce.to_bytes(4, "big") + b"\x00",
        hashlib.sha256,
    ).digest()
    block1 = hmac.new(
        SECRET, KVENC_LABEL + nonce.to_bytes(4, "big") + b"\x01",
        hashlib.sha256,
    ).digest()
    assert ct[:32] == block0
    assert ct[32:] == block1


def test_different_nonces_produce_different_keystreams():
    plaintext = b"hunter2"
    ct1 = _kvenc_xor(SECRET, 1, plaintext)
    ct2 = _kvenc_xor(SECRET, 2, plaintext)
    assert ct1 != ct2


def test_empty_plaintext_roundtrips():
    assert _kvenc_xor(SECRET, 0, b"") == b""


def test_cli_and_server_agree():
    """Server's kvenc_xor (in backend/) and CLI's _kvenc_xor must agree
    bit-for-bit. Skipped if the backend isn't importable (running tests
    purely against the installed CLI wheel)."""
    import sys
    from pathlib import Path

    backend_src = Path(__file__).resolve().parents[2] / "backend" / "src"
    if not backend_src.exists():
        return  # nothing to check
    sys.path.insert(0, str(backend_src))
    try:
        from core.security import kvenc_xor as server_xor  # type: ignore
    except ImportError:
        return
    finally:
        sys.path.pop(0)

    secret_hex = "ab" * 32
    nonce = 1234567
    plaintext = b"the rain in spain falls mainly on the plain"
    cli_ct = _kvenc_xor(bytes.fromhex(secret_hex), nonce, plaintext)
    server_ct = server_xor(secret_hex, nonce, plaintext)
    assert cli_ct == server_ct
