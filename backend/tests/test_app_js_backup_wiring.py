# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Structural tests for v1.8.1's admin-UI backup/restore wiring.

Sibling pattern to `test_app_js_p4_wiring.py` — the repo has no JS
test runtime, so we pin the wiring via substring/regex assertions
on `app.js` + `index.html`. The live-DOM verification path is the
staging walkthrough; these tests catch refactor regressions
(someone renames an action and forgets the markup, or vice versa).

Wired-up checks:
  * `index.html` has the new markup IDs we depend on
  * `app.js` defines the new handlers
  * The action dispatcher (ACTIONS table) routes the new
    `downloadAppBackup` data-action
  * The cache-bust on `app.js?v=...` was bumped (per the v1.6.9
    pre-commit hook policy)
  * Restore-file `change` listener is wired at boot
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_JS = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "app.js",
))
_INDEX_HTML = os.path.normpath(os.path.join(
    _HERE, "..", "src", "static", "index.html",
))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ----- markup: required IDs -----

def test_index_has_whole_instance_dump_controls():
    """Whole-instance download needs the include-logs checkbox + the
    button targeting `downloadBackup`."""
    html = _read(_INDEX_HTML)
    assert 'id="backupIncludeLogs"' in html
    assert 'data-action="downloadBackup"' in html


def test_index_has_per_app_list_container():
    """The per-app dump rows are injected by `loadPerAppBackupList`
    into this container; the loader needs the element to exist at
    boot."""
    html = _read(_INDEX_HTML)
    assert 'id="perAppBackupList"' in html
    assert 'id="perAppIncludeLogs"' in html


def test_index_has_restore_controls():
    """Restore needs the file picker, the force-overwrite checkbox,
    the preview pane (auto-detected dump_kind feedback), the action
    button, and the result-render target."""
    html = _read(_INDEX_HTML)
    for marker in (
        'id="restoreFile"',
        'id="forceRestore"',
        'id="restorePreview"',
        'data-action="uploadRestore"',
        'id="restoreResult"',
    ):
        assert marker in html, f"missing markup hook: {marker}"


# ----- handlers: definitions present -----

def test_app_js_defines_new_handlers():
    src = _read(_APP_JS)
    for name in (
        "downloadBackup",
        "downloadAppBackup",
        "loadPerAppBackupList",
        "uploadRestore",
        "_refreshRestorePreview",
        "_formatRestoreResult",
    ):
        pattern = rf"(async\s+)?function\s+{name}\s*\("
        assert re.search(pattern, src), f"handler not defined: {name}"


def test_app_js_routes_endpoints_to_v18_paths():
    """The handlers must call the v1.8.0 endpoints, not the legacy
    /keys/backup + /keys/restore. The legacy backend endpoints still
    exist for operator scripts, but the UI is on the new ones."""
    src = _read(_APP_JS)
    # downloadBackup hits /backup; per-app hits /backup/app/<app>
    assert "/backup" in src
    assert "/backup/app/" in src
    # restore routes to /restore + /restore/app/<app>
    assert "/restore" in src
    assert "/restore/app/" in src
    # query-string param shapes
    assert "include_logs=1" in src
    assert "force_overwrite=" in src
    # Legacy /keys/backup must NOT still be wired from the UI as a
    # URL (backend endpoint stays for operator scripts, but the UI is
    # on the new path now). Tolerate references inside comments —
    # match the URL-building shape `${API_BASE}/keys/backup` or
    # quoted string literals only.
    for legacy in ("/keys/backup", "/keys/restore"):
        for shape in (f"`{legacy}", f"'{legacy}", f'"{legacy}', f"API_BASE}}{legacy}"):
            assert shape not in src, \
                f"UI still references legacy endpoint via {shape!r}"


# ----- action dispatcher: new action wired -----

def test_actions_table_routes_download_app_backup():
    """The action dispatcher's ACTIONS table must include the new
    `downloadAppBackup` entry — otherwise the per-app row buttons
    silently no-op."""
    src = _read(_APP_JS)
    # Loose match — the table entry should be present in some shape
    # `downloadAppBackup: (el) => downloadAppBackup(el.dataset.app)`.
    assert re.search(
        r"downloadAppBackup\s*:\s*\(el\)\s*=>\s*downloadAppBackup\s*\(",
        src,
    ), "ACTIONS table missing downloadAppBackup entry"


def test_view_switch_loads_per_app_list():
    """Tab switch into the Backup view must call
    `loadPerAppBackupList` so the list refreshes on each visit
    (catalogs may have been published since last view)."""
    src = _read(_APP_JS)
    assert re.search(
        r"targetId\s*===\s*['\"]backup['\"][^;]*loadPerAppBackupList",
        src,
    ), "Backup tab switch doesn't trigger loadPerAppBackupList"


def test_restore_file_change_wires_preview():
    """The restore file-picker's `change` should call
    `_refreshRestorePreview` so the operator sees the dump_kind +
    app before committing."""
    src = _read(_APP_JS)
    # Boot-time wiring — look for the addEventListener('change',
    # _refreshRestorePreview) shape on the restoreFile element.
    assert re.search(
        r"restoreFile.*addEventListener\(['\"]change['\"]\s*,\s*_refreshRestorePreview",
        src,
        re.DOTALL,
    ), "restoreFile change listener not wired to _refreshRestorePreview"


# ----- cache-bust: bumped -----

def test_index_app_js_cache_bust_bumped_for_v181():
    """Per the v1.6.9 cache-bust policy: every change to app.js bumps
    `?v=N` in index.html. v1.7.1 landed at v=22; v1.8.1's UI changes
    must bump beyond that. (Higher is fine; the pre-commit hook
    handles auto-bump going forward, but tests pin the floor.)"""
    html = _read(_INDEX_HTML)
    m = re.search(r'app\.js\?v=(\d+)', html)
    assert m, "no app.js?v=N cache-bust found in index.html"
    n = int(m.group(1))
    assert n >= 23, f"app.js cache-bust is v={n}; expected >=23 for v1.8.1"
