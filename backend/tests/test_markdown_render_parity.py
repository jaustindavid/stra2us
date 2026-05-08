# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Parity test: vendored backend markdown sanitizer must match
the canonical CLI implementation.

The CLI's `tools/stra2us_cli/sanitizers/markdown.py` is the
authoritative source. The backend has a vendored copy at
`backend/src/services/markdown_render.py` because the docker
build context can't reach `tools/`. This test imports both and
asserts byte-equal output on the FR's XSS corpus + a few
allowlist edge cases — if anyone edits one without the other,
CI fails loudly.

Tracked as a P3 followup: consolidate by changing the docker
build context to the repo root and pip-installing
`tools/stra2us_cli` into the backend image.
"""

from __future__ import annotations

import os
import sys

import pytest

# Add `tools/` to sys.path so we can import the canonical CLI
# version. Backend runtime never does this; tests do because the
# parity check needs both copies side by side.
_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.normpath(os.path.join(_HERE, "..", "..", "tools"))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from services.markdown_render import sanitize_markdown as backend_sanitize  # noqa: E402
from stra2us_cli.sanitizers.markdown import sanitize_markdown as cli_sanitize  # noqa: E402


# Subset of the FR's XSS corpus + a few representative happy-path
# cases. Stays in lockstep with `tools/tests/test_sanitizer_markdown.py`.
_CORPUS = [
    # Happy paths
    "## Heading\n\nHello [docs](https://example.com/x).",
    "Hello **world** _italic_ `code`.",
    "- a\n- b\n\n> quoted",
    "![logo](logo.svg)",
    "![logo](/app/demo/_assets/logo.svg)",
    # XSS corpus
    "<script>alert(1)</script>",
    '<a href="https://x.example" onclick="bad()">x</a>',
    '<iframe src="https://x.example"></iframe>',
    "<style>body { background: red }</style>",
    "[click](javascript:alert(1))",
    "[click](data:text/html,<script>alert(1)</script>)",
    "[home](/admin)",
    "![bad](https://attacker.example.com/x.png)",
    "![x](../etc/passwd)",
    # Malformed
    "<<script>alert(1)<<<",
    # Markdown chars
    "Pipes | and `code with backticks`",
]


@pytest.mark.parametrize("source", _CORPUS)
def test_byte_equal_output_for_corpus(source):
    """Identical input → identical output. If a future change
    tweaks one side and not the other, the difference shows up
    here in the diff."""
    assert backend_sanitize(source, app="demo") == cli_sanitize(source, app="demo")


def test_max_bytes_behavior_matches():
    """Both copies must enforce the same byte cap with the same
    exception type."""
    big = "x" * 100
    from services.markdown_render import MarkdownSanitizeError as BackendErr
    from stra2us_cli.sanitizers.markdown import MarkdownSanitizeError as CliErr
    with pytest.raises(BackendErr):
        backend_sanitize(big, app="demo", max_bytes=10)
    with pytest.raises(CliErr):
        cli_sanitize(big, app="demo", max_bytes=10)


def test_image_rewrite_uses_per_app_path():
    """Both copies should rewrite bare-filename `<img>` to the
    requesting app's namespace. Catches a regression where one
    side gets the app slug wrong."""
    src = "![logo](logo.svg)"
    a = backend_sanitize(src, app="appA")
    b = cli_sanitize(src, app="appA")
    assert a == b
    assert "/app/appA/_assets/logo.svg" in a
