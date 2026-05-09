// Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
// See LICENSE in the repo root.
const API_BASE = '/api/admin';

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Navigation Logic
document.querySelectorAll('.nav-links a').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        
        // Update active class
        document.querySelectorAll('.nav-links a').forEach(l => l.classList.remove('active'));
        e.target.classList.add('active');
        
        // Show correct view
        const targetId = e.target.getAttribute('data-target');
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active-view'));
        document.getElementById(targetId).classList.add('active-view');

        // Fetch specifics immediately on tab switch
        if (targetId === 'dashboard') fetchStats();
        if (targetId === 'keys') fetchKeys();
        if (targetId === 'admin_users') fetchAdminUsers();
        if (targetId === 'catalogs') { closeCatalogDetail(); fetchCatalogList(); }
        if (targetId === 'logs') { logClientsLoaded = false; fetchLogs(); }
    });
});

// Helper for HTTP requests — returns {ok, status, data}
async function fetchAPI(endpoint, method = 'GET', body = null) {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) options.body = JSON.stringify(body);
    const res = await fetch(`${API_BASE}${endpoint}`, options);
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
}

// Format Time
function formatTime(unixTime) {
    const d = new Date(unixTime * 1000);
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
}

// 0. Redis Health Check
async function checkRedisStatus() {
    try {
        const { ok, data } = await fetchAPI('/stats');
        updateStatus(ok);
        return ok ? data : null;
    } catch (e) {
        updateStatus(false);
        return null;
    }
}

function updateStatus(isOnline) {
    const dot = document.querySelector('.status-dot');
    const text = document.getElementById('redisStatus');
    if (isOnline) {
        dot.className = 'status-dot online';
        text.innerText = 'Connected';
        text.style.color = 'var(--accent-success)';
    } else {
        dot.className = 'status-dot offline';
        text.innerText = 'Offline';
        text.style.color = 'var(--accent-danger)';
    }
}

// 1. Dashboard / Stats
async function fetchStats() {
    const data = await checkRedisStatus();
    if (!data) {
        document.getElementById('queueCount').innerText = '--';
        document.getElementById('kvCount').innerText = '--';
        document.getElementById('queueList').innerHTML = '<div class="text-muted">Redis is unreachable. Please check the server logs.</div>';
        document.getElementById('kvList').innerHTML = '<div class="text-muted">Redis is unreachable.</div>';
        return;
    }
    
    document.getElementById('queueCount').innerText = data.queues.length;
    document.getElementById('kvCount').innerText = data.kvs.length;

    const qList = document.getElementById('queueList');
    const sortedQueues = [...data.queues].sort((a, b) => a.topic.localeCompare(b.topic));
    qList.innerHTML = sortedQueues.map(q => {
        const t = escapeHtml(q.topic);
        return `
        <div class="data-item">
            <div><strong>${t}</strong> (Msg Count: ${q.count})</div>
            <div>
                <button class="btn-sm" data-action="peekData" data-kind="q" data-target="${t}">Peek</button>
                <button class="btn-sm" data-action="openMonitor" data-target="${t}">Monitor</button>
                <button class="btn-sm danger" data-action="deleteData" data-kind="q" data-target="${t}">Delete</button>
            </div>
        </div>
    `}).join('') || '<div class="text-muted">No active queues</div>';

    const kvList = document.getElementById('kvList');
    const sortedKvs = [...data.kvs].sort((a, b) => a.key.localeCompare(b.key));
    kvList.innerHTML = sortedKvs.map(k => {
        const key = escapeHtml(k.key);
        // Encrypted records get a lock badge so an operator scanning the list
        // can tell at a glance which values are confidential. The flag comes
        // from the server's per-record sidecar, surfaced in /stats.
        const encBadge = k.encrypted
            ? ' <span class="badge badge-encrypted" title="Encrypted on the wire to devices">🔒 encrypted</span>'
            : '';
        return `
        <div class="data-item">
            <div><strong>${key}</strong>${encBadge}</div>
            <div>
                <button class="btn-sm" data-action="editData" data-kind="kv" data-target="${key}">Edit</button>
                <button class="btn-sm" data-action="peekData" data-kind="kv" data-target="${key}">Read</button>
                <button class="btn-sm danger" data-action="deleteData" data-kind="kv" data-target="${key}">Delete</button>
            </div>
        </div>
    `}).join('') || '<div class="text-muted">No active KV pairs</div>';
}

async function peekData(type, keyId) {
    const { data } = await fetchAPI(`/peek/${type}/${keyId}`);
    const modal = document.getElementById('peekModal');
    document.getElementById('peekTitle').innerText = `Data: ${type}/${keyId}`;
    const code = document.getElementById('peekDataContent');
    if (data.status === 'empty') {
        code.innerText = 'Empty / Value Not Found';
    } else {
        // Peek shows the *stored* value (plaintext) regardless of the
        // encrypted flag — encryption only happens on the device-facing
        // GET path. The flag annotation just tells the operator what
        // device clients see on the wire.
        const encLine = (type === 'kv' && data.encrypted)
            ? 'Encrypted: yes (device GETs return msgpack ext 0x21)\n\n'
            : (type === 'kv' ? 'Encrypted: no\n\n' : '');
        const decoded = JSON.stringify(data.message, null, 2);
        code.innerText = `${encLine}Decoded MessagePack:\n${decoded}\n\nHex Format:\n${data.hex}`;
    }
    modal.style.display = 'block';
}

async function deleteData(type, keyId) {
    if(!confirm(`Are you sure you want to delete ${type}/${keyId}?`)) return;
    await fetchAPI(`/${type}/${keyId}`, 'DELETE');
    fetchStats();
}

// Close Modal
document.querySelectorAll('.close-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
        const modal = e.target.closest('.modal');
        if (!modal) return;
        if (modal.id === 'aclModal') {
            closeAclModal();
        } else {
            modal.style.display = 'none';
        }
    });
});

// KV Management
function openKvModal(key = '', val = '', encrypted = false) {
    document.getElementById('kvKeyInput').value = key;
    const valEl = document.getElementById('kvValueInput');
    valEl.value = val;
    document.getElementById('kvEncryptedInput').checked = !!encrypted;
    // Mask the value field when editing an encrypted record so the plaintext
    // doesn't sit visible while the operator is here for an unrelated tweak.
    // The Reveal button toggles the visual mask without touching the value.
    const revealBtn = document.getElementById('kvRevealBtn');
    if (encrypted) {
        valEl.classList.add('value-masked');
        revealBtn.style.display = '';
        revealBtn.innerText = 'Reveal';
    } else {
        valEl.classList.remove('value-masked');
        revealBtn.style.display = 'none';
    }
    document.getElementById('kvModalTitle').innerText = key ? 'Edit KV Pair' : 'Add KV Pair';
    document.getElementById('kvKeyInput').disabled = !!key; // Disable modifying the key if editing
    document.getElementById('kvKeyInput').style.opacity = key ? '0.6' : '1';
    document.getElementById('kvModal').style.display = 'block';
}

function closeKvModal() {
    document.getElementById('kvModal').style.display = 'none';
}

async function editData(type, keyId) {
    if (type !== 'kv') return;
    const { data } = await fetchAPI(`/peek/kv/${keyId}`);
    if (data.status === 'ok') {
        const val = typeof data.message === 'object' ? JSON.stringify(data.message) : String(data.message);
        // Pre-fill the Encrypted checkbox from the current sidecar state.
        // Without this, the FR's "demote to plaintext on bare set" semantic
        // would silently downgrade an encrypted record any time someone
        // opened Edit and clicked Save without re-checking the box.
        openKvModal(keyId, val, !!data.encrypted);
    }
}

async function saveKv() {
    const key = document.getElementById('kvKeyInput').value.trim();
    const val = document.getElementById('kvValueInput').value.trim();
    const encrypted = document.getElementById('kvEncryptedInput').checked;
    if (!key || val === '') {
        alert("Both Key and Value are required.");
        return;
    }
    const { ok, status } = await fetchAPI(`/kv/${key}`, 'POST', { value: val, encrypted });
    if (!ok) {
        alert(`Failed to save KV pair (HTTP ${status})`);
        return;
    }
    closeKvModal();
    fetchStats();
}


// 2. Key Management
let allClientsData = {};

function formatAclSummary(acl) {
    const perms = acl.permissions || [];
    if (perms.length === 0)
        return '<span class="badge badge-no-access">No Access</span>';
    // `display:inline-block` makes each badge atomic (the wrap engine
    // can't split the prefix from the `: rw` mid-badge); `.join(' ')`
    // gives the wrap engine actual whitespace to break on between
    // badges. `white-space:nowrap` belt-and-suspenders the no-mid-
    // badge-split inside the inline-block. Replaces an earlier
    // `.join('')` + `&thinsp;` shape that wrapped poorly when a user
    // had several device-scope grants.
    return perms.map(p =>
        `<span class="badge badge-acl-prefix badge-acl-${p.access==='rw' ? 'rw' : 'r'}">` +
            `${escapeHtml(p.prefix)} : ${escapeHtml(p.access)}` +
        `</span>`
    ).join(' ');
}

async function fetchKeys() {
    const { data: clients } = await fetchAPI('/keys');
    allClientsData = {};
    clients.forEach(c => allClientsData[c.client_id] = c);

    const tbody = document.getElementById('clientsTableBody');
    const sortedClients = [...clients].sort((a, b) => a.client_id.localeCompare(b.client_id));
    tbody.innerHTML = sortedClients.map(c => {
        const id = escapeHtml(c.client_id);
        return `
        <tr>
            <td><strong>${id}</strong></td>
            <td>${formatAclSummary(c.acl)}</td>
            <td class="col-nowrap">
                <button class="btn-sm" data-action="openAclModal" data-target="${id}">Edit ACL</button>
                <button class="btn-sm danger" data-action="revokeClient" data-target="${id}">Revoke</button>
            </td>
        </tr>
    `}).join('');
}

document.getElementById('keyForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const clientId = document.getElementById('newClientId').value.trim();
    const { ok, data: res } = await fetchAPI('/keys', 'POST', { client_id: clientId });

    const display = document.getElementById('newSecretDisplay');
    if (!ok) {
        display.innerHTML = `<strong class="text-error">Error:</strong> ${res.detail || 'Unknown error'}`;
        display.classList.remove('hidden');
        return;
    }
    display.innerHTML = `<strong>Success!</strong> Secret key generated.<br><br>
                         Client ID: <code>${res.client_id}</code><br>
                         Key (Hex): <code>${res.secret}</code><br><br>
                         <small class="text-error">Warning: This key will not be displayed again.</small><br><br>
                         <small class="text-muted">Client has <strong>no access</strong> by default. Click Edit ACL to add permissions.</small>`;
    display.classList.remove('hidden');
    document.getElementById('newClientId').value = '';
    fetchKeys();
});

