// Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
// See LICENSE in the repo root.
// Customer-facing /app/ surface. One JS file serves both the landing
// form (no nav, just lookup) and the per-device page (read-only
// settings + future telemetry tail). Each page bootstraps based on
// what it finds in the DOM — landing has #lookupForm, device page
// has #deviceName etc.
//
// Phase 2 (landed): read-only settings cards driven by catalog YAML +
// per-device KV peeks.
// Phase 3 (landed): edit modal — single-input form locked to the
// device scope, no encrypted-flag UI (catalog-driven per FR).
// The edit primitives (input rendering, validation, encoding for the
// admin POST, mask/reveal) are *copied* from admin's catalog editor
// rather than extracted into a shared module — defer dedupe to a
// follow-on pass once both surfaces have stabilized.
// No telemetry tail yet (Phase 4).

'use strict';

const ADMIN_API = '/api/admin';
const APP_API   = '/api/app';
const CATALOG_PREFIX = '_catalog/';

// Module-scope state for the device page. Populated by initDevice and
// read by the edit-modal handlers — keeps the modal handlers from having
// to re-fetch /me or re-parse the catalog when the operator clicks Edit.
const _state = {
    appName: null,
    deviceId: null,
    catalogVars: {},      // { varName: { type, label, help, range, encrypted, ... } }
    currentEditVar: null, // varName currently being edited (modal open)
    currentEditEncrypted: false, // sidecar state of the current value
    telemetryTopic: null, // resolved Redis topic name (no `q:` prefix)
    heartbeatSeconds: 60, // catalog-declared expected cadence; drives
                          // status-badge thresholds + activity tail filter
    telemetryTimerId: null,
};

// Status-bucket boundaries are MULTIPLES of the catalog-declared
// heartbeat interval. App-agnostic: a device that heartbeeps every
// 5 minutes is healthy at 4 minutes since last message, while a 30s
// cadence device is offline by then. The multipliers were chosen so:
//   - 2x: tolerates one missed beat (jitter / brief network blip)
//   - 20x: ~10 minutes for a 30s cadence; ~100 minutes for 5min cadence
//   - beyond 20x: device hasn't been heard from for many cycles → offline
const ONLINE_MULTIPLIER  = 2;
const RECENT_MULTIPLIER  = 20;

// Resolve the catalog-declared telemetry topic, substituting `{app}`
// and `{device}` placeholders. Defaults to the FR's convention
// `<app>/public/heartbeep` when the catalog doesn't declare one.
function resolveTelemetryTopic(declared, appName, deviceId) {
    const tmpl = declared || '{app}/public/heartbeep';
    return tmpl
        .replaceAll('{app}', appName)
        .replaceAll('{device}', deviceId);
}

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

// ---- entrypoint: dispatch based on which page DOM we're on ----

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

    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const input = $('#deviceName');
        const submit = $('#lookupSubmit');
        const msg = $('#lookupMessage');

        const name = (input.value || '').trim();
        if (!name) return;

        // Reset feedback state
        msg.classList.add('hidden');
        msg.classList.remove('error', 'info');
        submit.disabled = true;
        submit.innerText = 'Looking up…';

        try {
            const r = await fetch(`${APP_API}/lookup_device?name=${encodeURIComponent(name)}`);
            if (r.status === 404) {
                showLandingMessage(
                    `No device by that name was found. Check the spelling on your device, or contact your administrator.`,
                    'error'
                );
                return;
            }
            if (!r.ok) {
                showLandingMessage(`Lookup failed (HTTP ${r.status}). Please try again.`, 'error');
                return;
            }
            const { app } = await r.json();
            // Found it — redirect to the canonical URL. Auth happens at
            // the destination via the existing admin middleware (basic
            // auth prompt or cookie reuse).
            window.location.assign(`/app/${encodeURIComponent(app)}/${encodeURIComponent(name)}`);
        } catch (e) {
            showLandingMessage(`Network error. Please try again.`, 'error');
        } finally {
            submit.disabled = false;
            submit.innerText = 'Continue';
        }
    });

    // If we landed here via a soft 404 (someone hit /app/<app>/<unknown>),
    // surface a friendlier hint so they know why the form is here.
    // Detection: the response status was 404 even though we rendered the
    // landing markup. Document.referrer doesn't reliably tell us; we
    // could add a query param later. For now, no-op — the form's hint
    // text covers the "I expected to land somewhere else" case.
}

