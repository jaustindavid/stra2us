# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Structural tests for the touched-state JS module
(backend/src/static/app/forms/touched_state.js).

Why structural-only at P0: the repo has no JS test runtime
(no Node, no Deno, no Playwright, no headless browser). The plan
calls for DOM unit tests on this module — we encode the behavioral
contract here as substring / structural assertions so a future
runtime can replace these with actual exercises, while still
catching obvious regressions today (e.g. somebody slips an `eval`
in, drops the `serialize` export, or smuggles inline DOM XML).

The actual DOM behavior is verified during the manual walkthrough
(P0 sign-off step 4) using the harness page at
`backend/src/static/app/forms/_test_harness.html`.
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_JS_PATH = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app", "forms", "touched_state.js",
))
_HARNESS_PATH = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app", "forms", "_test_harness.html",
))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ----- module surface -----

def test_module_exports_init_serialize_attach():
    src = _read(_JS_PATH)
    assert re.search(r"export\s*{\s*init,\s*serialize,\s*attachSubmitHandler\s*}", src)


def test_module_uses_strict_mode():
    """ES modules are strict by default, but the explicit pragma
    documents the contract for anyone reading the file directly."""
    src = _read(_JS_PATH)
    assert '"use strict"' in src


def test_module_has_no_eval_or_function_string():
    """`script-src 'self'` blocks `eval` and `new Function(...)` even
    when loaded from same-origin. Catch a regression that adds either."""
    src = _read(_JS_PATH)
    assert not re.search(r"\beval\s*\(", src)
    assert not re.search(r"\bnew\s+Function\b", src)
    assert not re.search(r"\bsetTimeout\s*\(\s*['\"]", src)
    assert not re.search(r"\bsetInterval\s*\(\s*['\"]", src)


def test_module_has_no_inline_html_strings_with_user_data():
    """The module manipulates the DOM via `getAttribute` /
    `dataset` / `value` — never via innerHTML with computed values.
    The contract is "no innerHTML anywhere"; this catches
    accidental introduction."""
    src = _read(_JS_PATH)
    assert ".innerHTML" not in src


# ----- behavioral contract: encoded as substring assertions -----

def test_dirty_flag_set_on_input_and_change():
    """The FR's "dirty flag flipped by the first input/change event."
    Both event names must be wired."""
    src = _read(_JS_PATH)
    assert 'addEventListener("input"' in src
    assert 'addEventListener("change"' in src


def test_data_original_attribute_referenced():
    src = _read(_JS_PATH)
    assert 'data-original' in src
    assert 'getAttribute("data-original")' in src


def test_data_valid_toggle_on_pattern_inputs():
    """Live red/green per the FR's "Validation" section. The toggle
    target is `data-valid`; the trigger is the `pattern` attribute
    presence (or `required`) plus `checkValidity()`."""
    src = _read(_JS_PATH)
    assert 'data-valid' in src or 'dataset.valid' in src
    assert 'checkValidity()' in src


def test_write_only_field_omitted_when_clean():
    """The most spec-critical behavior: untouched write_only fields
    must be omitted from the payload, not sent as empty string."""
    src = _read(_JS_PATH)
    # The `write_only && !dirty → continue` shape, regardless of
    # exact spelling.
    assert "writeOnly" in src or "write-only" in src or "writeonly" in src
    assert re.search(r"continue\s*;", src)  # explicit omit branch


def test_from_default_field_omitted_when_clean():
    """v1.6.7 (TODO #6): a clean field whose current value came from
    the catalog default (`data-from-default="true"`) must be omitted
    from the submit payload — same shape as the write_only-omit
    branch. Pre-v1.6.7 the serializer sent the catalog default as
    the field's data-original value, materializing per-device
    overrides on every "save" click even for fields the operator
    never touched.
    """
    src = _read(_JS_PATH)
    # The `data-from-default` attribute is dataset.fromDefault on
    # the element side; either spelling proves the wire-up.
    assert "fromDefault" in src or "from-default" in src or "fromdefault" in src
    # And the omission shape — there should be at least two
    # explicit `continue` statements in serialize() (one for
    # write_only, one for from_default).
    serialize_block = re.search(
        r"function serialize\([^)]*\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert serialize_block is not None
    continues = re.findall(r"continue\s*;", serialize_block.group(1))
    assert len(continues) >= 2, (
        f"Expected ≥2 `continue;` statements in serialize() for the "
        f"write_only and from_default omit branches; found {len(continues)}"
    )


def test_dirty_branches_distinguish_live_vs_original():
    """Submit serialization splits per the FR: dirty → live value;
    clean → data-original verbatim. Both code paths must exist."""
    src = _read(_JS_PATH)
    # Live value: `el.value` somewhere reachable from the dirty branch.
    assert "el.value" in src
    # Original: getAttribute("data-original")
    assert 'getAttribute("data-original")' in src


# ----- harness page sanity -----

def test_harness_loads_module_via_type_module():
    """The harness page must exercise the module the same way the
    production page will: `<script type="module">` from a same-origin
    URL, no inline handlers."""
    src = _read(_HARNESS_PATH)
    assert '<script type="module">' in src
    assert 'touched_state.js' in src


def test_harness_has_no_inline_event_handlers():
    src = _read(_HARNESS_PATH)
    # `on*=` attribute on any tag would break under enforcing CSP.
    assert not re.search(r"\son[a-z]+\s*=", src)
