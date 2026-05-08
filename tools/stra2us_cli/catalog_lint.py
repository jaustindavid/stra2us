# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Shared catalog lint — one implementation, two callers.

The CLI calls this at `catalog publish`; the backend calls it when a
catalog YAML lands at `_catalog/<app>/catalog.yaml`. Duplicating the
rules in two places is the exact way they drift, and the duplication
has bitten enough projects that it's worth the small upfront packaging
work. See `docs/fr_catalog_app_ui.md` "Implementation outline" step 2.

Lint rules cover:

* Field-level UI hints (`enum`, `min`, `max`, `step`, `widget`,
  `multiline`, `max_length`, `pattern`, `help_markdown`, `write_only`).
* Theme block (`*_color`, `font_family`, `logo_*`, `product_name`).
* UI block (`header_markdown`, `footer_markdown` size caps).
* Asset bundle (file count, file size, total size, content-type
  allowlist, filename shape) — `lint_asset_bundle`.

Errors fail publish. Warnings are surfaced but pass through.
Constraints come from the FR's tables; the parser
(`tools/stra2us_cli/catalog.py`) accepts a permissive shape so cross-
field semantics live in one place that both surfaces share.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Literal

from .catalog import Catalog, EnumChoice, Var

# ----- configuration knobs (env-overridable per docs/fr_catalog_app_ui.md "Configuration") -----

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_set(name: str, default: str) -> set[str]:
    raw = os.environ.get(name) or default
    return {s.strip() for s in raw.split(",") if s.strip()}


MARKDOWN_MAX_BYTES = _env_int("STRA2US_MARKDOWN_MAX_BYTES", 4096)
ASSET_MAX_BYTES = _env_int("STRA2US_ASSET_MAX_BYTES", 262144)  # 256 KiB
ASSET_BUNDLE_MAX_BYTES = _env_int("STRA2US_ASSET_BUNDLE_MAX_BYTES", 2_097_152)  # 2 MiB
ASSET_CONTENT_TYPES = _env_set(
    "STRA2US_ASSET_CONTENT_TYPES",
    "image/svg+xml,image/png,image/jpeg,image/webp",
)
THEME_FONT_ALLOWLIST = _env_set(
    "STRA2US_THEME_FONT_ALLOWLIST",
    "system-ui,sans-serif,serif,monospace",
)

# `#RRGGBB`, `#RGB`, `#RRGGBBAA`, `#RGBA` — alpha forms allowed since
# CSS supports them and they parse as a single hex literal. No
# `rgb()`, `var()`, or any function syntax. Case-insensitive.
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# Filename for assets — see "Assets" table in the FR. Lowercase,
# digits, dot/underscore/dash, no leading dot, ≤64 chars.
ASSET_FILENAME_RE = re.compile(r"^(?!\.)[a-z0-9._-]{1,64}$")

# Cosmetic length caps from the FR's theme allowlist table.
LOGO_ALT_MAX_LEN = 100
PRODUCT_NAME_MAX_LEN = 60

# Single-line "plain text" guard for theme strings that the renderer
# will inject into HTML chrome — no control chars, no newlines,
# no NUL. Catches paste mistakes and a category of injection-shape
# values that the renderer's escaping should handle but lint
# rejects up-front for clarity.
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ----- result objects -----

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    """One lint finding. `path` is a dotted location for the offending
    field (e.g. `theme.primary_color`, `vars.brightness.min`)."""
    severity: Severity
    path: str
    message: str


@dataclass(frozen=True)
class Asset:
    """Asset metadata for `lint_asset_bundle`. Filled in by the publish
    pipeline (filename, sniffed/declared content type, byte length)."""
    filename: str
    content_type: str
    size_bytes: int


# ----- field-level hint rules -----

_NUMERIC_TYPES = {"int", "float"}
_STRING_TYPES = {"string", "enum"}  # `type: enum` is a constrained string


def _is_numeric(var: Var) -> bool:
    return var.type in _NUMERIC_TYPES


def _is_stringy(var: Var) -> bool:
    # `widget: secret`, `multiline`, `max_length`, `pattern`,
    # `write_only` apply to `type: string`. The existing `type: enum`
    # is a closed string set; renderer-level hints aren't meaningful
    # there (the widget is implied), so we restrict to plain string.
    return var.type == "string"


