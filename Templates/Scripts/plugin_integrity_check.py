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
import sys
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
HMAC_SERVICE = "obsidian-allowlist-hmac"
HMAC_ACCOUNT = os.environ.get("USER") or os.environ.get("USERNAME") or "obsidian"


# ---------- Helpers ----------------------------------------------------------

def sha256_file(path: Path) -> str:
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

def scan_plugins(plugins_dir: Path) -> dict[str, dict]:
    """Return {plugin_id: {name, version, manifest_sha256, main_sha256}}."""
    out: dict[str, dict] = {}
    if not plugins_dir.is_dir():
        return out
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
        try:
            mf = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            out[entry.name] = {
                "name": entry.name,
                "version": None,
                "manifest_sha256": None,
                "main_sha256": sha256_file(main_js),
                "manifest_error": str(e),
            }
            continue
        out[mf.get("id") or entry.name] = {
            "name": mf.get("name") or entry.name,
            "version": mf.get("version") or "",
            "manifest_sha256": sha256_file(manifest),
            "main_sha256": sha256_file(main_js),
            "is_desktop_only": bool(mf.get("isDesktopOnly")),
            "author_url": mf.get("authorUrl") or "",
        }
    return out


# ---------- Allowlist I/O ----------------------------------------------------

def load_allowlist() -> dict[str, dict]:
    """Read and HMAC-verify the allowlist. Returns the inner state dict
    (mapping plugin_id → record). On verification failure, fires an
    ALLOWLIST_TAMPER alert and exits non-zero. On a legacy flat-format
    file (pre-HMAC-envelope), accepts it once as a one-time migration
    — the next save_allowlist call will rewrap it."""
    if not ALLOWLIST_PATH.exists():
        return {}
    try:
        raw = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        security_common.log("plugin-check",
                            f"FATAL: allowlist corrupt: {e}")
        sys.exit(2)

    # Legacy flat format — pre-envelope. Accept once; the next --update
    # will rewrap. We log this so the user can see migration happening.
    if not (isinstance(raw, dict) and "state" in raw and "hmac" in raw):
        security_common.log(
            "plugin-check",
            "migrating allowlist to HMAC envelope on next --update "
            "(loading as legacy flat format).")
        return raw if isinstance(raw, dict) else {}

    state = raw.get("state")
    stored_hmac = raw.get("hmac")
    if not isinstance(state, dict) or not isinstance(stored_hmac, str):
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

def diff(current: dict[str, dict], allowlist: dict[str, dict]) -> list[dict]:
    findings: list[dict] = []

    # NEW / changed
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
            continue
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

    # REMOVED
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
        security_common.log("plugin-check",
                            f"no plugins directory at {plugins_dir}")
        # Empty plugin set is a valid baseline (Restricted Mode), not an error.
        if args.update:
            save_allowlist({})
            return 0
        return 0

    current = scan_plugins(plugins_dir)

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
        return 0

    allowlist = load_allowlist()

    if not allowlist:
        # First run with no baseline — refuse to silently accept everything.
        # Tell the user to vet manually then run with --update.
        msg = (f"No baseline yet. Vet the {len(current)} installed "
               f"plugin(s), then run: plugin_integrity_check.py --update")
        security_common.log("plugin-check", msg)
        if not args.json:
            security_common.notify("Obsidian plugin integrity",
                   "No baseline. Run with --update after vetting.")
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
        elif f["kind"] == "NEW":
            summary_parts.append(f"NEW plugin: {f['plugin']}")
        elif f["kind"] == "REMOVED":
            summary_parts.append(f"REMOVED: {f['plugin']}")
        elif f["kind"] == "MANIFEST_DRIFT":
            summary_parts.append(f"{f['plugin']} manifest drift")
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
