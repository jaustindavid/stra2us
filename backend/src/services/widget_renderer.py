# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Per-field widget HTML for the customer-facing app form
(P3 of `docs/fr_catalog_app_ui_plan.md`).

Implements the FR's "Renderer dispatch" table:

    type=int, has enum                    → <select>
    type=int, has min+max+widget=slider   → <input type="range">
    type=int, has min/max                 → <input type="number" min max>
    type=int, otherwise                   → <input type="number">

    type=str, has enum, widget=radio      → radio button group
    type=str, has enum                    → <select>
    type=str, multiline=true              → <textarea>
    type=str, widget=secret               → <input type="password">
    type=str, has pattern                 → <input type="text" pattern>
    type=str, otherwise                   → <input type="text">

Plus extensions for the existing schema's other types:

    type=enum (legacy, with `values:`)    → <select>
    type=float                            → <input type="number" step="any" …>
    type=bool                             → <select> with true/false

Forward compat: unknown `widget:` hints fall through to the
type-default, never raise. The FR's "old catalogs render at
reduced fidelity on older servers" promise holds in reverse too —
new catalogs render *something* even when they declare a widget
hint this server doesn't know about.

**Input contract.** `var` is the parsed YAML dict for a single
catalog field, not a pydantic model. The backend stays decoupled
from `tools/stra2us_cli` — same posture as the validation
duplication in `routes_app_assets.py` and `theme_serializer.py`
(see comments there). The CLI's pydantic schema is the
authoritative validation; the backend trusts that catalogs in
KV passed it at publish, and defends against malformed shapes
with `.get(...)` access here.

What this module DOES NOT do:
* Render labels / help text / help_markdown — that's `page_renderer`'s
  job; this module is the inner form control only.
* Wire any client-side behavior. The output is CSP-clean
  (`script-src 'self'`); P0's `touched_state.js` reads the
  emitted `data-original`, `data-write-only`, `value`, etc.
  attributes when P4 wires it up.
* Decide value resolution. Caller passes the resolved current
  value (post-fallback chain device → app → catalog default).