// Device provisioning: one-shot create-client + grant device-on-app ACL.
// Calls /api/admin/provision_device. Display mirrors the bare /keys
// success view above — same "key won't be shown again" warning, same
// table refresh — but also surfaces the auto-applied ACL so the
// operator can confirm it matches expectations before using the
// secret.
document.getElementById('provisionDeviceForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const app = document.getElementById('provisionAppName').value.trim();
    const clientId = document.getElementById('provisionClientId').value.trim();
    const display = document.getElementById('provisionResultDisplay');

    const { ok, data: res } = await fetchAPI('/provision_device', 'POST', {
        client_id: clientId,
        app,
    });

    if (!ok) {
        display.innerHTML = `<strong class="text-error">Error:</strong> ${escapeHtml(res.detail || 'Unknown error')}`;
        display.classList.remove('hidden');
        return;
    }
    const aclLines = res.acl.permissions
        .map(p => `&nbsp;&nbsp;${escapeHtml(p.prefix)} <code>${escapeHtml(p.access)}</code>`)
        .join('<br>');
    // Two response shapes — `created: true` (new client, secret to display)
    // and `created: false` (existing client, secret intentionally null
    // since we don't re-leak existing secrets via provision). The UI
    // copy distinguishes the two cases so the operator knows whether
    // they need to copy the secret right now.
    let header, secretBlock;
    if (res.created) {
        header = `<strong>Provisioned!</strong> <code>${escapeHtml(res.client_id)}</code> for app <code>${escapeHtml(app)}</code>.`;
        secretBlock = `<strong>Secret (hex):</strong> <code>${escapeHtml(res.secret)}</code><br>
                       <small class="text-error">Save this now — it won't be shown again.</small>`;
    } else {
        header = `<strong>ACL updated</strong> for existing client <code>${escapeHtml(res.client_id)}</code> on app <code>${escapeHtml(app)}</code>.`;
        secretBlock = `<small class="text-muted">Existing secret left untouched — the device's authentication is unchanged.</small>`;
    }
    display.innerHTML = `${header}<br><br>
                         ${secretBlock}<br><br>
                         <strong>ACL:</strong><br>${aclLines}`;
    display.classList.remove('hidden');
    document.getElementById('provisionClientId').value = '';
    fetchKeys();
});

async function revokeClient(id) {
    if(!confirm(`Revoke client ${id}? This action cannot be undone.`)) return;
    await fetchAPI(`/keys/${id}`, 'DELETE');
    fetchKeys();
}

// --- ACL Editor ---
// The editor is shared between HMAC clients (/keys/<id>/acl) and admin
// users (/admin_users/<user>/acl). `_aclTarget` carries the save endpoint
// and a refresh callback so the modal stays decoupled from its caller.
let aclEditingClientId = null;
let aclCurrentPermissions = [];
let aclNewAccess = 'rw';
let _aclTarget = null;  // { endpoint: string, refresh: () => void }

function openAclModal(clientId) {
    const client = allClientsData[clientId];
    _openAclEditor({
        subjectLabel: clientId,
        permissions: (client.acl || {}).permissions || [],
        endpoint: `/keys/${clientId}/acl`,
        refresh: fetchKeys,
        // Device-side editor: load the "admin users with access" panel.
        deviceClientId: clientId,
    });
    aclEditingClientId = clientId;  // kept for any legacy reference
}

function openAdminAclModalFor(username) {
    const entry = _adminUsersById[username];
    if (!entry) return;
    _openAclEditor({
        subjectLabel: `admin: ${username}`,
        permissions: (entry.acl || {}).permissions || [],
        endpoint: `/admin_users/${encodeURIComponent(username)}/acl`,
        refresh: fetchAdminUsers,
        // Admin-user-side editor: enable the device picker.
        showDevicePicker: true,
    });
}

function _openAclEditor({ subjectLabel, permissions, endpoint, refresh, deviceClientId, showDevicePicker }) {
    _aclTarget = { endpoint, refresh };
    aclCurrentPermissions = permissions.map(p => ({...p}));
    aclNewAccess = 'rw';

    document.getElementById('aclClientName').textContent = subjectLabel;
    document.getElementById('aclNewPrefix').value = '';
    const toggle = document.getElementById('aclNewAccessToggle');
    toggle.textContent = 'rw';
    toggle.className = 'acl-access-toggle access-rw';

    // Show/hide the per-context affordances:
    // - device picker: only when editing an admin user (lets operator
    //   click devices instead of typing prefixes)
    // - admins-with-access: only when editing a client/device
    document.getElementById('aclSelectDevicesBtn').style.display =
        showDevicePicker ? '' : 'none';
    if (deviceClientId) {
        loadAdminsWithAccess(deviceClientId);
    } else {
        document.getElementById('aclModalAdminsSection').style.display = 'none';
    }

    renderAclPermissions();
    document.getElementById('aclModal').style.display = 'block';
}

function closeAclModal() {
    document.getElementById('aclModal').style.display = 'none';
    aclEditingClientId = null;
    aclCurrentPermissions = [];
    _aclTarget = null;
}

// --- Device picker (called from the admin-user ACL editor) ----------

// Mirrors the backend's `_prefix_matches` (api/dependencies.py): an ACL
// rule with `prefix` covers `path` when prefix is `*`, equals path, or is
// a parent (path starts with prefix + "/"). Used by the picker to mark
// devices as "already granted" when a wildcard or app-level rule covers
// them, not just exact <app>/<device> matches.
//
// Pre-v1.6.3 the picker used `p.prefix === token` everywhere, so a user
// with `*:rw` (or `<app>:rw`) saw every device as un-granted — surprising
// for global admins and led to the picker pushing redundant rules in.
function _aclPrefixCovers(prefix, path) {
    if (!prefix) return false;
    if (prefix === '*') return true;
    if (prefix === path) return true;
    return path.startsWith(prefix + '/');
}

let _devicePickerSelected = new Set();   // "<app>/<device>" tokens

async function openDevicePicker() {
    const { ok, data: clients } = await fetchAPI('/keys');
    if (!ok) {
        alert('Failed to load device list.');
        return;
    }

    // Group clients by app. A client's app is the head of its
    // <app>/<client_id> ACL prefix (the "device on app" pattern from
    // provision_device). Clients without that shape (internal probes,
    // legacy custom-ACL clients) are skipped.
    const byApp = {};
    clients.forEach(c => {
        const app = _deriveApp(c);
        if (!app) return;
        if (!byApp[app]) byApp[app] = [];
        byApp[app].push(c.client_id);
    });

    _devicePickerSelected.clear();
    const list = document.getElementById('devicePickerList');
    const apps = Object.keys(byApp).sort();
    if (apps.length === 0) {
        list.innerHTML = '<p class="text-muted">No device-shaped clients found.</p>';
    } else {
        list.innerHTML = apps.map(app => {
            const devices = byApp[app].sort();
            return `
                <div class="device-picker-app">
                    <h4>${escapeHtml(app)}</h4>
                    <ul>
                        ${devices.map(d => {
                            const token = `${app}/${d}`;
                            // Pre-check if this device is already covered
                            // by any existing ACL rule — exact match,
                            // app-level wildcard, or `*:rw`. Mirrors the
                            // backend's _prefix_matches semantics (see
                            // _aclPrefixCovers above). Pre-v1.6.3 used
                            // strict equality, so `*:rw` users saw every
                            // device as un-granted.
                            const already = aclCurrentPermissions.some(
                                p => _aclPrefixCovers(p.prefix, token)
                            );
                            return `<li>
                                <label>
                                    <input type="checkbox"
                                           data-token="${escapeHtml(token)}"
                                           ${already ? 'disabled title="Already in ACL"' : ''}
                                           data-change-action="devicePickerToggle">
                                    ${escapeHtml(d)}${already ? ' <span class="text-muted">(already granted)</span>' : ''}
                                </label>
                            </li>`;
                        }).join('')}
                    </ul>
                </div>
            `;
        }).join('');
    }

    _updateDevicePickerCount();
    document.getElementById('devicePickerModal').style.display = 'block';
}

function _deriveApp(client) {
    // Find the prefix shaped "<X>/<client_id>" — that's the "device
    // on app" pattern, where <X> is the app name.
    const perms = (client.acl && client.acl.permissions) || [];
    for (const p of perms) {
        const prefix = p.prefix || '';
        const slash = prefix.indexOf('/');
        if (slash >= 0 && prefix.substring(slash + 1) === client.client_id) {
            return prefix.substring(0, slash);
        }
    }
    return null;
}

function _devicePickerToggle(checkbox) {
    const token = checkbox.dataset.token;
    if (checkbox.checked) {
        _devicePickerSelected.add(token);
    } else {
        _devicePickerSelected.delete(token);
    }
    _updateDevicePickerCount();
}

function _updateDevicePickerCount() {
    const btn = document.getElementById('devicePickerConfirm');
    const n = _devicePickerSelected.size;
    btn.textContent = `Add ${n} device${n === 1 ? '' : 's'}`;
    btn.disabled = (n === 0);
}

function closeDevicePicker() {
    document.getElementById('devicePickerModal').style.display = 'none';
    _devicePickerSelected.clear();
}

function confirmDevicePicker() {
    // Per design decisions: <app>/<device> at rw, <app>/public at r,
    // dedupe against any existing rules so re-applying the picker is
    // safe.
    // Dedupe via _aclPrefixCovers, not strict equality: if the operator
    // already has `*:rw` or `<app>:rw`, don't push a redundant
    // `<app>/<device>:rw` (or `<app>/public:r`) row. Pre-v1.6.3 we'd
    // pile in redundant rules whenever a wildcard or app-level rule
    // already covered the picker's tokens.
    const apps = new Set();
    _devicePickerSelected.forEach(token => {
        const slash = token.indexOf('/');
        const app = token.substring(0, slash);
        apps.add(app);
        if (!aclCurrentPermissions.some(p => _aclPrefixCovers(p.prefix, token))) {
            aclCurrentPermissions.push({prefix: token, access: 'rw'});
        }
    });
    apps.forEach(app => {
        const publicPrefix = `${app}/public`;
        if (!aclCurrentPermissions.some(p => _aclPrefixCovers(p.prefix, publicPrefix))) {
            aclCurrentPermissions.push({prefix: publicPrefix, access: 'r'});
        }
    });
    closeDevicePicker();
    renderAclPermissions();
}

// --- Admin users with access (device-side ACL editor) ---------------

