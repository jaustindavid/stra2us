# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""CSP middleware tests (docs/fr_catalog_app_ui.md "Content Security
Policy" + P0 of fr_catalog_app_ui_plan.md).

Asserts the header shape (every directive present, Report-Only by
default in P0), the path-prefix-based mode switch (so P3 can ship
the customer-facing route in enforcing mode without affecting other
routes), and the `/api/_csp_report` sink that browsers POST
violations to.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from middleware.csp import (
    DEFAULT_DIRECTIVES,
    CSPMiddleware,
    REPORT_PATH,
    build_policy,
    router as csp_router,
)


def _app(**csp_kw):
    app = FastAPI()
    app.add_middleware(CSPMiddleware, **csp_kw)
    app.include_router(csp_router)

    @app.get("/")
    def root():
        return PlainTextResponse("hi")

    @app.get("/app/critterchron/dev")
    def app_route():
        return PlainTextResponse("hi")

    @app.get("/admin/dashboard")
    def admin_route():
        return PlainTextResponse("hi")

    return app


# ----- header shape -----

def test_every_fr_directive_present_in_policy_string():
    """Every directive named in the FR + a `report-uri` must appear
    in the serialized policy. Update DEFAULT_DIRECTIVES + the FR
    in lockstep."""
    policy = build_policy()
    for name, value in DEFAULT_DIRECTIVES:
        assert f"{name} {value}" in policy
    assert "report-uri /api/_csp_report" in policy
    assert "report-to csp-endpoint" in policy


def test_no_unsafe_inline_or_eval():
    """Strict CSP is the whole point. If anyone ever adds
    'unsafe-inline' or 'unsafe-eval' to the default directives,
    this test should be updated and the FR's "Content Security
    Policy" notes section should explain why."""
    policy = build_policy()
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy


def test_cloudflare_insights_allowlisted_only_where_needed():
    """The CF Browser Insights beacon needs `script-src` (loads
    `beacon.min.js`) + `connect-src` (reports back). The host
    must NOT appear in any other directive — keeps the
    third-party trust as narrow as possible. If a future change
    adds `static.cloudflareinsights.com` to `img-src` or
    `frame-src` etc, that's a meaningful CSP relaxation; this
    test fails to make it visible."""
    policy = build_policy()
    cf_host = "static.cloudflareinsights.com"
    # Required in these two directives.
    for directive in ("script-src", "connect-src"):
        # `<directive> '...' ... <cf_host> ...;` — the host is
        # immediately after `'self'` in the source list.
        assert f"{directive} 'self' https://{cf_host}" in policy, (
            f"{directive} must allowlist {cf_host}"
        )
    # NOT in any other directive — count occurrences should be
    # exactly two (script-src + connect-src).
    assert policy.count(cf_host) == 2, (
        f"{cf_host} appeared in unexpected directive(s)"
    )


def test_report_only_header_emitted_by_default():
    client = TestClient(_app())
    r = client.get("/")
    assert r.status_code == 200
    assert "Content-Security-Policy-Report-Only" in r.headers
    assert "Content-Security-Policy" not in r.headers
    assert "default-src 'self'" in r.headers["Content-Security-Policy-Report-Only"]


def test_reporting_endpoints_header_present():
    client = TestClient(_app())
    r = client.get("/")
    assert "Reporting-Endpoints" in r.headers
    assert "csp-endpoint=" in r.headers["Reporting-Endpoints"]


# ----- mode switch by path prefix (the P3 / P5 hooks) -----

def test_enforce_path_prefix_uses_enforcing_header():
    """P3 will pass `/app/<app>/<device>/` here so the customer-facing
    route ships in enforcing mode while admin/api stay Report-Only."""
    client = TestClient(_app(enforce_path_prefixes=["/app/"]))
    r = client.get("/app/critterchron/dev")
    assert "Content-Security-Policy" in r.headers
    assert "Content-Security-Policy-Report-Only" not in r.headers


def test_other_paths_remain_report_only_when_enforce_prefix_set():
    client = TestClient(_app(enforce_path_prefixes=["/app/"]))
    r = client.get("/admin/dashboard")
    assert "Content-Security-Policy-Report-Only" in r.headers
    assert "Content-Security-Policy" not in r.headers


def test_enforce_default_flips_everything_except_overrides():
    """P5's eventual flip: enforce by default, with a small override
    list of paths that need to stay in Report-Only while the audit
    catches up."""
    client = TestClient(_app(
        enforce_default=True,
        report_only_path_prefixes=["/admin/"],
    ))
    r1 = client.get("/")
    assert "Content-Security-Policy" in r1.headers
    assert "Content-Security-Policy-Report-Only" not in r1.headers

    r2 = client.get("/admin/dashboard")
    assert "Content-Security-Policy-Report-Only" in r2.headers


# ----- report sink -----

def test_csp_report_endpoint_accepts_level2_shape():
    """Browsers shipping `Content-Security-Policy-Report-Only` post
    a `{"csp-report": {...}}` body to `report-uri`."""
    client = TestClient(_app())
    body = {
        "csp-report": {
            "document-uri": "https://stra2us.example/admin/",
            "violated-directive": "script-src 'self'",
            "blocked-uri": "inline",
        }
    }
    r = client.post(REPORT_PATH, json=body,
                    headers={"content-type": "application/csp-report"})
    assert r.status_code == 204


def test_csp_report_endpoint_accepts_level3_reports_json():
    """Newer browsers post `application/reports+json` with a list of
    report envelopes per the Reporting API."""
    client = TestClient(_app())
    body = [{
        "type": "csp-violation",
        "url": "https://stra2us.example/admin/",
        "body": {
            "documentURL": "https://stra2us.example/admin/",
            "effectiveDirective": "script-src",
            "blockedURL": "inline",
        }
    }]
    r = client.post(REPORT_PATH, json=body,
                    headers={"content-type": "application/reports+json"})
    assert r.status_code == 204


def test_csp_report_endpoint_logs_violation(caplog):
    """The middleware's contract is "WARNING-level structured log on
    every violation." The log shipper's job is to forward; the test
    just confirms the log line appears so the wiring is intact."""
    import logging
    client = TestClient(_app())
    with caplog.at_level(logging.WARNING, logger="stra2us.csp"):
        client.post(REPORT_PATH, json={"csp-report": {"violated-directive": "x"}})
    assert any("csp_violation" in r.message for r in caplog.records)


# ----- P5: actual main.py wiring -----

def test_main_app_enforces_csp_on_customer_route():
    """Regression test for P5's wiring in main.py. Customer-facing
    `/app/*` ships enforcing; admin/api stay Report-Only until
    the admin audit + cleanup land. If a future change drops the
    `enforce_path_prefixes` argument, this test fails loudly so
    the customer page doesn't silently regress to Report-Only."""
    import sys
    sys.path.insert(0, "src")
    import main
    client = TestClient(main.app)

    # Customer-facing landing (public, no auth) — enforcing.
    r = client.get("/app/")
    assert "Content-Security-Policy" in r.headers
    assert "Content-Security-Policy-Report-Only" not in r.headers

    # Admin-facing /health — Report-Only (admin UI not yet audited).
    r = client.get("/health")
    assert "Content-Security-Policy-Report-Only" in r.headers
    assert "Content-Security-Policy" not in r.headers


def test_csp_report_endpoint_handles_unparsed_body():
    """A malformed body shouldn't 500; we record what we got and
    return 204 like the well-formed path."""
    client = TestClient(_app())
    r = client.post(REPORT_PATH, content=b"not json",
                    headers={"content-type": "application/csp-report"})
    assert r.status_code == 204