"""

from __future__ import annotations

import html
import re
from typing import Any


# ----- helpers -----

def _esc(value: Any) -> str:
    """HTML-escape with quote=True so attribute values are safe."""
    return html.escape("" if value is None else str(value), quote=True)


def _enum_choices(var: dict) -> list[tuple[Any, str]] | None:
    """Return the var's enum choices as `[(value, label), …]`, or
    None if it doesn't have an enum.

    Bridges the two spellings:
      * P0 field-level hint: `var["enum"]` — list of bare scalars
        OR list of `{"value": …, "label": …}` dicts.
      * Legacy schema: `var["type"] == "enum"` + `var["values"]` —
        list of bare scalars.

    Object-form entries keep separate value + label; bare scalars
    use the value as both. Caller emits the label as the visible
    text and submits the value.
    """
    enum = var.get("enum")
    if enum:
        out: list[tuple[Any, str]] = []
        for entry in enum:
            if isinstance(entry, dict) and "value" in entry and "label" in entry:
                out.append((entry["value"], str(entry["label"])))
            else:
                out.append((entry, str(entry)))
        return out
    if var.get("type") == "enum" and var.get("values"):
        return [(v, str(v)) for v in var["values"]]
    return None


def _effective_min(var: dict) -> Any:
    """Numeric lower bound. Prefers explicit `min:` (P0 hint); falls
    back to `range[0]` (legacy schema). None when neither set."""
    if var.get("min") is not None:
        return var["min"]
    rng = var.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) >= 1:
        return rng[0]
    return None


def _effective_max(var: dict) -> Any:
    if var.get("max") is not None:
        return var["max"]
    rng = var.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) >= 2:
        return rng[1]
    return None


def _is_numeric(var: dict) -> bool:
    return var.get("type") in ("int", "float")


# ----- off-spec detection -----

def is_off_spec(var: dict, current: Any) -> bool:
    """True when `current` doesn't conform to the catalog's hints.

    Only meaningful when a catalog constraint exists; vars with no
    constraints can never be off-spec. The FR is explicit: show
    the value as-is, soft-warn, do not rewrite. Rendering uses
    this signal to add the warning badge AND to clamp the *widget
    display* (e.g. range slider pinned at max) without touching
    the `data-original` attribute that the form will submit when
    the customer doesn't interact.
    """
    if current is None or current == "":
        return False

    enum = _enum_choices(var)
    if enum is not None:
        values = [e[0] for e in enum]
        # Compare both as-is and via str() so an int catalog enum
        # detects a string-stored value of the same digits.
        return current not in values and str(current) not in [str(v) for v in values]

    if _is_numeric(var):
        try:
            n = float(current)
        except (TypeError, ValueError):
            # Non-numeric stored in a numeric field — definitely
            # off-spec.
            return True
        lo = _effective_min(var)
        hi = _effective_max(var)
        if lo is not None and n < float(lo):
            return True
        if hi is not None and n > float(hi):
            return True
        return False

    if var.get("type") == "string":
        max_len = var.get("max_length")
        if max_len is not None and len(str(current)) > max_len:
            return True
        pattern = var.get("pattern")
        if pattern is not None:
            try:
                if not re.fullmatch(pattern, str(current)):
                    return True
            except re.error:
                # Malformed pattern (lint catches this; defense in
                # depth) — treat as no-pattern.
                pass
        return False

    return False


# ----- widget renderers -----

def _common_attrs(name: str, current: Any, var: dict) -> str:
    """Attributes every form control gets, regardless of widget
    type:
      * `name` — what the form submits
      * `data-original` — the un-touched value, used by P4's
        touched-state JS to omit / preserve untouched fields.
        Always the *raw* current value, even when off-spec or
        when the widget will display something different (e.g.
        a clamped slider).
      * `data-write-only` — marks fields whose untouched submit
        should be omitted entirely. P4 reads this; P3 just
        emits it.
    """
    attrs = [f'name="{_esc(name)}"']
    attrs.append(f'data-original="{_esc(current if current is not None else "")}"')
    if var.get("write_only"):
        attrs.append('data-write-only="true"')
    return " ".join(attrs)


def _help_text_attr(var: dict) -> str:
    """Plain `help:` text exposed via `title=` for native browser
    tooltip support. P3 also emits a visible help line via
    `page_renderer`; this attribute is a redundancy for keyboard
    / screen-reader users hovering the input."""
    help_text = var.get("help")
    if not help_text:
        return ""
    one_line = str(help_text).replace("\n", " ").strip()
    return f' title="{_esc(one_line)}"'


def _render_select(name: str, var: dict, current: Any,
                   choices: list[tuple[Any, str]]) -> str:
    common = _common_attrs(name, current, var)
    options: list[str] = []
    for value, label in choices:
        selected = ""
        if str(value) == str(current):
            selected = " selected"
        options.append(
            f'<option value="{_esc(value)}"{selected}>{_esc(label)}</option>'
        )
    return (
        f'<select {common}{_help_text_attr(var)}>\n'
        + "\n".join(options)
        + "\n</select>"
    )


def _render_radio(name: str, var: dict, current: Any,
                  choices: list[tuple[Any, str]]) -> str:
    """Radio group. Each radio carries `data-original` so the
    touched-state JS can detect a clean form. The currently-selected
    radio matches `current` if it's in-spec; otherwise no radio
    is checked (off-spec is flagged by the page_renderer's warning
    badge)."""
    items: list[str] = []
    write_only_attr = ' data-write-only="true"' if var.get("write_only") else ""
    for value, label in choices:
        checked = " checked" if str(value) == str(current) else ""
        items.append(
            f'<label class="radio-option">'
            f'<input type="radio" name="{_esc(name)}" '
            f'value="{_esc(value)}"{checked} '
            f'data-original="{_esc(current if current is not None else "")}"'
            f'{write_only_attr}>'
            f' {_esc(label)}</label>'
        )
    return f'<div class="radio-group" role="radiogroup">{"".join(items)}</div>'


def _render_slider(name: str, var: dict, current: Any) -> str:
    """`<input type="range">`. min + max are required for a
    meaningful slider; if either is absent, fall back to
    `<input type="number">` (FR + lint warning)."""
    lo = _effective_min(var)
    hi = _effective_max(var)
    if lo is None or hi is None:
        return _render_number(name, var, current)

    # Off-spec clamp for the *display* only; data-original holds
    # the raw value. P4's snap-on-edit reads data-original to
    # recover the un-clamped figure if the user doesn't interact.
    display_value = current
    if current is not None and current != "":
        try:
            n = float(current)
            if n < float(lo):
                display_value = lo
            elif n > float(hi):
                display_value = hi
        except (TypeError, ValueError):
            display_value = lo

    common = _common_attrs(name, current, var)
    step = var.get("step")
    step_attr = f' step="{_esc(step)}"' if step is not None else ""
    return (
        f'<input type="range" {common} '
        f'min="{_esc(lo)}" max="{_esc(hi)}"{step_attr} '
        f'value="{_esc(display_value)}"'
        f'{_help_text_attr(var)}>'
    )


def _render_number(name: str, var: dict, current: Any) -> str:
    """`<input type="number">` with optional min/max/step. Browsers
    block submit on out-of-range values via HTML5 validation."""
    common = _common_attrs(name, current, var)
    parts = [f'<input type="number" {common}']
    lo = _effective_min(var)
    hi = _effective_max(var)
    if lo is not None:
        parts.append(f'min="{_esc(lo)}"')
    if hi is not None:
        parts.append(f'max="{_esc(hi)}"')
    step = var.get("step")
    if step is not None:
        parts.append(f'step="{_esc(step)}"')
    elif var.get("type") == "float":
        # Default to "any" precision for floats so the browser doesn't
        # round to integers. Authors can override via `step:`.
        parts.append('step="any"')
    parts.append(f'value="{_esc(current if current is not None else "")}"')
    return " ".join(parts) + _help_text_attr(var) + ">"


def _render_textarea(name: str, var: dict, current: Any) -> str:
    common = _common_attrs(name, current, var)
    parts = [f'<textarea {common}']
    max_len = var.get("max_length")
    if max_len is not None:
        parts.append(f'maxlength="{_esc(max_len)}"')
    parts.append(f'rows="4"')
    # write_only on a string-shape widget = ship empty content
    # regardless of stored value (FR explicit).
    display = "" if var.get("write_only") else (current if current is not None else "")
    return (
        " ".join(parts) + _help_text_attr(var) + ">"
        + _esc(display)
        + "</textarea>"
    )


def _render_secret(name: str, var: dict, current: Any) -> str:
    """`<input type="password">`. When `write_only=true` the field
    ships empty regardless of stored value (FR explicit). When
    `write_only=false` the current (plaintext) value goes into
    the field."""
    write_only = bool(var.get("write_only"))
    display_value = "" if write_only else (current if current is not None else "")
    common = _common_attrs(name, current, var)
    parts = [
        f'<input type="password" {common}',
        f'value="{_esc(display_value)}"',
        f'autocomplete="new-password"',
    ]
    max_len = var.get("max_length")
    if max_len is not None:
        parts.append(f'maxlength="{_esc(max_len)}"')
    return " ".join(parts) + _help_text_attr(var) + ">"


def _render_text(name: str, var: dict, current: Any) -> str:
    """Plain `<input type="text">` with optional pattern + maxlength.
    The fallback for any string-typed var that doesn't match a
    more specific dispatch row."""
    common = _common_attrs(name, current, var)
    # write_only ships empty regardless of stored value (FR
    # explicit). The original sits on data-original so an
    # untouched submit (P4) preserves the prior value.
    display = "" if var.get("write_only") else (current if current is not None else "")
    parts = [
        f'<input type="text" {common}',
        f'value="{_esc(display)}"',
    ]
    pattern = var.get("pattern")
    if pattern is not None:
        parts.append(f'pattern="{_esc(pattern)}"')
    max_len = var.get("max_length")
    if max_len is not None:
        parts.append(f'maxlength="{_esc(max_len)}"')
    return " ".join(parts) + _help_text_attr(var) + ">"


def _render_bool(name: str, var: dict, current: Any) -> str:
    """Two-option select with explicit `true`/`false`. The FR's
    dispatch table doesn't address bool; this is the existing
    schema's type. A `<select>` (rather than a checkbox) keeps
    the value submission semantics regular ("name=true" /
    "name=false" rather than absent-when-unchecked) and matches
    how the CLI's `coerce_value` reads bool strings."""
    common = _common_attrs(name, current, var)
    cur_str = str(current).lower() if current is not None else ""
    options = []
    for value in ("true", "false"):
        selected = " selected" if cur_str == value else ""
        options.append(f'<option value="{value}"{selected}>{value}</option>')
    return (
        f'<select {common}{_help_text_attr(var)}>'
        + "".join(options)
        + "</select>"
    )