async function loadAdminsWithAccess(clientId) {
    const section = document.getElementById('aclModalAdminsSection');
    section.style.display = 'block';
    const list = document.getElementById('aclModalAdminsList');
    list.innerHTML = '<li class="text-muted">Loading...</li>';

    const { ok, data } = await fetchAPI(
        `/keys/${encodeURIComponent(clientId)}/admins`
    );
    if (!ok) {
        list.innerHTML = '<li class="text-muted">Failed to load.</li>';
        return;
    }
    if (data.app === null) {
        // Not a device-shaped client; the relationship view doesn't apply.
        section.style.display = 'none';
        return;
    }
    if (!data.admins || data.admins.length === 0) {
        list.innerHTML = '<li class="text-muted">No admins have access to this device.</li>';
        return;
    }
    list.innerHTML = data.admins.map(a => {
        const uname = escapeHtml(a.username);
        const access = escapeHtml(a.access);
        return `<li>
            <a href="#" data-action="jumpToAdminUser" data-uname="${uname}">${uname}</a>
            <span class="badge access-${access} badge-spaced">${access}</span>
        </li>`;
    }).join('');
}

async function jumpToAdminUser(username) {
    // Per design decision #3: clean stack. Close the current device
    // ACL modal, then open the user's ACL modal.
    closeAclModal();
    // Switch to the admin_users view so the user is on the right page
    // when the modal closes.
    const link = document.querySelector('.nav-links a[data-target="admin_users"]');
    if (link) link.click();
    // The view-switching click handler triggers fetchAdminUsers; await
    // explicitly so the user data is in _adminUsersById before we open.
    await fetchAdminUsers();
    openAdminAclModalFor(username);
}

function renderAclPermissions() {
    const list = document.getElementById('aclPermissionsList');
    if (aclCurrentPermissions.length === 0) {
        list.innerHTML = '<div class="text-muted acl-empty">No rules &mdash; client has no access to anything.</div>';
        return;
    }
    list.innerHTML = aclCurrentPermissions.map((p, i) => `
        <div class="acl-rule-row">
            <span class="acl-prefix-label">${escapeHtml(p.prefix)}</span>
            <button class="acl-access-toggle ${p.access === 'rw' ? 'access-rw' : 'access-r'}"
                    data-action="aclToggleAccess" data-index="${i}">${escapeHtml(p.access)}</button>
            <button class="btn-sm danger" data-action="aclRemoveRule" data-index="${i}">&#x2715;</button>
        </div>
    `).join('');
}

function aclToggleAccess(index) {
    aclCurrentPermissions[index].access =
        aclCurrentPermissions[index].access === 'rw' ? 'r' : 'rw';
    renderAclPermissions();
}

function aclRemoveRule(index) {
    aclCurrentPermissions.splice(index, 1);
    renderAclPermissions();
}

function toggleNewAccess() {
    aclNewAccess = aclNewAccess === 'rw' ? 'r' : 'rw';
    const btn = document.getElementById('aclNewAccessToggle');
    btn.textContent = aclNewAccess;
    btn.className = `acl-access-toggle ${aclNewAccess === 'rw' ? 'access-rw' : 'access-r'}`;
}

function aclAddRule() {
    const prefix = document.getElementById('aclNewPrefix').value.trim();
    if (!prefix) { document.getElementById('aclNewPrefix').focus(); return; }
    // Prevent duplicates
    if (aclCurrentPermissions.some(p => p.prefix === prefix)) {
        document.getElementById('aclNewPrefix').select();
        return;
    }
    aclCurrentPermissions.push({ prefix, access: aclNewAccess });
    document.getElementById('aclNewPrefix').value = '';
    renderAclPermissions();
}

async function saveAcl() {
    if (!_aclTarget) return;
    const { ok, status, data } = await fetchAPI(_aclTarget.endpoint, 'PUT', {
        permissions: aclCurrentPermissions
    });
    if (!ok) {
        alert(`Save failed (HTTP ${status}): ${data.detail || JSON.stringify(data)}`);
        return;
    }
    const refresh = _aclTarget.refresh;
    closeAclModal();
    if (refresh) refresh();
}

document.getElementById('aclModalClose').addEventListener('click', closeAclModal);
document.getElementById('aclNewPrefix').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); aclAddRule(); }
});

// 2a. Admin Users
let _adminUsersById = {};

async function fetchAdminUsers() {
    const { ok, data } = await fetchAPI('/admin_users');
    const tbody = document.getElementById('adminUsersTableBody');
    if (!ok) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-muted">Failed to load (HTTP error).</td></tr>`;
        return;
    }
    _adminUsersById = {};
    data.forEach(u => { _adminUsersById[u.username] = u; });

    // Server returns sorted alphabetically; sort defensively in case
    // older endpoints don't.
    const rows = [...data].sort((a, b) => a.username.localeCompare(b.username));
    tbody.innerHTML = rows.map(u => {
        const uname = escapeHtml(u.username);
        const source = renderSourceBadge(u.source);
        const status = u.provisioned
            ? '<span class="badge badge-provisioned">provisioned</span>'
            : '<span class="badge badge-needs-setup" title="No ACL row in Redis — this user sees no KV. Run migrate_admin_acls.py or edit here.">needs setup</span>';
        // Delete is only meaningful for OAuth/acl-only users — the
        // operation removes their admin_acls row. For htpasswd users,
        // deleting the ACL row leaves them able to authenticate but
        // with deny-all (rescue users still get the wildcard via
        // RESCUE_USERS, others get nothing). Show the button for OAuth
        // users only to avoid foot-shooting; htpasswd ACL clearing is
        // a deliberate operator action via redis-cli if ever needed.
        const deleteBtn = (u.source === 'oauth' || u.source === 'acl-only')
            ? `<button class="btn-sm btn-danger" data-action="confirmDeleteAdminUser" data-uname="${uname}">Delete</button>`
            : '';
        return `
            <tr>
                <td><strong>${uname}</strong></td>
                <td>${source}</td>
                <td>${formatAclSummary(u.acl)}</td>
                <td>${status}</td>
                <td>
                    <button class="btn-sm" data-action="openAdminAclModalFor" data-uname="${uname}">Edit ACL</button>
                    ${deleteBtn}
                </td>
            </tr>
        `;
    }).join('') || '<tr><td colspan="5" class="text-muted">No admin users found.</td></tr>';
}

function renderSourceBadge(source) {
    // OAuth = blue (sign-in via Google on browser hostname).
    // htpasswd = purple (Basic auth on device hostname / rescue).
    // acl-only = orange (no auth path — orphaned row, clean up).
    switch (source) {
        case 'oauth':
            return '<span class="badge badge-auth-oauth" title="Signs in via Google OAuth on the browser hostname">OAuth</span>';
        case 'htpasswd':
            return '<span class="badge badge-auth-htpasswd" title="Signs in via Basic auth on the device hostname (rescue path)">htpasswd</span>';
        case 'acl-only':
        default:
            return '<span class="badge badge-auth-acl-only" title="ACL row exists but no auth path — likely orphaned. Delete or add to htpasswd / OAuth.">acl-only</span>';
    }
}

async function addOauthAdmin() {
    const input = document.getElementById('newOauthAdminEmail');
    const email = (input.value || '').trim();
    if (!email) {
        alert('Enter an email address.');
        input.focus();
        return;
    }
    if (!email.includes('@') || !email.includes('.')) {
        alert('Doesn\'t look like an email — check spelling.');
        input.focus();
        return;
    }
    if (_adminUsersById[email]) {
        alert(`'${email}' already exists in the table — use Edit ACL to change permissions.`);
        return;
    }
    // Create the row by setting an initial ACL. Default: empty
    // permissions (deny-all). Operator opens the ACL editor next to
    // grant. Could pre-populate with a wildcard, but that's a
    // dangerous default — explicit is safer.
    const initialAcl = { permissions: [] };
    const { ok, data } = await fetchAPI(
        `/admin_users/${encodeURIComponent(email)}/acl`,
        'PUT',
        initialAcl,
    );
    if (!ok) {
        alert(`Failed: ${(data && data.detail) || 'unknown error'}`);
        return;
    }
    input.value = '';
    await fetchAdminUsers();
    // Open the ACL editor immediately so the operator can grant
    // permissions — the user is otherwise sitting in deny-all.
    openAdminAclModalFor(email);
}

async function confirmDeleteAdminUser(username) {
    if (!confirm(`Delete the ACL row for '${username}'?\n\n` +
                 `This revokes their permissions immediately. ` +
                 `If they're an OAuth user, their next sign-in attempt ` +
                 `will land on the unauthorized page.`)) {
        return;
    }
    const { ok, data } = await fetchAPI(
        `/admin_users/${encodeURIComponent(username)}/acl`,
        'DELETE',
    );
    if (!ok) {
        alert(`Failed to delete: ${(data && data.detail) || 'unknown error'}`);
        return;
    }
    await fetchAdminUsers();
}

document.getElementById('addOauthAdminBtn').addEventListener('click', addOauthAdmin);
document.getElementById('newOauthAdminEmail').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        addOauthAdmin();
    }
});

// 2b. Catalogs (M3a — read-only)
// Reads published catalog YAMLs out of /kv/_catalog/* via the admin
// scan + peek endpoints and renders them as a browsable table. Parsing
// is client-side (js-yaml) — the server stores the YAML opaquely.

const CATALOG_PREFIX = '_catalog/';

// Cached parsed catalog for the currently-open app — lets the key editor
// modal look up the var descriptor (type / range / help / ...) without
// re-fetching. Cleared on back-nav or tab switch.
let _currentCatalog = null;   // { app, vars: {name: varDescriptor} }

function _formatScope(scope) {
    if (!Array.isArray(scope)) return escapeHtml(String(scope || ''));
    return scope.map(escapeHtml).join(',');
}

function _formatDefault(v) {
    if (v.default_per_device) return '<em class="text-muted">per-device</em>';
    if (v.default === undefined || v.default === null) return '<span class="text-muted">&mdash;</span>';
    return escapeHtml(JSON.stringify(v.default));
}

function _formatRange(v) {
    if (v.type === 'enum' && Array.isArray(v.values)) {
        return v.values.map(x => `<code>${escapeHtml(String(x))}</code>`).join(' ');
    }
    if (Array.isArray(v.range) && v.range.length === 2) {
        return `<code>[${escapeHtml(String(v.range[0]))}, ${escapeHtml(String(v.range[1]))}]</code>`;
    }
    return '<span class="text-muted">&mdash;</span>';
}

function _formatHelp(v) {
    const raw = (v.help || '').trim();
    if (!raw) return '';
    // Collapse the first line as a summary; the full help hides in `title`.
    const firstLine = raw.split('\n')[0];
    return `<span title="${escapeHtml(raw)}">${escapeHtml(firstLine)}</span>`;
}