function showLandingMessage(text, kind) {
    const msg = $('#lookupMessage');
    msg.classList.remove('hidden', 'error', 'info');
    msg.classList.add(kind);
    msg.innerText = text;
}

// =====================================================================
// Device page: per-device settings view
// =====================================================================

async function initDevice() {
    // URL shape: /app/<app>/<device> (with or without trailing slash)
    const pathParts = window.location.pathname
        .split('/')
        .filter(Boolean); // drops empty segments
    if (pathParts.length < 3 || pathParts[0] !== 'app') {
        renderDeviceError('Bad URL — expected /app/&lt;app&gt;/&lt;device&gt;.');
        return;
    }
    const appName = decodeURIComponent(pathParts[1]);
    const deviceId = decodeURIComponent(pathParts[2]);

    _state.appName = appName;
    _state.deviceId = deviceId;

    $('#deviceName').innerText = deviceId;
    document.title = `${deviceId} — ${appName}`;

    // Modal close handlers — wire once at boot.
    $('#editModalClose').addEventListener('click', closeEditModal);
    $('#editModalCancel').addEventListener('click', closeEditModal);
    $('#editModalSave').addEventListener('click', saveEditModal);
    $('#editModal').addEventListener('click', (ev) => {
        // Click on the overlay (not the modal box) closes
        if (ev.target.id === 'editModal') closeEditModal();
    });
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && !$('#editModal').classList.contains('hidden')) {
            closeEditModal();
        }
    });

    // Bootstrap: identity + catalog in parallel; cards depend on both.
    let me, catalogYaml;
    try {
        [me, catalogYaml] = await Promise.all([
            fetchMe(),
            fetchCatalogYaml(appName),
        ]);
    } catch (e) {
        renderDeviceError(`Couldn't load your device's settings: ${escapeHtml(e.message)}`);
        return;
    }

    // Sanity check: the user should have rw on this device (the server
    // route already verified, but if cookies got crossed somewhere this
    // catches it client-side too with a clearer message).
    const expectedPrefix = `${appName}/${deviceId}`;
    const ownsDevice = (me.acl.permissions || []).some(p =>
        p.access === 'rw' && (p.prefix === expectedPrefix || p.prefix === '*')
    );
    if (!ownsDevice) {
        renderDeviceError(
            `You're signed in as <code>${escapeHtml(me.username)}</code>, ` +
            `but that account doesn't have write access to this device. ` +
            `If this is a mistake, contact your administrator.`
        );
        return;
    }

    // Catalog parsing — js-yaml isn't loaded on the customer page (we
    // don't want to drag in the admin UI's deps). Tiny JSON-ish parse:
    // for v1 we accept that the catalog YAML payload is a string, and
    // pull just the `vars:` map by handing it to the server's existing
    // catalog handler... actually no, we don't have a server JSON
    // catalog endpoint. For now: parse the YAML in the simplest way
    // possible — naive line scanner that pulls out `label:` and `help:`
    // for each top-level var. Phase 3 can drop in a proper YAML lib if
    // this gets fragile.
    const catalog = parseCatalog(catalogYaml);
    _state.catalogVars = catalog.vars;
    const customerVars = Object.entries(catalog.vars).filter(([_, v]) => v.label);

    // Resolve telemetry topic (catalog declaration with default
    // convention `{app}/public/heartbeep`). Phase 4 uses this to
    // tail the device's status + recent activity.
    _state.telemetryTopic = resolveTelemetryTopic(
        catalog.telemetryTopic, appName, deviceId
    );

    // Resolve heartbeat cadence — catalog declaration if present,
    // else 60s catch-all. Status-badge thresholds and activity-tail
    // freshness filter both derive from this.
    if (catalog.heartbeatIntervalSeconds) {
        _state.heartbeatSeconds = catalog.heartbeatIntervalSeconds;
    }

    // Kick off telemetry fetch in parallel with the per-var peeks.
    // First fetch fires now; refreshes happen on a timer + when the
    // tab regains focus.
    refreshTelemetry();
    _state.telemetryTimerId = setInterval(refreshTelemetry, 30000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshTelemetry();
    });

    if (customerVars.length === 0) {
        $('#settingsCards').innerHTML =
            `<p class="hint">No customer-facing settings defined for this device's catalog yet.</p>`;
        return;
    }

    // Fetch each var's per-device value. Render skeleton cards immediately,
    // populate values as fetches resolve so the page doesn't wait on the
    // slowest peek.
    $('#settingsCards').innerHTML = customerVars.map(([name, v]) =>
        renderSettingCardSkeleton(name, v)
    ).join('');

    bindCardActions();

    customerVars.forEach(([name, v]) => {
        fetchPerDeviceValue(appName, deviceId, name).then(res => {
            populateSettingValue(name, res, v, appName, deviceId);
        });
    });
}

