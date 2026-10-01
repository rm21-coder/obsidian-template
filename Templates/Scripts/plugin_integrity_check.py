#!/usr/bin/env python3
"""
plugin_integrity_check.py — Detect malicious or unexpected changes in
Obsidian community plugins.

What it does
------------
On each run:
  1. Walks <vault>/.obsidian/plugins/ (default ~/Obsidian; override with --vault) and computes a SHA-256 hash
     of every plugin's executable bundle (main.js) and manifest.json.
  2. Compares against a signed allowlist at ~/.local/share/obsidian-security/
     plugin_allowlist.json — a per-plugin record of {id, name, version,
     manifest_sha256, main_sha256, vetted_at}.
  3. Reports any of:
        NEW            plugin not in the allowlist
        REMOVED        allowlist plugin no longer present
        VERSION_CHANGE manifest version differs from allowlist
        BUNDLE_CHANGE  main.js hash differs but version is the same
                       (this is the strongest "supply-chain compromise"
                       signal — a silent swap of bundle behind a pinned
                       version)
        MANIFEST_DRIFT manifest.json hash differs but version unchanged
  4. On any non-empty finding: emits a desktop notification, appends a
     structured JSON line to the alert log, and exits non-zero so the
     scheduler's log (launchd / Task Scheduler) shows it.
  5. With --update, accepts the current state as the new allowlist (used
     when the user has just vetted a plugin update).

This control is what catches:
  - A plugin's GitHub repo being hijacked and a malicious release pushed
    behind the same version number.
  - The user accidentally enabling a community plugin without vetting.
  - A plugin update that was supposed to be a bugfix but ships unexpected
    code (visible to you because BUNDLE_CHANGE fires when version is
    unchanged but main.js differs).

What it does NOT do
-------------------
  - Block anything. This is a detection control. Pair with LuLu for egress
    blocking and with Obsidian's Restricted Mode for prevention.
  - Scan plugin source for malicious patterns. Hash diffs are enough to
    flag the moment of compromise; deep analysis is a follow-up.

Usage
-----
    plugin_integrity_check.py                # check, alert if drift
    plugin_integrity_check.py --update       # adopt current state as new baseline
    plugin_integrity_check.py --json         # print JSON report only (no alerting)
    plugin_integrity_check.py --vault PATH   # custom vault path

Exit codes
----------
    0  no findings (or --update completed)
    1  drift detected
    2  hard error (vault not found, allowlist corrupt, etc.)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
import unicodedata
from pathlib import Path

import security_common

DEFAULT_VAULT = Path.home() / "Obsidian"
STATE_DIR = security_common.state_dir()
ALLOWLIST_PATH = STATE_DIR / "plugin_allowlist.json"

# HMAC envelope for the allowlist (the trust anchor of this control).
#
# Why: 0600 file permissions alone don't defend against the documented
# threat actor — a process running as the user. Such a process could
# overwrite plugin_allowlist.json with attacker-favorable hashes; the
# next diff would then return zero findings against a fully active
# malicious plugin.
#
# How: a 256-bit random key generated on first use is stored by
# security_common under service obsidian-allowlist-hmac — macOS Keychain,
# Windows DPAPI-encrypted file, or a 0600 file elsewhere. On every save
# the allowlist is wrapped as {"state": ..., "hmac": ...}; on every
# load the HMAC is recomputed and compared in constant time. Mismatch
# fires an ALLOWLIST_TAMPER alert and exits non-zero.
#
# The key store is user-scoped (Keychain / DPAPI both require the logged-in
# user), so forging the allowlist means running code as the user — activity
# that belongs to the endpoint security layer (your EDR) to catch; see
# docs/Security-Harness.md on the process-audit hand-off.

# The trust-anchor key is created/stored by security_common: Keychain on macOS,
# a DPAPI-encrypted file on Windows, a 0600 file elsewhere.
# The scheduled job that runs this script, per platform. Named here so an
# interactive --update can refresh the job's recorded exit status; see the
# kickstart call in main().
AGENT_LABEL = "com.obsidian.security.plugin-check"
AGENT_WINDOWS_TASK = r"\Obsidian\security-plugin-check"

HMAC_SERVICE = "obsidian-allowlist-hmac"
HMAC_ACCOUNT = os.environ.get("USER") or os.environ.get("USERNAME") or "obsidian"


# ---------- Helpers ----------------------------------------------------------

# Recorded in place of a hash for anything that is not a plain file. Cannot
# equal a real digest, so it always differs from a vetted baseline.
NOT_A_FILE = "not-a-regular-file"


def _regular_file(path: Path) -> bool:
    """True only for a plain file, checked without following a symlink and
    without opening it: opening a FIFO blocks this run, and launchd starts no
    other run while one is going -- the control would be silenced for good."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def sha256_file(path: Path) -> str:
    if not _regular_file(path):
        return NOT_A_FILE
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def append_alert(record: dict) -> None:
    # Shared implementation: size-capped with gzip rotation (a pre-baseline
    # install once grew this file to 342 MB). See security_common.append_alert.
    security_common.append_alert(record)


