# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the value resolution chain (P3).

The resolver walks `<app>/<device>/<key>` → `<app>/public/<key>` →
catalog default → None per the FR + fr_application_view.md. Tests
exercise each rung of the ladder plus the encrypted-flag pass-through.
"""

from __future__ import annotations

import asyncio

import msgpack
import pytest

from services.value_resolver import resolve_value


class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    async def get(self, key):
        return self._kv.get(key)

    def stash(self, key: str, value):
        self._kv[f"kv:{key}"] = msgpack.packb(value, use_bin_type=True)

    def stash_encrypted(self, key: str, value):
        self._kv[f"kv:{key}"] = msgpack.packb(value, use_bin_type=True)
        self._kv[f"kv:{key}:enc"] = b"1"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def redis():
    return _FakeRedis()


# ----- per-rung resolution -----

def test_device_override_wins(redis):
    redis.stash("demo/dev1/brightness", 99)
    redis.stash("demo/public/brightness", 50)
    rv = _run(resolve_value(redis, "demo", "dev1", "brightness",
                            {"type": "int", "default": 30}))
    assert rv.value == "99"
    assert not rv.from_default


def test_app_scope_used_when_no_device_override(redis):
    redis.stash("demo/public/brightness", 50)
    rv = _run(resolve_value(redis, "demo", "dev1", "brightness",
                            {"type": "int", "default": 30}))
    assert rv.value == "50"
    assert not rv.from_default


def test_catalog_default_used_when_kv_chain_empty(redis):
    rv = _run(resolve_value(redis, "demo", "dev1", "brightness",
                            {"type": "int", "default": 30}))
    assert rv.value == "30"
    assert rv.from_default


def test_returns_none_when_no_value_anywhere(redis):
    rv = _run(resolve_value(redis, "demo", "dev1", "brightness",
                            {"type": "int"}))
    assert rv.value is None
    assert not rv.from_default


# ----- type coercion to form-string -----

def test_bool_true_as_lowercase_string(redis):
    redis.stash("demo/dev1/debug", True)
    rv = _run(resolve_value(redis, "demo", "dev1", "debug", {"type": "bool"}))
    assert rv.value == "true"


def test_bool_false_as_lowercase_string(redis):
    redis.stash("demo/dev1/debug", False)
    rv = _run(resolve_value(redis, "demo", "dev1", "debug", {"type": "bool"}))
    assert rv.value == "false"


def test_int_stored_as_string(redis):
    redis.stash("demo/dev1/n", 42)
    rv = _run(resolve_value(redis, "demo", "dev1", "n", {"type": "int"}))
    assert rv.value == "42"


def test_float_preserves_precision(redis):
    redis.stash("demo/dev1/r", 3.14)
    rv = _run(resolve_value(redis, "demo", "dev1", "r", {"type": "float"}))
    assert rv.value == "3.14"


def test_string_passes_through(redis):
    redis.stash("demo/dev1/greeting", "hello\nworld")
    rv = _run(resolve_value(redis, "demo", "dev1", "greeting",
                            {"type": "string"}))
    assert rv.value == "hello\nworld"


# ----- encrypted flag -----

def test_encrypted_flag_propagates_from_device_scope(redis):
    redis.stash_encrypted("demo/dev1/wifi", "secret")
    rv = _run(resolve_value(redis, "demo", "dev1", "wifi",
                            {"type": "string"}))
    assert rv.encrypted is True
    assert rv.value == "secret"


def test_encrypted_flag_from_app_scope(redis):
    redis.stash_encrypted("demo/public/wifi", "fleet-default")
    rv = _run(resolve_value(redis, "demo", "dev1", "wifi",
                            {"type": "string"}))
    assert rv.encrypted is True


def test_encrypted_flag_absent_for_plaintext(redis):
    redis.stash("demo/dev1/n", 99)
    rv = _run(resolve_value(redis, "demo", "dev1", "n", {"type": "int"}))
    assert rv.encrypted is False


# ----- corruption / edge cases -----

def test_corrupted_msgpack_treated_as_unset(redis):
    """A garbled record shouldn't 500 the page render; surface as
    "no value" and let the catalog default kick in."""
    redis._kv["kv:demo/dev1/n"] = b"\xff\xff\xff"
    rv = _run(resolve_value(redis, "demo", "dev1", "n",
                            {"type": "int", "default": 10}))
    assert rv.value == "10"
    assert rv.from_default