async function fetchMe() {
    const r = await fetch(`${ADMIN_API}/me`);
    if (!r.ok) throw new Error(`/me returned ${r.status}`);
    return await r.json();
}

async function fetchCatalogYaml(appName) {
    const path = `${CATALOG_PREFIX}${encodeURIComponent(appName)}`;
    const r = await fetch(`${ADMIN_API}/peek/kv/${path}`);
    if (!r.ok) throw new Error(`catalog ${appName} returned ${r.status}`);
    const data = await r.json();
    if (data.status !== 'ok' || typeof data.message !== 'string') {
        throw new Error(`catalog ${appName} not available`);
    }
    return data.message;
}

async function fetchPerDeviceValue(appName, deviceId, varName) {
    const path = `${encodeURIComponent(appName)}/${encodeURIComponent(deviceId)}/${encodeURIComponent(varName)}`;
    const r = await fetch(`${ADMIN_API}/peek/kv/${path}`);
    if (!r.ok) return { state: 'error', error: `HTTP ${r.status}` };
    const data = await r.json();
    if (data.status === 'empty' || data.message === null) {
        return { state: 'unset' };
    }
    return { state: 'set', value: data.message, encrypted: !!data.encrypted };
}

function renderSettingCardSkeleton(name, v) {
    return `
        <div class="setting-card" data-var="${escapeHtml(name)}">
            <div class="setting-label">${escapeHtml(v.label || name)}${_formatDefaultAnnotation(v)}</div>
            ${v.help ? `<div class="setting-help">${escapeHtml(v.help)}</div>` : ''}
            <div class="setting-value-row">
                <span class="setting-value" id="val-${escapeHtml(name)}">Loading&hellip;</span>
                <button class="reveal-btn hidden" id="reveal-${escapeHtml(name)}" type="button" data-var="${escapeHtml(name)}">Reveal</button>
            </div>
            <button class="edit-btn" type="button" data-var="${escapeHtml(name)}">Edit</button>
        </div>
    `;
}

// Catalog-declared default surfaced inline with the var label, in
// muted italic. Skipped when no `default:` is in the catalog or the
// declared default is empty (no useful info to convey). Helps the
// customer see the operator's intent ("your device's heartbeep is 45;
// the catalog default is 30 — yes, you've overridden it") and
// communicates the unset case without needing a separate placeholder
// line in the value area.
function _formatDefaultAnnotation(v) {
    if (v.default === undefined || v.default === null || v.default === '') return '';
    const s = (typeof v.default === 'string') ? v.default : String(v.default);
    return ` <span class="setting-default-hint">[default: ${escapeHtml(s)}]</span>`;
}

