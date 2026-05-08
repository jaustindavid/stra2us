# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""SVG sanitizer tests (docs/fr_catalog_app_ui.md "Library choice"
+ P0 of fr_catalog_app_ui_plan.md).

Standard SVG-XSS corpus per the FR:
- `<script>` anywhere
- `<foreignObject>` carrying HTML
- `<use href="...">` to an external doc
- JS in `style`
- `xlink:href="javascript:..."`

Plus the XML-bomb / external-DTD class that defusedxml catches.
"""

from __future__ import annotations

import pytest

from stra2us_cli.sanitizers import SvgSanitizeError, sanitize_svg


SVG_HEADER = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
XLINK_HEADER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'viewBox="0 0 100 100">'
)


# ----- happy paths -----

def test_clean_svg_round_trips_geometry():
    src = SVG_HEADER + b'<circle cx="50" cy="50" r="30" fill="red"/></svg>'
    out = sanitize_svg(src)
    assert b'<circle' in out
    assert b'cx="50"' in out
    assert b'fill="red"' in out


def test_viewbox_preserved_case_sensitive():
    """SVG attr names are case-sensitive — `viewBox` (camelCase)
    must survive; lowercased `viewbox` would be invalid SVG."""
    out = sanitize_svg(SVG_HEADER + b'<rect width="10" height="10"/></svg>')
    assert b'viewBox="0 0 100 100"' in out


def test_gradient_preserved():
    src = (
        XLINK_HEADER +
        b'<defs><linearGradient id="g" gradientUnits="userSpaceOnUse">'
        b'<stop offset="0" stop-color="#fff"/>'
        b'<stop offset="1" stop-color="#000"/>'
        b'</linearGradient></defs>'
        b'<rect width="100" height="100" fill="url(#g)"/>'
        b'</svg>'
    )
    # Note: fill="url(#g)" is a content reference inside an attribute
    # value, not an `href`. The sanitizer doesn't (and shouldn't)
    # parse fill values; CSS-context url() inside an SVG fill is
    # fine because the parent SVG is same-origin and the ref is
    # to a `#`-prefixed local id.
    out = sanitize_svg(src)
    assert b'<linearGradient' in out
    assert b'gradientUnits="userSpaceOnUse"' in out


def test_use_with_fragment_ref_preserved():
    src = (
        XLINK_HEADER +
        b'<defs><circle id="c" cx="5" cy="5" r="3"/></defs>'
        b'<use xlink:href="#c"/></svg>'
    )
    out = sanitize_svg(src)
    assert b'<use' in out
    assert b'href="#c"' in out


# ----- corpus rejections (raise) -----

def test_script_rejected():
    src = SVG_HEADER + b'<script>alert(1)</script></svg>'
    with pytest.raises(SvgSanitizeError, match="<script>"):
        sanitize_svg(src)


def test_foreignobject_rejected():
    src = SVG_HEADER + b'<foreignObject><body>x</body></foreignObject></svg>'
    with pytest.raises(SvgSanitizeError, match="foreignObject"):
        sanitize_svg(src)


def test_event_handler_attribute_rejected():
    src = SVG_HEADER + b'<circle cx="5" cy="5" r="3" onclick="bad()"/></svg>'
    with pytest.raises(SvgSanitizeError, match="event handlers"):
        sanitize_svg(src)


def test_event_handler_attribute_case_insensitive():
    """Defenders should not let `OnClick` slip through on case flip."""
    src = SVG_HEADER + b'<circle cx="5" cy="5" r="3" OnClick="bad()"/></svg>'
    with pytest.raises(SvgSanitizeError, match="event handlers"):
        sanitize_svg(src)


def test_javascript_xlink_href_rejected():
    src = (
        XLINK_HEADER +
        b'<use xlink:href="javascript:alert(1)"/></svg>'
    )
    with pytest.raises(SvgSanitizeError, match="`#` refs"):
        sanitize_svg(src)


def test_external_use_href_rejected():
    src = XLINK_HEADER + b'<use xlink:href="https://evil.example/x.svg#c"/></svg>'
    with pytest.raises(SvgSanitizeError, match="`#` refs"):
        sanitize_svg(src)


def test_external_image_href_rejected():
    src = XLINK_HEADER + b'<use href="http://evil/leak"/></svg>'
    with pytest.raises(SvgSanitizeError, match="`#` refs"):
        sanitize_svg(src)


# ----- defusedxml protections -----

def test_billion_laughs_rejected():
    """Standard XML-bomb with nested entity expansion. defusedxml
    raises before we walk the tree."""
    bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE x ['
        b'<!ENTITY a "AA">'
        b'<!ENTITY b "&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;">'
        b']>'
        b'<svg xmlns="http://www.w3.org/2000/svg">&c;</svg>'
    )
    with pytest.raises(SvgSanitizeError, match="unsafe XML"):
        sanitize_svg(bomb)


def test_external_dtd_rejected():
    src = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    )
    # defusedxml's behavior on a public DTD reference: it raises
    # `ExternalReferenceForbidden` once parsing reaches the doctype
    # on systems that resolve it. We accept either the early raise
    # or a clean parse if the ref is never fetched — but at minimum,
    # nothing fishy survives in the output.
    try:
        out = sanitize_svg(src)
    except SvgSanitizeError:
        return  # rejected, good
    # If parsed, the output should be a benign empty <svg>.
    assert b'<svg' in out
    assert b'DTD' not in out


def test_entity_definition_rejected():
    src = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg [<!ENTITY x "data">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
    )
    with pytest.raises(SvgSanitizeError, match="unsafe XML"):
        sanitize_svg(src)


# ----- attribute / tag stripping (drops without raising) -----

def test_style_attribute_stripped_without_raising():
    """Inline `style=` opens up url()/expression() vectors. We drop
    the attribute silently rather than reject the SVG — benign
    editors attach `style="fill:red"` as a routine matter, and
    rejecting wholesale would break too many real-world SVGs."""
    src = SVG_HEADER + b'<circle cx="5" cy="5" r="3" style="fill:url(evil)"/></svg>'
    out = sanitize_svg(src)
    assert b'style=' not in out
    assert b'<circle' in out  # element survives, just stripped


def test_unknown_tag_dropped():
    """Anything outside the allowlist (here, the SMIL-era
    `<animate>`) is dropped. Surviving siblings stay."""
    src = (
        SVG_HEADER +
        b'<animate attributeName="x" from="0" to="100"/>'
        b'<circle cx="5" cy="5" r="3"/>'
        b'</svg>'
    )
    out = sanitize_svg(src)
    assert b'<animate' not in out
    assert b'<circle' in out


def test_unknown_attribute_dropped():
    src = SVG_HEADER + b'<circle cx="5" cy="5" r="3" data-tracking="leak"/></svg>'
    out = sanitize_svg(src)
    assert b'data-tracking' not in out


# ----- structural -----

def test_non_svg_root_rejected():
    src = b'<html><body>nope</body></html>'
    with pytest.raises(SvgSanitizeError, match="root"):
        sanitize_svg(src)


def test_malformed_xml_rejected():
    with pytest.raises(SvgSanitizeError, match="malformed XML"):
        sanitize_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><circle')


def test_string_input_accepted():
    """The publish path may have already decoded bytes to text;
    accept either."""
    out = sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="3"/></svg>'
    )
    assert b'<circle' in out
