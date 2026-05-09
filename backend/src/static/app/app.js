// Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
// See LICENSE in the repo root.
//
// Customer-facing /app/ surface JS. One file, two pages:
//   - landing form (no nav, just lookup) → initLanding
//   - per-device page (status badge, telemetry tail, Reveal flow)
//     → initDevice
// Each page bootstraps based on what it finds in the DOM.
//
// **P3 trim** (docs/fr_catalog_app_ui_plan.md). Pre-P3 this file
// also rendered the settings list client-side (catalog YAML →
// js-yaml → setting-card HTML) and ran an edit modal. The settings
// form is now server-rendered (see backend/src/services/page_renderer.py),
// so app.js retains:
//   - landing-form lookup
//   - status-badge + activity-tail telemetry refresh
//   - Reveal-button flow for encrypted secrets
// and drops:
//   - catalog YAML fetch + parse
//   - settings-card rendering / population
//   - the entire edit modal (replaced by inline form widgets)
//   - the cdn.jsdelivr.net script-src (P5 audit win)
//
// **P4 boundary.** P4 wires touched-state behavior into the inline
// form via the standalone `forms/touched_state.js` module from P0;
// this file does not duplicate that work.

// `<script type="module">` runs in strict mode automatically; the
// pragma is informational for anyone reading the file directly.
'use strict';

// Touched-state form behavior (P0 module, wired in P4). The module
// reads `data-original` / `data-write-only` / `data-valid` from the
// server-rendered widgets and produces a per-field dirty-aware
// payload via `serialize(form)`. `attachSubmitHandler` intercepts
// browser-native submit so we can POST that payload via fetch.
import {
    init as initTouchedState,
    serialize as serializeForm,
    attachSubmitHandler,
} from './forms/touched_state.js';

const ADMIN_API = '/api/admin';
const APP_API   = '/api/app';

// Status-bucket boundaries are MULTIPLES of the catalog-declared
// heartbeat interval. App-agnostic: a device that heartbeeps every
// 5 minutes is healthy at 4 minutes since last message, while a 30s
// cadence device is offline by then. Multipliers chosen so:
//   - 2x  : tolerates one missed beat (jitter / brief network blip)
//   - 20x : ~10 minutes for a 30s cadence; ~100 minutes for 5min
//   - past 20x : device hasn't been heard from for many cycles → offline
const ONLINE_MULTIPLIER  = 2;
const RECENT_MULTIPLIER  = 20;

// Module-scope state for the device page. Populated by initDevice
// from the data-* attributes the server renders onto `<body>`.
const _state = {
    appName: null,
    deviceId: null,
    telemetryTopic: null,
    heartbeatSeconds: 60,
    telemetryTimerId: null,
};

function $(sel)  { return document.querySelector(sel); }
function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---- entrypoint ----

document.addEventListener('DOMContentLoaded', () => {
    if (document.body.classList.contains('app-landing-page')) {
        initLanding();
    } else if (document.body.classList.contains('app-device-page')) {
        initDevice();
    }
});

// =====================================================================
// Landing page: bare-URL form + lookup
// =====================================================================

function initLanding() {
    const form = $('#lookupForm');
    if (!form) return;

    const input = $('#deviceName');
    const button = form.querySelector('button[type="submit"]');
    const message = $('#lookupMessage');

    function showError(msg) {
        message.classList.remove('hidden', 'info');
        message.classList.add('error');
        message.innerText = msg;
    }
    function clearMessage() {
        message.classList.add('hidden');
        message.classList.remove('error', 'info');
        message.innerText = '';
    }

    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        clearMessage();
        const name = (input.value || '').trim();
        if (!name) return;
        button.disabled = true;
        try {
            const r = await fetch(`${APP_API}/lookup_device?name=${encodeURIComponent(name)}`);
            if (r.status === 404) {
                showError(`No device named "${name}". Check the spelling and try again.`);
                return;
            }
            if (!r.ok) {
                showError(`Lookup failed (HTTP ${r.status}). Try again in a moment.`);
                return;
            }
            const { app } = await r.json();
            window.location.href = `/app/${encodeURIComponent(app)}/${encodeURIComponent(name)}`;
        } catch (e) {
            showError(`Couldn't reach the server: ${e.message}`);
        } finally {
            button.disabled = false;
        }
    });
}

// =====================================================================
// Device page: status badge + activity tail + Reveal flow
//
// The settings form itself is server-rendered (P3). This function
// only wires the live-updating bits.
// =====================================================================

