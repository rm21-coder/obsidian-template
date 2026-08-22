#!/usr/bin/env python3
"""
integrity_monitor.py — Daily integrity sweep over the second-brain workflow.

Four independent integrity checks, all built on a single state file
~/.local/share/obsidian-security/integrity_state.json:

    1. ~/Obsidian/Templates/Scripts/ — every .py / .sh hashed and pinned.
       The expected pinning is "stable code, rare edits"; an unexpected
       change to (e.g.) source_mail_pull.py or sync-vault.sh is a red flag —
       those scripts ingest external content and have file-write authority
       over the vault.

    2. Persistence agents hashed and pinned — ~/Library/LaunchAgents/ plists
       on macOS, or the \\Obsidian\\ Task Scheduler task definitions on Windows.
       New agents/tasks appearing, or existing ones being silently rewritten,
       is a classic persistence-mechanism abuse.

    3. ~/.local/share/obsidian-security/ — the controls' own state
       directory. Watches *.json trust anchors (notably plugin_allowlist.
       json). Without this, a process running as the user — i.e., the
       very threat actor the controls are designed to detect — could
       silently rewrite the allowlist with attacker-favorable hashes and
       neutralize the strongest control without surfacing any finding.
       integrity_state.json itself is excluded from this scan (it is the
       script's own state file; including it would create a chicken-and-
       egg with save_state). The append-only alerts.log is naturally
       excluded by the .json filter.

    4. ~/Obsidian/                — bulk-deletion guard. Compares the
       count of .md files against the previous baseline; alerts if the
       count drops by more than DELETION_THRESHOLD (default 50 or 5%
       of the prior count, whichever is greater). Catches both ransomware
       and a misbehaving sync.

For each detected change, the script:
    - appends a structured JSON record to the alert log in the state dir
    - emits a desktop notification
    - exits non-zero so the scheduler's log (launchd / Task Scheduler) captures it

The state file is updated only on `--update` (treats a clean run as the
new baseline). This is deliberate: a script you didn't expect to change
should not silently re-baseline itself.

Usage
-----
    integrity_monitor.py                # check, alert if drift
    integrity_monitor.py --update       # adopt current state as new baseline
    integrity_monitor.py --json         # print JSON report; no alerts
    integrity_monitor.py --scripts-dir PATH --launchagents-dir PATH --vault PATH

Exit codes
----------
    0 no findings (or --update completed)
    1 drift detected
    2 hard error
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import security_common

DEFAULT_SCRIPTS = Path.home() / "Obsidian" / "Templates" / "Scripts"
DEFAULT_LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"
DEFAULT_VAULT = Path.home() / "Obsidian"
STATE_DIR = security_common.state_dir()
STATE_PATH = STATE_DIR / "integrity_state.json"

DELETION_FLOOR = 50          # absolute floor below which a drop is fine
DELETION_RATIO = 0.05         # 5% relative
# .ps1/.psd1 cover the Windows scheduler layer (Templates/Scripts/windows/).
SCRIPT_EXTS = {".py", ".sh", ".plist", ".ps1", ".psd1"}

# Third-party LaunchAgents that rewrite themselves on their own schedule
# (vendor auto-updaters). Their recurring CONTENT_CHANGE is noise, and a
# control that cries wolf gets tuned out -- which is its own risk.
#
# Scope of the suppression is deliberately narrow: patterns are fnmatch-style,
# keyed by scan scope, and suppress ONLY CONTENT_CHANGE. A NEW_FILE or DELETED
# finding still fires even when the path matches, so a newly planted agent
# cannot hide behind a trusted vendor name, and removal of one is still seen.
# Prefer exact filenames over broad wildcards when adding entries here.
CONTENT_CHANGE_IGNORE = {
    "launchagents": [
        "com.adobe.ccxprocess.plist",   # Adobe Creative Cloud updater
    ],
}


def content_change_ignored(scope: str, rel: str) -> bool:
    """True if content churn on this path is deliberately not alerted on."""
    return any(fnmatch.fnmatch(rel, pat)
               for pat in CONTENT_CHANGE_IGNORE.get(scope, ()))


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


# ---------- Scanners ---------------------------------------------------------

def scan_dir(directory: Path, *, exts: set[str]) -> dict[str, dict]:
    """Hash every file in `directory` matching `exts`. Returns {rel_path:
    {sha256, size, mtime}}.  Symlinks are followed (rare here) but their
    target file is what gets hashed; we record the symlink's own mtime."""
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    # Prune virtualenvs, caches, and VCS/Obsidian metadata. Required because
    # the watched dir (~/Obsidian/Templates/Scripts) contains a .venv whose
    # thousands of site-package .py files would otherwise flood the baseline.
    prune = {".venv", "__pycache__", ".pytest_cache", ".git",
             "node_modules", ".obsidian", ".trash"}
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if any(p in prune or p.startswith(".")
               for p in path.relative_to(directory).parts[:-1]):
            continue
        try:
            stat = path.stat()
            rel = str(path.relative_to(directory))
            out[rel] = {
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        except OSError as e:
            out[str(path.relative_to(directory))] = {"error": str(e)}
    return out


def scan_scheduled_tasks() -> dict[str, dict]:
    """Windows analog of hashing ~/Library/LaunchAgents plists: hash the exported
    XML definition of each Task Scheduler task under \\Obsidian\\. Catches a vault
    task being silently rewritten, added, or removed (persistence abuse). Scoped
    to \\Obsidian\\ to avoid the churn of system/vendor tasks; the volatile <Date>
    registration timestamp is stripped so a no-op re-register isn't flagged."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$o=@{};foreach($t in Get-ScheduledTask -TaskPath '\\Obsidian\\'){"
        "$o[$t.TaskName]=(Export-ScheduledTask -TaskName $t.TaskName -TaskPath '\\Obsidian\\')};"
        "$o|ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30)
    except Exception:
        return {}
    raw = p.stdout.decode("utf-8", "ignore").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for name, xml in data.items():
        if not isinstance(xml, str):
            continue
        norm = re.sub(r"<Date>.*?</Date>", "", xml)   # drop re-register timestamp
        out[name] = {
            "sha256": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
            "size": len(norm),
        }
    return out


def scan_persistence(launchagents: Path) -> dict[str, dict]:
    """Persistence-agent scan: LaunchAgents plists on macOS, Task Scheduler
    (\\Obsidian\\) task definitions on Windows."""
    if sys.platform == "win32":
        return scan_scheduled_tasks()
    return scan_dir(launchagents, exts={".plist"})


def scan_state_dir() -> dict[str, dict]:
    """Hash *.json trust anchors in STATE_DIR. Specifically excludes
    integrity_state.json itself — it is this script's own state file,
    rewritten on every --update; including it would record a hash that
    is stale the moment save_state finishes (a fixed-point that doesn't
    exist for SHA-256). The append-only alerts.log is excluded by the
    *.json glob. plugin_allowlist.json is the primary trust anchor we
    catch tampering of here; that file is also defended in depth by an
    HMAC envelope inside plugin_integrity_check.py."""
    out: dict[str, dict] = {}
    if not STATE_DIR.is_dir():
        return out
    for path in sorted(STATE_DIR.glob("*.json")):
        if path.name == "integrity_state.json":
            continue
        try:
            stat = path.stat()
            out[path.name] = {
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        except OSError as e:
            out[path.name] = {"error": str(e)}
    return out


def count_vault_md(vault: Path) -> int:
    """Count .md files outside hidden dirs and templates. We deliberately
    exclude .trash, .obsidian and node_modules to keep the count stable
    across non-content changes."""
    if not vault.is_dir():
        return 0
    count = 0
    skip_prefixes = (".obsidian", ".trash", "node_modules")
    for p in vault.rglob("*.md"):
        try:
            rel = p.relative_to(vault)
        except ValueError:
            continue
        if any(part in skip_prefixes or part.startswith(".") for part in rel.parts):
            continue
        count += 1
    return count


# ---------- State I/O --------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[integrity] FATAL: state corrupt: {e}", file=sys.stderr)
        sys.exit(2)


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, STATE_PATH)
    # Owner-only: chmod 0600 on POSIX, an icacls ACL on Windows (where chmod
    # alone would silently leave the inherited ACL in place).
    security_common.restrict_file(STATE_PATH)


# ---------- Diff -------------------------------------------------------------

def diff_dir(label: str, current: dict, baseline: dict) -> list[dict]:
    findings: list[dict] = []
    for rel, cur in current.items():
        if rel not in baseline:
            findings.append({"kind": "NEW_FILE", "scope": label, "path": rel,
                             "sha256": cur.get("sha256")})
            continue
        old = baseline[rel]
        if cur.get("sha256") != old.get("sha256"):
            if content_change_ignored(label, rel):
                continue
            findings.append({
                "kind": "CONTENT_CHANGE", "scope": label, "path": rel,
                "old_sha": old.get("sha256"), "new_sha": cur.get("sha256"),
                "old_size": old.get("size"), "new_size": cur.get("size"),
            })
    for rel in baseline:
        if rel not in current:
            findings.append({"kind": "DELETED", "scope": label, "path": rel,
                             "last_known_sha": baseline[rel].get("sha256")})
    return findings


def diff_md_count(current: int, baseline: int) -> dict | None:
    if baseline <= 0:
        return None
    drop = baseline - current
    if drop <= 0:
        return None
    threshold = max(DELETION_FLOOR, int(baseline * DELETION_RATIO))
    if drop >= threshold:
        return {
            "kind": "BULK_DELETE",
            "scope": "vault",
            "previous_count": baseline,
            "current_count": current,
            "deleted": drop,
            "threshold": threshold,
        }
    return None


# ---------- Main -------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scripts-dir", default=str(DEFAULT_SCRIPTS))
    p.add_argument("--launchagents-dir", default=str(DEFAULT_LAUNCHAGENTS))
    p.add_argument("--vault", default=str(DEFAULT_VAULT))
    p.add_argument("--update", action="store_true",
                   help="adopt current state as new baseline")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    scripts = Path(os.path.expanduser(args.scripts_dir)).resolve()
    launchagents = Path(os.path.expanduser(args.launchagents_dir)).resolve()
    vault = Path(os.path.expanduser(args.vault)).resolve()

    current = {
        "scripts": scan_dir(scripts, exts=SCRIPT_EXTS),
        "launchagents": scan_persistence(launchagents),
        "state_dir": scan_state_dir(),
        "vault_md_count": count_vault_md(vault),
    }

    if args.update:
        save_state(current)
        n_scripts = len(current["scripts"])
        n_agents = len(current["launchagents"])
        n_state = len(current["state_dir"])
        n_md = current["vault_md_count"]
        print(f"[integrity] baseline updated: "
              f"{n_scripts} script files, {n_agents} agent plists, "
              f"{n_state} state-dir trust anchors, "
              f"{n_md} markdown files in vault.")
        return 0

    baseline = load_state()
    if not baseline:
        msg = ("No baseline. Run with --update once you have verified the "
               "current state is clean.")
        print(f"[integrity] {msg}", file=sys.stderr)
        if not args.json:
            security_common.notify("Workflow integrity monitor", msg)
        if args.json:
            print(json.dumps({"status": "no_baseline", "current": current},
                             indent=2))
        return 2

    findings: list[dict] = []
    findings += diff_dir("scripts", current["scripts"],
                         baseline.get("scripts", {}))
    findings += diff_dir("launchagents", current["launchagents"],
                         baseline.get("launchagents", {}))
    findings += diff_dir("state_dir", current["state_dir"],
                         baseline.get("state_dir", {}))
    bulk = diff_md_count(current["vault_md_count"],
                         baseline.get("vault_md_count", 0))
    if bulk:
        findings.append(bulk)

    if args.json:
        print(json.dumps({
            "status": "ok" if not findings else "drift",
            "findings": findings,
            "current_summary": {
                "scripts": len(current["scripts"]),
                "launchagents": len(current["launchagents"]),
                "state_dir": len(current["state_dir"]),
                "vault_md_count": current["vault_md_count"],
            },
        }, indent=2))
        return 0 if not findings else 1

    if not findings:
        return 0

    summary_parts: list[str] = []
    for f in findings[:5]:
        if f["kind"] == "CONTENT_CHANGE":
            summary_parts.append(f"{f['scope']}: {f['path']} changed")
        elif f["kind"] == "NEW_FILE":
            summary_parts.append(f"NEW {f['scope']}: {f['path']}")
        elif f["kind"] == "DELETED":
            summary_parts.append(f"DELETED {f['scope']}: {f['path']}")
        elif f["kind"] == "BULK_DELETE":
            summary_parts.append(
                f"vault: -{f['deleted']} md files "
                f"({f['previous_count']} → {f['current_count']})")
    if len(findings) > 5:
        summary_parts.append(f"… +{len(findings) - 5} more")
    summary = "; ".join(summary_parts)

    security_common.notify("Workflow integrity ALERT", summary)
    append_alert({
        "control": "workflow_integrity",
        "summary": summary,
        "findings": findings,
    })

    print(f"[integrity] DRIFT: {summary}", file=sys.stderr)
    for f in findings:
        print(f"  - {json.dumps(f)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
