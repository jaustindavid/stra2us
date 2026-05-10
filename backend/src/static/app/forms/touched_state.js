// Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
// See LICENSE in the repo root.
//
// Touched-state form behavior for the customer-facing app page
// (docs/fr_catalog_app_ui.md "Implications for displaying out-of-spec
// values" + P4 of docs/fr_catalog_app_ui_plan.md).
//
// Three behaviors, all spec'd in the FR:
//
// 1. Per-field dirty tracking. Each field carries `data-original`
//    (its stored value as the page rendered). The first `input` /
//    `change` event flips the field's dirty flag.
//
// 2. Live `pattern` feedback. Inputs with a `pattern` attribute get
//    `data-valid="true|false"` toggled on each `input` event; the
//    base stylesheet styles those attributes red/green. HTML5's
//    own `pattern` validation is submit-time; this is the
//    per-keystroke "live red/green" experience.
//
// 3. Submit serialization. On form submit:
//      - dirty == true   → live <input> value goes through;
//      - dirty == false  → `data-original` value goes through verbatim;
//      - write_only AND  → field omitted entirely (server treats
//        dirty == false      absence as "preserve current").
//
// The module is loaded via <script type="module"> from
// `/app/_static/forms/touched_state.js` — same-origin under the FR's
// `script-src 'self'` CSP, no inline handlers anywhere. P0 ships
// the module and unit-test harness; P4 wires it into the rendered
// customer form.

"use strict";

// Marker attribute set on a field once we've recorded its baseline.
// We avoid re-binding a field that already has listeners attached
// (idempotent `init` lets a future caller re-run it after a partial
// DOM update without doubling event handlers).
const BOUND_ATTR = "data-touched-state-bound";

// Selector for "form fields we manage." Limits to the input families
// the FR's renderer emits: text-shaped inputs, textareas, selects,
// and the radio group (handled at the input level, with `name`
// dedup'ing in `serialize`).
const FIELD_SELECTOR = [
  "input[name]",
  "textarea[name]",
  "select[name]",
].join(",");

// True if the field is one of the input types whose value the form
// reads via `.value`. Filters out submit / button / image inputs
// that share the `<input>` tag but aren't catalog-driven values.
function _isValueInput(el) {
  if (el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
  if (el.tagName !== "INPUT") return false;
  const t = (el.type || "text").toLowerCase();
  return t !== "submit" && t !== "button" && t !== "image" && t !== "reset";
}

// Read or assign the baseline. We set `data-original` server-side
// at render time, but `init` will fall back to the field's current
// `.value` if the attribute is missing — handy for the test harness
// and for any field the renderer can't pre-fill (e.g. a brand-new
// catalog key that has no stored value yet).
function _ensureOriginal(el) {
  if (el.hasAttribute("data-original")) return;
  el.setAttribute("data-original", el.value || "");
}

// Bind the dirty + live-validity listeners to a single field.
// Idempotent: already-bound fields are a no-op.
//
// `data-valid` is intentionally NOT set on first paint — the FR's
// "Validation" section says feedback happens "as the user types,"
// which means the field stays neutral (no green / no red) until
// the first input event. The base stylesheet's `[data-valid="..."]`
// rules only fire once the JS sets the attribute on a keystroke.
// This avoids the noisy first-paint state where every valid
// pattern field gets the green-border treatment by default.
function _bindField(el) {
  if (el.hasAttribute(BOUND_ATTR)) return;
  _ensureOriginal(el);
  el.dataset.dirty = "false";

  const onChange = () => {
    el.dataset.dirty = "true";
  };
  const onInput = () => {
    el.dataset.dirty = "true";
    if (el.hasAttribute("pattern") || el.hasAttribute("required")) {
      el.dataset.valid = el.checkValidity() ? "true" : "false";
    }
  };
  el.addEventListener("input", onInput);
  el.addEventListener("change", onChange);
  el.setAttribute(BOUND_ATTR, "1");
}

// Build the submit payload per the FR's serialize rules. Returns a
// plain object mapping `name` → string value. Caller chooses what to
// do with it (POST as JSON, build a FormData, etc.). Keeping this
// separate from the submit listener keeps the module easy to test
// and lets P4's wiring code use whatever transport it prefers.
function serialize(form) {
  const out = {};
  const seen = new Set();
  const fields = form.querySelectorAll(FIELD_SELECTOR);
  for (const el of fields) {
    if (!_isValueInput(el)) continue;
    const name = el.getAttribute("name");
    if (!name || seen.has(name)) continue;
    // For radio groups, find the checked input.
    if (el.type === "radio") {
      const checked = form.querySelector(
        `input[type=radio][name="${CSS.escape(name)}"]:checked`,
      );
      if (!checked) continue;
      seen.add(name);
      const dirty = checked.dataset.dirty === "true";
      out[name] = dirty
        ? checked.value
        : (checked.getAttribute("data-original") || "");
      continue;
    }
    if (el.type === "checkbox") {
      seen.add(name);
      const dirty = el.dataset.dirty === "true";
      // Checkboxes: dirty → live `checked`; clean → original raw value
      // string ("true"/"false"/whatever the renderer wrote). The FR
      // doesn't list checkbox as a v1 widget, but the dispatch table's
      // `widget: …` extensibility means a future widget might use
      // one — handle it correctly today rather than punt.
      if (dirty) {
        out[name] = el.checked ? "true" : "false";
      } else {
        out[name] = el.getAttribute("data-original") || "";
      }
      continue;
    }
    seen.add(name);
    const dirty = el.dataset.dirty === "true";
    const writeOnly = el.dataset.writeOnly === "true";
    const fromDefault = el.dataset.fromDefault === "true";
    if (writeOnly && !dirty) {
      // Omit entirely — server treats absence as "preserve current."
      continue;
    }
    if (fromDefault && !dirty) {
      // v1.6.7 (TODO #6): the field's current value came from the
      // catalog's `default:`, not from any stored KV record. The
      // operator hasn't touched it. Omit it from the submit so we
      // don't materialize a per-device override they didn't ask
      // for — the resolution chain (per-device → app-scope →
      // catalog default) keeps producing the same value on the
      // next page load. Pre-v1.6.7 this materialized the default
      // into per-device KV, surprising the operator who only
      // edited one other field.
      continue;
    }
    out[name] = dirty
      ? el.value
      : (el.getAttribute("data-original") || "");
  }
  return out;
}

// Wire every managed field in `root` (a form, or any container that
// holds form fields) for dirty tracking + live validity. Returns the
// number of fields newly bound. Re-running on the same root is safe.
function init(root) {
  if (!root) return 0;
  let bound = 0;
  const fields = root.querySelectorAll(FIELD_SELECTOR);
  for (const el of fields) {
    if (!_isValueInput(el)) continue;
    if (el.hasAttribute(BOUND_ATTR)) continue;
    _bindField(el);
    bound += 1;
  }
  return bound;
}

// Convenience: install a submit handler that intercepts the default
// form submission, builds the payload via `serialize`, and hands it
// to `onSubmit(payload, event)`. The handler must call
// `event.preventDefault()` itself if it's doing async work; this
// wrapper does not preventDefault on its own so a no-JS fallback
// path stays available if the page somehow loads without `init`.
function attachSubmitHandler(form, onSubmit) {
  form.addEventListener("submit", (event) => {
    const payload = serialize(form);
    onSubmit(payload, event);
  });
}

export { init, serialize, attachSubmitHandler };