# ---------- HMAC key handling -----------------------------------------------

def _require_hmac_key() -> bytes:
    """Return the HMAC key, creating it on first use via security_common
    (Keychain / DPAPI / 0600 file by platform). Fatal if it can neither be
    read nor created — without it we cannot honor the integrity contract."""
    key = security_common.get_or_create_hmac_key(HMAC_SERVICE, HMAC_ACCOUNT)
    if key is None or len(key) < 16:
        security_common.log("plugin-check",
                            "FATAL: could not obtain the HMAC key.")
        sys.exit(2)
    return key


def _canonical_state_bytes(state: dict) -> bytes:
    """Canonical JSON serialization of the state for HMAC. sort_keys
    guarantees byte-stability across Python versions."""
    return json.dumps(state, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _compute_hmac(state: dict, key: bytes) -> str:
    return hmac.new(key, _canonical_state_bytes(state),
                    hashlib.sha256).hexdigest()


# ---------- Plugin scanner ---------------------------------------------------

# Settings that let a plugin run code from note content, or run code on its
# own, per plugin id. Hashing main.js proves the plugin is the one vetted; it
# says nothing about whether that plugin has been told to execute a script.
# Curated from each installed bundle (adversarial review, 2026-10-01) rather
# than the whole data.json: most settings are layout, and alerting on every
# UI tweak would train people to adopt alerts unread.
SECURITY_SETTINGS: dict[str, tuple[str, ...]] = {
    "obsidian-meta-bind-plugin": ("enableJs", "excludedFolders",
                                  "ignoreCodeBlockRestrictions", "devMode",
                                  "buttonTemplates", "inputFieldTemplates"),
    "dataview": ("enableDataviewJs", "enableInlineDataviewJs",
                 "dataviewJsKeyword", "inlineJsQueryPrefix"),
    "templater-obsidian": ("enable_system_commands", "user_scripts_folder",
                           "startup_templates", "trigger_on_file_creation",
                           "templates_folder", "enable_folder_templates",
                           "folder_templates", "enable_file_templates",
                           "file_templates", "templates_pairs", "shell_path",
                           "enabled_templates_hotkeys"),
    # Macros (including runOnStartup ones) run user scripts through eval.
    "quickadd": ("choices", "macros", "devMode"),
    # A startup script is run with AsyncFunction on every plugin load.
    "obsidian-excalidraw-plugin": ("startupScriptPath", "scriptFolderPath",
                                   "pinnedScripts"),
    # Field formulas and custom functions are compiled with new Function and
    # recalculated automatically.
    "metadata-menu": ("presetFields", "classFilesPath", "fileClassQueries",
                      "isAutoCalculationEnabled", "globalFileClass"),
    # Added to every tasks block; "filter by function" there is JavaScript.
    "obsidian-tasks-plugin": ("globalQuery", "presets"),
    # Not code execution: exposes the vault on the network.
    "omnisearch": ("httpApiEnabled", "DANGER_httpHost"),
}

# Settings files are small; anything larger is not one, and is not parsed.
SETTINGS_MAX_BYTES = 16 * 1024 * 1024


def _read_json(path: Path):
    """("absent" | "unreadable" | "ok", value). Never raises and never blocks:
    a non-file is refused before opening, and every parse failure -- bad
    encoding, nesting deep enough for RecursionError on Python 3.9 -- is
    recorded as unreadable rather than escaping and killing the run."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    if not stat.S_ISREG(st.st_mode) or st.st_size > SETTINGS_MAX_BYTES:
        return "unreadable", None
    try:
        # Decoded as Obsidian's adapter does: invalid UTF-8 becomes U+FFFD
        # rather than failing, so a stray byte cannot make a file this check
        # gives up on while the plugin reads it fine. A BOM is NOT stripped:
        # JSON.parse rejects one too, and the plugin falls back to defaults.
        return "ok", json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except Exception:
        return "unreadable", None


def _value_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


# Referenced files: a vetted setting that names a script or template only
# vouches for the name (review round 2). Hashed as well:
#   - any file a watched setting names outright (a QuickAdd UserScript .js,
#     a Templater startup/folder/file template, Excalidraw's startup script),
#     as written or with ".md" added, as Templater and QuickAdd resolve them;
#   - the code and template FOLDERS below, recursively, .md and .js only,
#     skipping dot-folders (Templater's folder holds Scripts/.venv).
# Folder NAMES elsewhere -- Meta Bind's excludedFolders -- are not code.
REF_FOLDER_KEYS: dict[str, tuple[str, ...]] = {
    "templater-obsidian": ("templates_folder", "user_scripts_folder"),
    "obsidian-excalidraw-plugin": ("scriptFolderPath",),
    "metadata-menu": ("classFilesPath",),
}
REF_SUFFIXES = (".md", ".js")
REF_MAX_FILES = 2000


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


REF_EXTRA_SUFFIXES = (".js", ".mjs", ".cjs", ".json")   # what a script can require()
# Watched settings that name folders to EXCLUDE, not files to run.
REF_SKIP_KEYS: dict[str, tuple[str, ...]] = {"obsidian-meta-bind-plugin": ("excludedFolders",)}
# Where a bare name is resolved by link name (Meta Bind's resolveFilePathLike).
# Templater and QuickAdd resolve template paths exactly, so a bare word there
# -- a folder or category name -- is not a reference, and matching it against
# note names would alert on ordinary note edits.
BARE_NAME_KEYS: dict[str, tuple[str, ...]] = {
    "obsidian-meta-bind-plugin": ("buttonTemplates", "inputFieldTemplates")}
REF_WALK_SECONDS = 20


def _obsidian_path(raw: str) -> str:
    """Obsidian's normalizePath, plus the "./" a plugin may accept: backslashes
    to slashes, runs of slashes collapsed, leading/trailing slashes and "./"
    dropped, non-breaking spaces as spaces, NFC."""
    p = raw.replace("\\", "/").replace("\u00a0", " ").replace("\u202f", " ")
    p = re.sub(r"/{2,}", "/", p).strip()
    while p.startswith("./"):
        p = p[2:]
    return unicodedata.normalize("NFC", p.strip("/"))


def _hash_ref(vault: Path, target: Path) -> str:
    """Content hash of a referenced file. A symlink is followed wherever it
    points -- the plugin reads the target -- and its destination recorded."""
    if target.is_symlink():
        try:
            dest = os.readlink(target)
            real = target.resolve()
        except (OSError, RuntimeError):
            return "symlink->unresolvable"
        return f"symlink->{dest}:" + (sha256_file(real) if real.is_file() else NOT_A_FILE)
    return sha256_file(target)


# The automation folder sits inside Templater's templates folder. Its code is
# the integrity monitor's to watch, and its .md files are reports the jobs
# rewrite every night -- hashed here they would alert nightly.
AUTOMATION_DIR = Path(__file__).resolve().parent


def _walk(root: Path, suffixes: tuple, budget: dict) -> list[Path]:
    """Files under root with these suffixes: never following a directory
    symlink (a loop would stall the run), skipping dot-folders, bounded in
    files and time. Running out is recorded, not silently truncated."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and (here / d).resolve() != AUTOMATION_DIR)
        for f in sorted(filenames):
            if not f.startswith(".") and f.lower().endswith(suffixes):
                out.append(Path(dirpath) / f)
                budget["files"] -= 1
            if budget["files"] < 0 or time.monotonic() > budget["deadline"]:
                budget["overrun"] = True
                return out
    return out


def _referenced_files(vault: Path, plugin_id: str, data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    budget = {"files": REF_MAX_FILES, "deadline": time.monotonic() + REF_WALK_SECONDS,
              "overrun": False}
    by_name: dict[str, list[Path]] | None = None

    def add(path: Path) -> None:
        out[path.relative_to(vault).as_posix()] = _hash_ref(vault, path)

    skip = REF_SKIP_KEYS.get(plugin_id, ())
    bare_ok = set(BARE_NAME_KEYS.get(plugin_id, ()))
    pairs = [(k, raw) for k in SECURITY_SETTINGS.get(plugin_id, ())
             if k in data and k not in skip for raw in _strings(data[k])]
    named: list[Path] = []
    for key, raw in pairs:
        name = _obsidian_path(raw)
        if not name or len(name) > 1024 or "\n" in name:
            continue
        hit = None
        for cand in (name, name + ".md"):
            target = vault / cand
            if target.is_file() or target.is_symlink():
                hit = target
                break
        if hit is None and "/" not in name and key in bare_ok:
            # A bare note name, resolved by link name as Obsidian does.
            if by_name is None:
                by_name = {}
                for f in _walk(vault, REF_SUFFIXES, {"files": 200000,
                               "deadline": time.monotonic() + REF_WALK_SECONDS,
                               "overrun": False}):
                    by_name.setdefault(f.stem.casefold(), []).append(f)
            for f in by_name.get(name.casefold().removesuffix(".md"), []):
                named.append(f)
            continue
        if hit is not None:
            named.append(hit)
    for f in named:
        add(f)
        # A script can require() its neighbours: hash its folder's code too.
        if f.suffix.lower() in (".js", ".mjs", ".cjs") and f.parent != vault:
            for g in _walk(f.parent, REF_EXTRA_SUFFIXES, budget):
                add(g)
    for key in REF_FOLDER_KEYS.get(plugin_id, ()):
        folder = data.get(key)
        if not isinstance(folder, str) or not _obsidian_path(folder):
            continue
        root = vault / _obsidian_path(folder)
        if not root.is_dir():
            continue
        for g in _walk(root, REF_SUFFIXES, budget):
            add(g)
    if budget["overrun"]:
        # Not a state a baseline can hold: reported on every run until the
        # referenced folders are small enough to hash completely.
        out["(overrun)"] = "referenced files exceed the hashing budget"
    return out


def read_security_settings(plugin_dir: Path, plugin_id: str) -> dict | None:
    """{"file": state, "keys": {key: {"set": bool, "sha256": digest}}} for a
    watched plugin, or None. Presence and value are separate fields, so no
    value written into data.json can impersonate "not set"."""
    keys = SECURITY_SETTINGS.get(plugin_id)
    if not keys:
        return None
    path = plugin_dir / "data.json"
    state, data = _read_json(path)
    if state == "ok" and not isinstance(data, dict):
        state = "unreadable"
    record = {"file": state, "keys": {}}
    if state == "unreadable":
        # Still compared: a file this check cannot parse may be one the plugin
        # can, and edits inside it must not go unseen once this is adopted.
        record["raw_sha256"] = sha256_file(path)
    if state == "ok":
        for k in keys:
            record["keys"][k] = ({"set": True, "sha256": _value_digest(data[k])}
                                 if k in data else {"set": False})
        vault = plugin_dir.parent.parent.parent
        record["refs"] = _referenced_files(vault, plugin_id, data)
    return record


def _js_string(value) -> str:
    """String(value) as JavaScript computes it, for the values JSON can hold.
    Obsidian keys plugins by String() of each community-plugins.json entry,
    so ["templater-obsidian"] loads templater-obsidian."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if not value.is_integer() else str(int(value))
    if isinstance(value, list):
        return ",".join("" if v is None else _js_string(v) for v in value)
    return "[object Object]"


def read_enabled(plugins_dir: Path) -> set[str] | None:
    """Plugin ids Obsidian loads from .obsidian/community-plugins.json, or
    None if the list is absent or unreadable."""
    state, data = _read_json(plugins_dir.parent / "community-plugins.json")
    if state != "ok" or not isinstance(data, list):
        return None
    try:
        return {_js_string(x) for x in data}
    except RecursionError:
        return None


# Obsidian can be pointed at another configuration folder (any name starting
# with "."), recorded in the Electron profile rather than the vault. This
# control reads .obsidian only, so a second config folder is recorded as an
# entry of its own: one appearing is a finding until vetted.
_CONFIG_MARKERS = ("community-plugins.json", "app.json", "plugins")
# Only .obsidian itself is skipped. Obsidian accepts any ".name" (review round
# 2: .trash, .git and .claude all worked as config folders), and a symlinked
# one is followed by Obsidian, so it is recorded too.
_NOT_CONFIG = {".obsidian"}


def scan_config_dirs(vault: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        entries = sorted(vault.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.name.startswith(".") or d.name in _NOT_CONFIG:
            continue
        try:
            if not d.is_dir() or not any((d / m).exists() for m in _CONFIG_MARKERS):
                continue
        except OSError:
            continue
        out[f"config-dir:{d.name}"] = {"name": d.name, "version": "",
                                       "manifest_sha256": None,
                                       "main_sha256": None,
                                       "config_dir": True,
                                       "symlink": d.is_symlink()}
    return out


def scan_plugins(plugins_dir: Path) -> dict[str, dict]:
    """Return {plugin_id: {name, version, manifest_sha256, main_sha256}}."""
    out: dict[str, dict] = {}
    if not plugins_dir.is_dir():
        return out
    enabled = read_enabled(plugins_dir)
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        main_js = entry / "main.js"
        if not manifest.exists() or not main_js.exists():
            # Incomplete plugin — record what we can; flag separately.
            out[entry.name] = {
                "name": entry.name,
                "version": None,
                "manifest_sha256": sha256_file(manifest) if manifest.exists() else None,
                "main_sha256": sha256_file(main_js) if main_js.exists() else None,
                "incomplete": True,
            }
            continue
        state, mf = _read_json(manifest)
        if state != "ok" or not isinstance(mf, dict):
            # Any failure -- bad encoding, deep nesting, a FIFO -- is recorded,
            # never raised: a crash here used to end the run with no alert.
            out[entry.name] = {
                "name": entry.name,
                "version": None,
                "manifest_sha256": None if state == "absent" else sha256_file(manifest),
                "main_sha256": sha256_file(main_js),
                "manifest_error": f"manifest {state}",
            }
            continue
        raw_id = mf.get("id")
        # Obsidian keys a plugin by String(id): ["dataview"] is "dataview".
        mid = _js_string(raw_id) if raw_id not in (None, "") else entry.name
        key = mid
        if key in out:
            # Two folders declaring one id used to collapse into a single
            # entry, so the second folder's main.js was never compared with
            # anything. Keep both; the suffixed key is unvetted by
            # construction, so it surfaces as a new plugin.
            key = f"{key}@{entry.name}"
        out[key] = {
            "name": _js_string(mf.get("name") or entry.name),
            "version": _js_string(mf.get("version") or ""),
            "manifest_sha256": sha256_file(manifest),
            "main_sha256": sha256_file(main_js),
            "is_desktop_only": bool(mf.get("isDesktopOnly")),
            "author_url": _js_string(mf.get("authorUrl") or ""),
            # Installed is not loaded: switching on an installed, disabled
            # plugin changes what runs without touching any file hashed above.
            # None when the enabled list cannot be read: recorded, compared.
            "enabled": (mid in enabled) if enabled is not None else None,
        }
        settings = read_security_settings(entry, mid)
        if settings is not None:
            out[key]["settings"] = settings
    return out


# ---------- Allowlist I/O ----------------------------------------------------

def load_allowlist() -> dict[str, dict]:
    """Read and HMAC-verify the allowlist. Returns the inner state dict
    (mapping plugin_id → record). On verification failure, fires an
    ALLOWLIST_TAMPER alert and exits non-zero. A file that is not in the
    signed envelope is tamper too -- see below."""
    # lstat, not exists(): a symlink loop made exists() False and the run
    # took the quiet "no allowlist" path; a FIFO in its place blocked
    # read_text forever with launchd refusing to start the next run. Only a
    # missing entry is "no allowlist"; anything else that is not a regular
    # file is tamper. Adversarial review round 2, 2026-09-25.
    try:
        st = os.lstat(ALLOWLIST_PATH)
    except FileNotFoundError:
        return {}
    except OSError as e:
        _fire_tamper(f"allowlist cannot be examined ({type(e).__name__})")
    if not stat.S_ISREG(st.st_mode):
        _fire_tamper("allowlist is not a regular file")
    # Every way the file can fail to be a readable JSON document is tamper,
    # not a crash or a quiet log line. Non-JSON used to exit 2 with only a log
    # line; invalid UTF-8 and a directory in its place escaped as uncaught
    # exceptions -- no alert, no notification, the control simply stopped
    # reporting. Adversarial review of the M-DASH fixes, 2026-09-25.
    try:
        raw = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError, RecursionError) as e:
        # JSONDecodeError and UnicodeDecodeError are ValueErrors. RecursionError
        # is not: deeply nested JSON raises it on the system Python 3.9 that
        # launchd runs, and it escaped as a crash with no alert.
        _fire_tamper(f"allowlist unreadable or not JSON ({type(e).__name__})")

    # An unsigned file is refused, not "migrated". This branch used to
    # accept any file lacking the envelope keys as a trusted pre-v1.4
    # allowlist, with no verification -- so the HMAC could be bypassed
    # entirely by writing the old flat format instead of forging a
    # signature: a format downgrade. "Accept once" was not enforced either;
    # every run trusted it. Found by Microsoft M-DASH 2026-09-23 (CWE-347).
    #
    # Nothing is stranded by refusing it. --update never reads the old file
    # -- it re-scans the plugins on disk and writes a fresh signed envelope
    # -- so a genuine pre-envelope install recovers with one --update, and
    # a missing file already fails closed as "no baseline" above.
    if not (isinstance(raw, dict) and "state" in raw and "hmac" in raw):
        _fire_tamper("allowlist is not in the signed envelope — refusing an "
                     "unsigned allowlist (vet, then run --update to re-sign)")

    state = raw.get("state")
    stored_hmac = raw.get("hmac")
    if not isinstance(state, dict) or not isinstance(stored_hmac, str) \
            or not stored_hmac.isascii():
        # isascii: hmac.compare_digest raises TypeError on a non-ASCII str.
        _fire_tamper("allowlist envelope malformed")  # noqa: returns sys.exit
    key = _require_hmac_key()
    expected = _compute_hmac(state, key)
    if not hmac.compare_digest(expected, stored_hmac):
        _fire_tamper("HMAC mismatch — allowlist forged or corrupted")
    return state


def _fire_tamper(reason: str) -> None:
    """Notify, log, and exit non-zero. Returns NoReturn (sys.exit)."""
    msg = f"ALLOWLIST_TAMPER: {reason}"
    security_common.notify("Obsidian plugin integrity ALERT", msg)
    append_alert({
        "control": "plugin_integrity",
        "kind": "ALLOWLIST_TAMPER",
        "summary": msg,
        "reason": reason,
    })
    security_common.log("plugin-check", f"FATAL: {msg}")
    sys.exit(1)


def save_allowlist(allowlist: dict[str, dict]) -> None:
    """Write allowlist wrapped in an HMAC-SHA256 envelope keyed by the
    Keychain-stored key (created on first call if absent)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = _require_hmac_key()
    wrapped = {
        "state": allowlist,
        "hmac": _compute_hmac(allowlist, key),
        "envelope_version": 1,
    }
    tmp = ALLOWLIST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(wrapped, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, ALLOWLIST_PATH)
    # Restrictive permissions — defense-in-depth alongside HMAC. chmod 0600 on
    # POSIX, an icacls ACL on Windows (where chmod alone would silently leave
    # the inherited ACL in place).
    security_common.restrict_file(ALLOWLIST_PATH)


# ---------- Diff -------------------------------------------------------------

def _settings_changes(old: dict, cur: dict) -> dict:
    """{key: {"from": state, "to": state}} for watched settings that differ,
    plus "(file)" when data.json itself went absent/unreadable/readable."""
    def state(rec, k):
        if rec.get("file") != "ok":
            return rec.get("file")
        v = rec.get("keys", {}).get(k)
        if not v or not v.get("set"):
            return "not set"
        return "set:" + v.get("sha256", "")[:12]
    changed = {}
    if old.get("file") != cur.get("file"):
        changed["(file)"] = {"from": old.get("file"), "to": cur.get("file")}
    elif old.get("raw_sha256") != cur.get("raw_sha256"):
        changed["(file content)"] = {"from": old.get("raw_sha256"), "to": cur.get("raw_sha256")}
    old_refs, cur_refs = old.get("refs") or {}, cur.get("refs") or {}
    for f in sorted(set(old_refs) | set(cur_refs)):
        if old_refs.get(f) != cur_refs.get(f):
            changed[f"(referenced) {f}"] = {"from": old_refs.get(f, "absent"),
                                            "to": cur_refs.get(f, "absent")}
    for k in sorted(set(old.get("keys", {})) | set(cur.get("keys", {}))):
        a, b = state(old, k), state(cur, k)
        if a != b:
            changed[k] = {"from": a, "to": b}
    return changed


def diff(current: dict[str, dict], allowlist: dict[str, dict]) -> list[dict]:
    findings: list[dict] = []

    for pid, cur in current.items():
        if pid not in allowlist:
            findings.append({"kind": "NEW", "plugin": pid, "current": cur})
            continue
        old = allowlist[pid]
        if cur.get("version") != old.get("version"):
            findings.append({
                "kind": "VERSION_CHANGE",
                "plugin": pid,
                "from": old.get("version"),
                "to": cur.get("version"),
                "main_changed": cur.get("main_sha256") != old.get("main_sha256"),
            })
        else:
            if cur.get("main_sha256") != old.get("main_sha256"):
                findings.append({
                    "kind": "BUNDLE_CHANGE",
                    "plugin": pid,
                    "version": cur.get("version"),
                    "old_sha": old.get("main_sha256"),
                    "new_sha": cur.get("main_sha256"),
                })
            if cur.get("manifest_sha256") != old.get("manifest_sha256"):
                findings.append({
                    "kind": "MANIFEST_DRIFT",
                    "plugin": pid,
                    "version": cur.get("version"),
                    "old_sha": old.get("manifest_sha256"),
                    "new_sha": cur.get("manifest_sha256"),
                })
        # Checked on every path, including a version change: approving a
        # plugin update must not silently approve a settings change made with
        # it (adversarial review, 2026-10-01).
        missing = [f for f in ("enabled", "settings") if f in cur and f not in old]
        if missing:
            # Recorded since 2026-10-01; an older signed allowlist cannot vouch
            # for these, so their absence is a finding until vetted.
            findings.append({"kind": "NOT_BASELINED", "plugin": pid, "fields": missing})
        if "enabled" in cur and "enabled" in old and cur["enabled"] != old["enabled"]:
            findings.append({"kind": "ENABLED_CHANGE", "plugin": pid,
                             "from": old["enabled"], "to": cur["enabled"]})
        if "(overrun)" in ((cur.get("settings") or {}).get("refs") or {}):
            findings.append({"kind": "REFERENCE_LIMIT", "plugin": pid})
        if ("settings" in cur and "settings" in old
                and isinstance(old["settings"], dict) and "keys" in old["settings"]):
            changed = _settings_changes(old["settings"], cur["settings"])
            if changed:
                findings.append({"kind": "SETTINGS_CHANGE", "plugin": pid,
                                 "changed": changed})
        elif "settings" in cur and "settings" in old:
            # A baseline from the first, superseded format of this field.
            findings.append({"kind": "NOT_BASELINED", "plugin": pid,
                             "fields": ["settings"]})

    for pid in allowlist:
        if pid not in current:
            findings.append({"kind": "REMOVED", "plugin": pid,
                             "last_known": allowlist[pid]})

    return findings


# ---------- Main -------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault", default=str(DEFAULT_VAULT),
                   help=f"vault path (default: {DEFAULT_VAULT})")
    p.add_argument("--update", action="store_true",
                   help="adopt current state as the new allowlist")
    p.add_argument("--json", action="store_true",
                   help="print JSON report; suppress notifications")
    args = p.parse_args(argv)

    vault = Path(os.path.expanduser(args.vault)).resolve()
    plugins_dir = vault / ".obsidian" / "plugins"
    if not plugins_dir.is_dir():
        # An empty plugin set is a valid baseline (no community plugins), so
        # this is not an error -- but it is compared like any other state.
        # It used to return 0 here unconditionally, so deleting the folder,
        # or moving the configuration to another folder, silenced the check.
        security_common.log("plugin-check",
                            f"no plugins directory at {plugins_dir}")

    current = scan_plugins(plugins_dir)
    current.update(scan_config_dirs(vault))

    if args.update:
        # Tag every entry with vetted_at. The user is asserting they
        # reviewed the bundle right now.
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        for pid in current:
            current[pid]["vetted_at"] = ts
        save_allowlist(current)
        security_common.log(
            "plugin-check",
            f"allowlist updated: {len(current)} plugins recorded.",
            stream=sys.stdout)
        # Same reasoning as integrity_monitor: the allowlist is already
        # written, and without this the scheduler keeps reporting the drift run
        # that prompted the adopt, so the dashboard shows this control failing
        # while the state is clean. The triggered run has no --update and so
        # cannot trigger another.
        if not security_common.kickstart_agent(
                AGENT_LABEL, windows_task=AGENT_WINDOWS_TASK):
            security_common.log(
                "plugin-check",
                "note: could not trigger a fresh scheduled run — the job's "
                "recorded status stays stale until it next runs on its own.",
                stream=sys.stdout)
        return 0

    allowlist = load_allowlist()

    if not allowlist:
        # First run with no baseline — refuse to silently accept everything.
        # Tell the user to vet manually then run with --update.
        # Worded so it does not invite adopting whatever is installed: on a
        # machine that HAD a baseline, a missing allowlist means it was
        # deleted, and --update would certify the state the deletion hid.
        # (The integrity monitor reports the deletion independently, since
        # the allowlist is one of its state-dir trust anchors.)
        msg = (f"No allowlist. If this machine never had one, vet the "
               f"{len(current)} installed plugin(s), then run "
               f"plugin_integrity_check.py --update. If it DID have one, it "
               f"has been deleted: investigate before adopting anything.")
        security_common.log("plugin-check", msg)
        if not args.json:
            security_common.notify("Obsidian plugin integrity",
                   "No allowlist. If one existed it was deleted: investigate "
                   "before --update.")
        if args.json:
            print(json.dumps({"status": "no_baseline",
                              "current": current}, indent=2))
        return 2

    findings = diff(current, allowlist)

    if args.json:
        print(json.dumps({
            "status": "ok" if not findings else "drift",
            "findings": findings,
            "scanned": len(current),
            "baseline_count": len(allowlist),
        }, indent=2))
        return 0 if not findings else 1

    if not findings:
        # Quiet success — write a heartbeat so the user can confirm runs.
        return 0

    # Findings — alert.
    summary_parts: list[str] = []
    for f in findings[:5]:
        if f["kind"] == "BUNDLE_CHANGE":
            summary_parts.append(f"{f['plugin']} bundle changed (same version)")
        elif f["kind"] == "VERSION_CHANGE":
            summary_parts.append(f"{f['plugin']} {f['from']} -> {f['to']}")
        elif f["kind"] == "NEW" and f["plugin"].startswith("config-dir:"):
            summary_parts.append(f"NEW Obsidian config folder: {f['plugin'][11:]}")
        elif f["kind"] == "NEW":
            summary_parts.append(f"NEW plugin: {f['plugin']}")
        elif f["kind"] == "REMOVED":
            summary_parts.append(f"REMOVED: {f['plugin']}")
        elif f["kind"] == "MANIFEST_DRIFT":
            summary_parts.append(f"{f['plugin']} manifest drift")
        elif f["kind"] == "SETTINGS_CHANGE":
            summary_parts.append(f"{f['plugin']} security setting changed: "
                                 + ", ".join(sorted(f["changed"])))
        elif f["kind"] == "ENABLED_CHANGE":
            state = {True: "ENABLED", False: "disabled", None: "enabled state unreadable"}
            summary_parts.append(f"{f['plugin']} {state.get(f['to'], f['to'])}")
        elif f["kind"] == "REFERENCE_LIMIT":
            summary_parts.append(f"{f['plugin']}: its scripts/templates are too many "
                                 "to hash completely; not all are watched")
        elif f["kind"] == "NOT_BASELINED":
            summary_parts.append(f"{f['plugin']} {'/'.join(f['fields'])} not yet "
                                 "baselined: vet, then --update")
    if len(findings) > 5:
        summary_parts.append(f"… +{len(findings) - 5} more")
    summary = "; ".join(summary_parts)

    security_common.notify("Obsidian plugin integrity ALERT", summary)
    append_alert({
        "control": "plugin_integrity",
        "summary": summary,
        "findings": findings,
    })

    security_common.log("plugin-check", f"DRIFT: {summary}")
    for f in findings:
        security_common.log("plugin-check", f"  - {json.dumps(f)}")
    return 1


def _run(argv: list[str]) -> int:
    """main(), with any unexpected failure turned into an alert. A control that
    dies on malformed input reports nothing, which is exactly what an attacker
    wants from it; on launchd the traceback reached a log nobody reads and the
    exit code looked like ordinary drift (adversarial review, 2026-10-01)."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:            # incl. RecursionError, MemoryError
        msg = f"CONTROL_ERROR: the check itself failed ({type(exc).__name__}): {exc}"[:400]
        # Each channel on its own: one failing must not take the others with it.
        for report in (
                lambda: security_common.log("plugin-check", f"FATAL: {msg}"),
                lambda: append_alert({"control": "plugin_integrity",
                                      "kind": "CONTROL_ERROR", "summary": msg}),
                lambda: ("--json" in argv
                         or security_common.notify("Obsidian plugin integrity ALERT", msg))):
            try:
                report()
            except Exception:
                pass
        return 3


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
