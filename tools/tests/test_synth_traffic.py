# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for v1.7.2 Sprint 5's synth-traffic action loop +
`stra2us synth-traffic` CLI dispatch.

Action loop is the interesting unit — paced rate, error
counting, mode dispatch, deadline behavior. Tested against a
recording stub client (similar shape to test_publish_lint.py)
with mocked time + sleep so the rate-loop is deterministic.

CLI dispatch is a thin wrapper; one dispatch test confirms the
verb routes and the argparse defaults are sensible.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from stra2us_cli import cli as cli_module
from stra2us_cli import synth
from stra2us_cli.client import Stra2usError


# ----- recording client (tracks per-call args + can fail on demand) -

class _StubClient:
    """Stand-in for Stra2usClient. Records every post_queue + put
    + get call. Tests can install error-injectors keyed by call
    index to verify error-counting behavior."""

    def __init__(self):
        self.base_url = "http://test"
        self.client_id = "synth-probe"
        self.queue_posts: list[tuple[str, Any]] = []
        self.kv_puts: list[tuple[str, Any]] = []
        self.kv_gets: list[str] = []
        self._store: dict[str, Any] = {}
        # Hooks for failure injection (set in tests). None = no
        # error; a string = the Stra2usError message to raise.
        self.fail_post_at: dict[int, str] = {}
        self.fail_put_at: dict[int, str] = {}
        self.fail_get_at: dict[int, str] = {}
        self._post_calls = 0
        self._put_calls = 0
        self._get_calls = 0

    def post_queue(self, topic, payload, ttl=None):
        self._post_calls += 1
        if self._post_calls in self.fail_post_at:
            raise Stra2usError(self.fail_post_at[self._post_calls])
        self.queue_posts.append((topic, payload))
        return _FakeResponse()

    def put(self, key, value, encrypted=False):
        self._put_calls += 1
        if self._put_calls in self.fail_put_at:
            raise Stra2usError(self.fail_put_at[self._put_calls])
        self.kv_puts.append((key, value))
        self._store[key] = value
        return _FakeResponse()

    def get(self, key):
        self._get_calls += 1
        if self._get_calls in self.fail_get_at:
            raise Stra2usError(self.fail_get_at[self._get_calls])
        self.kv_gets.append(key)
        return self._store.get(key)


class _FakeResponse:
    status_code = 200


# ----- time control: monkeypatch _now + _sleep ---------------------

@pytest.fixture
def fake_clock(monkeypatch):
    """Drives `synth._now()` and `synth._sleep()` deterministically.
    Each `_sleep(s)` advances the fake clock by s seconds, so the
    action loop completes after exactly `duration_seconds` of
    fake-time even at low rates."""
    state = {"now": 1000.0}

    def fake_now():
        return state["now"]

    def fake_sleep(seconds):
        state["now"] += seconds

    monkeypatch.setattr(synth, "_now", fake_now)
    monkeypatch.setattr(synth, "_sleep", fake_sleep)
    return state


# ----- action loop: mode dispatch -----

def test_run_q_only_posts_to_queue(fake_clock):
    """mode=q-only POSTs to the queue each tick; doesn't touch KV."""
    stub = _StubClient()
    result = synth.run(
        client=stub,
        queue_topic="critterchron/public/heartbeep",
        duration_seconds=3.0,
        rate_hz=1.0,
        mode="q-only",
    )
    assert result.queue_posts == 3
    assert result.kv_puts == 0
    assert result.kv_gets == 0
    assert result.total_errors == 0
    # Each posted payload carries a tick-counter for traceability.
    assert {p["tick"] for _, p in stub.queue_posts} == {1, 2, 3}


def test_run_kv_only_puts_then_gets(fake_clock):
    """mode=kv-only PUTs a tick-tagged value then GETs to verify
    round-trip. Each tick = 1 PUT + 1 GET."""
    stub = _StubClient()
    result = synth.run(
        client=stub,
        kv_key="critterchron/dev1/test",
        duration_seconds=3.0,
        rate_hz=1.0,
        mode="kv-only",
    )
    assert result.kv_puts == 3
    assert result.kv_gets == 3
    assert result.kv_get_mismatches == 0
    assert result.queue_posts == 0


def test_run_both_does_q_plus_kv(fake_clock):
    stub = _StubClient()
    result = synth.run(
        client=stub,
        queue_topic="t",
        kv_key="k",
        duration_seconds=2.0,
        rate_hz=1.0,
        mode="both",
    )
    assert result.queue_posts == 2
    assert result.kv_puts == 2
    assert result.kv_gets == 2


# ----- validation -----

def test_run_rejects_zero_rate():
    with pytest.raises(ValueError, match="rate_hz must be positive"):
        synth.run(
            client=_StubClient(),
            queue_topic="t",
            duration_seconds=1.0, rate_hz=0.0, mode="q-only",
        )


def test_run_rejects_rate_above_default_ceiling():
    """Default 100 Hz ceiling guards against typo-DoS."""
    with pytest.raises(ValueError, match="exceeds.*ceiling"):
        synth.run(
            client=_StubClient(),
            queue_topic="t",
            duration_seconds=1.0, rate_hz=500.0, mode="q-only",
        )