def _lint_field_enum(var: Var, name: str, issues: list[LintIssue]) -> None:
    if var.enum is None:
        return
    base = f"vars.{name}.enum"
    if var.type not in ("int", "string"):
        issues.append(LintIssue(
            "error", base,
            f"`enum:` UI hint only valid on `type: int` or `type: string` (got {var.type!r})",
        ))
        return
    if not var.enum:
        issues.append(LintIssue("error", base, "`enum:` must contain at least one value"))
        return
    # Detect mixed shape (some EnumChoice objects, some bare scalars).
    has_obj = any(isinstance(e, EnumChoice) for e in var.enum)
    has_bare = any(not isinstance(e, EnumChoice) for e in var.enum)
    if has_obj and has_bare:
        issues.append(LintIssue(
            "error", base,
            "`enum:` entries must be all bare scalars or all `{value, label}` objects (no mixing)",
        ))
    # Numeric enum + min/max is mutually exclusive (FR explicit).
    if var.type == "int" and (var.min is not None or var.max is not None):
        issues.append(LintIssue(
            "error", base,
            "`enum` and `min`/`max` are mutually exclusive on numeric fields",
        ))
    # Type match per entry.
    seen_values: list[object] = []
    seen_labels: list[str] = []
    for i, entry in enumerate(var.enum):
        if isinstance(entry, EnumChoice):
            value: object = entry.value
            label = entry.label
        else:
            value = entry
            label = str(entry)
        if var.type == "int" and not isinstance(value, int):
            issues.append(LintIssue(
                "error", f"{base}[{i}].value",
                f"int enum entry must be int (got {type(value).__name__})",
            ))
        if var.type == "string" and not isinstance(value, str):
            issues.append(LintIssue(
                "error", f"{base}[{i}].value",
                f"string enum entry must be string (got {type(value).__name__})",
            ))
        if value in seen_values:
            issues.append(LintIssue(
                "warning", f"{base}[{i}]",
                f"duplicate enum value {value!r}",
            ))
        seen_values.append(value)
        if label in seen_labels:
            issues.append(LintIssue(
                "warning", f"{base}[{i}].label",
                f"duplicate enum label {label!r}",
            ))
        seen_labels.append(label)


def _lint_field_numeric_bounds(var: Var, name: str, issues: list[LintIssue]) -> None:
    base = f"vars.{name}"
    for hint, value in (("min", var.min), ("max", var.max), ("step", var.step)):
        if value is None:
            continue
        if not _is_numeric(var):
            issues.append(LintIssue(
                "error", f"{base}.{hint}",
                f"`{hint}:` only valid on numeric types (got {var.type!r})",
            ))
    if var.min is not None and var.max is not None and var.min > var.max:
        issues.append(LintIssue(
            "error", base,
            f"`min` ({var.min}) > `max` ({var.max})",
        ))


def _lint_field_widget(var: Var, name: str, issues: list[LintIssue]) -> None:
    if var.widget is None:
        return
    base = f"vars.{name}.widget"
    w = var.widget
    if w == "slider":
        if var.type != "int":
            issues.append(LintIssue(
                "error", base,
                f"`widget: slider` only valid on `type: int` (got {var.type!r})",
            ))
        elif var.min is None or var.max is None:
            # Plan's "bonus warnings" list: slider without min+max.
            issues.append(LintIssue(
                "warning", base,
                "`widget: slider` without `min` and `max` falls back to a number input",
            ))
    elif w == "secret":
        if not _is_stringy(var):
            issues.append(LintIssue(
                "error", base,
                f"`widget: secret` only valid on `type: string` (got {var.type!r})",
            ))
    elif w == "radio":
        if var.type != "string":
            issues.append(LintIssue(
                "error", base,
                f"`widget: radio` only valid on `type: string` (got {var.type!r})",
            ))
        elif var.enum is None:
            issues.append(LintIssue(
                "error", base,
                "`widget: radio` requires `enum:` to declare the choice set",
            ))


