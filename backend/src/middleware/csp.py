# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Content Security Policy middleware.

Stra2us today ships **no** CSP header.
`docs/fr_catalog_app_ui.md` introduces one because the markdown
renderer and theme paths land in territory where CSP is the
difference between "safe by construction" and "safe pending the
next sanitizer CVE."

P0 ships this as `Content-Security-Policy-Report-Only` on every
response, with reports posted to `/api/_csp_report` (logged at
WARNING). P5 audits surfaces, fixes violations the audit reveals,
and flips to enforcing.

The customer-facing `/app/<app>/<device>/...` route is "new
template territory" per the FR — it ships CSP-clean by
construction and is allowlisted to enforcing-mode early (callers
pass `enforce_path_prefixes` from main.py). Admin/api routes stay
Report-Only until P5.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# The full FR policy. Order is informational; browsers don't care.
# Update this list and `docs/fr_catalog_app_ui.md` together.
DEFAULT_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("script-src", "'self'"),
    ("style-src", "'self'"),
    ("img-src", "'self'"),
    ("font-src", "'self'"),
    ("connect-src", "'self'"),
    ("frame-ancestors", "'none'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("object-src", "'none'"),
)

# Where browsers send violation reports. Path-only — the browser
# emits a same-origin POST. Kept under the public `/api/` namespace
# so reports can land without the auth gate intercepting them
# (`/api/_csp_report` is added to the public list in main.py).
REPORT_PATH = "/api/_csp_report"

# Logger name kept stable for grep-ability and for log routing
# rules. The CSP report endpoint emits at WARNING so the existing
# log shipper picks it up without extra config.
_logger = logging.getLogger("stra2us.csp")


def build_policy(*, report_path: str = REPORT_PATH) -> str:
    """Serialize `DEFAULT_DIRECTIVES` plus `report-uri` / `report-to`
    into a single header value. The same string is used for both
    `Content-Security-Policy` and `Content-Security-Policy-Report-Only`
    — the only difference between enforcing and reporting modes is
    which header name we emit it under.
    """
    parts = [f"{name} {value}" for name, value in DEFAULT_DIRECTIVES]
    # `report-uri` is the older directive (Level 2) but every
    # current browser still honors it. `report-to` is the newer
    # mechanism but requires a `Reporting-Endpoints` header to bind
    # the group; emitting both keeps coverage simple. The path is
    # same-origin, so the browser POSTs to us directly.
    parts.append(f"report-uri {report_path}")
    parts.append(f"report-to csp-endpoint")
    return "; ".join(parts)


class CSPMiddleware(BaseHTTPMiddleware):
    """Attach the FR's CSP to every response.

    Args:
      app: ASGI app (Starlette/FastAPI hands this in via
        `add_middleware`).
      enforce_path_prefixes: paths whose responses should carry the
        enforcing `Content-Security-Policy` header instead of
        `-Report-Only`. P0 leaves this empty — every response is
        Report-Only. P3 adds the customer-facing `/app/<app>/`
        prefix (the new template, CSP-clean by construction). P5
        flips remaining surfaces by emptying the parallel
        `report_only_path_prefixes` allowlist or by passing an
        `enforce_default=True` flag.
      enforce_default: when True, every path that doesn't appear
        in `report_only_path_prefixes` gets the enforcing header.
        P0 ships False; P5 flips to True.
      report_only_path_prefixes: paths that *stay* in Report-Only
        even when `enforce_default=True`. Empty in P0; populated by
        P5 with the audit's pending-fix list (if any).
      report_path: where `report-uri` points. Default
        `/api/_csp_report`; tests pass an alternate to assert
        header shape.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        enforce_path_prefixes: Iterable[str] = (),
        enforce_default: bool = False,
        report_only_path_prefixes: Iterable[str] = (),
        report_path: str = REPORT_PATH,
    ) -> None:
        super().__init__(app)
        self._enforce_prefixes = tuple(enforce_path_prefixes)
        self._enforce_default = enforce_default
        self._report_only_prefixes = tuple(report_only_path_prefixes)
        self._policy = build_policy(report_path=report_path)

    def _header_name(self, path: str) -> str:
        """Decide which header to emit for `path`. Enforcing wins
        over Report-Only when both prefix lists match — the FR's
        intent under P5 is "if we're confident enough to enforce a
        path, that confidence shouldn't be overridden by an
        explicit report-only override on the same path."
        """
        if any(path.startswith(p) for p in self._enforce_prefixes):
            return "Content-Security-Policy"
        if self._enforce_default and not any(
            path.startswith(p) for p in self._report_only_prefixes
        ):
            return "Content-Security-Policy"
        return "Content-Security-Policy-Report-Only"

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        header = self._header_name(request.url.path)
        response.headers[header] = self._policy
        # `Reporting-Endpoints` binds the `report-to` group named
        # `csp-endpoint` in the policy string. Same value on every
        # response — cheap, and the spec requires the binding to
        # exist for the directive to be honored.
        response.headers["Reporting-Endpoints"] = (
            f'csp-endpoint="{REPORT_PATH}"'
        )
        return response


# ----- report sink -----

router = APIRouter()


@router.post(REPORT_PATH, include_in_schema=False)
async def csp_report(request: Request) -> Response:
    """Receive a CSP violation report from the browser.

    Browsers post one of two shapes:
      * `application/csp-report` (Level 2): `{"csp-report": {...}}`
      * `application/reports+json` (Level 3): `[{"type": "csp-violation", "body": {...}}, ...]`

    We accept either, log at WARNING with the parsed body so the
    structured-log shipper picks it up, and return 204. P5 reads
    these to populate the audit checklist; P0 just records them.
    """
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else None
    except json.JSONDecodeError:
        body = {"_unparsed": body_bytes.decode("utf-8", errors="replace")}
    _logger.warning(
        "csp_violation",
        extra={
            "csp_report": body,
            "ua": request.headers.get("user-agent", ""),
            "referer": request.headers.get("referer", ""),
        },
    )
    return Response(status_code=204)