def test_run_allows_high_rate_when_ceiling_raised(fake_clock):
    """Operator can intentionally exceed the default ceiling."""
    stub = _StubClient()
    result = synth.run(
        client=stub,
        queue_topic="t",
        duration_seconds=0.1, rate_hz=500.0,
        mode="q-only",
        max_rate_hz=10000.0,
    )
    # At 500 Hz × 0.1s = ~50 calls (modulo loop overhead).
    assert result.queue_posts >= 1


def test_run_requires_queue_for_q_modes():
    with pytest.raises(ValueError, match="--queue"):
        synth.run(
            client=_StubClient(),
            duration_seconds=1.0, rate_hz=1.0, mode="q-only",
        )


def test_run_requires_kv_key_for_kv_modes():
    with pytest.raises(ValueError, match="--kv-key"):
        synth.run(
            client=_StubClient(),
            duration_seconds=1.0, rate_hz=1.0, mode="kv-only",
        )


# ----- error counting -----

def test_run_counts_queue_errors_continues_loop(fake_clock):
    """A failing queue POST doesn't stop the loop — subsequent
    ticks still fire. Critical for warm-up: a flaky network
    shouldn't abort the whole run."""
    stub = _StubClient()
    stub.fail_post_at = {2: "transient network error"}
    result = synth.run(
        client=stub,
        queue_topic="t",
        duration_seconds=3.0, rate_hz=1.0, mode="q-only",
    )
    assert result.queue_posts == 2  # ticks 1 + 3 succeeded
    assert result.queue_errors == 1
    assert result.last_error == "transient network error"


def test_run_kv_put_failure_does_not_bypass_pacer(fake_clock):
    """Regression test (caught during Sprint 6 smoke bring-up): a
    failing kv PUT must NOT short-circuit the per-tick pacing.
    Previously the loop did `continue` on PUT-error, which skipped
    the sleep — a fast-failing PUT (connection refused) then drove
    the loop at ~4000 Hz instead of the configured 1 Hz. The rate
    ceiling is a safety guarantee; it has to hold under errors too.

    Fake-clock setup: every PUT fails. Across a 5-second window at
    1 Hz, we expect ~5 ticks regardless of per-tick error count."""
    stub = _StubClient()
    # Every PUT fails — drives the error path.
    stub.fail_put_at = {i: f"refused on tick {i}" for i in range(1, 1000)}
    result = synth.run(
        client=stub,
        kv_key="k",
        duration_seconds=5.0, rate_hz=1.0, mode="kv-only",
    )
    # 5s × 1Hz = ~5 ticks. Loop must NOT have looped thousands of
    # times despite every PUT failing.
    assert result.kv_put_errors == 5
    assert result.kv_puts == 0
    assert result.kv_gets == 0  # PUT failed → GET correctly skipped


def test_run_counts_kv_round_trip_mismatch(fake_clock):
    """If the GET returns something other than what we just PUT,
    that's a server-side bug — recorded as a mismatch, not a
    generic error."""
    stub = _StubClient()
    # Override get() to return wrong value for tick 1.
    original_get = stub.get
    call_count = {"n": 0}

    def faulty_get(key):
        call_count["n"] += 1
        if call_count["n"] == 1:
            stub._get_calls += 1
            stub.kv_gets.append(key)
            return "wrong-value"
        return original_get(key)

    stub.get = faulty_get
    result = synth.run(
        client=stub,
        kv_key="k",
        duration_seconds=2.0, rate_hz=1.0, mode="kv-only",
    )
    assert result.kv_get_mismatches == 1
    assert "mismatch" in (result.last_error or "")


# ----- duration parsing -----

def test_parse_duration_bare_number_is_seconds():
    assert synth.parse_duration("30") == 30.0
    assert synth.parse_duration("0.5") == 0.5


def test_parse_duration_with_suffixes():
    assert synth.parse_duration("30s") == 30.0
    assert synth.parse_duration("5m") == 300.0
    assert synth.parse_duration("1h") == 3600.0


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        synth.parse_duration("garbage")
    with pytest.raises(ValueError):
        synth.parse_duration("")
    with pytest.raises(ValueError):
        synth.parse_duration("5x")


# ----- CLI dispatch -----

def test_cli_dispatch_routes_synth_traffic(monkeypatch):
    """`stra2us synth-traffic ...` routes to cmd_synth_traffic.
    Doesn't actually run the loop — just verifies the verb wires
    through to the right handler."""
    called = {"with": None}

    def fake_cmd(args):
        called["with"] = args
        return 0

    monkeypatch.setattr(cli_module, "cmd_synth_traffic", fake_cmd)
    rc = cli_module.main([
        "synth-traffic",
        "--queue", "critterchron/public/heartbeep",
        "--duration", "10s",
        "--rate", "2",
        "--mode", "q-only",
    ])
    assert rc == 0
    assert called["with"] is not None
    assert called["with"].verb == "synth-traffic"
    assert called["with"].queue == "critterchron/public/heartbeep"
    assert called["with"].duration == "10s"
    assert called["with"].rate == "2"
    assert called["with"].mode == "q-only"


def test_cli_dispatch_defaults():
    """Verify the argparse defaults are sensible — operator running
    `synth-traffic --queue X` gets a 30s run at 1 Hz in both mode."""
    parser = cli_module._build_parser()
    args = parser.parse_args([
        "synth-traffic",
        "--queue", "t",
        "--kv-key", "k",
    ])
    assert args.duration == "30s"
    assert args.rate == "1"
    assert args.mode == "both"
    assert args.allow_high_rate is False
