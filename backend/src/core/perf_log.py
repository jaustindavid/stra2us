"""Lightweight perf logging — slow requests get one XADD to system:perf_log.

The middleware in main.py records every request's total wall time and writes
an entry to the perf-log stream when the threshold is exceeded. Endpoints
that want sub-phase breakdowns use the PerfPhases helper:

    phases = PerfPhases(request)
    with phases.phase("redis_keys"):
        raw_keys = await redis.keys(pattern)
    with phases.phase("strlen_loop"):
        for k in raw_keys:
            size = await redis.strlen(k)

PerfPhases attaches its dict to request.state.perf_phases; the middleware
folds it into the stream entry's `phase_breakdown` field. Same name twice
accumulates (covers loops). Endpoints not using PerfPhases get total_ms
only — zero overhead.

Storage matches the system:activity_log idiom: capped via XADD MAXLEN,
plus an age-based XTRIM on each write. No new infrastructure.
"""
import contextlib
import json
import os
import time

from core.redis_client import get_redis_client

PERF_LOG_STREAM = "system:perf_log"
PERF_LOG_MAXLEN = 5000
PERF_LOG_RETENTION_SEC = 24 * 60 * 60

DEFAULT_THRESHOLD_MS = float(os.environ.get("STRA2US_PERF_LOG_THRESHOLD_MS", "100"))


class PerfPhases:
    """Per-request collector for sub-phase timings.

    Construct once at the top of an endpoint, then wrap each phase in
    `with phases.phase("name"):`. Repeated phases with the same name
    sum (intentional — covers per-iteration timing inside a loop).
    """

    def __init__(self, request):
        request.state.perf_phases = {}
        self._phases = request.state.perf_phases

    @contextlib.contextmanager
    def phase(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._phases[name] = round(self._phases.get(name, 0) + elapsed_ms, 2)


async def write_perf_entry(
    *,
    method: str,
    path: str,
    total_ms: float,
    status_code: int,
    client_id: str,
    phases: dict | None = None,
) -> None:
    """Append one entry to the perf-log stream. Failure is swallowed by
    the caller — perf logging must never break a request."""
    redis = get_redis_client()
    fields = {
        "timestamp": str(int(time.time())),
        "method":    method,
        "path":      path,
        "total_ms":  f"{total_ms:.2f}",
        "status":    str(status_code),
        "client_id": client_id,
    }
    if phases:
        fields["phase_breakdown"] = json.dumps(phases, separators=(",", ":"))
    await redis.xadd(PERF_LOG_STREAM, fields, maxlen=PERF_LOG_MAXLEN, approximate=True)
    min_id = str((int(time.time()) - PERF_LOG_RETENTION_SEC) * 1000)
    await redis.xtrim(PERF_LOG_STREAM, minid=min_id)
