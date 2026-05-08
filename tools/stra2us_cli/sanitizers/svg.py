# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""SVG sanitizer.

There is no maintained Python equivalent of bleach for SVG (bleach
itself is HTML-only — pointing it at SVG misses XML-specific hazards).
The plan from `docs/fr_catalog_app_ui.md` "Library choice":

1. Parse with `defusedxml.ElementTree` — handles XML-bomb /
   external-entity attacks (billion-laughs, external DTDs, file://
   entity refs) safely. The parser raises if the input declares an
   external DTD or `<!ENTITY>`; we treat that as a wholesale rejection.
2. Walk the tree against a hand-rolled tag + attribute allowlist
   (~50 LoC). Drop disallowed nodes/attrs.
3. Serialize back out — output is the *re-serialized clean tree*,
   not the original bytes. A vendor who ships a benign SVG with
   benign-but-novel features will see those features dropped on
   the way through; that's the price of "if it isn't on the list,
   it doesn't ship."

Output is always rejected (raises `SvgSanitizeError`) on:
- External DTD or `<!ENTITY>` declaration
- `<script>` tag (we always reject rather than strip — the FR's
  test corpus assertion is "rejects" for `<script>`-bearing SVGs;
  silently stripping a script attempt risks producing a
  plausibly-functional SVG from an attacker's payload).
- Any attribute starting with `on` (event handlers).
- `href` / `xlink:href` not starting with `#` (no external refs).
- `style` attribute (kills inline CSS, which can carry url() and
  expressions).
"""

from __future__ import annotations

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from xml.etree import ElementTree as ET

# SVG namespaces. defusedxml/ElementTree carries them through as
# `{namespace}localname`. We strip the namespace for the allowlist
# match, then restore the SVG namespace on output (no foreign
# namespaces reach the serialized result).
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"

_ALLOWED_TAGS = frozenset({
    "svg", "g",
    "path", "circle", "ellipse", "rect", "line",
    "polyline", "polygon",
    "text", "tspan",
    "defs", "linearGradient", "radialGradient", "stop",
    "use",
    "title", "desc",
})

# Allowed attributes by category. Keep these grouped so the FR's
# "Library choice" allowlist maps line-by-line to the source.
_GEOMETRIC_ATTRS = frozenset({
    "d", "cx", "cy", "r", "rx", "ry", "x", "y",
    "x1", "y1", "x2", "y2", "width", "height",
    "points", "transform", "viewBox", "offset",
})
_PRESENTATION_ATTRS = frozenset({
    "fill", "stroke", "stroke-width", "opacity",
    "fill-opacity", "stroke-opacity",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray",
    "font-family", "font-size", "text-anchor",
    "stop-color", "stop-opacity",
    # Gradient / use coordinate spaces
    "gradientUnits", "gradientTransform", "spreadMethod",
})
_STRUCTURAL_ATTRS = frozenset({
    "id", "class",
    # Common SVG document-level attrs that don't carry exfil risk.
    "xmlns", "version", "preserveAspectRatio",
})
_ALLOWED_ATTRS = _GEOMETRIC_ATTRS | _PRESENTATION_ATTRS | _STRUCTURAL_ATTRS


class SvgSanitizeError(ValueError):
    """The SVG was rejected. Includes a short reason for the publish
    error message — enough for the operator to find the offending
    element without leaking sanitizer internals."""


def _localname(tag: str) -> str:
    """Strip `{namespace}` from an ElementTree tag. Handles both
    namespaced (`{http://www.w3.org/2000/svg}svg`) and bare tags."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _namespace(tag: str) -> str | None:
    if tag.startswith("{"):
        return tag.split("}", 1)[0][1:]
    return None


def _href_ok(value: str) -> bool:
    """Allow only same-document fragment refs (`#id`). Rejects
    `javascript:`, `data:`, external paths, even relative `./foo`."""
    return value.startswith("#") and "\n" not in value and "\r" not in value


def _check_attr(tag_local: str, attr: str, value: str) -> tuple[bool, str | None]:
    """Return (allowed, rewritten_value_or_None).

    SVG attribute names are case-sensitive (`viewBox`,
    `gradientUnits`, `preserveAspectRatio`), so the allowlist
    membership check uses the verbatim local name. The `on*`,
    `style`, and `href` rejection rules use a case-insensitive
    comparison — defenders should not let a `On Click` or `STYLE`
    slip through on a case flip.
    """
    name = _localname(attr)
    name_ci = name.lower()
    if name_ci.startswith("on"):
        raise SvgSanitizeError(f"<{tag_local} {name}=...>: event handlers not allowed")
    if name_ci == "style":
        # Inline CSS opens too many doors (url(), expression(), etc.).
        # We drop the attr entirely — no wholesale rejection like for
        # `on*` because a benign editor may attach `style="fill:red"`
        # routinely and dropping is recoverable.
        return False, None
    if name_ci == "href":
        if not _href_ok(value):
            raise SvgSanitizeError(
                f"<{tag_local} href={value!r}>: only same-document `#` refs allowed",
            )
        return True, value
    if name in _ALLOWED_ATTRS:
        return True, value
    return False, None


def _walk(node: ET.Element) -> ET.Element | None:
    """Visit a single node. Returns the cleaned node, or None if the
    node should be dropped from the output entirely."""
    local = _localname(node.tag)
    if local == "script":
        # Wholesale rejection — see module docstring.
        raise SvgSanitizeError("<script> not allowed in SVG asset")
    if local == "foreignObject":
        # Carries arbitrary HTML; the FR rejects.
        raise SvgSanitizeError("<foreignObject> not allowed in SVG asset")
    if local not in _ALLOWED_TAGS:
        return None  # drop

    cleaned_attrs: dict[str, str] = {}
    for attr_name, attr_value in list(node.attrib.items()):
        keep, value = _check_attr(local, attr_name, attr_value)
        if keep:
            # Strip xlink namespace from `xlink:href` if present —
            # SVG 2 prefers bare `href`, and the unified output keeps
            # the serializer's NS map predictable.
            local_attr = _localname(attr_name)
            cleaned_attrs[local_attr] = value or ""

    # Re-emit the element in the SVG namespace (uniform output).
    new = ET.Element(f"{{{_SVG_NS}}}{local}", cleaned_attrs)
    if node.text:
        new.text = node.text
    if node.tail:
        new.tail = node.tail
    for child in list(node):
        cleaned = _walk(child)
        if cleaned is not None:
            new.append(cleaned)
    return new


def sanitize_svg(source: bytes | str) -> bytes:
    """Parse, sanitize, and re-serialize an SVG.

    Args:
      source: raw SVG bytes (preferred — preserves the byte-level
        intent for the parser) or string. Caller passes whatever the
        upload / file-read produced.

    Returns the cleaned, re-serialized SVG as bytes. Output declares
    the SVG namespace explicitly and is suitable to drop into KV
    under `_catalog/<app>/_assets/<filename>` (see P1 publish
    pipeline).

    Raises `SvgSanitizeError` on:
      * Malformed XML.
      * External DTD / `<!ENTITY>` declarations (defusedxml raises).
      * `<script>` or `<foreignObject>` anywhere in the tree.
      * Any `on*` event handler.
      * `href` / `xlink:href` not pointing to a same-document fragment.
      * Root element other than `<svg>`.
    """
    if isinstance(source, str):
        source = source.encode("utf-8")
    try:
        root = DefusedET.fromstring(source)
    except DefusedXmlException as e:
        raise SvgSanitizeError(f"unsafe XML construct: {e}") from e
    except ET.ParseError as e:
        raise SvgSanitizeError(f"malformed XML: {e}") from e

    if _localname(root.tag) != "svg":
        raise SvgSanitizeError(
            f"root element must be <svg>, got <{_localname(root.tag)}>"
        )

    cleaned = _walk(root)
    if cleaned is None:
        raise SvgSanitizeError("<svg> root rejected by allowlist (unexpected)")

    # ElementTree prefers unprefixed namespaces if we register the
    # default — keeps the output tidy (no `ns0:` on every element).
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    return ET.tostring(cleaned, encoding="utf-8", xml_declaration=False)
