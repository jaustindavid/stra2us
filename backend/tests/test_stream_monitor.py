"""Regression tests for the sparse-publisher case on the
customer-facing app page tail.

The bug: `stream_monitor` used to read `limit` raw entries from
XREVRANGE and *then* filter by `client_id`. On a shared topic
where one publisher is slow and many others are chatty, a small
window misses the slow publisher entirely. The fix pages
backward through the stream when a client_id filter is in play,
returning up to `limit` matches.

These tests poke `stream_monitor` directly (the FastAPI auth
dependency is bypassed by passing the default arg) against a
small in-memory fake of the bits of redis-py we use.
"""

import asyncio
import time

import msgpack
import pytest

from api import routes_admin


class _FakeStreamRedis:
    """Minimal async fake supporting XADD-shape entries and the
    `xrevrange(max, min, count)` slice we actually call. Supports
    the `(<id>` exclusive upper-bound syntax the paged scan uses.
    """

    def __init__(self):
        self.entries = []  # list of (msg_id_bytes, fields_dict_bytes)

    def add(self, msg_id: str, client_id: str, payload, exp: int):
        fields = {
            b"client_id": client_id.encode("utf-8"),
            b"payload": msgpack.packb(payload, use_bin_type=True),
            b"exp": str(exp).encode("utf-8"),
        }
        self.entries.append((msg_id.encode("utf-8"), fields))

    @staticmethod
    def _id_tuple(s: str):
        ms, _, seq = s.partition("-")
        return (int(ms), int(seq) if seq else 0)

    async def xrevrange(self, key, max="+", min="-", count=None):
        # Resolve bounds. `+`/`-` are unbounded; `(<id>` is exclusive.
        def parse_bound(b, default_hi):
            if b in ("+", "-"):
                return None, False
            exclusive = False
            if isinstance(b, str) and b.startswith("("):
                exclusive = True
                b = b[1:]
            return self._id_tuple(b), exclusive

        hi, hi_excl = parse_bound(max, True)
        lo, lo_excl = parse_bound(min, False)

        out = []
        for msg_id, fields in reversed(self.entries):
            t = self._id_tuple(msg_id.decode())
            if hi is not None:
                if hi_excl and t >= hi:
                    continue
                if not hi_excl and t > hi:
                    continue
            if lo is not None:
                if lo_excl and t <= lo:
                    continue
                if not lo_excl and t < lo:
                    continue
            out.append((msg_id, fields))
            if count is not None and len(out) >= count:
                break
        return out


def _call(fr, **kwargs):
    """Run stream_monitor against a fake redis and return the
    parsed message list."""
    import core.redis_client as rc
    saved = rc.get_redis_client
    routes_admin.get_redis_client = lambda: fr  # noqa: E305
    try:
        return asyncio.run(routes_admin.stream_monitor(**kwargs))
    finally:
        routes_admin.get_redis_client = saved


def _seed_interleaved(fr, quiet_at):
    """50 entries from chatty_client + 3 from quiet_client placed
    at the given indices (0 = oldest). Stream ids are 1ms apart.
    """
    exp = int(time.time()) + 86400
    quiet = set(quiet_at)
    n = 50 + len(quiet_at)
    quiet_seen = 0
    for i in range(n):
        if i in quiet:
            cid = "quiet_client"
            payload = {"i": i, "kind": "q"}
            quiet_seen += 1
        else:
            cid = "chatty_client"
            payload = {"i": i, "kind": "c"}
        # 1-second gaps to keep received_at distinct (ms-prefix /1000).
        ms = 1_700_000_000_000 + i * 1000
        fr.add(f"{ms}-0", cid, payload, exp)
    # sanity for caller
    assert quiet_seen == len(quiet_at)


def test_sparse_publisher_returns_all_matches():
    fr = _FakeStreamRedis()
    _seed_interleaved(fr, quiet_at=[5, 25, 45])

    out = _call(fr, topic="critterchron/public/heartbeep",
                limit=3, client_id=["quiet_client"])

    assert len(out) == 3
    assert all(m["client_id"] == "quiet_client" for m in out)
    # newest-first
    indices = [m["data"]["i"] for m in out]
    assert indices == sorted(indices, reverse=True)
    assert set(indices) == {5, 25, 45}


def test_chatty_publisher_returns_newest_three():
    fr = _FakeStreamRedis()
    _seed_interleaved(fr, quiet_at=[5, 25, 45])

    out = _call(fr, topic="critterchron/public/heartbeep",
                limit=3, client_id=["chatty_client"])

    assert len(out) == 3
    assert all(m["client_id"] == "chatty_client" for m in out)
    indices = [m["data"]["i"] for m in out]
    assert indices == sorted(indices, reverse=True)
    # The three highest-i chatty entries (i = 0..52 minus quiet
    # positions 5/25/45) are 52, 51, 50.
    assert indices == [52, 51, 50]


def test_unfiltered_preserves_old_behavior():
    fr = _FakeStreamRedis()
    _seed_interleaved(fr, quiet_at=[5, 25, 45])

    out = _call(fr, topic="critterchron/public/heartbeep",
                limit=3, client_id=None)

    assert len(out) == 3
    indices = [m["data"]["i"] for m in out]
    assert indices == [52, 51, 50]


def test_no_match_returns_empty_and_caps_scan():
    """When the filter matches nothing, we must not loop forever.
    The max-batches safety cap bounds the scan; the fake's
    xrevrange returns < batch on the final page so the scan
    terminates naturally. This test just asserts the empty-list
    behavior plus that the call returns (i.e. didn't hang)."""
    fr = _FakeStreamRedis()
    _seed_interleaved(fr, quiet_at=[5, 25, 45])

    out = _call(fr, topic="critterchron/public/heartbeep",
                limit=3, client_id=["nobody_here"])
    assert out == []
