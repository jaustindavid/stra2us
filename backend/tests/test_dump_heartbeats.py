"""Tests for the admin `/dump_heartbeats/<client_id>` debug endpoint.

The endpoint resolves the client's heartbeat topic via the same
catalog path the customer `/app/` page uses, gates on the resolved
`q/<topic>` ACL, and streams a JSONL body with a leading `_meta`
line followed by one entry per matching stream record (newest-first).

These tests poke `dump_heartbeats` directly — the FastAPI auth
dependency is replaced by stuffing `request.state.admin_user`, the
same auth-bypass pattern used by `test_stream_monitor.py` and
`test_backup_restore.py`. The fake redis is a small in-memory class
that supports just the operations the handler calls.
"""

import asyncio
import json
import time
import types

import msgpack
import pytest
import yaml

from fastapi import HTTPException

from api import routes_admin
from api import routes_app_theme


def _id_tuple(s: str):
    ms, _, seq = s.partition("-")
    return (int(ms), int(seq) if seq else 0)


class _FakeRedis:
    """In-memory async fake covering the slice of redis-py used by
    `dump_heartbeats` plus the catalog read it triggers via
    `load_catalog_dict`.

    Supports:
      - `get(key)` against a plain key/value store.
      - `xrange(key, min, max, count)` with `+`/`-` and `(<id>`
        exclusive-bound syntax for cursored paging.
    """

    def __init__(self):
        self.kv: dict[str, bytes] = {}
        self.streams: dict[str, list[tuple[bytes, dict]]] = {}

    # --- KV side ---
    def set_kv(self, key: str, value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.kv[key] = value

    def set_acl(self, client_id: str, acl: dict):
        self.kv[f"client:{client_id}:acl"] = json.dumps(acl).encode("utf-8")

    def set_catalog(self, app: str, doc: dict):
        # load_catalog_dict expects a msgpack-wrapped YAML string.
        yaml_str = yaml.safe_dump(doc)
        self.kv[f"kv:_catalog/{app}"] = msgpack.packb(yaml_str, use_bin_type=True)

    async def get(self, key):
        return self.kv.get(key)

    # --- Stream side ---
    def stream_add(self, key: str, msg_id: str, client_id: str,
                   payload_bytes: bytes, exp: int):
        fields = {
            b"client_id": client_id.encode("utf-8"),
            b"payload": payload_bytes,
            b"exp": str(exp).encode("utf-8"),
        }
        self.streams.setdefault(key, []).append((msg_id.encode("utf-8"), fields))

    async def xrange(self, key, min="-", max="+", count=None):
        entries = self.streams.get(key, [])

        def parse_bound(b):
            if b in ("+", "-"):
                return None, False
            exclusive = False
            if isinstance(b, str) and b.startswith("("):
                exclusive = True
                b = b[1:]
            return _id_tuple(b), exclusive

        lo, lo_excl = parse_bound(min)
        hi, hi_excl = parse_bound(max)
        out = []
        for msg_id, fields in entries:
            t = _id_tuple(msg_id.decode())
            if lo is not None:
                if lo_excl and t <= lo:
                    continue
                if not lo_excl and t < lo:
                    continue
            if hi is not None:
                if hi_excl and t >= hi:
                    continue
                if not hi_excl and t > hi:
                    continue
            out.append((msg_id, fields))
            if count is not None and len(out) >= count:
                break
        return out


def _call_dump(fr: _FakeRedis, *, client_id: str, admin_user: str,
               admin_acl: dict):
    """Call `dump_heartbeats` directly, returning (status_or_200,
    body_lines or detail_str, headers_dict). On HTTPException, returns
    (status, detail, None)."""
    import core.redis_client as rc
    from api import dependencies as deps
    saved_get = rc.get_redis_client
    saved_routes_admin = routes_admin.get_redis_client
    saved_routes_theme = routes_app_theme.get_redis_client
    saved_deps = deps.get_redis_client
    routes_admin.get_redis_client = lambda: fr
    routes_app_theme.get_redis_client = lambda: fr
    deps.get_redis_client = lambda: fr

    # Synthesize a request object with the bits get_admin_context reads
    # plus what the handler itself touches. FastAPI's Request has a
    # `state` attribute that `get_admin_context` reads `admin_user` off
    # of, and `load_admin_acl` does a redis lookup for the user's
    # admin_acls:<user> row.
    fr.kv[f"admin_acls:{admin_user}"] = json.dumps(admin_acl).encode("utf-8")

    class _State:
        pass

    state = _State()
    state.admin_user = admin_user

    request = types.SimpleNamespace(state=state)

    async def _run():
        return await routes_admin.dump_heartbeats(client_id, request)

    try:
        try:
            response = asyncio.run(_run())
        except HTTPException as exc:
            return exc.status_code, exc.detail, None

        # StreamingResponse — drain the async iterator into a string.
        body_chunks: list[str] = []

        async def _drain():
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    body_chunks.append(chunk.decode("utf-8"))
                else:
                    body_chunks.append(chunk)

        asyncio.run(_drain())
        body = "".join(body_chunks)
        lines = [ln for ln in body.split("\n") if ln]
        return response.status_code, lines, dict(response.headers)
    finally:
        routes_admin.get_redis_client = saved_routes_admin
        routes_app_theme.get_redis_client = saved_routes_theme
        deps.get_redis_client = saved_deps


# --- shared seed helpers ---------------------------------------------------

def _device_acl(app: str, client_id: str):
    return {
        "permissions": [
            {"prefix": f"{app}/{client_id}", "access": "rw"},
            {"prefix": f"{app}/public", "access": "rw"},
        ]
    }


_WILDCARD_ADMIN = {"permissions": [{"prefix": "*", "access": "rw"}]}


def _seed_basic(fr: _FakeRedis, app: str = "critterchron"):
    """Three device clients (`chatty`, `rachel_raccoon`, `other`) all
    under the same app. Their heartbeats are interleaved on the
    catalog-default `critterchron/public/heartbeep` topic.
    """
    for cid in ("chatty", "rachel_raccoon", "other"):
        fr.set_acl(cid, _device_acl(app, cid))

    fr.set_catalog(app, {"name": app})  # catalog exists, default topic
    return f"q:{app}/public/heartbeep"


def _xadd_msgpack(fr, key, ms, cid, body):
    fr.stream_add(
        key,
        f"{ms}-0",
        cid,
        msgpack.packb(body, use_bin_type=True),
        exp=int(time.time()) + 86400,
    )


# --- tests -----------------------------------------------------------------

def test_happy_path_returns_meta_then_newest_first_matches():
    fr = _FakeRedis()
    key = _seed_basic(fr)

    base = 1_770_000_000_000
    # Interleave: chatty, rachel, other, chatty, rachel, other, rachel
    _xadd_msgpack(fr, key, base + 0, "chatty",         {"i": 0})
    _xadd_msgpack(fr, key, base + 1000, "rachel_raccoon", {"i": 1})
    _xadd_msgpack(fr, key, base + 2000, "other",        {"i": 2})
    _xadd_msgpack(fr, key, base + 3000, "chatty",       {"i": 3})
    _xadd_msgpack(fr, key, base + 4000, "rachel_raccoon", {"i": 4})
    _xadd_msgpack(fr, key, base + 5000, "other",        {"i": 5})
    _xadd_msgpack(fr, key, base + 6000, "rachel_raccoon", {"i": 6})

    status, lines, headers = _call_dump(
        fr,
        client_id="rachel_raccoon",
        admin_user="rescue",
        admin_acl=_WILDCARD_ADMIN,
    )

    assert status == 200
    cd = headers.get("content-disposition") or headers.get("Content-Disposition")
    assert cd and 'attachment;' in cd
    assert "heartbeats-rachel_raccoon-" in cd
    assert cd.endswith('.jsonl"')

    # Every line must be valid JSON.
    parsed = [json.loads(ln) for ln in lines]

    # First line is the metadata record.
    meta = parsed[0]
    assert "_meta" in meta
    assert meta["_meta"]["topic"] == "critterchron/public/heartbeep"
    assert meta["_meta"]["client_id"] == "rachel_raccoon"
    assert meta["_meta"]["stream_max_age_days"] == 7
    assert isinstance(meta["_meta"]["generated_at_iso"], str)

    data_lines = parsed[1:]
    assert len(data_lines) == 3
    assert all(d["client_id"] == "rachel_raccoon" for d in data_lines)

    # Newest-first: ts_ms strictly decreasing.
    ts = [d["ts_ms"] for d in data_lines]
    assert ts == sorted(ts, reverse=True)
    # The three rachel entries are i=6,4,1 in newest-first order.
    assert [d["data"]["i"] for d in data_lines] == [6, 4, 1]
    # Both decoded `data` and raw `payload_hex` populated on every line.
    for d in data_lines:
        assert d["data"] is not None
        assert isinstance(d["payload_hex"], str) and d["payload_hex"]


def test_empty_match_set_yields_only_meta_line():
    fr = _FakeRedis()
    key = _seed_basic(fr)
    base = 1_770_000_000_000
    # Seed entries from OTHER clients only.
    _xadd_msgpack(fr, key, base + 0, "chatty", {"i": 0})
    _xadd_msgpack(fr, key, base + 1000, "other", {"i": 1})

    status, lines, headers = _call_dump(
        fr,
        client_id="rachel_raccoon",
        admin_user="rescue",
        admin_acl=_WILDCARD_ADMIN,
    )
    assert status == 200
    assert len(lines) == 1
    meta = json.loads(lines[0])
    assert "_meta" in meta
    assert meta["_meta"]["client_id"] == "rachel_raccoon"


def test_no_app_affinity_returns_400():
    """A client whose ACL has no `<app>/<client_id>` prefix can't
    have its heartbeat topic resolved. The handler returns 400 with
    the documented detail message."""
    fr = _FakeRedis()
    # `weird_client` has only a non-device-shaped ACL.
    fr.set_acl("weird_client", {"permissions": [{"prefix": "raw_topic", "access": "r"}]})

    status, detail, _ = _call_dump(
        fr,
        client_id="weird_client",
        admin_user="rescue",
        admin_acl=_WILDCARD_ADMIN,
    )
    assert status == 400
    assert "no app affinity" in detail.lower()


def test_acl_deny_on_resolved_topic_returns_403():
    """A scoped admin whose ACL doesn't cover the resolved topic
    gets 403. The client is critterchron-scoped but the calling
    admin only has `someother_app:rw` — so the check_acl on the
    resolved `q/critterchron/public/heartbeep` fails."""
    fr = _FakeRedis()
    _seed_basic(fr, app="critterchron")
    scoped_admin = {"permissions": [{"prefix": "someother_app", "access": "rw"}]}

    status, detail, _ = _call_dump(
        fr,
        client_id="rachel_raccoon",
        admin_user="scoped_op",
        admin_acl=scoped_admin,
    )
    assert status == 403


def test_bad_msgpack_payload_data_null_hex_preserved():
    fr = _FakeRedis()
    key = _seed_basic(fr)
    # Hand-stuff a non-msgpack payload (raw bytes that don't decode).
    raw = b"\xff\xfe\xfd\xfc not msgpack at all"
    fr.stream_add(
        key,
        "1770000999000-0",
        "rachel_raccoon",
        raw,
        exp=int(time.time()) + 86400,
    )

    status, lines, _ = _call_dump(
        fr,
        client_id="rachel_raccoon",
        admin_user="rescue",
        admin_acl=_WILDCARD_ADMIN,
    )
    assert status == 200
    parsed = [json.loads(ln) for ln in lines]
    assert "_meta" in parsed[0]
    assert len(parsed) == 2
    entry = parsed[1]
    assert entry["data"] is None
    assert entry["payload_hex"] == raw.hex()


def test_paging_boundary_650_entries_all_present_newest_first():
    """Seed >600 matching entries so the XRANGE pagination has to
    advance the cursor at least twice (page size = 500). Without
    proper cursor advancement a single-page implementation would
    silently truncate at 500 entries."""
    fr = _FakeRedis()
    key = _seed_basic(fr)
    fr.set_acl("noisy_client", _device_acl("critterchron", "noisy_client"))

    base = 1_770_000_000_000
    for i in range(650):
        _xadd_msgpack(fr, key, base + i * 1000, "noisy_client", {"i": i})

    status, lines, _ = _call_dump(
        fr,
        client_id="noisy_client",
        admin_user="rescue",
        admin_acl=_WILDCARD_ADMIN,
    )
    assert status == 200
    parsed = [json.loads(ln) for ln in lines]
    assert "_meta" in parsed[0]
    data_lines = parsed[1:]
    assert len(data_lines) == 650

    # Newest-first, no duplicates, no gaps. The `i` field walks
    # 649 -> 0.
    indices = [d["data"]["i"] for d in data_lines]
    assert indices == list(range(649, -1, -1))
    assert len(set(indices)) == 650