function initDevice() {
    // Read server-provided context off the body's data-* attributes.
    // The renderer sets these from the catalog before serving HTML;
    // see backend/src/api/routes_app.py:_render_device_page.
    const body = document.body;
    _state.appName = body.dataset.app;
    _state.deviceId = body.dataset.device;
    _state.telemetryTopic = body.dataset.telemetryTopic
        || `${_state.appName}/public/heartbeep`;
    const hb = parseInt(body.dataset.heartbeatSeconds, 10);
    if (Number.isFinite(hb) && hb > 0) {
        _state.heartbeatSeconds = hb;
    }

    // Set the device-name header from the URL slug.
    const nameEl = $('#deviceName');
    if (nameEl) nameEl.innerText = _state.deviceId;

    // Wire the touched-state form behavior (P4). The page_renderer
    // emits exactly one `<form class="catalog-form">`. init() binds
    // dirty + live-pattern listeners; attachSubmitHandler intercepts
    // submit so we POST the per-field payload via fetch instead of
    // the browser's default form submit. Without this, the browser
    // would post the raw form-control values — including the
    // visually-clamped (off-spec) values + the empty write_only
    // fields — and stomp the customer's data.
    const form = $('.catalog-form');
    if (form) {
        initTouchedState(form);
        attachSubmitHandler(form, onFormSubmit);
    }

    // Bind the Reveal-button flow once at page load. The buttons
    // themselves are rendered server-side by page_renderer for
    // encrypted non-write_only fields.
    bindRevealButtons();

    // Telemetry tail: first fetch fires immediately, then every 30s,
    // and on tab regain-focus so a customer who left the tab open
    // sees a fresh badge when they return.
    refreshTelemetry();
    _state.telemetryTimerId = setInterval(refreshTelemetry, 30000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshTelemetry();
    });
}

// =====================================================================
// FORM SUBMIT (P4)
//
// `attachSubmitHandler` calls this with the touched-state-aware
// payload + the submit event. We preventDefault, build a
// urlencoded body from the payload, POST to the form's action, and
// reload on success so the page re-renders with the now-stored
// values. The server-side handler (`backend/src/api/routes_app_form.py`)
// iterates whatever fields it received and writes them — fields
// the JS omitted (untouched write_only) never reach the server,
// so the prior KV value is preserved by absence.
// =====================================================================

async function onFormSubmit(payload, event) {
    event.preventDefault();
    const form = event.target;

    // Visually disable the submit button so a customer who clicks
    // twice doesn't fire two POSTs in flight (the second would race
    // and likely succeed too, but the optics are worse).
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.dataset.savingOriginalText = submitBtn.innerText;
        submitBtn.innerText = 'Saving…';
    }

    // Build form-urlencoded body. URLSearchParams handles all the
    // escaping; mirrors what the browser would send for a native
    // POST with `enctype="application/x-www-form-urlencoded"`. Same
    // wire format the server's strict-naive handler from P3 already
    // accepts — no server change needed for P4.
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(payload)) {
        body.append(k, v);
    }

    let ok = false;
    try {
        const r = await fetch(form.action, {
            method: 'POST',
            body,
            // 'follow' (default) lets fetch chase the 303 redirect
            // back to the GET. We don't use the response body — we
            // just need to know the POST landed.
            credentials: 'same-origin',
        });
        ok = r.ok;
    } catch (e) {
        // Network error. Surface in console for now; P5 audit could
        // surface to the customer with an inline message.
        console.error('form submit failed', e);
    }

    if (ok) {
        // Reload so the page re-renders with the stored values. The
        // touched-state attributes (data-original) get refreshed
        // server-side from the new KV state.
        window.location.reload();
        return;
    }

    // Error path — re-enable the submit so the customer can retry.
    if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerText = submitBtn.dataset.savingOriginalText || 'Save';
    }
}

// =====================================================================
// REVEAL FLOW
//
// page_renderer emits `<button class="reveal-btn" data-var="<name>">`
// next to the masked password input for encrypted, non-write_only
// fields. Click → fetch decrypted value via the existing admin
// peek/kv path → fill the input → flip the button to "Hide".
// =====================================================================

function bindRevealButtons() {
    document.body.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.reveal-btn');
        if (!btn) return;
        const varName = btn.dataset.var;
        if (!varName) return;
        toggleReveal(varName, btn);
    });
}

async function toggleReveal(varName, btn) {
    const input = document.getElementById(`field-${varName}`);
    if (!input) return;
    if (btn.innerText === 'Hide') {
        input.value = '';
        input.placeholder = '••••••••';
        btn.innerText = 'Reveal';
        return;
    }
    btn.disabled = true;
    btn.innerText = '…';
    try {
        const path = `${encodeURIComponent(_state.appName)}/${encodeURIComponent(_state.deviceId)}/${encodeURIComponent(varName)}`;
        const r = await fetch(`${ADMIN_API}/peek/kv/${path}`);
        if (!r.ok) {
            btn.innerText = 'Reveal';
            return;
        }
        const data = await r.json();
        if (data.status === 'ok' && data.message !== null && data.message !== undefined) {
            input.value = (typeof data.message === 'string')
                ? data.message
                : String(data.message);
            input.placeholder = '';
            btn.innerText = 'Hide';
        } else {
            btn.innerText = 'Reveal';
        }
    } catch (e) {
        btn.innerText = 'Reveal';
    } finally {
        btn.disabled = false;
    }
}