async function fetchCatalogList() {
    const listEl = document.getElementById('catalogAppList');
    const countEl = document.getElementById('catalogAppCount');
    listEl.innerHTML = '<div class="text-muted">Loading&hellip;</div>';

    const { ok, data } = await fetchAPI(`/kv_scan?prefix=${encodeURIComponent(CATALOG_PREFIX)}`);
    if (!ok) {
        listEl.innerHTML = `<div class="text-muted">Failed to list catalogs (HTTP ${data && data.detail ? escapeHtml(data.detail) : 'error'}).</div>`;
        countEl.innerText = '--';
        return;
    }

    const items = Array.isArray(data.items) ? data.items : [];
    countEl.innerText = items.length;

    if (items.length === 0) {
        listEl.innerHTML = `<div class="text-muted">No catalogs published yet. Apps publish with <code>stra2us catalog publish</code>; see <a href="https://github.com/" target="_blank" rel="noopener">tools/README.md</a>.</div>`;
        return;
    }

    listEl.innerHTML = items.map(it => {
        const app = it.key.startsWith(CATALOG_PREFIX) ? it.key.slice(CATALOG_PREFIX.length) : it.key;
        const kb = (it.bytes / 1024).toFixed(1);
        return `
            <div class="catalog-app-row" data-action="openCatalogDetail" data-app="${escapeHtml(app)}">
                <span class="catalog-app-name">${escapeHtml(app)}</span>
                <span class="catalog-app-meta">${kb} KB &middot; <code>${escapeHtml(it.key)}</code></span>
                <span class="catalog-app-chevron">&rsaquo;</span>
            </div>
        `;
    }).join('');
}

async function openCatalogDetail(app) {
    document.getElementById('catalogListPane').classList.add('hidden');
    document.getElementById('catalogDetailPane').classList.remove('hidden');
    document.getElementById('catalogDetailTitle').innerText = app;
    // Fresh app — lazy-loaded tabs must refetch.
    _catalogTabsLoaded = {};
    switchCatalogTab('variables');
    closeDeviceDetail();

    const errEl = document.getElementById('catalogDetailError');
    const body = document.getElementById('catalogVarsBody');
    errEl.classList.add('hidden');
    body.innerHTML = `<tr><td colspan="6" class="text-muted">Loading&hellip;</td></tr>`;

    const key = `${CATALOG_PREFIX}${app}`;
    const { ok, data } = await fetchAPI(`/peek/kv/${encodeURIComponent(key).replace(/%2F/g, '/')}`);
    if (!ok || data.status !== 'ok') {
        errEl.innerText = `Could not load _catalog/${app}: ${data && data.status ? data.status : 'error'}`;
        errEl.classList.remove('hidden');
        body.innerHTML = '';
        return;
    }

    const yamlText = typeof data.message === 'string' ? data.message : '';
    if (!yamlText) {
        errEl.innerText = `Stashed value at _catalog/${app} is not a string (got ${typeof data.message}). Was it published as YAML text?`;
        errEl.classList.remove('hidden');
        body.innerHTML = '';
        return;
    }

    let cat;
    try {
        cat = jsyaml.load(yamlText);
    } catch (e) {
        errEl.innerText = `YAML parse error: ${e.message}`;
        errEl.classList.remove('hidden');
        body.innerHTML = '';
        return;
    }

    if (!cat || typeof cat !== 'object' || !cat.vars) {
        errEl.innerText = 'Catalog has no `vars` section. File a bug with the app owner — catalog is malformed.';
        errEl.classList.remove('hidden');
        body.innerHTML = '';
        return;
    }

    _currentCatalog = { app, vars: cat.vars };

    const rows = Object.entries(cat.vars).map(([name, v]) => {
        v = v || {};
        return `
            <tr class="catalog-var-row" data-action="openKeyEditor" data-var-name="${escapeHtml(name)}">
                <td><code>${escapeHtml(name)}</code></td>
                <td>${escapeHtml(v.type || '?')}</td>
                <td>${_formatScope(v.scope)}</td>
                <td>${_formatDefault(v)}</td>
                <td>${_formatRange(v)}</td>
                <td>${_formatHelp(v)}</td>
            </tr>
        `;
    }).join('');
    body.innerHTML = rows || `<tr><td colspan="6" class="text-muted">Catalog has no variables.</td></tr>`;
}

function closeCatalogDetail() {
    _currentCatalog = null;
    document.getElementById('catalogListPane').classList.remove('hidden');
    document.getElementById('catalogDetailPane').classList.add('hidden');
    // Reset to Variables tab so the next open starts in a known state.
    switchCatalogTab('variables');
    closeDeviceDetail();
}


// --- Catalog detail tabs (M3c) --------------------------------------------
// Three tabs in the app detail pane: Variables | Devices | Raw. Data for
// Devices/Raw is fetched lazily on first entry per app-open.

const CATALOG_TABS = ['variables', 'devices', 'raw'];
let _catalogTabsLoaded = {};  // { devices: bool, raw: bool }

function switchCatalogTab(tab) {
    if (!CATALOG_TABS.includes(tab)) return;
    CATALOG_TABS.forEach(t => {
        const pane = document.getElementById(`catalog${t.charAt(0).toUpperCase()+t.slice(1)}TabPane`);
        const btn = document.querySelector(`.catalog-tab[data-tab="${t}"]`);
        if (pane) pane.classList.toggle('hidden', t !== tab);
        if (btn) btn.classList.toggle('active', t === tab);
    });

    if (tab === 'devices' && !_catalogTabsLoaded.devices) {
        fetchCatalogDevices();
    }
    if (tab === 'raw' && !_catalogTabsLoaded.raw) {
        fetchCatalogRaw();
    }
}

// Parse the middle segment out of `<app>/<device>/<name>`. Keys with only
// two segments (`<app>/<name>`) are app-scope and contribute no device.
function _parseDeviceFromKey(app, keyName) {
    if (!keyName.startsWith(`${app}/`)) return null;
    const rest = keyName.slice(app.length + 1);
    const firstSlash = rest.indexOf('/');
    if (firstSlash < 0) return null;  // app-scope key, no device segment
    return rest.slice(0, firstSlash);
}

async function _scanAppKeys(app) {
    const { ok, data } = await fetchAPI(`/kv_scan?prefix=${encodeURIComponent(app + '/')}`);
    if (!ok) return { ok: false, items: [] };
    return { ok: true, items: Array.isArray(data.items) ? data.items : [], truncated: !!data.truncated };
}

async function fetchCatalogDevices() {
    if (!_currentCatalog) return;
    const app = _currentCatalog.app;
    const listEl = document.getElementById('catalogDevicesList');
    const countEl = document.getElementById('catalogDevicesCount');
    listEl.innerHTML = '<div class="text-muted">Loading&hellip;</div>';

    // A device is any HMAC client that can read/write under <app>:
    // exact `<app>` ACL, wildcard `*`, or a deeper `<app>/...` sub-prefix.
    // Device IDs are client IDs — by convention also the path segment
    // used in <app>/<device>/<key> overrides, which is what the
    // effective-value table below joins against.
    const { ok, data } = await fetchAPI(`/catalog/${encodeURIComponent(app)}/devices`);
    if (!ok) {
        listEl.innerHTML = '<div class="text-muted">Failed to list devices.</div>';
        countEl.innerText = '--';
        return;
    }

    const devices = Array.isArray(data.devices) ? data.devices : [];
    countEl.innerText = devices.length;
    _catalogTabsLoaded.devices = true;

    if (devices.length === 0) {
        listEl.innerHTML = `<div class="text-muted">No HMAC clients have access to <code>${escapeHtml(app)}</code>. Issue a client whose ACL grants <code>${escapeHtml(app)}</code> (or a wildcard) to populate this list.</div>`;
        return;
    }

    listEl.innerHTML = devices.map(dev => `
        <div class="catalog-app-row" data-action="openDeviceDetail" data-dev="${escapeHtml(dev)}">
            <span class="catalog-app-name">${escapeHtml(dev)}</span>
            <span class="catalog-app-chevron">&rsaquo;</span>
        </div>
    `).join('');
}

let _currentDeviceDetailId = null;

function closeDeviceDetail() {
    document.getElementById('catalogDevicesListPane').classList.remove('hidden');
    document.getElementById('catalogDeviceDetailPane').classList.add('hidden');
    _currentDeviceDetailId = null;
}

// Resolve an effective value for one var at one device, mirroring the
// lookup chain used on-device: device override → app-scope → catalog default.
async function openDeviceDetail(deviceId) {
    if (!_currentCatalog) return;
    const app = _currentCatalog.app;
    _currentDeviceDetailId = deviceId;
    document.getElementById('catalogDevicesListPane').classList.add('hidden');
    document.getElementById('catalogDeviceDetailPane').classList.remove('hidden');
    document.getElementById('catalogDeviceDetailTitle').innerHTML =
        `<code>${escapeHtml(app)}/${escapeHtml(deviceId)}</code>`;

    const body = document.getElementById('catalogDeviceEffectiveBody');
    body.innerHTML = `<tr><td colspan="6" class="text-muted">Resolving&hellip;</td></tr>`;

    const entries = Object.entries(_currentCatalog.vars);
    // Fan out reads — bounded by the catalog size (usually < 50 vars).
    const results = await Promise.all(entries.map(async ([name, v]) => {
        v = v || {};
        const scope = Array.isArray(v.scope) ? v.scope : [];
        const [devRes, appRes] = await Promise.all([
            scope.includes('device') ? _fetchScopeValue(app, name, deviceId) : Promise.resolve({ state: 'na', value: null }),
            scope.includes('app') ? _fetchScopeValue(app, name, null) : Promise.resolve({ state: 'na', value: null }),
        ]);
        return { name, v, devRes, appRes };
    }));

    body.innerHTML = results.map(({ name, v, devRes, appRes }) => {
        const catDefault = _formatDefault(v);
        const devCell = _effectiveCell(devRes);
        const appCell = _effectiveCell(appRes);

        // Resolve effective + source.
        let effHtml, sourceHtml;
        if (devRes.state === 'set') {
            effHtml = _formatValueCell(devRes.value, devRes.encrypted);
            sourceHtml = `<span class="badge source-device">device</span>`;
        } else if (appRes.state === 'set') {
            effHtml = _formatValueCell(appRes.value, appRes.encrypted);
            sourceHtml = `<span class="badge source-app">app</span>`;
        } else if (v.default !== undefined && v.default !== null && !v.default_per_device) {
            // Catalog defaults can't be encrypted — they live in the YAML
            // declaration, never go through the sidecar — so render plain.
            effHtml = `<code>${escapeHtml(JSON.stringify(v.default))}</code>`;
            sourceHtml = `<span class="badge source-default">default</span>`;
        } else {
            effHtml = `<span class="text-muted">(unset)</span>`;
            sourceHtml = `<span class="badge source-unset">&mdash;</span>`;
        }

        return `
            <tr class="catalog-var-row" data-action="openKeyEditorForDevice" data-var-name="${escapeHtml(name)}" data-device-id="${escapeHtml(deviceId)}">
                <td><code>${escapeHtml(name)}</code></td>
                <td>${devCell}</td>
                <td>${appCell}</td>
                <td>${catDefault}</td>
                <td>${effHtml}</td>
                <td>${sourceHtml}</td>
            </tr>
        `;
    }).join('') || `<tr><td colspan="6" class="text-muted">Catalog has no variables.</td></tr>`;
}

