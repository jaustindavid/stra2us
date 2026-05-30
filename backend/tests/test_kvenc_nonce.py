# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the kvenc per-client nonce fix (v1.8.x security pass #3).

Background: the kvenc keystream is HMAC(secret, label||nonce||counter),
XORed with the plaintext. Pre-fix the nonce was the wall-clock second,
so two *different* encrypted values served to one client in the same
second shared a keystream — a two-time pad (C1 ⊕ C2 == P1 ⊕ P2), which
a wire observer can crib-drag.

The fix hands out a strictly-increasing per-client nonce
(`next_kvenc_nonce`, max(now, last+1)), so two encrypted values to one
client never share a keystream. It's a server-only change: the nonce
still travels in X-Response-Timestamp, and clients verify + decrypt
from that value unchanged.

These tests prove (a) the nonce is unique/monotonic per client even
within one second, and (b) at the crypto level the two-time pad is gone
with distinct nonces but present with a shared one (demonstrating both
the bug and that the fix closes it).
"""

from __future__ import annotations

import asyncio

import pytest

from core.security import kvenc_xor


def _run(coro):
    """Project convention (mirrors test_value_resolver.py) — no pytest-asyncio."""
    return asyncio.run(coro)


SECRET = "ab" * 32  # 64 hex chars → 32-byte secret


# ----- fake Redis that faithfully emulates the nonce Lua script -------

class _FakeRedisEval:
    """Emulates the `_KVENC_NONCE_LUA` read-max-write. The real script
    runs inside Redis (atomic across workers); this mirrors its 4 lines
    so `next_kvenc_nonce` can be exercised without a live server."""

    def __init__(self):
        self.store: dict[str, int] = {}

    async def eval(self, script, numkeys, *args):
        key, now = args[0], int(args[1])
        last = int(self.store.get(key, 0))
        if last >= now:
            now = last + 1
        self.store[key] = now
        return now


@pytest.fixture
def patched_time(monkeypatch):
    """Freeze time so multiple nonce allocations land in one 'second'."""
    from api import routes_device as rd
    monkeypatch.setattr(rd.time, "time", lambda: 1_700_000_000.0)
    return rd


# ----- nonce uniqueness / monotonicity --------------------------------

def test_nonce_strictly_increasing_in_one_second(patched_time):
    """Five allocations for one client within the SAME frozen second
    must yield five strictly-increasing nonces — the core of the fix."""
    rd = patched_time
    redis = _FakeRedisEval()
    nonces = [_run(rd.next_kvenc_nonce(redis, "dev1")) for _ in range(5)]
    assert nonces == sorted(set(nonces))           # all unique, ascending
    assert nonces[0] == 1_700_000_000
    assert nonces == [1_700_000_000 + i for i in range(5)]


def test_nonce_independent_per_client(patched_time):
    """Two clients in the same second get independent sequences — a
    nonce collision only matters within one client (same secret), and
    each client's monotonic counter is its own."""
    rd = patched_time
    redis = _FakeRedisEval()
    a1 = _run(rd.next_kvenc_nonce(redis, "devA"))
    b1 = _run(rd.next_kvenc_nonce(redis, "devB"))
    a2 = _run(rd.next_kvenc_nonce(redis, "devA"))
    assert a1 == b1 == 1_700_000_000   # each client's first alloc == now
    assert a2 == 1_700_000_001         # devA advanced independently of devB


# ----- crypto property: the two-time pad is gone ----------------------

def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def test_shared_nonce_leaks_via_two_time_pad():
    """Demonstrates the ORIGINAL bug: with the SAME nonce, the keystream
    cancels and the ciphertext-XOR equals the plaintext-XOR — a wire
    observer recovers P1⊕P2 with no key knowledge."""
    p1 = b"wifi-password-123"
    p2 = b"api-token-abcdef!"
    c1 = kvenc_xor(SECRET, 1_700_000_000, p1)
    c2 = kvenc_xor(SECRET, 1_700_000_000, p2)  # same nonce (the bug)
    assert _xor(c1, c2) == _xor(p1, p2)        # keystream cancelled → leak


def test_distinct_nonces_defeat_two_time_pad():
    """The FIX: with DISTINCT nonces (what `next_kvenc_nonce` guarantees),
    the keystreams differ and do NOT cancel — the ciphertext-XOR reveals
    nothing about the plaintext-XOR."""
    p1 = b"wifi-password-123"
    p2 = b"api-token-abcdef!"
    c1 = kvenc_xor(SECRET, 1_700_000_000, p1)
    c2 = kvenc_xor(SECRET, 1_700_000_001, p2)  # distinct nonce (the fix)
    assert _xor(c1, c2) != _xor(p1, p2)        # no cancellation → no leak


def test_roundtrip_with_server_supplied_nonce():
    """Client-transparency: a client that decrypts with the nonce the
    server emitted (X-Response-Timestamp) recovers the plaintext —
    proving the fix needs no client change (XOR is symmetric, the
    keystream depends only on (secret, nonce) which both sides share)."""
    pt = b"super-secret-wifi-pw"
    nonce = 1_700_000_042
    ct = kvenc_xor(SECRET, nonce, pt)          # server encrypts
    recovered = kvenc_xor(SECRET, nonce, ct)   # client decrypts w/ same nonce
    assert recovered == pt