// Clip help text for narrow surfaces (the edit modal especially).
// Catalog convention encouraged in catalog_spec.md: first paragraph =
// short blurb, rest = long-form details. Implementation honours that
// by clipping at the first newline; falls back to a word-count cap
// for legacy / un-paragraphed help. Picks whichever produces less
// content so wordy single-paragraph descriptions still get truncated.
// Trailing ellipsis signals "there's more."
const _CLIP_HELP_MAX_WORDS = 20;
function _clipHelp(help) {
    if (!help) return '';
    const trimmed = String(help).trim();
    const beforeNewline = trimmed.split(/\r?\n/)[0].trim();
    const newlineClipped = beforeNewline !== trimmed;

    const words = trimmed.split(/\s+/);
    const firstNWords = words.slice(0, _CLIP_HELP_MAX_WORDS).join(' ');
    const wordsClipped = words.length > _CLIP_HELP_MAX_WORDS;

    let chosen, chosenClipped;
    if (beforeNewline.length <= firstNWords.length) {
        chosen = beforeNewline;
        chosenClipped = newlineClipped;
    } else {
        chosen = firstNWords;
        chosenClipped = wordsClipped;
    }
    return chosenClipped ? chosen + '…' : chosen;
}

function populateSettingValue(varName, res, varDesc, appName, deviceId) {
    const valEl = document.getElementById(`val-${varName}`);
    const revealBtn = document.getElementById(`reveal-${varName}`);
    const card = valEl ? valEl.closest('.setting-card') : null;
    if (!valEl || !card) return;

    // Stash the resolved state on the card so the edit modal can read it
    // when the operator clicks Edit.  Encoded as data attrs (or in
    // _state.cardState if structured data is needed).
    card.dataset.state = res.state;
    card.dataset.encrypted = res.encrypted ? '1' : '';
    if (res.state === 'set') {
        // String form for prefill. JSON.stringify covers any structured
        // values; primitives stringify naturally.
        const s = (typeof res.value === 'string')
            ? res.value
            : (typeof res.value === 'number' || typeof res.value === 'boolean')
                ? String(res.value)
                : JSON.stringify(res.value);
        card.dataset.value = s;
    } else {
        delete card.dataset.value;
    }

    if (res.state === 'error') {
        valEl.innerText = `(error: ${res.error})`;
        valEl.classList.add('unset');
        revealBtn.classList.add('hidden');
    } else if (res.state === 'unset') {
        // Don't render the "(not set)" placeholder — the
        // `[default: X]` annotation next to the label (added by
        // _formatDefaultAnnotation) communicates the same thing
        // more usefully ("here's what's effective right now"). Just
        // collapse the value area silently.
        valEl.innerText = '';
        valEl.classList.add('unset');
        revealBtn.classList.add('hidden');
    } else if (res.encrypted) {
        // Encrypted record: dot-mask by default + show Reveal button.
        // The actual plaintext lives in the card's data-value attr (for
        // the edit modal); the visible text is just dots.
        valEl.classList.remove('unset');
        valEl.innerText = '••••••••••••';
        valEl.title = 'encrypted';
        revealBtn.classList.remove('hidden');
        revealBtn.innerText = 'Reveal';
    } else {
        valEl.classList.remove('unset');
        valEl.innerText = formatValueForDisplay(res.value);
        revealBtn.classList.add('hidden');
    }
}

// Card-level button delegation: handle Reveal + Edit clicks on the
// settings list. One delegated listener avoids per-card listener attach
// (which would have to re-bind every time we re-render).
function bindCardActions() {
    $('#settingsCards').addEventListener('click', (ev) => {
        const btn = ev.target.closest('button');
        if (!btn) return;
        const varName = btn.dataset.var;
        if (!varName) return;
        if (btn.classList.contains('edit-btn')) {
            openEditModal(varName);
        } else if (btn.classList.contains('reveal-btn')) {
            toggleCardReveal(varName, btn);
        }
    });
}