function _effectiveCell(res) {
    if (res.state === 'na') return `<span class="text-muted">&mdash;</span>`;
    if (res.state === 'unset') return `<span class="text-muted">(unset)</span>`;
    if (res.state === 'error') return `<span class="text-muted">(err)</span>`;
    return _formatValueCell(res.value, res.encrypted);
}

// Open the M3b key editor from a device-effective row, locked to that
// device. The app-scope pane is suppressed in this entry path because the
// operator came in via a specific device — exposing app-wide writes here
// is a foot-gun.
function openKeyEditorForDevice(keyName, deviceId) {
    openKeyEditor(keyName, { lockedDevice: deviceId });
}

async function fetchCatalogRaw() {
    if (!_currentCatalog) return;
    const app = _currentCatalog.app;
    const body = document.getElementById('catalogRawBody');
    const countEl = document.getElementById('catalogRawCount');
    document.getElementById('catalogRawAppLabel').innerText = `${app}/`;
    body.innerHTML = `<tr><td colspan="4" class="text-muted">Loading&hellip;</td></tr>`;

    const scan = await _scanAppKeys(app);
    if (!scan.ok) {
        body.innerHTML = `<tr><td colspan="4" class="text-muted">Failed to scan app keys.</td></tr>`;
        countEl.innerText = '--';
        return;
    }

    _catalogTabsLoaded.raw = true;
    countEl.innerText = scan.items.length;

    if (scan.items.length === 0) {
        body.innerHTML = `<tr><td colspan="4" class="text-muted">No KV keys written under <code>${escapeHtml(app)}/</code> yet.</td></tr>`;
        return;
    }

    const catalogVars = new Set(Object.keys(_currentCatalog.vars || {}));

    body.innerHTML = scan.items.map(it => {
        const keyName = it.key;
        const dev = _parseDeviceFromKey(app, keyName);
        // Leaf name is the final path segment, which is the catalog var name.
        const leaf = keyName.slice(keyName.lastIndexOf('/') + 1);
        const scopeLabel = dev
            ? `<span class="badge source-device">device: ${escapeHtml(dev)}</span>`
            : `<span class="badge source-app">app</span>`;
        const tracked = catalogVars.has(leaf);
        const catalogLabel = tracked
            ? `<span class="badge source-default">in catalog</span>`
            : `<span class="badge source-unset" title="This key is not declared in the published catalog. Device firmware can still read it, but edits bypass the catalog contract.">off-catalog</span>`;
        return `
            <tr>
                <td><code>${escapeHtml(keyName)}</code></td>
                <td>${scopeLabel}</td>
                <td>${it.bytes} B</td>
                <td>${catalogLabel}</td>
            </tr>
        `;
    }).join('');
}


// --- Key editor modal (M3b) ----------------------------------------------
//
// Opens scoped to one (app, key) pair. Reads current values at app and
// device scope (no writes). Save/Unset are the only paths that mutate —
// explicit user action each time. See invariant 1 in catalog_spec.md:
// never materialize placeholder empty-string writes.

let _editorContext = null;  // { app, keyName, var }

function _kvPath(app, keyName, device) {
    // App-scope writes land under `<app>/public/<key>` per the
    // public/ namespace convention from docs/fr_application_view.md.
    // Per-device writes unchanged. Migration dependency: the
    // operator-side data move (`kv:<app>/<key>` → `kv:<app>/public/<key>`)
    // must complete before this code deploys, or app-scope reads/writes
    // will land at the new path while old data sits at the old.
    return device ? `${app}/${device}/${keyName}` : `${app}/public/${keyName}`;
}

// The admin POST /kv handler json.loads() the value string and falls back
// to raw string on parse error. Encode per type so round-trips match the
// CLI's msgpack shape (ints → msgpack int, strings → msgpack str, etc.).
function _encodeForAdmin(varDesc, raw) {
    switch (varDesc.type) {
        case 'int':
        case 'float':
        case 'bool':
            return String(raw);               // JSON-parseable: "60", "true"
        case 'enum':
        case 'string':
        default:
            return String(raw);               // raw string; admin falls through
    }
}

// Client-side validation mirroring catalog.py coerce_value. Returns
// {ok: true, value} or {ok: false, msg}.
function _validateInput(varDesc, rawStr) {
    const s = String(rawStr);
    if (s === '') return { ok: false, msg: 'value required (use Unset to clear)' };
    const t = varDesc.type;
    if (t === 'int') {
        if (!/^-?\d+$/.test(s)) return { ok: false, msg: `${t} expected integer, got ${JSON.stringify(s)}` };
        const n = parseInt(s, 10);
        if (Array.isArray(varDesc.range) && varDesc.range.length === 2) {
            const [lo, hi] = varDesc.range;
            if (n < lo || n > hi) return { ok: false, msg: `value ${n} outside recommended range [${lo}, ${hi}]` };
        }
        return { ok: true, value: n };
    }
    if (t === 'float') {
        if (!/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(s)) return { ok: false, msg: `float expected, got ${JSON.stringify(s)}` };
        const n = parseFloat(s);
        if (Array.isArray(varDesc.range) && varDesc.range.length === 2) {
            const [lo, hi] = varDesc.range;
            if (n < lo || n > hi) return { ok: false, msg: `value ${n} outside recommended range [${lo}, ${hi}]` };
        }
        return { ok: true, value: n };
    }
    if (t === 'bool') {
        const lc = s.toLowerCase();
        if (['true', '1', 'yes', 'y', 'on'].includes(lc)) return { ok: true, value: true };
        if (['false', '0', 'no', 'n', 'off'].includes(lc)) return { ok: true, value: false };
        return { ok: false, msg: `bool expected (true/false), got ${JSON.stringify(s)}` };
    }
    if (t === 'enum') {
        const vals = Array.isArray(varDesc.values) ? varDesc.values : [];
        if (!vals.includes(s)) return { ok: false, msg: `enum value must be one of: ${vals.join(', ')}` };
        return { ok: true, value: s };
    }
    // string
    return { ok: true, value: s };
}

function _editControlHtml(scope, varDesc) {
    const id = `editInput_${scope}`;
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
    // String — use textarea so long values (brightness_schedule's multi-segment
    // schedules, wifi_password) aren't truncated by single-line input width.
    // rows=2 keeps the modal compact; the textarea grows naturally as the
    // operator types or pastes more. Reveal button gets surfaced by
    // `_populateScopeInput` if the fetched value is encrypted; the
    // Encrypted checkbox is pre-filled the same way and POSTed via
    // `saveScope` so the operator can flip the flag without leaving the UI.
    // String-only on purpose: the server's read path only knows how to
    // decrypt str/bin payloads, so allowing the checkbox on int/bool/enum
    // would be a write-time footgun (the device read would 500 later).
    return `<textarea id="${id}" rows="2" placeholder="new value"></textarea>` +
           `<button class="btn-sm hidden mt-xs" type="button" id="reveal_${scope}" ` +
           `data-action="toggleReveal" data-target="${id}">Reveal</button>` +
           `<label class="encrypted-toggle-label">` +
           `<input type="checkbox" id="encrypted_${scope}" class="encrypted-toggle-checkbox">` +
           `Encrypted &mdash; device GETs return ciphertext (msgpack ext 0x21)` +
           `</label>`;
}

// Format a resolved value as a <code> cell for any read-only display
// (current-value line in the editor, per-scope and effective columns in
// the device-effectives table). When `encrypted` is true, masks by default
// with a Reveal button — the same pattern any operator-facing value
// surface needs so a wifi_password doesn't flash up unprompted while
// they're clicking around.
//
// Pre-#1c, this took an `inClickableRow` flag to splice
// `event.stopPropagation();` into the button's inline onclick when the
// surrounding row had its own click handler. Under #1c's delegated
// dispatch, that's automatic: the body-level click delegate uses
// `event.target.closest('[data-action]')`, which returns the BUTTON
// (not the row) when the click target is the button. The row's
// data-action only fires when the click hits the row outside any
// descendant `[data-action]` element. Parameter dropped.
function _formatValueCell(value, encrypted) {
    const json = JSON.stringify(value);
    if (encrypted) {
        const dots = '•'.repeat(Math.min(json.length, 12));
        return `<code class="reveal-target" data-real="${escapeHtml(json)}">${dots}</code>` +
               ` <button class="btn-sm" type="button" data-action="toggleRevealReadonly">Reveal</button>`;
    }
    return `<code>${escapeHtml(json)}</code>`;
}

function _renderCurrent(state, value, encrypted) {
    if (state === 'unset') return `<span class="text-muted">(unset)</span>`;
    if (state === 'error') return `<span class="text-muted">(error: ${escapeHtml(String(value))})</span>`;
    return _formatValueCell(value, encrypted);
}

async function _fetchScopeValue(app, keyName, device) {
    const path = _kvPath(app, keyName, device);
    const { ok, data } = await fetchAPI(`/peek/kv/${path}`);
    if (!ok) return { state: 'error', value: `HTTP ${data && data.detail ? data.detail : 'err'}` };
    if (data.status === 'empty' || data.message === null || data.message === '') {
        return { state: 'unset', value: null };
    }
    return { state: 'set', value: data.message, encrypted: !!data.encrypted };
}

