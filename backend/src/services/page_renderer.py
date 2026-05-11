# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Customer-facing app page assembly (P3 of
`docs/fr_catalog_app_ui_plan.md`).

Combines:

* `widget_renderer` — per-field form control HTML.
* `markdown_cache` — sanitized header / footer / per-field
  markdown blocks, cached by `(app, publish_hash, block_id)`.
* Off-spec warning markup — values stored outside the catalog's
  declared range/enum/pattern get a soft-warning badge per the
  FR's "Implications for displaying out-of-spec values."
* Section chrome — `<header>` with logo + product name from
  `theme.logo_asset` + `theme.product_name`. P1's asset URL
  convention (`/app/<app>/_assets/<file>?v=<sha256>`) feeds this.

Output shape (per the FR's "Combined example"):

    <section class="catalog-app">
      <header class="catalog-app-chrome">
        <img src="/app/<slug>/_assets/<logo>?v=…" alt="<logo_alt>">
        <h1 class="catalog-product-name"><product_name></h1>
      </header>
      <div class="catalog-header-md">…sanitized markdown…</div>
      <form method="post" action="/app/<slug>/<device>" class="catalog-form">
        <div class="setting-card">
          <label for="…">…</label>
          <div class="setting-help">…plain help…</div>
          <div class="setting-help-md">…sanitized help_markdown…</div>
          <span class="setting-warning">…off-spec badge…</span>
          <…widget…>
        </div>
        …
        <button type="submit">Save</button>
      </form>
      <div class="catalog-footer-md">…sanitized markdown…</div>
    </section>

P3 ships the static markup above. P4 wires `touched_state.js` to
the form for dirty/snap/write-only behavior; the markup already
carries the `data-original` / `data-write-only` attributes the
JS reads.
"""

from __future__ import annotations

import hashlib
import html
import json
from typing import Any

from .markdown_cache import render_block as render_markdown_block
from .widget_renderer import is_off_spec, render_widget


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# Theme keys that participate in `publish_hash` — must mirror what
# `theme_serializer.theme_hash` cares about. Used here only to
# compute the markdown cache key; the serializer itself is the
# authoritative implementation for theme CSS.
def compute_publish_hash(catalog: dict) -> str:
    """SHA-256-prefix hash over the entire catalog dict — bumps
    on any republish that changes catalog content.

    Used as the `publish_hash` component of the markdown cache
    key. Slightly over-invalidates (a `vars:` change bumps the
    hash even if no markdown content changed), but that's
    acceptable: the cache rebuilds in microseconds and over-
    invalidation never produces stale content."""
    blob = json.dumps(catalog, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def _customer_facing_vars(catalog: dict) -> list[tuple[str, dict]]:
    """Filter catalog vars to those that should appear on the
    customer page. Convention from `docs/fr_application_view.md`:
    presence of a `label:` field is the visibility gate."""
    out: list[tuple[str, dict]] = []
    for name, var in (catalog.get("vars") or {}).items():
        if isinstance(var, dict) and var.get("label"):
            out.append((name, var))
    return out


def _render_chrome(app: str, theme: dict | None) -> str:
    """Section header: optional logo + product name.

    `theme.logo_asset` resolves to `/app/<app>/_assets/<file>` (P1
    asset route). The renderer emits the `?v=` cache-bust query
    only when the catalog has been published with an
    `_assets_index` (we'd need the asset's sha256 prefix; P3
    skips this and lets the asset route's own ETag handle
    revalidation). P3's markup is "basic placement" per the
    plan's note — P3 wires the chrome, P5's full polish lives
    elsewhere."""
    if theme is None:
        theme = {}
    parts = ['<header class="catalog-app-chrome">']
    logo_asset = theme.get("logo_asset")
    if logo_asset:
        logo_alt = theme.get("logo_alt") or theme.get("product_name") or ""
        parts.append(
            f'<img class="catalog-logo" '
            f'src="/app/{_esc(app)}/_assets/{_esc(logo_asset)}" '
            f'alt="{_esc(logo_alt)}">'
        )
    product_name = theme.get("product_name")
    if product_name:
        parts.append(
            f'<h1 class="catalog-product-name">{_esc(product_name)}</h1>'
        )
    parts.append("</header>")
    return "".join(parts)


def _render_help(var: dict, app: str, publish_hash: str,
                 var_name: str) -> str:
    """Plain `help:` (text) + optional `help_markdown:` (sanitized
    inline). Both render under the form input; markdown is
    cached by block_id `help.<varname>`."""
    parts: list[str] = []
    plain = var.get("help")
    if plain:
        parts.append(
            f'<div class="setting-help">{_esc(plain)}</div>'
        )
    md = var.get("help_markdown")
    if md:
        rendered = render_markdown_block(
            app=app, publish_hash=publish_hash,
            block_id=f"help.{var_name}", source=md,
        )
        parts.append(
            f'<div class="setting-help-md">{rendered}</div>'
        )
    return "".join(parts)


def _render_off_spec_badge(current_value: str | None) -> str:
    """Soft warning shown alongside off-spec stored values. The
    FR's "Implications for displaying out-of-spec values" prose:
    *"Show the value as-is, with a soft-warning indicator."* The
    badge text quotes the verbatim value so the customer sees
    what the device wrote, even when the widget itself can only
    display catalog-valid choices."""
    return (
        '<span class="setting-warning" role="status">'
        f'<strong>{_esc(current_value)}</strong>'
        ' &mdash; not in current allowed values'
        '</span>'
    )


def _render_setting_card(name: str, var: dict, current: str | None,
                         encrypted: bool, from_default: bool, app: str,
                         publish_hash: str) -> str:
    """One `<div class="setting-card">` containing label, help,
    optional off-spec badge, and the widget.

    `from_default=True` signals that `current` came from the catalog's
    `default:` field rather than from a stored KV value — i.e. the
    resolution chain bottomed out at step 3. The renderer surfaces
    this as `data-from-default="true"` on the input element, which
    the touched-state serializer reads to skip clean fields that
    are still showing the catalog default. Without this signal the
    form-submit would materialize per-device overrides for every
    untouched field whose value came from the catalog, surprising
    the operator who only edited one field. (TODO #6, v1.6.7.)"""
    label = var.get("label") or name
    parts = [f'<div class="setting-card" data-var="{_esc(name)}">']
    parts.append(
        f'<label class="setting-label" for="field-{_esc(name)}">'
        f'{_esc(label)}</label>'
    )
    parts.append(_render_help(var, app, publish_hash, name))

    off_spec = is_off_spec(var, current)
    if off_spec:
        parts.append(_render_off_spec_badge(current))

    # v1.6.8 (commit 1): encrypted fields now render through the
    # same widget_renderer path as everything else. Pre-v1.6.8 we
    # had a separate encrypted-Reveal branch that emitted an empty
    # input + a Reveal button (the plaintext was deliberately kept
    # out of the HTML — fetched on click via /peek/kv/). That
    # design's data-original="" meant the touched-state serializer's
    # clean branch always sent empty, clobbering the stored value
    # on any untouched Save. The v1.6.7/v1.6.8 patches tried to
    # paper over it; commit 1 strips the encrypted-Reveal branch
    # entirely and lets data-original carry the plaintext like
    # any other field. Visual masking (the `<input type="password">`
    # dots) and a Show/Hide button get re-added as a thin overlay
    # in commit 2 — that's a UX layer, not a data-flow concern.
    widget = render_widget(name, var, current)
    # Inject id="field-<name>" so the <label for=...> lines up.
    # The widget renderer emits `name="..."` first; we splice
    # the id after it. Cheap string surgery; cleaner than
    # threading an `id` parameter through every renderer.
    widget_with_id = widget.replace(
        f'name="{_esc(name)}"',
        f'name="{_esc(name)}" id="field-{_esc(name)}"',
        1,
    )
    # v1.6.7 (TODO #6): tag fields whose current value came from
    # the catalog default. The touched-state serializer skips
    # clean+from-default fields so saving one edit doesn't
    # silently materialize per-device overrides for every other
    # field's catalog default.
    if from_default:
        widget_with_id = widget_with_id.replace(
            f'id="field-{_esc(name)}"',
            f'id="field-{_esc(name)}" data-from-default="true"',
            1,
        )
    parts.append(widget_with_id)

    # v1.6.8 commit 2: shoulder-surf-protection toggle for
    # `widget: secret` fields. The widget renderer emits the
    # input as `type="password"` (browser masks visually with
    # dots); the button flips `input.type` between password
    # and text for peek-on-demand. Purely client-side toggle —
    # no server fetch, no data-flow involvement. The button is
    # skipped for `write_only` fields since those deliberately
    # render empty and have no value to peek at.
    if var.get("widget") == "secret" and not var.get("write_only"):
        parts.append(
            f'<button type="button" class="reveal-btn" '
            f'data-var="{_esc(name)}">Show</button>'
        )

    parts.append("</div>")
    return "".join(parts)


def render_page(*, app: str, device: str, catalog: dict,
                values: dict[str, "_ResolvedValueLike"]) -> str:
    """Assemble the full customer-facing form HTML.

    `values` maps each customer-facing var name to a resolved
    value (typically `services.value_resolver.ResolvedValue`).
    Caller is responsible for the resolution chain — this
    module renders.
    """
    publish_hash = compute_publish_hash(catalog)
    theme = catalog.get("theme") or {}
    ui = catalog.get("ui") or {}

    parts: list[str] = []
    parts.append('<section class="catalog-app">')
    parts.append(_render_chrome(app, theme))

    header_md = ui.get("header_markdown")
    if header_md:
        rendered = render_markdown_block(
            app=app, publish_hash=publish_hash,
            block_id="header", source=header_md,
        )
        parts.append(
            f'<div class="catalog-header-md">{rendered}</div>'
        )

    parts.append(
        f'<form method="post" action="/app/{_esc(app)}/{_esc(device)}" '
        f'class="catalog-form">'
    )
    for name, var in _customer_facing_vars(catalog):
        rv = values.get(name)
        current = rv.value if rv is not None else None
        encrypted = bool(rv and rv.encrypted)
        from_default = bool(rv and rv.from_default)
        parts.append(_render_setting_card(name, var, current, encrypted,
                                          from_default, app, publish_hash))
    parts.append(
        '<div class="catalog-form-actions">'
        '<button type="submit" class="btn-primary">Save</button>'
        '</div>'
    )
    parts.append("</form>")

    footer_md = ui.get("footer_markdown")
    if footer_md:
        rendered = render_markdown_block(
            app=app, publish_hash=publish_hash,
            block_id="footer", source=footer_md,
        )
        parts.append(
            f'<div class="catalog-footer-md">{rendered}</div>'
        )

    parts.append("</section>")
    return "".join(parts)


# Type hint for the values dict — kept structural (anything with
# `.value` and `.encrypted`) so callers can pass `ResolvedValue`
# from `value_resolver` or any equivalent shape.
class _ResolvedValueLike:
    value: str | None
    encrypted: bool