def _lint_field_string_only(var: Var, name: str, issues: list[LintIssue]) -> None:
    base = f"vars.{name}"
    for hint, value in (
        ("multiline", var.multiline),
        ("max_length", var.max_length),
        ("pattern", var.pattern),
        ("write_only", var.write_only),
    ):
        if not value:  # None, False, or 0 — nothing to validate
            continue
        if not _is_stringy(var):
            issues.append(LintIssue(
                "error", f"{base}.{hint}",
                f"`{hint}:` only valid on `type: string` (got {var.type!r})",
            ))
    if var.max_length is not None and var.max_length <= 0:
        issues.append(LintIssue(
            "error", f"{base}.max_length",
            f"`max_length` must be positive (got {var.max_length})",
        ))
    if var.pattern is not None:
        try:
            re.compile(var.pattern)
        except re.error as e:
            issues.append(LintIssue(
                "error", f"{base}.pattern",
                f"invalid regex: {e}",
            ))


def _lint_field_help_markdown(var: Var, name: str, issues: list[LintIssue]) -> None:
    if var.help_markdown is None:
        return
    size = len(var.help_markdown.encode("utf-8"))
    if size > MARKDOWN_MAX_BYTES:
        issues.append(LintIssue(
            "error", f"vars.{name}.help_markdown",
            f"exceeds STRA2US_MARKDOWN_MAX_BYTES ({size} > {MARKDOWN_MAX_BYTES})",
        ))


# ----- theme rules -----

def _lint_theme_color(value: str, path: str, issues: list[LintIssue]) -> None:
    if not HEX_COLOR_RE.match(value):
        issues.append(LintIssue(
            "error", path,
            f"must be #RRGGBB or #RGB hex, got {value!r}",
        ))


def _lint_theme(catalog: Catalog, asset_listing: set[str] | None,
                issues: list[LintIssue]) -> None:
    theme = catalog.theme
    if theme is None:
        return
    for key in ("primary_color", "accent_color", "bg_color", "text_color"):
        value = getattr(theme, key)
        if value is not None:
            _lint_theme_color(value, f"theme.{key}", issues)
    if theme.font_family is not None and theme.font_family not in THEME_FONT_ALLOWLIST:
        issues.append(LintIssue(
            "error", "theme.font_family",
            f"{theme.font_family!r} not in font allowlist "
            f"({sorted(THEME_FONT_ALLOWLIST)})",
        ))
    if theme.logo_asset is not None:
        if not ASSET_FILENAME_RE.match(theme.logo_asset):
            issues.append(LintIssue(
                "error", "theme.logo_asset",
                f"asset filename must match {ASSET_FILENAME_RE.pattern}, "
                f"got {theme.logo_asset!r}",
            ))
        elif asset_listing is not None and theme.logo_asset not in asset_listing:
            issues.append(LintIssue(
                "error", "theme.logo_asset",
                f"references {theme.logo_asset!r} but _assets/{theme.logo_asset} "
                "not in bundle",
            ))
    if theme.logo_alt is not None:
        if len(theme.logo_alt) > LOGO_ALT_MAX_LEN:
            issues.append(LintIssue(
                "error", "theme.logo_alt",
                f"length {len(theme.logo_alt)} exceeds {LOGO_ALT_MAX_LEN}",
            ))
        if _CTRL_CHARS_RE.search(theme.logo_alt):
            issues.append(LintIssue(
                "error", "theme.logo_alt",
                "must not contain control characters / newlines",
            ))
    if theme.product_name is not None:
        if len(theme.product_name) > PRODUCT_NAME_MAX_LEN:
            issues.append(LintIssue(
                "error", "theme.product_name",
                f"length {len(theme.product_name)} exceeds {PRODUCT_NAME_MAX_LEN}",
            ))
        if _CTRL_CHARS_RE.search(theme.product_name):
            issues.append(LintIssue(
                "error", "theme.product_name",
                "must not contain control characters / newlines",
            ))


# ----- ui block rules -----

def _lint_ui(catalog: Catalog, issues: list[LintIssue]) -> None:
    ui = catalog.ui
    if ui is None:
        return
    for key in ("header_markdown", "footer_markdown"):
        value = getattr(ui, key)
        if value is None:
            continue
        size = len(value.encode("utf-8"))
        if size > MARKDOWN_MAX_BYTES:
            issues.append(LintIssue(
                "error", f"ui.{key}",
                f"exceeds STRA2US_MARKDOWN_MAX_BYTES ({size} > {MARKDOWN_MAX_BYTES})",
            ))