// Stuff the resolved scope value back into the scope's edit input so an
// operator tweaking — e.g. bumping `heartbeep` from 300 to 600, or
// appending a new segment to `brightness_schedule` — doesn't have to
// re-type from scratch or paste from a separate `stra2us get`.
// Coerces structured values (rare, e.g. nested arrays) to JSON so the
// wire form is what's shown. When `encrypted` is true, applies the
// visual mask + reveals the per-scope Reveal button (rendered hidden
// by `_editControlHtml` for string-typed vars).
function _populateScopeInput(scope, value, encrypted) {
    const el = document.getElementById(`editInput_${scope}`);
    if (!el) return;
    let s;
    if (value === null || value === undefined) s = '';
    else if (typeof value === 'string') s = value;
    else if (typeof value === 'number' || typeof value === 'boolean') s = String(value);
    else s = JSON.stringify(value);
    el.value = s;

    const revealBtn = document.getElementById(`reveal_${scope}`);
    if (revealBtn) {
        if (encrypted) {
            el.classList.add('value-masked');
            revealBtn.style.display = '';
            revealBtn.innerText = 'Reveal';
        } else {
            el.classList.remove('value-masked');
            revealBtn.style.display = 'none';
        }
    }
    // Pre-fill the Encrypted checkbox from current sidecar state. Without
    // this, the FR's "demote to plaintext on bare set" semantic would
    // silently downgrade an encrypted record any time someone opened the
    // catalog editor and clicked Save without re-checking the box —
    // exactly the trap the dashboard editor's prefill already guards
    // against.
    const encryptedCheckbox = document.getElementById(`encrypted_${scope}`);
    if (encryptedCheckbox) encryptedCheckbox.checked = !!encrypted;
}

// Toggle the visual mask on an editable input (used by Reveal buttons next
// to encrypted-record textareas). The text content is unchanged — only the
// CSS-driven mask flips.
function _toggleReveal(inputId, btn) {
    const el = document.getElementById(inputId);
    if (!el) return;
    const masked = el.classList.toggle('value-masked');
    btn.innerText = masked ? 'Reveal' : 'Hide';
}

// Toggle the read-only resolution display (the "Current: ..." line) between
// the dot-mask and the actual value. Read-only views can't use CSS text-
// security, so we swap textContent in/out instead. The Reveal button must
// be the next sibling of the <code class="reveal-target" data-real="...">.
function _toggleRevealReadonly(btn) {
    const target = btn.previousElementSibling;
    if (!target) return;
    const realVal = target.getAttribute('data-real') || '';
    if (target.dataset.shown === '1') {
        const dots = '•'.repeat(Math.min(realVal.length, 12));
        target.textContent = dots;
        target.dataset.shown = '0';
        btn.textContent = 'Reveal';
    } else {
        target.textContent = realVal;
        target.dataset.shown = '1';
        btn.textContent = 'Hide';
    }
}

function openKeyEditor(keyName, opts = {}) {
    if (!_currentCatalog) return;
    const v = _currentCatalog.vars[keyName];
    if (!v) return;
    const lockedDevice = opts.lockedDevice || null;
    _editorContext = { app: _currentCatalog.app, keyName, var: v, lockedDevice };

    const hasApp = Array.isArray(v.scope) && v.scope.includes('app');
    const hasDevice = Array.isArray(v.scope) && v.scope.includes('device');
    const showAppPane = hasApp && !lockedDevice;

    document.getElementById('keyEditorTitle').innerHTML =
        `<code>${escapeHtml(keyName)}</code> <span class="badge badge-type">${escapeHtml(v.type)}</span>`;
    document.getElementById('keyEditorMeta').innerHTML = `
        <div><strong>App:</strong> <code>${escapeHtml(_currentCatalog.app)}</code></div>
        <div><strong>Scope:</strong> ${_formatScope(v.scope)}</div>
        <div><strong>Catalog default:</strong> ${_formatDefault(v)}</div>
        <div><strong>Range / values:</strong> ${_formatRange(v)}</div>
        ${v.help ? `<div class="key-editor-help">${escapeHtml(v.help.trim())}</div>` : ''}
        <div class="form-hint-sm">Ranges are <em>recommended</em>, not enforced on-device — the firmware is the arbiter.</div>
    `;

    // App pane
    const appPane = document.getElementById('keyEditorAppPane');
    if (showAppPane) {
        appPane.innerHTML = `
            <h4>App scope <span class="form-hint-sm">&mdash; <code>${escapeHtml(_currentCatalog.app)}/${escapeHtml(keyName)}</code></span></h4>
            <div class="key-editor-row">
                <div class="key-editor-current">Current: <span id="currentApp">&hellip;</span></div>
                <div class="key-editor-edit">
                    ${_editControlHtml('app', v)}
                    <button class="primary-btn btn-sm" data-action="saveScope" data-scope="app">Save</button>
                    <button class="btn-sm btn-danger" data-action="unsetScope" data-scope="app">Unset</button>
                </div>
                <div class="key-editor-error hidden" id="errorApp"></div>
            </div>
        `;
        appPane.classList.remove('hidden');
        _fetchScopeValue(_editorContext.app, keyName, null).then(res => {
            document.getElementById('currentApp').innerHTML = _renderCurrent(res.state, res.value, res.encrypted);
            if (res.state === 'set') _populateScopeInput('app', res.value, res.encrypted);
        });
    } else if (lockedDevice && hasApp) {
        appPane.innerHTML = `
            <div class="form-hint-sm key-editor-scope-note">
                Editing the device-level override for <code>${escapeHtml(lockedDevice)}</code>.
                To make an app-wide change, close this and edit from the catalog's
                <strong>Variables</strong> tab.
            </div>
        `;
        appPane.classList.remove('hidden');
    } else {
        appPane.innerHTML = '';
        appPane.classList.add('hidden');
    }

    // Device pane
    const devPane = document.getElementById('keyEditorDevicePane');
    if (hasDevice) {
        const pickerHtml = lockedDevice
            ? `<div class="key-editor-device-picker">
                   <strong>Device:</strong> <code>${escapeHtml(lockedDevice)}</code>
                   <input type="hidden" id="deviceIdInput" value="${escapeHtml(lockedDevice)}">
               </div>`
            : `<div class="key-editor-device-picker">
                   <input type="text" id="deviceIdInput" placeholder="device id (e.g. ricky)">
                   <button class="btn-sm" data-action="loadDeviceScope">Load</button>
               </div>`;
        const initialCurrent = lockedDevice
            ? '&hellip;'
            : '(enter device id and press Load)';
        devPane.innerHTML = `
            <h4>Device scope</h4>
            <div class="key-editor-row">
                ${pickerHtml}
                <div class="key-editor-current">Current: <span id="currentDevice" class="text-muted">${initialCurrent}</span></div>
                <div class="key-editor-edit ${lockedDevice ? '' : 'hidden'}" id="deviceEditRow">
                    ${_editControlHtml('device', v)}
                    <button class="primary-btn btn-sm" data-action="saveScope" data-scope="device">Save</button>
                    <button class="btn-sm btn-danger" data-action="unsetScope" data-scope="device">Unset</button>
                </div>
                <div class="key-editor-error hidden" id="errorDevice"></div>
            </div>
        `;
        devPane.classList.remove('hidden');
        if (lockedDevice) {
            _fetchScopeValue(_editorContext.app, keyName, lockedDevice).then(res => {
                document.getElementById('currentDevice').innerHTML = _renderCurrent(res.state, res.value, res.encrypted);
                if (res.state === 'set') _populateScopeInput('device', res.value, res.encrypted);
            });
        }
    } else {
        devPane.innerHTML = '';
        devPane.classList.add('hidden');
    }

    document.getElementById('keyEditorModal').style.display = 'block';
}

function closeKeyEditor() {
    const ctx = _editorContext;
    _editorContext = null;
    document.getElementById('keyEditorModal').style.display = 'none';
    // If the editor was locked to a device and that device's detail pane
    // is open behind the modal, re-resolve effective values so any edits
    // are reflected without making the user leave and re-enter the tab.
    if (ctx && ctx.lockedDevice && _currentDeviceDetailId === ctx.lockedDevice) {
        openDeviceDetail(ctx.lockedDevice);
    }
}

async function loadDeviceScope() {
    if (!_editorContext) return;
    const devId = document.getElementById('deviceIdInput').value.trim();
    if (!devId) return;
    document.getElementById('currentDevice').innerHTML = '&hellip;';
    const res = await _fetchScopeValue(_editorContext.app, _editorContext.keyName, devId);
    document.getElementById('currentDevice').innerHTML = _renderCurrent(res.state, res.value, res.encrypted);
    document.getElementById('deviceEditRow').classList.remove('hidden');
    if (res.state === 'set') _populateScopeInput('device', res.value, res.encrypted);
}

function _scopeDevice(scope) {
    if (scope === 'app') return null;
    const devId = document.getElementById('deviceIdInput').value.trim();
    if (!devId) throw new Error('enter a device id before saving at device scope');
    return devId;
}

async function saveScope(scope) {
    if (!_editorContext) return;
    const errEl = document.getElementById(scope === 'app' ? 'errorApp' : 'errorDevice');
    errEl.classList.add('hidden');
    try {
        const device = _scopeDevice(scope);
        const input = document.getElementById(`editInput_${scope}`);
        const validation = _validateInput(_editorContext.var, input.value);
        if (!validation.ok) {
            errEl.innerText = validation.msg;
            errEl.classList.remove('hidden');
            return;
        }
        const path = _kvPath(_editorContext.app, _editorContext.keyName, device);
        const body = { value: _encodeForAdmin(_editorContext.var, input.value) };
        // Forward the Encrypted flag if the editor surfaced the checkbox
        // (string-only). Sending it explicitly — true or false — ensures
        // the server's `KVPayload.encrypted` Pydantic default doesn't
        // silently demote an encrypted record on a bare re-save.
        const encryptedCheckbox = document.getElementById(`encrypted_${scope}`);
        if (encryptedCheckbox) body.encrypted = encryptedCheckbox.checked;
        const res = await fetchAPI(`/kv/${path}`, 'POST', body);
        if (!res.ok) {
            errEl.innerText = `write failed: HTTP ${res.status}`;
            errEl.classList.remove('hidden');
            return;
        }
        // Re-fetch to show the stored (post-msgpack-round-trip) value.
        const fresh = await _fetchScopeValue(_editorContext.app, _editorContext.keyName, device);
        document.getElementById(scope === 'app' ? 'currentApp' : 'currentDevice').innerHTML =
            _renderCurrent(fresh.state, fresh.value, fresh.encrypted);
        input.value = '';
    } catch (e) {
        errEl.innerText = e.message;
        errEl.classList.remove('hidden');
    }
}

