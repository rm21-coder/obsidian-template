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

    5. Coverage added 2026-10-03, each its own scope:
         script_config Templates/Scripts/.config/*.json, the jobs' own
                       settings (the scripts scope skips dot-directories).
         user_site     Python's per-user site-packages, loaded at startup by
                       any interpreter run without -s. Normally empty.
         templates     ~/Obsidian/Templates (outside Scripts/): the .md and
                       .js files QuickAdd runs or fills.
         venv          the scripts' virtualenv: every code file (.py, .pyc,
                       .pth, .so, .dylib, ...), pyvenv.cfg and the interpreter
                       links. A .pth line or a site-packages edit runs in
                       every job without touching a watched script.
         agent_config  the Claude CLI's instructions and the settings keys
                       that run commands or widen permissions (hooks,
                       statusLine, apiKeyHelper, env, permissions, MCP
                       servers) in ~/.claude, ~/.claude.json and the vault.
                       Only those keys: the rest of those files churn.
         secrets       ~/dev/secrets/.env (the same under %USERPROFILE% on
                       Windows): its hash, and (POSIX) a finding
                       whenever group or others can read it.
       plus a bytecode check with no baseline: every cached .pyc of the
       scripts that an interpreter would load instead of the source must
       compile from that source. Apple's /usr/bin/python3, which runs the
       security controls, keeps its cache in ~/Library/Caches/
       com.apple.python, outside every other scope; every .pyc there whose
       source exists is checked, the standard library's included. Each interpreter's own
       cache is checked by that interpreter: the venv's in a child process
       whose own imports bypass the cache being checked.
       A baseline from before a scope existed reports NOT_BASELINED once for
       that scope, not every file in it as new.

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
import importlib.util
import json
import marshal
import os
import re
import stat as _stat
import subprocess
import sys
from pathlib import Path

import security_common

DEFAULT_SCRIPTS = Path.home() / "Obsidian" / "Templates" / "Scripts"
DEFAULT_LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"
DEFAULT_VAULT = Path.home() / "Obsidian"
STATE_DIR = security_common.state_dir()
STATE_PATH = STATE_DIR / "integrity_state.json"

# The scheduled job that runs this script, per platform. Named here so an
# interactive --update can refresh the job's recorded exit status; see the
# kickstart call in main().
AGENT_LABEL = "com.obsidian.security.integrity"
AGENT_WINDOWS_TASK = r"\Obsidian\security-integrity"

DELETION_FLOOR = 50          # absolute floor below which a drop is fine
DELETION_RATIO = 0.05         # 5% relative
# .ps1/.psd1 cover the Windows scheduler layer (Templates/Scripts/windows/).
# .js/.applescript/.txt/.yaml added 2026-09-25 after the adversarial review:
# the meeting-pull prompt template, requirements.txt, the handoff transform,
# the dashboard applet source and pipeline config are executed or steer
# execution, and none of them was hashed.
# .pyc/.pyd/.so/.dylib/.pth added 2026-10-03: a module or bytecode file
# planted beside the scripts shadows a package (the scripts' folder comes
# first on sys.path), and a .pth there would run at startup.
SCRIPT_EXTS = {".py", ".sh", ".plist", ".ps1", ".psd1",
               ".js", ".applescript", ".txt", ".yaml", ".yml",
               ".pyc", ".pyd", ".so", ".dylib", ".pth", ".pyw"}

TEMPLATE_EXTS = {".md", ".js"}
# .pyw is importable source on Windows; .pem is the CA bundle requests trusts
# (one added certificate intercepts every API call, the gateway key included).
VENV_EXTS = {".py", ".pyw", ".pyc", ".pth", ".so", ".dylib", ".pyd", ".dll", ".exe",
             ".metallib", ".pem"}
# Files read to decide what code to load: plugin and backend registries.
VENV_NAMES = {"pyvenv.cfg", "entry_points.txt"}
# Settings keys that run a command, change the environment or widen what the
# CLI may do without asking. Everything else in these files is UI state.
AGENT_SETTINGS_KEYS = ("hooks", "disableAllHooks", "statusLine", "apiKeyHelper",
                       "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper",
                       "env", "permissions", "mcpServers", "enabledMcpjsonServers",
                       "enableAllProjectMcpServers")
# Home of the Claude config and the secrets file; a module setting so tests
# can point it elsewhere.
HOME = Path.home()
# Scopes that hash files against the baseline, in report order.
BASELINE_SCOPES = ("scripts", "launchagents", "state_dir", "script_config",
                   "templates", "venv", "user_site", "agent_config", "secrets")

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

def scan_dir(directory: Path, *, exts: set[str],
             extra_prune: frozenset[str] = frozenset()) -> dict[str, dict]:
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
             "node_modules", ".obsidian", ".trash"} | set(extra_prune)
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
            # as_posix(), not str(): baseline keys are relative paths, and
            # str(Path) is backslash-separated on Windows. A baseline is only
            # ever compared against itself on one machine, so the old form was
            # self-consistent -- but it made the state file platform-specific
            # and put the same latent defect here that made RAG-sync state
            # unportable. Deliberately NOT normalized on read: collapsing
            # backslashes to slashes inside a tamper-detection control would
            # let two distinct paths share a key (a POSIX filename may legally
            # contain a backslash), and re-running --update costs nothing.
            rel = path.relative_to(directory).as_posix()
            out[rel] = {
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        except OSError as e:
            out[path.relative_to(directory).as_posix()] = {"error": str(e)}
    return out


class PersistenceScanError(Exception):
    """The persistence-agent scan could not enumerate what is installed.

    Distinct from "no tasks": an empty result adopted by --update would save
    an empty baseline, and the next working run would then report every task
    as NEW_FILE instead of comparing it against what it was."""


def scan_scheduled_tasks() -> dict[str, dict]:
    """Windows analog of hashing ~/Library/LaunchAgents plists: hash the exported
    XML definition of each Task Scheduler task under \\Obsidian\\. Catches a vault
    task being silently rewritten, added, or removed (persistence abuse). Scoped
    to \\Obsidian\\ to avoid the churn of system/vendor tasks; the volatile <Date>
    registration timestamp is stripped so a no-op re-register isn't flagged.

    Raises PersistenceScanError when enumeration fails. "No tasks" is the
    script's own empty hashtable, which serialises as "{}"; empty output, a
    non-zero exit, bad JSON or a non-object are failures, not "no tasks"."""
    # 'Stop', not 'SilentlyContinue': silenced, an access-denied or CIM
    # failure from Get-ScheduledTask still printed "{}" with exit 0, which is
    # "no tasks" and adoptable by --update. The one error that does mean "no
    # tasks" is ObjectNotFound (the \\Obsidian\\ folder does not exist yet);
    # everything else, including a task whose definition will not export,
    # exits 3 with the message on stderr.
    ps = (
        "$ErrorActionPreference='Stop';"
        "try{"
        "try{$ts=@(Get-ScheduledTask -TaskPath '\\Obsidian\\')}"
        "catch{if($_.CategoryInfo.Category -eq 'ObjectNotFound'){$ts=@()}else{throw}};"
        "$o=@{};foreach($t in $ts){"
        "$x=Export-ScheduledTask -TaskName $t.TaskName -TaskPath '\\Obsidian\\';"
        "if(-not $x){throw ('export returned nothing for '+$t.TaskName)};"
        "$o[$t.TaskName]=[string]$x};"
        "$o|ConvertTo-Json -Depth 3 -Compress;"
        "}catch{[Console]::Error.WriteLine($_.Exception.Message);exit 3}"
    )
    try:
        p = subprocess.run(
            [security_common.POWERSHELL_EXE, "-NoProfile", "-NonInteractive",
             "-Command", ps],
            capture_output=True, timeout=30)
    except Exception as exc:
        raise PersistenceScanError(
            f"could not run PowerShell: {type(exc).__name__}: {exc}") from exc
    if p.returncode != 0:
        err = " ".join((p.stderr or b"").decode("utf-8", "ignore").split())[:200]
        raise PersistenceScanError(
            f"PowerShell exited {p.returncode}" + (f": {err}" if err else ""))
    raw = p.stdout.decode("utf-8", "ignore").strip()
    if not raw:
        raise PersistenceScanError("PowerShell returned no output")
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise PersistenceScanError(f"unparseable task list: {exc}") from exc
    if not isinstance(data, dict):
        raise PersistenceScanError(
            f"task list is a {type(data).__name__}, not an object")
    out: dict[str, dict] = {}
    for name, xml in data.items():
        if not isinstance(xml, str) or not xml:
            # A task that is there but whose definition we could not read
            # is a gap in the scan, not a task to leave out of it.
            raise PersistenceScanError(f"no exported definition for task {name!r}")
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
            if not _stat.S_ISREG(stat.st_mode):
                # Never open it: a FIFO planted here blocked this scan (and
                # the plugin check) forever. Recorded, so it differs from the
                # baseline hash and is reported.
                out[path.name] = {"error": "not a regular file"}
                continue
            out[path.name] = {
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        except OSError as e:
            out[path.name] = {"error": str(e)}
    return out


def _file_entry(path: Path) -> dict:
    try:
        st = path.stat()
        if not _stat.S_ISREG(st.st_mode):
            return {"error": "not a regular file"}
        return {"sha256": sha256_file(path), "size": st.st_size, "mtime": int(st.st_mtime)}
    except OSError as e:
        return {"error": str(e)}


def scan_templates(vault: Path) -> dict[str, dict]:
    """The templates QuickAdd fills and the user scripts it runs. Scripts/ is
    the scripts scope's."""
    return scan_dir(vault / "Templates", exts=TEMPLATE_EXTS,
                    extra_prune=frozenset({"Scripts"}))


def scan_venv(venv: Path) -> dict[str, dict]:
    """Every code file in the scripts' virtualenv, pyvenv.cfg, and the
    interpreter links in bin/ (Scripts\\ on Windows), hashed through to their
    targets. Changes legitimately only when the requirements are reinstalled."""
    out: dict[str, dict] = {}
    if not venv.is_dir():
        return out
    for path in venv.rglob("*"):
        try:
            rel_parts = path.relative_to(venv).parts
        except ValueError:
            continue
        top_exe = len(rel_parts) == 2 and rel_parts[0] in ("bin", "Scripts")
        if not (path.suffix.lower() in VENV_EXTS or path.name in VENV_NAMES or top_exe):
            continue
        if not path.is_file():
            continue
        out[path.relative_to(venv).as_posix()] = _file_entry(path)
    return out


def scan_script_config(scripts: Path) -> dict[str, dict]:
    """Templates/Scripts/.config/*.json: the jobs' own settings. The scripts
    scope skips dot-directories, and meeting_pull.json names the tools the
    unattended Claude session may use."""
    cfg = scripts / ".config"
    if not cfg.is_dir():
        return {}
    return {p.name: _file_entry(p) for p in sorted(cfg.iterdir()) if p.suffix == ".json"}


def scan_user_site(home: Path) -> dict[str, dict]:
    """Python's per-user site-packages, which every interpreter run without
    -s loads at startup -- Apple's /usr/bin/python3, which runs the security
    controls, included. Normally empty or absent."""
    out: dict[str, dict] = {}
    roots = [home / "Library" / "Python"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Python")
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and (path.suffix.lower() in VENV_EXTS or path.name in VENV_NAMES):
                out[f"{root.name}/{path.relative_to(root).as_posix()}"] = _file_entry(path)
    return out


def _settings_digest(path: Path, *, mcp_only: bool = False) -> dict | None:
    """Hash of the security-relevant keys of a JSON settings file, or None
    when the file does not exist."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"unreadable: {e.__class__.__name__}"}
    if not isinstance(data, dict):
        return {"error": "not a JSON object"}
    if mcp_only:
        picked = {"mcpServers": data.get("mcpServers")}
        projects = data.get("projects")
        if isinstance(projects, dict):
            picked["projects"] = {k: v.get("mcpServers") for k, v in sorted(projects.items())
                                  if isinstance(v, dict) and v.get("mcpServers")}
    else:
        picked = {k: data[k] for k in AGENT_SETTINGS_KEYS if k in data}
    blob = json.dumps(picked, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}


def scan_agent_config(vault: Path, home: Path) -> dict[str, dict]:
    """The Claude CLI's standing instructions and the settings that make it
    run commands or skip asking. Keys are labelled by root, not full paths,
    so the state file does not carry the user's home path."""
    out: dict[str, dict] = {}
    whole = {"home:.claude/CLAUDE.md": home / ".claude" / "CLAUDE.md",
             "vault:CLAUDE.md": vault / "CLAUDE.md",
             "vault:.claude/CLAUDE.md": vault / ".claude" / "CLAUDE.md"}
    for key, path in whole.items():
        if path.exists():
            out[key] = _file_entry(path)
    keyed = {"home:.claude/settings.json": (home / ".claude" / "settings.json", False),
             "home:.claude/settings.local.json": (home / ".claude" / "settings.local.json", False),
             "vault:.claude/settings.json": (vault / ".claude" / "settings.json", False),
             "vault:.claude/settings.local.json": (vault / ".claude" / "settings.local.json", False),
             "scripts:.claude/settings.json": (vault / "Templates" / "Scripts" / ".claude" / "settings.json", False),
             "scripts:.claude/settings.local.json": (vault / "Templates" / "Scripts" / ".claude" / "settings.local.json", False),
             "home:.claude.json#mcpServers": (home / ".claude.json", True)}
    for key, (path, mcp_only) in keyed.items():
        entry = _settings_digest(path, mcp_only=mcp_only)
        if entry is not None:
            out[key] = entry
    return out


def scan_secrets(home: Path) -> dict[str, dict]:
    path = home / "dev" / "secrets" / ".env"
    if not path.exists():
        return {}
    entry = _file_entry(path)
    try:
        entry["mode"] = _stat.S_IMODE(path.stat().st_mode)
    except OSError:
        pass
    return {"secrets/.env": entry}


def secrets_permission_findings(current: dict[str, dict]) -> list[dict]:
    """POSIX only: a secrets file group or others can read. Windows guards it
    with an ACL, which a mode does not describe."""
    if os.name != "posix":
        return []
    entry = current.get("secrets/.env") or {}
    mode = entry.get("mode")
    if mode is not None and mode & 0o077:
        return [{"kind": "PERMISSIONS", "scope": "secrets", "path": "secrets/.env",
                 "mode": oct(mode)}]
    return []


# ---------- Bytecode ---------------------------------------------------------

def _bytecode_sources(scripts: Path) -> list[Path]:
    return sorted([*scripts.glob("*.py"), *(scripts / "windows").glob("*.py")])


def _pyc_problem(src: Path, pyc: Path, optimize: int) -> str | None:
    """Why `pyc` is not what `src` compiles to, or None when it is -- or when
    this interpreter would not load it anyway (another version's magic, or a
    stale timestamp or hash, which makes Python recompile from source)."""
    try:
        if not _stat.S_ISREG(pyc.stat().st_mode):
            return "not a regular file"
        data = pyc.read_bytes()
        source = src.read_bytes()
        st = src.stat()
    except OSError as e:
        return f"unreadable: {e}"
    if data[:4] != importlib.util.MAGIC_NUMBER:
        return None
    flags = int.from_bytes(data[4:8], "little")
    if flags & ~0b11:
        return None                      # rejected by the loader
    if flags == 0:                       # timestamp-based
        if (int.from_bytes(data[8:12], "little") != (int(st.st_mtime) & 0xFFFFFFFF)
                or int.from_bytes(data[12:16], "little") != (st.st_size & 0xFFFFFFFF)):
            return None
    elif flags & 0b10:                   # checked hash: loaded only if it matches
        if data[8:16] != importlib.util.source_hash(source):
            return None
    # an unchecked hash-based .pyc is loaded whatever the source says
    try:
        code = marshal.loads(data[16:])
    except Exception as e:  # noqa: BLE001 -- any failure is the finding
        return f"unreadable bytecode: {e.__class__.__name__}"
    try:
        expected = compile(source, str(src), "exec", dont_inherit=True, optimize=optimize)
    except (SyntaxError, ValueError):
        return None                      # the source itself would not import
    return None if code == expected else "does not match its source"


def bytecode_findings(scripts: Path, *, explicit_pycache: bool = False) -> list[dict]:
    """Every cached .pyc of the scripts that THIS interpreter would load must
    be the compiled source. explicit_pycache: look in <dir>/__pycache__
    rather than where this process's own cache settings point (used by the
    child run, whose cache is redirected so its imports skip __pycache__)."""
    out = []
    tag = sys.implementation.cache_tag
    for src in _bytecode_sources(scripts):
        for opt in (0, 1, 2):
            suffix = "" if opt == 0 else f".opt-{opt}"
            if explicit_pycache:
                pyc = src.parent / "__pycache__" / f"{src.stem}.{tag}{suffix}.pyc"
            else:
                try:
                    pyc = Path(importlib.util.cache_from_source(
                        str(src), optimization="" if opt == 0 else opt))
                except (NotImplementedError, ValueError):
                    continue
            if not pyc.exists():
                continue
            problem = _pyc_problem(src, pyc, opt)
            if problem:
                out.append({"kind": "BYTECODE_MISMATCH", "scope": "bytecode",
                            "path": str(pyc), "detail": problem})
    return out


def _venv_python(scripts: Path) -> Path | None:
    for rel in (("bin", "python3"), ("Scripts", "python.exe")):
        cand = scripts / ".venv" / Path(*rel)
        if cand.exists():
            return cand
    return None


def prefix_cache_findings() -> list[dict]:
    """With a cache prefix (Apple's /usr/bin/python3 uses ~/Library/Caches/
    com.apple.python), every module this interpreter imports -- the standard
    library included -- is cached there, in a user-writable tree. Each .pyc
    for this interpreter whose source still exists must compile from it."""
    prefix = getattr(sys, "pycache_prefix", None)
    if not prefix or not Path(prefix).is_dir():
        return []
    root = Path(prefix)
    tag = sys.implementation.cache_tag
    out = []
    for pyc in root.rglob(f"*.{tag}*.pyc"):
        name = pyc.name
        stem, _, rest = name.partition(f".{tag}")
        opt = 0
        if rest.startswith(".opt-") and rest[5:6].isdigit():
            opt = int(rest[5])
        src = Path(os.sep) / pyc.parent.relative_to(root) / f"{stem}.py"
        if not src.is_file():
            continue
        problem = _pyc_problem(src, pyc, opt)
        if problem:
            out.append({"kind": "BYTECODE_MISMATCH", "scope": "bytecode",
                        "path": str(pyc), "detail": problem})
    return out


def venv_bytecode_findings(scripts: Path) -> list[dict]:
    """The venv interpreter's cache, checked by that interpreter in a child
    process. -X pycache_prefix points the child's own imports at an empty
    directory, so a tampered .pyc in __pycache__ cannot run inside the check
    that is looking for it; -E, -s and -S keep PYTHON* variables, the user
    site and the base install's sitecustomize out of it too."""
    venv_py = _venv_python(scripts)
    if venv_py is None or venv_py.resolve().parent == Path(sys.executable).resolve().parent:
        return []
    import tempfile
    with tempfile.TemporaryDirectory(prefix="integrity_pyc_") as empty:
        try:
            p = subprocess.run(
                [str(venv_py), "-E", "-s", "-S", "-B", "-X", f"pycache_prefix={empty}",
                 str(Path(__file__).resolve()), "--bytecode-only", "--scripts-dir", str(scripts)],
                capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            return [{"kind": "BYTECODE_UNCHECKED", "scope": "bytecode",
                     "path": str(venv_py), "detail": e.__class__.__name__}]
    try:
        found = json.loads(p.stdout.decode("utf-8", "replace"))
        if p.returncode == 0 and isinstance(found, list):
            return found
    except ValueError:
        pass
    return [{"kind": "BYTECODE_UNCHECKED", "scope": "bytecode", "path": str(venv_py),
             "detail": f"exit {p.returncode}"}]


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
        security_common.log("integrity", f"FATAL: state corrupt: {e}")
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
    p.add_argument("--bytecode-only", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    scripts = Path(os.path.expanduser(args.scripts_dir)).resolve()
    launchagents = Path(os.path.expanduser(args.launchagents_dir)).resolve()
    vault = Path(os.path.expanduser(args.vault)).resolve()

    if args.bytecode_only:      # the child run of venv_bytecode_findings()
        print(json.dumps(bytecode_findings(scripts, explicit_pycache=True)))
        return 0

    persistence_error: str | None = None
    try:
        persistence = scan_persistence(launchagents)
    except PersistenceScanError as exc:
        persistence, persistence_error = {}, str(exc)
        security_common.log(
            "integrity", f"persistence-agent scan failed: {persistence_error}")

    current = {
        "scripts": scan_dir(scripts, exts=SCRIPT_EXTS),
        "launchagents": persistence,
        "state_dir": scan_state_dir(),
        "script_config": scan_script_config(scripts),
        "templates": scan_templates(vault),
        "venv": scan_venv(scripts / ".venv"),
        "user_site": scan_user_site(HOME),
        "agent_config": scan_agent_config(vault, HOME),
        "secrets": scan_secrets(HOME),
        "vault_md_count": count_vault_md(vault),
    }

    if args.update:
        if persistence_error is not None:
            # Adopting {} here would record "no scheduled tasks" as the
            # trusted state, and every task would read as NEW next time.
            security_common.log(
                "integrity",
                "REFUSED --update: could not enumerate the scheduled tasks "
                f"({persistence_error}). Baseline left unchanged; fix the "
                "scan and re-run --update.",
                stream=sys.stdout)
            return 2
        save_state(current)
        n_scripts = len(current["scripts"])
        n_agents = len(current["launchagents"])
        n_state = len(current["state_dir"])
        n_md = current["vault_md_count"]
        security_common.log(
            "integrity",
            f"baseline updated: {n_scripts} script files, "
            f"{n_agents} agent plists, {n_state} state-dir trust anchors, "
            f"{len(current['script_config'])} script configs, "
            f"{len(current['templates'])} templates, {len(current['venv'])} venv files, "
            f"{len(current['user_site'])} user-site files, "
            f"{len(current['agent_config'])} agent config entries, "
            f"{len(current['secrets'])} secrets file(s), "
            f"{n_md} markdown files in vault.",
            stream=sys.stdout)
        # The adopt is already complete; this only refreshes the scheduler's
        # record of the last run. Skip it and launchd still reports the drift
        # run that prompted the rebaseline, so the dashboard shows this control
        # failing while the state is in fact clean -- the confusing case that
        # motivated this call. The run it triggers executes this script WITHOUT
        # --update, so it cannot kickstart again; there is no recursion here.
        if not security_common.kickstart_agent(
                AGENT_LABEL, windows_task=AGENT_WINDOWS_TASK):
            security_common.log(
                "integrity",
                "note: could not trigger a fresh scheduled run — the job's "
                "recorded status stays stale until it next runs on its own.",
                stream=sys.stdout)
        return 0

    baseline = load_state()
    if not baseline:
        msg = ("No baseline. Run with --update once you have verified the "
               "current state is clean.")
        security_common.log("integrity", msg)
        if not args.json:
            security_common.notify("Workflow integrity monitor", msg)
        if args.json:
            print(json.dumps({"status": "no_baseline", "current": current},
                             indent=2))
        return 2

    findings: list[dict] = []
    for scope in BASELINE_SCOPES:
        if scope not in baseline:
            # A baseline from before this scope existed: one finding, not
            # every file in it reported as new.
            findings.append({"kind": "NOT_BASELINED", "scope": scope,
                             "count": len(current[scope])})
            continue
        if scope == "launchagents" and persistence_error is not None:
            # One finding naming the cause, not a DELETED per baselined task.
            findings.append({"kind": "SCAN_FAILED", "scope": scope,
                             "detail": persistence_error})
            continue
        findings += diff_dir(scope, current[scope], baseline[scope])
    findings += secrets_permission_findings(current["secrets"])
    findings += bytecode_findings(scripts)
    findings += prefix_cache_findings()
    findings += venv_bytecode_findings(scripts)
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
                "script_config": len(current["script_config"]),
                "templates": len(current["templates"]),
                "venv": len(current["venv"]),
                "user_site": len(current["user_site"]),
                "agent_config": len(current["agent_config"]),
                "secrets": len(current["secrets"]),
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
        elif f["kind"] == "SCAN_FAILED":
            summary_parts.append(f"{f['scope']}: scan failed ({f['detail']})")
        elif f["kind"] == "NOT_BASELINED":
            summary_parts.append(f"{f['scope']}: not yet baselined")
        elif f["kind"] == "PERMISSIONS":
            summary_parts.append(f"secrets file readable by others ({f['mode']})")
        elif f["kind"] in ("BYTECODE_MISMATCH", "BYTECODE_UNCHECKED"):
            summary_parts.append(f"bytecode: {Path(f['path']).name} {f['detail']}")
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

    security_common.log("integrity", f"DRIFT: {summary}")
    for f in findings:
        security_common.log("integrity", f"  - {json.dumps(f)}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