function toggleCardReveal(varName, btn) {
    const card = document.querySelector(`.setting-card[data-var="${varName}"]`);
    if (!card) return;
    const valEl = card.querySelector('.setting-value');
    const realValue = card.dataset.value || '';
    if (btn.innerText === 'Reveal') {
        valEl.innerText = realValue;
        btn.innerText = 'Hide';
    } else {
        valEl.innerText = '••••••••••••';
        btn.innerText = 'Reveal';
    }
}

function formatValueForDisplay(v) {
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try { return JSON.stringify(v); } catch (e) { return String(v); }
}

function renderDeviceError(htmlMsg) {
    $('#settingsCards').innerHTML = `<div class="error">${htmlMsg}</div>`;
}

// ---- naive catalog YAML parser ----
//
// We intentionally don't load js-yaml here (no build step, want to keep
// the customer page tiny). The catalog YAML schema is constrained:
// top-level `vars:` map, each var has `type:`, `scope:`, `default:`,
// `range:`, `help:`, and optionally `label:`/`encrypted:`. We only need
// `label`, `help`, `type`, and `encrypted` for the settings cards in
// this phase, so a line-scanning parser is sufficient.
//
// Drop in a real YAML lib if this gets fragile. Phase 3 (which lifts
// the catalog editor primitives into a shared module) is a natural
// time to revisit.
// Parse the catalog YAML into the shape this page needs:
//   { telemetryTopic, heartbeatIntervalSeconds, vars }
//
// Uses js-yaml (loaded via the CDN script in device.html) — same lib
// + version pinning the admin UI uses. Replaces an earlier hand-rolled
// line-scanning parser that mishandled `|` block scalars (the bare
// `|` showed up as the rendered help text instead of the multi-line
// content beneath it). YAML has too many edge cases — anchors, tags,
// flow style, multi-line strings — for a hand-roll to stay correct as
// the catalog spec grows.
//
// `telemetry_topic`: catalog-declared topic to tail. Supports `{app}`
// and `{device}` placeholders. Default: `{app}/public/heartbeep`.
//
// `heartbeat_interval_seconds`: app's expected cadence for the
// telemetry stream. Used to derive the status-badge thresholds — so a
// device that heartbeeps every 5 minutes isn't called "Offline" at
// 4 minutes since last message. Default: 60s.
function parseCatalog(yamlText) {
    let doc;
    try {
        doc = jsyaml.load(yamlText);
    } catch (e) {
        throw new Error(`catalog YAML parse error: ${e.message}`);
    }
    if (!doc || typeof doc !== 'object') {
        throw new Error('catalog YAML did not parse as a mapping');
    }
    const vars = (doc.vars && typeof doc.vars === 'object') ? doc.vars : {};
    let heartbeatIntervalSeconds = null;
    if (typeof doc.heartbeat_interval_seconds === 'number'
        && doc.heartbeat_interval_seconds > 0) {
        heartbeatIntervalSeconds = doc.heartbeat_interval_seconds;
    }
    const telemetryTopic = (typeof doc.telemetry_topic === 'string')
        ? doc.telemetry_topic
        : null;
    return { telemetryTopic, heartbeatIntervalSeconds, vars };
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
// API) — covers the common "I left the tab open and came back" UX
// without polling aggressively when the user isn't looking. No
// real-time push (no SSE / websockets) — see FR's "Not proposing".
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
        renderStatusBadge('unknown', null, 'Couldn\'t reach the server.');
        renderActivityList(null, 'Couldn\'t load recent activity.');
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
    // Activity tail intentionally bounded by COUNT (last 10), not by
    // freshness. Each row shows its own age so the customer can tell
    // at a glance whether the messages are recent or stale.
    renderActivityList(messages, null);
}

