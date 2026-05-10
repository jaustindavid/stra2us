# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the activity-log middleware's error-handling enhancements
(v1.6.6).

Pre-v1.6.6 the middleware caught and re-raised exceptions but didn't:
  * emit a structured-context log line (request path, method,
    client_id) alongside the bare traceback FastAPI's default
    handler produces; and
  * tag the activity_log entry with the exception class —
    "Error (500)" was the same regardless of whether a TimeoutError,
    KeyError, or RedisConnectionError caused it.

These tests pin both behaviors. The contract of the device data
APIs (`/kv/` and `/q/`) is "never 500" — auth failures map to 401,
valid misses map to 200 with `{"status":"not_found"}` — so any
500 is a bug to investigate. Making it easy to localize is what
these enhancements close.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ----- minimal fake Redis ------------------------------------------

class _FakeRedis:
    """Stand-in supporting the few ops the activity-log middleware
    touches. The middleware writes via xadd; everything else is
    incidental to these tests."""

    def __init__(self):
        self.xadd_calls: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict, **kwargs: Any) -> str:
        self.xadd_calls.append((stream, dict(fields)))
        return "0-1"

    async def get(self, key):  # unused, but middleware doesn't call
        return None


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch the redis client at every import site the middleware
    or the test app might reach for. The activity-log middleware
    in `main.py` does `from core.redis_client import get_redis_client`
    at module top, so the binding lives on `main` itself —
    patching `core.redis_client.get_redis_client` alone misses it.
    Patch both to be safe."""
    fr = _FakeRedis()
    from core import redis_client
    import main
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fr)
    monkeypatch.setattr(main, "get_redis_client", lambda: fr)
    return fr


# ----- a tiny app that mounts the middleware + a raising route -----

@pytest.fixture
def app_with_middleware(fake_redis):
    """Build a FastAPI app with the activity_log_middleware attached
    and three routes:
      * /kv/{key} — raises a custom exception (the test target)
      * /kv/ok    — returns 200 cleanly (negative control)
      * /admin/x  — raises but is NOT under /kv/ or /q/ (must NOT
                    write to activity_log)
    """
    # Import main here so the redis monkeypatch is in place when the
    # module-level FastAPI app is built. We don't use main.app
    # directly — instead, we extract the middleware function and
    # mount it on a fresh app so we don't drag in every other route.
    from main import activity_log_middleware

    a = FastAPI()
    a.middleware("http")(activity_log_middleware)

    class CustomFault(Exception):
        """Distinctive exception name for the activity-log tag check."""

    # Order matters: the explicit clean-path route must be
    # registered BEFORE the `{key:path}` catch-all, otherwise
    # FastAPI's first-match dispatch sends it into the raising
    # handler.
    @a.get("/kv/clean/path")
    async def kv_clean():
        return {"ok": True}

    @a.get("/kv/{key:path}")
    async def raise_for_kv(key: str):
        raise CustomFault(f"intentional fault for key={key}")

    @a.get("/admin/x")
    async def raise_for_admin():
        raise CustomFault("intentional fault on admin path")

    return a, CustomFault


# ----- tests --------------------------------------------------------

def test_kv_500_logs_with_request_context(app_with_middleware, fake_redis, caplog):
    """An unhandled exception in a /kv/ handler emits a
    `stra2us.errors` log record at ERROR level, carrying the
    request method, path, and client_id in the message."""
    app, _ = app_with_middleware
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="stra2us.errors"):
        r = client.get(
            "/kv/critterchron/timmy_tanuki/ir",
            headers={"X-Client-ID": "timmy_tanuki"},
        )

    assert r.status_code == 500
    error_records = [
        rec for rec in caplog.records if rec.name == "stra2us.errors"
    ]
    assert len(error_records) == 1, (
        f"Expected exactly one stra2us.errors record, got {len(error_records)}: "
        f"{[r.getMessage() for r in error_records]}"
    )
    msg = error_records[0].getMessage()
    # The format-string includes method, path, client_id.
    assert "GET" in msg
    assert "/kv/critterchron/timmy_tanuki/ir" in msg
    assert "timmy_tanuki" in msg  # client_id from X-Client-ID
    # `logger.exception()` attaches the traceback as exc_info.
    assert error_records[0].exc_info is not None


def test_kv_500_tags_activity_log_with_exception_class(
    app_with_middleware, fake_redis, caplog
):
    """The activity_log entry's `status` field includes the
    exception class name in brackets — `Error (500) [CustomFault]`
    rather than the pre-v1.6.6 bare `Error (500)`."""
    app, _ = app_with_middleware
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="stra2us.errors"):
        r = client.get("/kv/some/key", headers={"X-Client-ID": "dev1"})
    assert r.status_code == 500

    # Exactly one xadd to the activity log for this request.
    relevant = [
        fields for stream, fields in fake_redis.xadd_calls
        if stream == "system:activity_log"
    ]
    assert len(relevant) == 1
    entry = relevant[0]

    # Path + client_id correctness (sanity, not the focus).
    assert entry["client_id"] == "dev1"
    assert entry["action"] == "GET /kv/some/key"
    # The new shape: status carries the exception class.
    assert entry["status"] == "Error (500) [CustomFault]"


def test_kv_200_does_not_tag_activity_log(app_with_middleware, fake_redis):
    """A clean 200 doesn't add a class tag — the bracket suffix is
    only for failures. Negative control."""
    app, _ = app_with_middleware
    client = TestClient(app)

    r = client.get("/kv/clean/path", headers={"X-Client-ID": "dev1"})
    assert r.status_code == 200

    relevant = [
        fields for stream, fields in fake_redis.xadd_calls
        if stream == "system:activity_log"
    ]
    assert len(relevant) == 1
    # No "[..]" in the status — pre-v1.6.6 success shape preserved.
    assert "[" not in relevant[0]["status"]
    # This test route doesn't go through `read_kv` so kv_hit isn't
    # set — falls into the generic-success branch ("Success (200)"),
    # not the Hit/Miss one.
    assert relevant[0]["status"] == "Success (200)"


def test_non_kv_500_still_logs_but_skips_activity_entry(
    app_with_middleware, fake_redis, caplog
):
    """A 500 outside `/kv/` or `/q/` still emits the structured
    error log (the contract is "log every middleware-caught
    exception, not just device-API ones") but does NOT write an
    activity_log entry — the activity-log stream is scoped to
    device data APIs by design."""
    app, _ = app_with_middleware
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="stra2us.errors"):
        r = client.get("/admin/x")
    assert r.status_code == 500

    # Error log fired.
    error_records = [
        rec for rec in caplog.records if rec.name == "stra2us.errors"
    ]
    assert len(error_records) == 1
    assert "/admin/x" in error_records[0].getMessage()

    # No activity_log entry — middleware's path filter skipped this.
    relevant = [
        fields for stream, fields in fake_redis.xadd_calls
        if stream == "system:activity_log"
    ]
    assert relevant == []