async function unsetScope(scope) {
    if (!_editorContext) return;
    const errEl = document.getElementById(scope === 'app' ? 'errorApp' : 'errorDevice');
    errEl.classList.add('hidden');
    try {
        const device = _scopeDevice(scope);
        const path = _kvPath(_editorContext.app, _editorContext.keyName, device);
        const human = device ? `${_editorContext.app}/${device}/${_editorContext.keyName}` : `${_editorContext.app}/${_editorContext.keyName}`;
        if (!confirm(`Delete ${human}? The scope will fall back to the next layer (device → app → compiled-in default).`)) return;
        const res = await fetchAPI(`/kv/${path}`, 'DELETE');
        if (!res.ok) {
            errEl.innerText = `delete failed: HTTP ${res.status}`;
            errEl.classList.remove('hidden');
            return;
        }
        document.getElementById(scope === 'app' ? 'currentApp' : 'currentDevice').innerHTML =
            _renderCurrent('unset', null);
        // Reset the input + mask + Encrypted checkbox to "fresh / empty"
        // state. Without this the input still holds the just-deleted
        // value, and a subsequent Save silently re-creates the record
        // (the operator's Unset clicked-and-confirmed appears to "undo
        // itself" on Save) — confusing enough to merit being explicit.
        _populateScopeInput(scope, '', false);
    } catch (e) {
        errEl.innerText = e.message;
        errEl.classList.remove('hidden');
    }
}


// 3. Activity Logs
let logFilterClients = new Set();
let logKnownClients = [];
let logClientsLoaded = false;

async function loadLogClients() {
    if (logClientsLoaded) return;
    const { data: clients } = await fetchAPI('/keys');
    logKnownClients = clients.map(c => c.client_id).sort();
    logClientsLoaded = true;
    renderLogChips();
}

function renderLogChips() {
    const container = document.getElementById('logFilterChips');
    container.innerHTML = logKnownClients.map(id => {
        const eid = escapeHtml(id);
        const active = logFilterClients.has(id) ? ' active' : '';
        return `<button class="filter-chip${active}" data-client="${eid}">${eid}</button>`;
    }).join('');

    container.querySelectorAll('.filter-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const clientId = chip.dataset.client;
            if (logFilterClients.has(clientId)) {
                logFilterClients.delete(clientId);
            } else {
                logFilterClients.add(clientId);
            }
            renderLogChips();
            fetchLogs();
        });
    });
}

async function fetchLogs() {
    await loadLogClients();
    // Limit picked to surface ~24h of typical critterchron-fleet traffic
    // in one view (~13 devices × heartbeep every 30s ≈ 1500/hr from
    // devices alone, plus admin/UI calls). Stream itself is bounded by
    // `MAXLEN ~ 150000` in main.py — caller can request more by hand.
    // If we ever want a UI control or pagination, see the discussion in
    // admin_ui_todo.md.
    let endpoint = '/logs?limit=2000';
    for (const id of logFilterClients) {
        endpoint += `&client_id=${encodeURIComponent(id)}`;
    }
    const { data: logs } = await fetchAPI(endpoint);
    const tbody = document.getElementById('logsTableBody');
    tbody.innerHTML = logs.map(l => `
        <tr>
            <td class="col-log-timestamp">${formatTime(l.timestamp)}</td>
            <td class="col-log-client-id">${escapeHtml(l.client_id)}</td>
            <td>${escapeHtml(l.action)}</td>
            <td class="${_logStatusClass(l.status)}">${escapeHtml(l.status)}</td>
        </tr>
    `).join('');
}

// Color-class logic for activity log status text. Was a
// `startsWith('Success')` check; broke when the middleware grew
// Hit/Miss/Not Modified entries for KV + firmware reads — those
// start with neither "Success" nor "Error" but are still 200/304s.
// Now driven by the embedded HTTP status code so any new prefix
// the middleware grows (e.g. "Cached" for some future caching
// layer) gets coloured correctly without touching this list.
//
// Returns a CSS class name (`.log-status-ok` / `.log-status-err`
// / `.text-muted`) rather than a `color:` value so the caller can
// drop it into the `class="..."` attribute. Pre-#1c this returned
// a CSS color and got spliced into an inline `style=` attr —
// blocked by `style-src 'self'` once admin flips to enforcing.
function _logStatusClass(s) {
    const m = (s || '').match(/\((\d{3})\)/);
    if (!m) return 'text-muted';                  // shape we don't recognize
    const code = parseInt(m[1], 10);
    if (code >= 200 && code < 400) return 'log-status-ok';
    return 'log-status-err';
}

// Polling for real-time updates (every 5 seconds)
setInterval(() => {
    const activeViewId = document.querySelector('.active-view').id;
    if (activeViewId === 'dashboard') fetchStats();
    if (activeViewId === 'logs') fetchLogs();
}, 5000);