function renderStatusBadge(bucket, ageSec, customDetail) {
    const badge = $('#statusBadge');
    const detail = $('#statusDetail');
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

// =====================================================================
// EDIT MODAL
//
// TODO(dedupe): the input-rendering, validation, encoding, and
// mask/reveal primitives below are *copied* from admin's catalog
// editor in static/app.js (search for `_editControlHtml`,
// `_validateInput`, `_encodeForAdmin`, `_populateScopeInput`,
// `_toggleReveal`). When both surfaces have stabilized, lift them
// into a shared module mounted at `/_shared/edit_primitives.js` and
// have admin import them too. Meanwhile, fixes to either copy should
// be mirrored in the other.
// =====================================================================

function openEditModal(varName) {
    const v = _state.catalogVars[varName];
    if (!v) return;
    const card = document.querySelector(`.setting-card[data-var="${varName}"]`);
    if (!card) return;

    _state.currentEditVar = varName;
    _state.currentEditEncrypted = card.dataset.encrypted === '1';
    const currentValue = card.dataset.value || '';

    $('#editModalTitle').innerText = v.label || varName;
    const helpEl = $('#editModalHelp');
    if (v.help) {
        helpEl.innerText = _clipHelp(v.help);
        helpEl.classList.remove('hidden');
    } else {
        helpEl.innerText = '';
    }

    // Build the input control + populate.
    const inputWrap = $('#editModalInputWrap');
    inputWrap.innerHTML = editControlHtml(v);
    populateInput(v, currentValue, _state.currentEditEncrypted);

    // Reset error
    const err = $('#editModalError');
    err.classList.add('hidden');
    err.innerText = '';

    // Reset button
    const saveBtn = $('#editModalSave');
    saveBtn.disabled = false;
    saveBtn.innerText = 'Save';

    // Show + focus first input
    $('#editModal').classList.remove('hidden');
    const firstInput = inputWrap.querySelector('input, textarea, select');
    if (firstInput) firstInput.focus();
}

function closeEditModal() {
    $('#editModal').classList.add('hidden');
    _state.currentEditVar = null;
    _state.currentEditEncrypted = false;
}

async function saveEditModal() {
    const varName = _state.currentEditVar;
    if (!varName) return;
    const v = _state.catalogVars[varName];
    const inputEl = $('#editModalInputWrap').querySelector('input, textarea, select');
    if (!inputEl) return;

    const err = $('#editModalError');
    err.classList.add('hidden');
    err.innerText = '';

    const validation = validateInput(v, inputEl.value);
    if (!validation.ok) {
        err.innerText = validation.msg;
        err.classList.remove('hidden');
        return;
    }

    const saveBtn = $('#editModalSave');
    saveBtn.disabled = true;
    saveBtn.innerText = 'Saving…';

    const path = `${encodeURIComponent(_state.appName)}/${encodeURIComponent(_state.deviceId)}/${encodeURIComponent(varName)}`;
    const body = {
        value: encodeForAdmin(v, inputEl.value),
        // Preserve the encrypted flag — the customer can't toggle it,
        // but we MUST pass the current state so the FR's "demote on
        // bare set" semantic doesn't silently drop it.
        encrypted: _state.currentEditEncrypted,
    };

    try {
        const r = await fetch(`${ADMIN_API}/kv/${path}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!r.ok) {
            err.innerText = `Save failed: HTTP ${r.status}`;
            err.classList.remove('hidden');
            saveBtn.disabled = false;
            saveBtn.innerText = 'Save';
            return;
        }
    } catch (e) {
        err.innerText = `Network error: ${e.message}`;
        err.classList.remove('hidden');
        saveBtn.disabled = false;
        saveBtn.innerText = 'Save';
        return;
    }

    // Re-fetch the now-stored value and update the card. Avoids a stale
    // optimistic-update bug if the server's msgpack round-trip changed
    // the shape (e.g. JSON-decoded vs raw string).
    const fresh = await fetchPerDeviceValue(_state.appName, _state.deviceId, varName);
    populateSettingValue(varName, fresh, v, _state.appName, _state.deviceId);

    closeEditModal();
}

// ---- Primitives (copied from admin/app.js — see TODO above) ----

function editControlHtml(varDesc) {
    const id = 'editModalInput';
    const t = varDesc.type;
    if (t === 'enum' && Array.isArray(varDesc.values)) {
        const opts = varDesc.values
            .map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`)
            .join('');
        return `<select id="${id}"><option value="">&mdash; select &mdash;</option>${opts}</select>`;
    }
    if (t === 'bool') {
        return `<select id="${id}"><option value="">&mdash; select &mdash;</option><option value="true">true</option><option value="false">false</option></select>`;
    }
    if (t === 'int' || t === 'float') {
        let attrs = 'type="number"';
        if (t === 'float') attrs += ' step="any"';
        if (Array.isArray(varDesc.range) && varDesc.range.length === 2) {
            attrs += ` min="${escapeHtml(String(varDesc.range[0]))}" max="${escapeHtml(String(varDesc.range[1]))}"`;
        }
        return `<input id="${id}" ${attrs} placeholder="new value">`;
    }
    // String (default). Textarea so long values like brightness_schedule
    // and wifi_password aren't truncated. Reveal button only rendered
    // when the value is encrypted (handled by populateInput post-fill).
    return `<textarea id="${id}" rows="2" placeholder="new value"></textarea>` +
           `<button class="btn-secondary reveal-btn hidden" type="button" id="editModalReveal" onclick="toggleEditModalReveal()">Reveal</button>`;
}

function populateInput(varDesc, value, encrypted) {
    const el = document.getElementById('editModalInput');
    if (!el) return;
    el.value = value;
    const revealBtn = document.getElementById('editModalReveal');
    if (encrypted && revealBtn) {
        el.classList.add('value-masked');
        revealBtn.classList.remove('hidden');
        revealBtn.innerText = 'Reveal';
    } else if (revealBtn) {
        el.classList.remove('value-masked');
        revealBtn.classList.add('hidden');
    }
}

function toggleEditModalReveal() {
    const el = document.getElementById('editModalInput');
    const btn = document.getElementById('editModalReveal');
    if (!el || !btn) return;
    const masked = el.classList.toggle('value-masked');
    btn.innerText = masked ? 'Reveal' : 'Hide';
}

function validateInput(varDesc, rawStr) {
    const s = String(rawStr);
    if (s === '') return { ok: false, msg: 'Value required.' };
    const t = varDesc.type;
    if (t === 'int') {
        if (!/^-?\d+$/.test(s)) return { ok: false, msg: `Expected an integer.` };
        const n = parseInt(s, 10);
        if (Array.isArray(varDesc.range) && varDesc.range.length === 2) {
            const [lo, hi] = varDesc.range;
            if (n < lo || n > hi) return { ok: false, msg: `Value ${n} is outside the recommended range [${lo}, ${hi}].` };
        }
        return { ok: true, value: n };
    }
    if (t === 'float') {
        if (!/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(s)) return { ok: false, msg: `Expected a number.` };
        const n = parseFloat(s);
        if (Array.isArray(varDesc.range) && varDesc.range.length === 2) {
            const [lo, hi] = varDesc.range;
            if (n < lo || n > hi) return { ok: false, msg: `Value ${n} is outside the recommended range [${lo}, ${hi}].` };
        }
        return { ok: true, value: n };
    }
    if (t === 'bool') {
        const lc = s.toLowerCase();
        if (['true', '1', 'yes', 'y', 'on'].includes(lc)) return { ok: true, value: true };
        if (['false', '0', 'no', 'n', 'off'].includes(lc)) return { ok: true, value: false };
        return { ok: false, msg: `Expected true or false.` };
    }
    if (t === 'enum') {
        const vals = Array.isArray(varDesc.values) ? varDesc.values : [];
        if (!vals.includes(s)) return { ok: false, msg: `Must be one of: ${vals.join(', ')}` };
        return { ok: true, value: s };
    }
    return { ok: true, value: s };
}

// The admin POST handler json.loads() the value string and falls back
// to raw string on parse error. Encode per type so the round-trip
// matches the CLI's msgpack shape (ints → msgpack int, etc.).
function encodeForAdmin(varDesc, raw) {
    return String(raw);
}
