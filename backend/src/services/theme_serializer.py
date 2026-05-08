# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Per-app theme CSS serializer.

Lives at the heart of P2 of `docs/fr_catalog_app_ui_plan.md` /
`docs/fr_catalog_app_ui.md` "Content Security Policy" → "Theme CSS
serialization is data-not-string."

Builds a single CSS rule of the form:

    [data-app="<slug>"] {
        --app-primary: #5b3fb8;
        --app-accent:  #ffb86c;
        --app-bg:      #f7f3eb;
        --app-text:    #2a2a2a;
        --app-font:    system-ui;
    }

…from a validated theme dict. The shape is parameterized — values
flow through per-key validators that re-check the lint allowlist
(hex format / font allowlist) before emitting. A value that
*somehow* bypassed the publish-time lint must not be the only
thing standing between catalog input and a CSS-injection class of
bug. If a value fails re-validation here, the declaration is
dropped silently; the page falls back to the stra2us default via
the `var(--app-x, <stra2us-default>)` pattern in
`backend/src/static/app/styles.css`.

Why no string-concat of raw catalog input: the FR explicitly
calls this out. Even validated hex colors should pass through a
re-emit step rather than `f"{value}"` interpolation, so the trust
boundary is data → declaration, never string → CSS.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# These re-validators mirror catalog_lint's regexes and allowlists.
# Duplicated rather than imported from `stra2us_cli.catalog_lint`
# so the backend's import surface stays clean and the validation
# is independent — a buggy publish-time lint shouldn't be able to
# emit a bad declaration just because the publish-time check
# passed.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FONT_ALLOWLIST = frozenset({"system-ui", "sans-serif", "serif", "monospace"})

# App slug shape — must match the existing catalog APP_NAME_RE
# (`^[a-z][a-z0-9_]*$`). Selector-safe for `[data-app="…"]` with no
# escaping needed, by construction. The validator is cheap; we run
# it anyway so the serializer doesn't trust its caller.
_APP_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Mapping from catalog `theme.<key>` → CSS custom property name +
# value-shape validator. Order is intentional — output is stable
# across runs so cache headers + ETags work cleanly.
_DECLARATIONS: tuple[tuple[str, str, str], ...] = (
    # (theme key,         css var,          validator name)
    ("primary_color",     "--app-primary",  "color"),
    ("accent_color",      "--app-accent",   "color"),
    ("bg_color",          "--app-bg",       "color"),
    ("text_color",        "--app-text",     "color"),
    ("font_family",       "--app-font",     "font"),
)


def _safe_color(value: str) -> str | None:
    """Return the validated hex color string, or None if it's
    something the lint would have caught (or that lint missed but
    the serializer should still refuse). Matches `_HEX_COLOR_RE`
    *exactly* — no surrounding whitespace, no semicolons, no
    function syntax."""
    if not isinstance(value, str):
        return None
    if not _HEX_COLOR_RE.fullmatch(value):
        return None
    return value


def _safe_font(value: str) -> str | None:
    """Return the validated font-family name, or None. Allowlisted
    generic family names only — no web fonts, no quoted names, no
    fallback chains. The base stylesheet handles the fallback chain
    via `var(--app-font, <stra2us-default-stack>)`."""
    if not isinstance(value, str):
        return None
    if value not in _FONT_ALLOWLIST:
        return None
    return value


_VALIDATORS = {
    "color": _safe_color,
    "font": _safe_font,
}


def serialize_theme_css(app_slug: str, theme: dict[str, Any] | None) -> str:
    """Render the per-app theme CSS rule.

    Args:
      app_slug: catalog app name. Validated against `_APP_SLUG_RE`;
        a slug that doesn't match returns the empty string (no
        rule) — the route handler 404s upstream so this is
        defense-in-depth.
      theme: parsed `theme:` dict from the catalog YAML, or None
        when the catalog has no theme block. None / empty → an
        empty-rule body, which the customer page handles fine via
        `var(--app-x, <fallback>)`.

    Returns the CSS file body as a single string. Always ends with
    a newline. Empty input still produces a syntactically-valid
    selector with an empty body.
    """
    if not _APP_SLUG_RE.fullmatch(app_slug):
        return ""

    declarations: list[str] = []
    if theme:
        for key, css_var, kind in _DECLARATIONS:
            raw = theme.get(key)
            if raw is None:
                continue
            validator = _VALIDATORS[kind]
            value = validator(raw)
            if value is None:
                continue
            declarations.append(f"  {css_var}: {value};")

    selector = f'[data-app="{app_slug}"]'
    if declarations:
        body = "\n".join(declarations)
        return f"{selector} {{\n{body}\n}}\n"
    # Empty body still emits a (valid) rule so callers can confirm
    # the route exists + the catalog is reachable. CSS engines
    # accept `[data-app="x"] { }`.
    return f"{selector} {{\n}}\n"


def theme_hash(theme: dict[str, Any] | None) -> str:
    """SHA-256-prefix hash of the serialized theme block.

    Used as the cache-bust `?v=<hash>` query parameter on the
    `_theme.css` URL the page wrapper emits. The hash bumps when
    any theme key changes (or the order is rewritten — JSON's
    sort_keys handles that). The first 8 hex chars are
    sufficient; collisions across 16M values per app are a
    cache-correctness issue at most, never a security issue.

    Returns the hex prefix even for `None` / empty themes — every
    page render gets a stable cache key.
    """
    blob = json.dumps(theme or {}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]
