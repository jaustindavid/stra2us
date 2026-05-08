# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Markdown → sanitized HTML.

**Vendored copy.** Canonical source lives at
`tools/stra2us_cli/sanitizers/markdown.py`; the backend's
docker-compose build context is `./backend`, so `tools/` isn't
reachable at image-build time. Rather than restructuring the
build, we vendor the sanitizer here and add a drift test
(`backend/tests/test_markdown_render_parity.py`) that compares
output against the canonical version on the FR's XSS corpus.
The test imports both and fails if they disagree, so any change
to the CLI side that doesn't land here will fail CI.

Marked as a P3 followup in `docs/fr_catalog_app_ui_progress.md`:
consolidate by switching the docker-compose build context to the
repo root and `pip install -e tools/` at image build time. That
collapses the two copies into one without losing the import-clean
posture for the rest of the backend.

Two-stage pipeline (the pragmatic Python recipe per the FR):

1. `markdown-it-py` renders markdown to HTML. Configured with the
   "zero" preset plus a small set of inlines/blocks; raw HTML
   passthrough disabled.
2. `bleach` cleans the HTML against a strict tag + attribute
   allowlist drawn from `docs/fr_catalog_app_ui.md` "Sanitization
   allowlist". Anything outside the allowlist is dropped.
3. Post-process: enforce `<a href>` shape (https-only absolute URLs;
   relative / same-origin links unwrapped to plain text), add
   `rel="noopener noreferrer" target="_blank"` to surviving links,
   and rewrite `<img src>` to the same-origin asset URL when the src
   is a bare filename.

The catalog provides values; the server provides selectors and
tags. Strict CSP (`script-src 'self'`, `style-src 'self'`) covers
us if anything ever slips past the allowlist; this is the first
line of defense.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import bleach
from markdown_it import MarkdownIt

# Matches the FR's "Markdown blocks → Allowed tags" table. Every
# row appears here exactly once; if you grow the list, update the
# FR + the sanitizer test corpus together.
_ALLOWED_TAGS = frozenset({
    "p", "br", "hr",
    "strong", "em", "code", "del",
    "h2", "h3", "h4",  # no h1 — page already has one
    "ul", "ol", "li", "blockquote", "pre",
    "a",
    "img",
})

# Per-tag attribute allowlist. `a.href` and `img.src/alt` are the
# only attributes we accept anywhere in catalog markdown.
_ALLOWED_ATTRS = {
    "a": ["href"],
    "img": ["src", "alt"],
}

# Catalog filename allowlist — must match catalog_lint.ASSET_FILENAME_RE.
# Duplicated here to keep the sanitizer importable without depending on
# the lint module (and vice versa). Kept tight on purpose: the only
# img.src shapes we accept are this filename pattern (which we'll
# rewrite to the asset URL) or the literal `/app/<app>/_assets/<file>`
# absolute path (which we'll accept as-is).
_ASSET_FILENAME_RE = re.compile(r"^(?!\.)[a-z0-9._-]{1,64}$")
_ALT_MAX_LEN = 200


class MarkdownSanitizeError(ValueError):
    """Raised when the input is structurally rejected (e.g. exceeds
    the size cap before parsing). Tag/attr stripping never raises —
    the sanitizer drops disallowed material silently per the FR."""


def _make_renderer() -> MarkdownIt:
    """Render markdown to HTML with raw HTML passthrough OFF.

    `html=False` means embedded `<script>` etc. in the markdown
    source is rendered as text, not parsed as HTML — first line of
    defense before bleach. We use the "default" preset for normal
    markdown features (headings, lists, links, images, code) and
    explicitly disable autolinking-as-html since we do our own
    href validation downstream.
    """
    md = MarkdownIt("default", {"html": False, "linkify": False, "breaks": False})
    # Disable inline HTML, raw block HTML, and autolinks. The
    # default preset already excludes these in `html=False` mode for
    # the most part; being explicit guards against upstream changes.
    md.disable(["html_inline", "html_block"], ignoreInvalid=True)
    return md


_RENDERER = _make_renderer()


