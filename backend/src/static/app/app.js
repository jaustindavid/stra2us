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

'use strict';

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

    const input = $('#deviceNameInput');
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
        detail.innerText = `Last seen ${formatAge(ageSec)} ago`;
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
                <span class="activity-when">${escapeHtml(when)} ago</span>
                <span class="activity-payload">${escapeHtml(payloadStr)}</span>
            </div>
        `;
    }).join('');
}

function formatAge(seconds) {
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return m > 0 ? `${h}h ${m}m` : `${h}h`;
    }
    return `${Math.floor(seconds / 86400)}d`;
}