// =====================================================================
// TELEMETRY TAIL + STATUS BADGE
//
// Single fetch against `/api/admin/stream/q/<topic>?client_id=<device>`
// drives both the status header and the recent-activity tail:
//   - status badge derived from age of the most-recent message
//   - tail rendered from the latest N messages
//
// Refreshes on a 30s timer + when the tab regains focus (visibility
// API). No real-time push.
// =====================================================================

async function refreshTelemetry() {
    if (!_state.telemetryTopic) return;
    const topic = encodeURIComponent(_state.telemetryTopic);
    const device = encodeURIComponent(_state.deviceId);
    const url = `${ADMIN_API}/stream/q/${topic}?client_id=${device}&limit=10`;

    let messages;
    try {
        const r = await fetch(url);
        if (!r.ok) {
            // 403 most likely — operator's ACL doesn't cover the topic.
            // Don't break the page; show "unknown" status and a hint.
            renderStatusBadge('unknown', null, `Status unavailable (HTTP ${r.status})`);
            renderActivityList(null, `Couldn't load recent activity (HTTP ${r.status}).`);
            return;
        }
        messages = await r.json();
    } catch (e) {
        renderStatusBadge('unknown', null, `Couldn't reach the server.`);
        renderActivityList(null, `Couldn't load recent activity.`);
        return;
    }

    // Empty stream: device hasn't published yet, or all messages expired.
    if (!Array.isArray(messages) || messages.length === 0) {
        renderStatusBadge('offline', null, 'No telemetry received yet');
        renderActivityList([], null);
        return;
    }

    // Newest-first: messages[0] is most recent.
    const latest = messages[0];
    const ageSec = Math.max(0, Math.floor(Date.now() / 1000) - latest.received_at);
    const onlineThresh = _state.heartbeatSeconds * ONLINE_MULTIPLIER;
    const recentThresh = _state.heartbeatSeconds * RECENT_MULTIPLIER;
    let bucket;
    if (ageSec < onlineThresh) bucket = 'online';
    else if (ageSec < recentThresh) bucket = 'recent';
    else bucket = 'offline';

    renderStatusBadge(bucket, ageSec, null);
    renderActivityList(messages, null);
}

function renderStatusBadge(bucket, ageSec, customDetail) {
    const badge = $('#statusBadge');
    const detail = $('#statusDetail');
    if (!badge || !detail) return;
    badge.classList.remove('status-loading', 'status-online', 'status-recent', 'status-offline', 'status-unknown');
    badge.classList.add(`status-${bucket}`);
    badge.innerText = ({
        online: 'Online',
        recent: 'Recently active',
        offline: 'Offline',
        unknown: 'Status unknown',
    })[bucket] || 'Status unknown';
    if (customDetail) {
        detail.innerText = customDetail;
    } else if (ageSec === null || ageSec === undefined) {
        detail.innerText = '';
    } else {
        detail.innerText = `Last seen ${formatAge(ageSec)}`;
    }
}

function renderActivityList(messages, errorMsg) {
    const listEl = $('#activityList');
    if (!listEl) return;
    if (errorMsg) {
        listEl.innerHTML = `<div class="activity-empty">${escapeHtml(errorMsg)}</div>`;
        return;
    }
    if (!messages || messages.length === 0) {
        listEl.innerHTML = `<div class="activity-empty">No recent telemetry from this device.</div>`;
        return;
    }
    listEl.innerHTML = messages.map(m => {
        const when = formatAge(Math.max(0, Math.floor(Date.now() / 1000) - m.received_at));
        const payloadStr = (typeof m.data === 'object' && m.data !== null)
            ? JSON.stringify(m.data)
            : String(m.data);
        return `
            <div class="activity-row">
                <span class="activity-when">${escapeHtml(when)}</span>
                <span class="activity-payload">${escapeHtml(payloadStr)}</span>
            </div>
        `;
    }).join('');
}

// Returns a relative-time phrase. The sub-5s case is a complete
// phrase on its own ("just now"); every other branch is a duration
// followed by " ago". Callers concatenate it directly into a
// surrounding sentence ("Last seen ${formatAge(...)}") without
// adding their own " ago" suffix — pre-v1.6.3 the sub-5s case
// rendered as "just now ago" because the suffix was tacked on at
// the call site.
function formatAge(seconds) {
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return m > 0 ? `${h}h ${m}m ago` : `${h}h ago`;
    }
    return `${Math.floor(seconds / 86400)}d ago`;
}