def _is_safe_https(url: str) -> bool:
    """True for absolute https URLs with a real host. Catches the
    relative-path and same-origin cases the FR strips, plus the
    `javascript:` / `data:` / `vbscript:` URL schemes that the
    layered defenses already reject (bleach's protocol allowlist
    catches them too — this is belt-and-suspenders).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    if not parsed.netloc:
        return False
    return True


def _allow_href(tag: str, name: str, value: str) -> bool:
    """bleach attribute filter for `a.href`. We accept only
    well-formed absolute https URLs; everything else is dropped (the
    `<a>` becomes an `<a>` with no href, which the post-pass unwraps
    to plain text)."""
    if tag != "a" or name != "href":
        return False
    return _is_safe_https(value)


def _allowed_attribute(tag: str, name: str, value: str) -> bool:
    """bleach uses this for every (tag, attr, value) triple. Dispatch
    on tag — `a.href` runs through the URL filter; `img.src/alt` are
    handled in post-process so we have the app slug in scope (bleach
    at this stage doesn't know it). Everything else from the
    declared per-tag allowlist passes through."""
    if tag == "a" and name == "href":
        return _allow_href(tag, name, value)
    if tag == "img" and name in ("src", "alt"):
        # Validated in post-process where we know the app slug.
        return True
    return name in _ALLOWED_ATTRS.get(tag, [])


_HREF_RE = re.compile(r"\shref=\"([^\"]*)\"")
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r"(\w+)=\"([^\"]*)\"")


def _resolve_img_src(src: str, app: str) -> str | None:
    """Return the canonical same-origin asset URL for an `<img src>`,
    or None to drop the image.

    Accepts:
      * Bare filename matching the asset filename allowlist
        (`logo.svg`) — rewrites to `/app/<app>/_assets/logo.svg`.
      * Already-canonical absolute path
        (`/app/<app>/_assets/<filename>`) — accepted as-is.

    Rejects everything else (other absolute paths, http(s) URLs,
    `data:`, `javascript:`, `..`, query strings, etc.). The FR's
    asset model is "self-hosted only"; the markdown sanitizer is
    where that promise is enforced for inline images.
    """
    if _ASSET_FILENAME_RE.match(src):
        return f"/app/{app}/_assets/{src}"
    expected_prefix = f"/app/{app}/_assets/"
    if src.startswith(expected_prefix):
        rest = src[len(expected_prefix):]
        if _ASSET_FILENAME_RE.match(rest):
            return src
    return None


def _post_process(html: str, app: str) -> str:
    """Apply post-bleach rules that need string-level surgery:

    * `<a>` with no `href` (URL was stripped by the filter): unwrap
      so the text content survives but the link tag is gone.
    * `<a href="https://...">`: add `rel="noopener noreferrer"` and
      `target="_blank"` per the FR. (bleach can be told to do this
      via `add_nofollow`, but we want the exact rel value the FR
      specifies; the regex is small and the source is already-clean
      bleach output, not arbitrary HTML.)
    * `<img>`: rewrite `src` to the asset URL or drop the image
      entirely if the src isn't a recognized asset reference. Cap
      `alt` length.
    """
    # Unwrap `<a>` tags missing href (bleach left the tag because
    # `<a>` is allowed, but our href filter rejected the value).
    html = re.sub(r"<a>(.*?)</a>", r"\1", html, flags=re.DOTALL)

    def _enrich_link(match: re.Match[str]) -> str:
        href = match.group(1)
        return f' href="{href}" rel="noopener noreferrer" target="_blank"'

    html = re.sub(r'\shref="([^"]*)"', _enrich_link, html)

    def _rewrite_img(match: re.Match[str]) -> str:
        attrs_blob = match.group(1)
        src: str | None = None
        alt: str | None = None
        for am in _ATTR_RE.finditer(attrs_blob):
            name = am.group(1).lower()
            value = am.group(2)
            if name == "src":
                src = value
            elif name == "alt":
                alt = value[:_ALT_MAX_LEN]
        if src is None:
            return ""  # drop entirely
        resolved = _resolve_img_src(src, app)
        if resolved is None:
            return ""
        if alt is None:
            return f'<img src="{resolved}">'
        # `alt` already passed bleach (text content); HTML-escape
        # the quote we'll wrap it in.
        safe_alt = alt.replace('"', "&quot;")
        return f'<img src="{resolved}" alt="{safe_alt}">'

    html = _IMG_RE.sub(_rewrite_img, html)
    return html


def sanitize_markdown(source: str, *, app: str, max_bytes: int | None = None) -> str:
    """Render `source` (raw markdown) to a sanitized HTML fragment.

    Args:
      source: raw markdown text from a catalog field.
      app: app slug — used to resolve inline `<img>` references to
        `/app/<app>/_assets/<filename>`.
      max_bytes: optional hard cap; raises `MarkdownSanitizeError` if
        the source exceeds it. Lint enforces the cap at publish; this
        argument is for callers that want a render-time defense in
        depth (in case lint is ever skipped).

    Returns the cleaned HTML as a string. Suitable for direct
    inclusion in a server-rendered template; safe under
    `script-src 'self'` and `style-src 'self'` CSP.
    """
    if max_bytes is not None and len(source.encode("utf-8")) > max_bytes:
        raise MarkdownSanitizeError(
            f"markdown source exceeds max_bytes ({max_bytes})"
        )
    raw_html = _RENDERER.render(source)
    cleaned = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_allowed_attribute,
        protocols=["https"],
        strip=True,
        strip_comments=True,
    )
    return _post_process(cleaned, app=app)