# ----- public entry points -----

def lint_catalog(catalog: Catalog, *,
                 asset_listing: Iterable[str] | None = None) -> list[LintIssue]:
    """Run every lint rule that can be evaluated from the catalog model.

    Pass `asset_listing` (filenames in the catalog bundle's `_assets/`
    directory) at publish time so `theme.logo_asset` and unused-asset
    warnings can be evaluated. Pass `None` when only the catalog YAML
    is available (e.g. server-side schema sanity check) — the
    asset-aware rules are skipped, syntactic rules still run.
    """
    listing = set(asset_listing) if asset_listing is not None else None
    issues: list[LintIssue] = []

    for name, var in catalog.vars.items():
        _lint_field_enum(var, name, issues)
        _lint_field_numeric_bounds(var, name, issues)
        _lint_field_widget(var, name, issues)
        _lint_field_string_only(var, name, issues)
        _lint_field_help_markdown(var, name, issues)

    _lint_theme(catalog, listing, issues)
    _lint_ui(catalog, issues)

    # Bonus: unused-asset warning. Only runs when we have an asset
    # listing to compare against.
    if listing is not None:
        referenced: set[str] = set()
        if catalog.theme and catalog.theme.logo_asset:
            referenced.add(catalog.theme.logo_asset)
        # Markdown blocks may reference `/app/<app>/_assets/<file>` via
        # `<img src=...>`; that resolution lives at render time. P0
        # only knows about explicit `theme.logo_asset` references, so
        # the unused-asset check covers theme-only references and is
        # conservative (won't false-positive on markdown-referenced
        # assets, will false-negative on truly-unused ones until the
        # markdown parser plumbs out its image references).
        unused = listing - referenced
        for filename in sorted(unused):
            issues.append(LintIssue(
                "warning", f"_assets/{filename}",
                "asset present in bundle but not referenced by theme",
            ))

    return issues


def lint_asset_bundle(assets: Iterable[Asset]) -> list[LintIssue]:
    """Validate the publish-time asset bundle against size and type
    limits. Per-file checks (filename shape, content-type, size) plus
    the bundle total cap. Caller passes already-collected metadata."""
    issues: list[LintIssue] = []
    total = 0
    seen: set[str] = set()
    for asset in assets:
        path = f"_assets/{asset.filename}"
        if not ASSET_FILENAME_RE.match(asset.filename):
            issues.append(LintIssue(
                "error", path,
                f"filename must match {ASSET_FILENAME_RE.pattern}",
            ))
        if asset.filename in seen:
            issues.append(LintIssue(
                "error", path, "duplicate filename in bundle",
            ))
        seen.add(asset.filename)
        if asset.content_type not in ASSET_CONTENT_TYPES:
            issues.append(LintIssue(
                "error", path,
                f"content type {asset.content_type!r} not in allowlist "
                f"({sorted(ASSET_CONTENT_TYPES)})",
            ))
        if asset.size_bytes > ASSET_MAX_BYTES:
            issues.append(LintIssue(
                "error", path,
                f"size {asset.size_bytes} exceeds STRA2US_ASSET_MAX_BYTES "
                f"({ASSET_MAX_BYTES})",
            ))
        total += asset.size_bytes
    if total > ASSET_BUNDLE_MAX_BYTES:
        issues.append(LintIssue(
            "error", "_assets/",
            f"bundle size {total} exceeds STRA2US_ASSET_BUNDLE_MAX_BYTES "
            f"({ASSET_BUNDLE_MAX_BYTES})",
        ))
    return issues


def errors(issues: Iterable[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if i.severity == "error"]


def warnings(issues: Iterable[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if i.severity == "warning"]


def format_issues(issues: Iterable[LintIssue]) -> str:
    """Multi-line string for CLI / server error reporting. Empty when
    there are no issues. Stable shape: `[severity] path: message`."""
    return "\n".join(f"[{i.severity}] {i.path}: {i.message}" for i in issues)
