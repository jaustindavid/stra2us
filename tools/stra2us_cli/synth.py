# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Synthetic device-traffic generator (v1.7.2 Sprint 5).

A short-lived job that POSTs signed device traffic to a target
host for a configured duration. Reuses the HMAC + msgpack
plumbing in `client.py` — this module is just the action loop
that drives it at a configured rate over a fixed window.

Use cases (from `docs/roadmap.md` Sprint 5):
* **Staging warm-up.** Quiet staging → activity log empty →
  smoke tests don't exercise much. One-shot synth-traffic
  populates the picture.
* **Foundation for Sprint 6.** The device-flow smoke test
  (v1.7.2 Sprint 6) invokes the same primitives — `run` is
  the shared entry point.
* **Code-path testing.** Validates a device-path change without
  rebooting a real device.

Output: per-tick status line on stderr (rate-aware so high-rate
runs aren't drowned in noise); summary stats on stdout at exit.

Safety:
* Rate-limit ceiling (default 100 Hz; override with
  `--allow-high-rate` if the operator really means it).
* Per-call timeout via the Stra2usClient (default 10s).
* Doesn't print the secret in any log line.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from typing import Iterable

from .client import Stra2usClient, Stra2usError


# Hard ceiling on rate. Operators can override with
# `--allow-high-rate` if they're DDOS'ing their own staging on
# purpose. Default exists so a typo of `--rate 100Hz` doesn't
# accidentally hammer prod.
DEFAULT_MAX_RATE_HZ = 100.0


@dataclasses.dataclass
class SynthResult:
    """Per-run summary stats. Surfaced to the operator on exit;
    also the return value of `run()` so callers (Sprint 6's
    smoke-device wrapper, in particular) can assert success
    programmatically."""
    elapsed_seconds: float
    queue_posts: int = 0
    queue_errors: int = 0
    kv_puts: int = 0
    kv_put_errors: int = 0
    kv_gets: int = 0
    kv_get_errors: int = 0
    kv_get_mismatches: int = 0
    last_error: str | None = None

    @property
    def total_calls(self) -> int:
        return (
            self.queue_posts + self.queue_errors
            + self.kv_puts + self.kv_put_errors
            + self.kv_gets + self.kv_get_errors
        )

    @property
    def total_errors(self) -> int:
        return (
            self.queue_errors + self.kv_put_errors
            + self.kv_get_errors + self.kv_get_mismatches
        )

    def summary_line(self) -> str:
        if self.elapsed_seconds <= 0:
            rate = 0.0
        else:
            rate = self.total_calls / self.elapsed_seconds
        return (
            f"synth-traffic: {self.elapsed_seconds:.1f}s elapsed, "
            f"{self.total_calls} calls "
            f"({self.queue_posts} q-POST, "
            f"{self.kv_puts} kv-PUT, "
            f"{self.kv_gets} kv-GET), "
            f"{self.total_errors} errors "
            f"({rate:.2f} Hz)"
        )


def _now() -> float:
    """Single time source; swapped in tests to make rate-loop
    behavior deterministic."""
    return time.time()


def _sleep(seconds: float) -> None:
    """Single sleep source; swapped in tests."""
    time.sleep(seconds)


def run(
    *,
    client: Stra2usClient,
    queue_topic: str | None = None,
    kv_key: str | None = None,
    duration_seconds: float = 300.0,
    rate_hz: float = 1.0,
    mode: str = "both",
    max_rate_hz: float = DEFAULT_MAX_RATE_HZ,
    progress_stream=None,
) -> SynthResult:
    """Run the action loop. Blocks for `duration_seconds`.

    `mode`:
      * `"q-only"`   — POST to `queue_topic` each tick.
      * `"kv-only"`  — PUT then GET on `kv_key` each tick;
                       verify round-trip.
      * `"both"`     — one POST + one PUT + one GET each tick.

    Rate enforcement: sleeps between ticks to maintain
    `rate_hz`. If a tick takes longer than 1/rate_hz, the next
    tick fires immediately (no catch-up burst). Hard ceiling at
    `max_rate_hz` to prevent typo-DoS.

    Returns a `SynthResult` capturing counts + last error. The
    caller decides what to do on errors — the loop continues
    through transient failures (sane for a soak / warm-up
    job) and surfaces totals at the end.
    """
    if rate_hz <= 0:
        raise ValueError(f"rate_hz must be positive (got {rate_hz})")
    if rate_hz > max_rate_hz:
        raise ValueError(
            f"rate_hz {rate_hz} exceeds the {max_rate_hz} Hz ceiling. "
            f"Pass --allow-high-rate to bypass this guard if you're "
            f"intentionally generating high-rate traffic."
        )
    if duration_seconds <= 0:
        raise ValueError(
            f"duration_seconds must be positive (got {duration_seconds})"
        )

    mode = mode.lower()
    if mode not in ("q-only", "kv-only", "both"):
        raise ValueError(
            f"mode must be one of 'q-only' | 'kv-only' | 'both' (got {mode!r})"
        )
    if mode in ("q-only", "both") and not queue_topic:
        raise ValueError(f"mode={mode!r} requires --queue <topic>")
    if mode in ("kv-only", "both") and not kv_key:
        raise ValueError(f"mode={mode!r} requires --kv-key <path>")

    result = SynthResult(elapsed_seconds=0.0)
    start = _now()
    deadline = start + duration_seconds
    tick_interval = 1.0 / rate_hz
    tick_count = 0

    while True:
        tick_start = _now()
        if tick_start >= deadline:
            break
        tick_count += 1

        if mode in ("q-only", "both"):
            try:
                payload = {
                    "tick": tick_count,
                    "ts": int(tick_start),
                    "synth": True,
                }
                client.post_queue(queue_topic, payload)
                result.queue_posts += 1
            except Stra2usError as e:
                result.queue_errors += 1
                result.last_error = str(e)

        if mode in ("kv-only", "both"):
            # Write a tick-tagged value, read it back, verify
            # round-trip. Distinguishes a network problem from a
            # storage-correctness problem in the error counters.
            #
            # Note: PUT-failure suppresses the GET (round-trip is
            # moot without a write to read back) but we explicitly
            # don't `continue` here — the pacing at the bottom of
            # the loop MUST run on every iteration, otherwise an
            # error-storm bypasses the rate ceiling and hammers the
            # server. (That was a real bug caught during Sprint 6
            # smoke-script bring-up: a connection-refused PUT
            # returned in microseconds, the pacer was skipped, and
            # a "1 Hz" run did ~4000 attempts/s.)
            payload = f"synth-tick-{tick_count}"
            put_ok = False
            try:
                client.put(kv_key, payload)
                result.kv_puts += 1
                put_ok = True
            except Stra2usError as e:
                result.kv_put_errors += 1
                result.last_error = str(e)
            if put_ok:
                try:
                    got = client.get(kv_key)
                    result.kv_gets += 1
                    if got != payload:
                        result.kv_get_mismatches += 1
                        result.last_error = (
                            f"kv round-trip mismatch on {kv_key}: "
                            f"wrote {payload!r}, got {got!r}"
                        )
                except Stra2usError as e:
                    result.kv_get_errors += 1
                    result.last_error = str(e)

        if progress_stream is not None and tick_count % max(1, int(rate_hz)) == 0:
            # One status line per second of wall-clock time at
            # configured rate. Doesn't depend on stderr being a
            # TTY — operators piping into a log file see them too.
            elapsed = _now() - start
            progress_stream.write(
                f"  [{elapsed:6.1f}s] tick {tick_count}: "
                f"q={result.queue_posts}/{result.queue_errors}e "
                f"kv={result.kv_puts}/{result.kv_put_errors}e "
                f"({result.kv_get_mismatches}m)\n"
            )
            progress_stream.flush()

        # Pace.
        tick_elapsed = _now() - tick_start
        sleep_for = tick_interval - tick_elapsed
        if sleep_for > 0:
            # Don't oversleep past the deadline.
            remaining = deadline - _now()
            _sleep(min(sleep_for, max(0.0, remaining)))

    result.elapsed_seconds = _now() - start
    return result


# ----- duration-string parsing --------------------------------------

_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
}


def parse_duration(s: str) -> float:
    """Convert "5m" / "30s" / "1h" / "300" into seconds (float).

    A bare number is treated as seconds. Sole-character unit
    suffixes (`s`, `m`, `h`) scale.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty duration string")
    if s[-1] in _DURATION_UNITS:
        try:
            value = float(s[:-1])
        except ValueError as e:
            raise ValueError(f"invalid duration: {s!r}") from e
        return value * _DURATION_UNITS[s[-1]]
    try:
        return float(s)
    except ValueError as e:
        raise ValueError(f"invalid duration: {s!r}") from e
