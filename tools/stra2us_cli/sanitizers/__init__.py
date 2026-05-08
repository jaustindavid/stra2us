# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Catalog content sanitizers (see docs/fr_catalog_app_ui.md).

Two surfaces:

* `markdown` — render catalog markdown blobs (`ui.header_markdown`,
  `ui.footer_markdown`, per-field `help_markdown`) into a hardened
  HTML allowlist. Run server-side at render time.
* `svg` — re-parse and re-serialize SVG assets through a
  hand-rolled allowlist walker. Run client-side (CLI) at publish
  time; rejected SVGs fail the publish.

One implementation, two callers — both the CLI (publish-time SVG
check, optional markdown preview) and the backend (render-time
markdown rendering) import from here.
"""

from .markdown import sanitize_markdown, MarkdownSanitizeError
from .svg import sanitize_svg, SvgSanitizeError

__all__ = [
    "sanitize_markdown",
    "MarkdownSanitizeError",
    "sanitize_svg",
    "SvgSanitizeError",
]