// 4. Backup / Restore
async function downloadBackup() {
    const res = await fetch(`${API_BASE}/keys/backup`);
    if (!res.ok) {
        alert('Backup failed. Check the server logs.');
        return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'stra2us_backup.json';
    a.click();
    URL.revokeObjectURL(url);
}

async function uploadRestore() {
    const fileInput = document.getElementById('restoreFile');
    const force = document.getElementById('forceRestore').checked;
    const resultDiv = document.getElementById('restoreResult');

    if (!fileInput.files.length) {
        alert('Please select a backup file first.');
        return;
    }

    const text = await fileInput.files[0].text();
    let payload;
    try {
        payload = JSON.parse(text);
    } catch (e) {
        alert('Invalid JSON file. Please select a valid backup.');
        return;
    }

    const res = await fetch(`${API_BASE}/keys/restore?force=${force}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });

    const data = await res.json();
    resultDiv.classList.remove('hidden');
    resultDiv.innerHTML = `
        <strong>Restore complete!</strong><br><br>
        ✅ Restored: <strong>${data.restored.length}</strong> clients<br>
        ⏭ Skipped (already exist): <strong>${data.skipped.length}</strong> clients<br>
        ♻️ Overwritten: <strong>${data.overwritten.length}</strong> clients
        ${data.restored.length ? `<br><br><small>New: ${data.restored.join(', ')}</small>` : ''}
        ${data.overwritten.length ? `<br><small>Overwritten: ${data.overwritten.join(', ')}</small>` : ''}
    `;
}

// 5. Topic Monitor
const MONITOR_COLORS = [
    '#6c63ff','#00c2ff','#f5a623','#43e97b','#ff6b6b',
    '#a18cd1','#fda085','#84fab0','#f093fb','#4facfe',
];
const monitorClientColors = {};
let monitorInterval = null;
let monitorSeenIds = new Set();
let monitorActive = false;
// Epoch (seconds; matches the server's `received_at` shape) of
// the most recent `monitorClear()`. Used by `monitorPoll`'s
// render loop to skip stream messages older than this — without
// it, Clear briefly empties the feed and the next poll re-adds
// every visible message. Initialized to 0 so a never-cleared
// session shows the full stream tail. Updated by `monitorClear`.
let monitorClearedAfter = 0;
let monitorFilterClients = new Set();
let monitorKnownClients = [];
let monitorClientsLoaded = false;

async function loadMonitorClients() {
    if (monitorClientsLoaded) return;
    const { data: clients } = await fetchAPI('/keys');
    monitorKnownClients = clients.map(c => c.client_id).sort();
    monitorClientsLoaded = true;
    renderMonitorChips();
}

function renderMonitorChips() {
    const container = document.getElementById('monitorFilterChips');
    if (!container) return;
    container.innerHTML = monitorKnownClients.map(id => {
        const eid = escapeHtml(id);
        const active = monitorFilterClients.has(id) ? ' active' : '';
        return `<button class="filter-chip${active}" data-client="${eid}">${eid}</button>`;
    }).join('');

    container.querySelectorAll('.filter-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const clientId = chip.dataset.client;
            if (monitorFilterClients.has(clientId)) {
                monitorFilterClients.delete(clientId);
            } else {
                monitorFilterClients.add(clientId);
            }
            renderMonitorChips();
            // Filter is server-side; reset feed so newly-included
            // history backfills. Use the soft variant —
            // `monitorClear` (the one wired to the Clear button)
            // stamps a cutoff that would suppress the very
            // backfill we want here.
            _monitorResetFeed();
            if (monitorActive) monitorPoll();
        });
    });
}

function monitorClientColorIndex(clientId) {
    // Returns the [0, 9] palette index this client is bound to.
    // Allocated on first sight (round-robin via dict size) and
    // cached so re-renders are stable. CSS class
    // `.monitor-color-${idx}` carries the actual color triplet —
    // see `styles.css` (P5 #1c lifted these out of inline
    // `style="..."` attributes).
    if (monitorClientColors[clientId] === undefined) {
        monitorClientColors[clientId] =
            Object.keys(monitorClientColors).length % MONITOR_COLORS.length;
    }
    return monitorClientColors[clientId];
}

function monitorFormatData(data) {
    if (data === null || data === undefined) return '<em>null</em>';
    if (typeof data === 'object') return escapeHtml(JSON.stringify(data));
    return escapeHtml(String(data));
}

async function monitorPoll() {
    const topic = document.getElementById('monitorTopic').value.trim();
    if (!topic) return;

    let url = `${API_BASE}/stream/q/${topic}`;
    if (monitorFilterClients.size > 0) {
        const params = new URLSearchParams();
        for (const id of monitorFilterClients) params.append('client_id', id);
        url += `?${params.toString()}`;
    }
    const res = await fetch(url);
    if (!res.ok) return;
    const messages = await res.json();

    const feed = document.getElementById('monitorFeed');
    let addedAny = false;

    // messages arrive newest-first from XREVRANGE; reverse so we prepend
    // oldest-first, ending with the newest entry at the top of the feed.
    for (const msg of [...messages].reverse()) {
        if (monitorSeenIds.has(msg.id)) continue;
        // Skip messages older than the most recent Clear. Without this,
        // hitting Clear mid-watch repopulated the feed on the next
        // poll: clearing `monitorSeenIds` in `monitorClear()` makes
        // every stream message look new again, and the polling fetch
        // reads from the start of the stream (no cursor). The
        // `monitorClearedAfter` epoch lets us "skip the past" without
        // touching the polling protocol. See TODO.md ("Monitor tab
        // 'Clear' button repopulates seconds later").
        if (msg.received_at <= monitorClearedAfter) continue;
        monitorSeenIds.add(msg.id);
        addedAny = true;

        const colorIdx = monitorClientColorIndex(msg.client_id);
        const d = new Date(msg.received_at * 1000);
        const ts = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const dataStr = monitorFormatData(msg.data);

        const row = document.createElement('div');
        row.className = 'monitor-row monitor-row-new';
        row.innerHTML = `
            <span class="monitor-ts">${ts}</span>
            <span class="monitor-badge monitor-color-${colorIdx}">${escapeHtml(msg.client_id)}</span>
            <span class="monitor-data">${dataStr}</span>
        `;
        // Prepend so newest is at top
        feed.insertBefore(row, feed.firstChild);

        // Remove animation class after it plays
        setTimeout(() => row.classList.remove('monitor-row-new'), 600);
    }

    // Cap feed at 200 rows
    while (feed.children.length > 200) {
        feed.removeChild(feed.lastChild);
    }
}

function openMonitor(topic) {
    // Navigate to the monitor view
    document.querySelectorAll('.nav-links a').forEach(a => a.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active-view'));
    const monitorLink = document.querySelector('.nav-links a[data-target="monitor"]');
    if (monitorLink) monitorLink.classList.add('active');
    document.getElementById('monitor').classList.add('active-view');

    // Stop any existing session, pre-fill topic, start fresh.
    // Soft reset — the operator's intent here is "show me this
    // topic's stream tail now"; using the cutoff-stamping
    // `monitorClear` would suppress the recent history.
    if (monitorActive) monitorStop();
    _monitorResetFeed();
    document.getElementById('monitorTopic').value = topic;
    monitorStart();
}

function monitorStart() {
    const topic = document.getElementById('monitorTopic').value.trim();
    if (!topic) { document.getElementById('monitorTopic').focus(); return; }

    monitorActive = true;
    document.getElementById('monitorFeedTitle').textContent = `q/${topic}`;
    document.getElementById('monitorStartBtn').style.display = 'none';
    document.getElementById('monitorStopBtn').style.display = '';
    const status = document.getElementById('monitorStatus');
    status.textContent = 'Live';
    status.className = 'monitor-status-live';

    loadMonitorClients();
    monitorPoll();
    monitorInterval = setInterval(monitorPoll, 2000);
}

function monitorStop() {
    clearInterval(monitorInterval);
    monitorInterval = null;
    monitorActive = false;
    document.getElementById('monitorStartBtn').style.display = '';
    document.getElementById('monitorStopBtn').style.display = 'none';
    const status = document.getElementById('monitorStatus');
    status.textContent = 'Stopped';
    status.className = 'monitor-status-off';
}

// Soft reset — empties the DOM + seenIds without stamping the
// "ignore older messages" cutoff. Used by the internal
// transitions where the operator's intent is "show me the
// stream tail again, just refreshed":
//   - `openMonitor(topic)` when clicking Monitor on a topic
//     from the dashboard or queue list
//   - the chip-toggle handler when adding/removing a client
//     filter (the filter changed; let the server's filtered
//     stream backfill into the feed)
// Both of those would have populated with recent history
// pre-v1.6.1; using the cutoff-stamping `monitorClear` here
// instead would silently swallow that history and leave the
// feed staring at "nothing" until brand-new messages land.
function _monitorResetFeed() {
    document.getElementById('monitorFeed').innerHTML = '';
    monitorSeenIds.clear();
}

// Explicit Clear — what the operator triggers from the Clear
// button. Stamps the cutoff so the next poll's render loop
// skips messages with `received_at <= monitorClearedAfter`,
// solving the "Clear, wait, watch it repopulate" bug from the
// pre-v1.6.1 version. Only the `data-action="monitorClear"`
// button hits this path; internal transitions use
// `_monitorResetFeed` above.
//
// `Math.floor(Date.now() / 1000)` matches the server's
// `received_at` shape (`unix_ms // 1000` — see
// routes_device.py's queue handler), so the comparison in
// monitorPoll is integer-equal.
function monitorClear() {
    monitorClearedAfter = Math.floor(Date.now() / 1000);
    _monitorResetFeed();
}

// Stop monitor when navigating away
document.querySelectorAll('.nav-links a').forEach(link => {
    link.addEventListener('click', () => {
        if (monitorActive && link.dataset.target !== 'monitor') {
            monitorStop();
        }
    });
});

async function applyWhoami() {
    // Hide nav entries the caller can't use. Backend still enforces —
    // this is UX, not a security boundary.
    //
    // Calls /me (the unified identity endpoint per fr_application_view.md
    // Phase 1) — supersedes the older /whoami. Returns a strict superset
    // (adds scope_kind/scope_app/scope_device); we only consume
    // is_superuser here, but the richer fields are available for any
    // future per-scope nav gating.
    const { ok, data } = await fetchAPI('/me');
    if (!ok || !data) return;
    if (!data.is_superuser) {
        document.querySelectorAll('.nav-superuser').forEach(el => {
            el.style.display = 'none';
        });
    }
}

// Security warnings — populates the #securityBanners div at the top
// of the page with anything /api/admin/security_warnings reports.
// Auth-gated endpoint so this only fires for logged-in admins; by
// design the banners stay visible across view switches (#securityBanners
// is outside .app-container).
async function fetchSecurityWarnings() {
    const { ok, data } = await fetchAPI('/security_warnings');
    if (!ok || !data || !Array.isArray(data.warnings)) return;
    const container = document.getElementById('securityBanners');
    if (!container) return;
    if (data.warnings.length === 0) {
        container.classList.add('hidden');
        container.innerHTML = '';
        return;
    }
    container.classList.remove('hidden');
    container.innerHTML = data.warnings.map(w => {
        const sev = (w.severity === 'error') ? 'severity-error' : 'severity-warning';
        const icon = (w.severity === 'error') ? '⛔' : '⚠';
        return `
            <div class="security-banner ${sev}" data-warning-id="${escapeHtml(w.id || '')}">
                <span class="security-banner-icon">${icon}</span>
                <div class="security-banner-body">
                    <div class="security-banner-message">${escapeHtml(w.message || '')}</div>
                    ${w.action ? `<code class="security-banner-action">${escapeHtml(w.action)}</code>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

// =====================================================================
// CLICK-DELEGATION FRAMEWORK (P5 #1c)
//
// Replaces the ~55 inline `onclick="…"` handlers that the admin UI
// inherited from a pre-CSP era. A single delegated click listener
// on `<body>` reads `data-action="<name>"` from the clicked element
// (or its nearest ancestor) and dispatches via the ACTIONS map
// below. Handlers receive `(el, event)` where `el` is the
// `[data-action]` element — usually a `<button>`, `<a>`, `<tr>`, or
// `<div>`. Handlers read whatever extra data they need from
// `el.dataset.<name>` (`data-*` attributes on the same element).
//
// Why NOT a single map per file or per module: this admin UI is a
// single page, all globals, all in one app.js. Keeping the
// dispatch table here keeps it grep-able — `git grep "data-action="`
// finds every callsite; `git grep "ACTIONS\['<name>'\]"` finds
// every handler.
//
// Row-vs-button: when both a row (e.g. `<tr data-action="openKeyEditor">`)
// and a button inside it (e.g. `<button data-action="toggleReveal">`)
// have data-action, clicking the button picks the button (closest()
// returns the nearest ancestor including target). The row's action
// only fires when the click hits the row outside any descendant
// `[data-action]`. No `event.stopPropagation()` needed.
//
// One change-event case (checkbox in the device picker) handled
// separately via `data-change-action` — click delegation alone
// would risk double-firing for keyboard-toggled checkboxes.
// =====================================================================

const ACTIONS = {
    // KV modal
    openKvModal: () => openKvModal(),
    closeKvModal: () => closeKvModal(),
    saveKv: () => saveKv(),
    toggleReveal: (el) => _toggleReveal(el.dataset.target, el),

    // ACL editor
    toggleNewAccess: () => toggleNewAccess(),
    aclAddRule: () => aclAddRule(),
    aclToggleAccess: (el) => aclToggleAccess(parseInt(el.dataset.index, 10)),
    aclRemoveRule: (el) => aclRemoveRule(parseInt(el.dataset.index, 10)),
    openAclModal: (el) => openAclModal(el.dataset.target),
    closeAclModal: () => closeAclModal(),
    saveAcl: () => saveAcl(),

    // Device picker (within ACL editor)
    openDevicePicker: () => openDevicePicker(),
    closeDevicePicker: () => closeDevicePicker(),
    confirmDevicePicker: () => confirmDevicePicker(),

    // Catalog views
    openCatalogDetail: (el) => openCatalogDetail(el.dataset.app),
    closeCatalogDetail: () => closeCatalogDetail(),
    switchCatalogTab: (el) => switchCatalogTab(el.dataset.tab),
    openDeviceDetail: (el) => openDeviceDetail(el.dataset.dev),
    closeDeviceDetail: () => closeDeviceDetail(),
    openKeyEditor: (el) => openKeyEditor(el.dataset.varName),
    openKeyEditorForDevice: (el) =>
        openKeyEditorForDevice(el.dataset.varName, el.dataset.deviceId),
    closeKeyEditor: () => closeKeyEditor(),
    saveScope: (el) => saveScope(el.dataset.scope),
    unsetScope: (el) => unsetScope(el.dataset.scope),
    loadDeviceScope: () => loadDeviceScope(),
    toggleRevealReadonly: (el) => _toggleRevealReadonly(el),

    // KV / Q operations from list rows
    peekData: (el) => peekData(el.dataset.kind, el.dataset.target),
    openMonitor: (el) => openMonitor(el.dataset.target),
    deleteData: (el) => deleteData(el.dataset.kind, el.dataset.target),
    editData: (el) => editData(el.dataset.kind, el.dataset.target),
    revokeClient: (el) => revokeClient(el.dataset.target),

    // Admin user management
    confirmDeleteAdminUser: (el) => confirmDeleteAdminUser(el.dataset.uname),
    openAdminAclModalFor: (el) => openAdminAclModalFor(el.dataset.uname),
    jumpToAdminUser: (el, ev) => {
        // The original inline-handler was
        // `event.preventDefault(); jumpToAdminUser('…')` because
        // the call site is an `<a href="#">`. The preventDefault
        // is essential — without it the URL hash changes.
        ev.preventDefault();
        jumpToAdminUser(el.dataset.uname);
    },

    // Live monitor
    monitorStart: () => monitorStart(),
    monitorStop: () => monitorStop(),
    monitorClear: () => monitorClear(),

    // Backup / restore
    downloadBackup: () => downloadBackup(),
    uploadRestore: () => uploadRestore(),
};

function _dispatchClick(event) {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;
    const handler = ACTIONS[action];
    if (typeof handler === 'function') {
        handler(el, event);
    }
    // No console-warn on missing action: catches typos but also
    // fires on every non-action click bubbling through unrelated
    // elements with `data-action` ancestors. Keep silent; bad
    // wiring shows as "button does nothing" — easy to notice.
}

// One change-event delegate, currently used only by the device
// picker checkbox. Separate from the click delegate because some
// browsers fire BOTH click + change when a checkbox is toggled
// via the keyboard, and an idempotent toggle handler would still
// double-fire. `data-change-action` keeps this surface tight.
function _dispatchChange(event) {
    const el = event.target.closest('[data-change-action]');
    if (!el) return;
    const action = el.dataset.changeAction;
    if (action === 'devicePickerToggle') {
        _devicePickerToggle(el);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.body.addEventListener('click', _dispatchClick);
    document.body.addEventListener('change', _dispatchChange);
});

// Init
applyWhoami();
fetchStats();
fetchSecurityWarnings();