# ----- public dispatch -----

def render_widget(name: str, var: dict, current: Any) -> str:
    """Render the widget for `name` / `var` with resolved current
    value. Returns an HTML fragment (no surrounding label or help).

    Dispatch order matches the FR's table; falls through to the
    type default for any combination not explicitly listed. The
    `widget:` hint is at most a *narrowing* signal — it can pick a
    more specific shape within a type, never escape the type's
    semantics."""
    enum_choices = _enum_choices(var)
    var_type = var.get("type")

    if var_type == "int":
        if enum_choices is not None:
            return _render_select(name, var, current, enum_choices)
        if (var.get("widget") == "slider"
                and _effective_min(var) is not None
                and _effective_max(var) is not None):
            return _render_slider(name, var, current)
        return _render_number(name, var, current)

    if var_type == "float":
        if enum_choices is not None:
            return _render_select(name, var, current, enum_choices)
        return _render_number(name, var, current)

    if var_type == "bool":
        return _render_bool(name, var, current)

    if var_type in ("string", "enum"):
        if enum_choices is not None:
            if var.get("widget") == "radio":
                return _render_radio(name, var, current, enum_choices)
            return _render_select(name, var, current, enum_choices)
        if var.get("multiline"):
            return _render_textarea(name, var, current)
        if var.get("widget") == "secret":
            return _render_secret(name, var, current)
        return _render_text(name, var, current)

    # Unknown / future type — fall back to a plain text input. The
    # form is still functional; the customer just doesn't get
    # type-aware browser validation.
    return _render_text(name, var, current)
