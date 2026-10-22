"""Fixes from a Windows tester's morning-pipeline report (2026-10-11).

Each test pins one finding that was real on a shipped install:

- #8 setup-rag.ps1 published Open WebUI on every interface (`-p 3000:8080`),
  and an existing container kept that binding through re-runs and updates.
- #5 only four of the content folders were gitignored, so a user's real
  People notes sat untracked and unignored in the vault's own git tree.
- #4 security-checks.sh called a bare `python3` (the Microsoft Store stub on
  Windows), and its integrity check passed whenever the control exited
  non-zero -- including when the baseline had silently failed.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CHECKS = REPO / "installers" / "lib" / "security-checks.sh"
WIN = REPO / "Templates" / "Scripts" / "windows"

# The vault's content folders (CLAUDE.md, "What this repository is"), less
# Templates/, which is scaffolding and ships.
CONTENT_FOLDERS = ("Actions", "Categories", "Clippings", "Creations", "Daily", "Groups",
                   "Knowledge", "Meetings", "Notes", "People", "Topics", "Z_archive",
                   "Z_attachments")


def _shipped_text_files():
    for root in (REPO / "installers", REPO / "Templates" / "Scripts"):
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".sh", ".ps1", ".psm1", ".py") \
                    and "tests" not in p.parts \
                    and not any(part.startswith(".venv") for part in p.parts):
                yield p


# ---------------------------------------------------------------------------
# #8 Open WebUI binding
# ---------------------------------------------------------------------------

def test_no_installer_publishes_open_webui_beyond_loopback() -> None:
    bare = re.compile(r"-p\s+['\"]?3000:8080")
    offenders = [f"{p.relative_to(REPO)}: {line.strip()}" for p in _shipped_text_files()
                 for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if bare.search(line) and not line.lstrip().startswith("#")]
    assert not offenders, offenders


def test_windows_checks_an_existing_container_on_setup_and_update() -> None:
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    assert "-p 127.0.0.1:3000:8080" in common
    assert "function Repair-OpenWebUIBinding" in common
    assert "docker port open-webui 8080" in common
    setup = (WIN / "setup-rag.ps1").read_text(encoding="utf-8")
    assert "Repair-OpenWebUIBinding" in setup and "Start-OpenWebUIContainer" in setup
    assert "docker run" not in setup        # one run command, in common.ps1
    assert "Repair-OpenWebUIBinding" in (WIN / "update.ps1").read_text(encoding="utf-8")


def test_nothing_advises_widening_ollama() -> None:
    """Ollama's API is unauthenticated. setup-rag.ps1 once told users to set
    OLLAMA_HOST=0.0.0.0 when the container could not reach it; any line that
    names that value now has to be warning against it."""
    offenders = []
    for p in _shipped_text_files():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "OLLAMA_HOST=0.0.0.0" in line and not re.search(
                    r"Do not set|do not set|suggested|#", line):
                offenders.append(f"{p.relative_to(REPO)}: {line.strip()}")
    assert not offenders, offenders


def test_windows_checks_ollama_on_setup_and_update() -> None:
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    assert "function Test-OllamaExposure" in common
    assert "Get-NetTCPConnection -LocalPort 11434 -State Listen" in common
    assert "GetEnvironmentVariable('OLLAMA_HOST'" in common
    assert "function Test-OpenWebUIReachesOllama" in common
    setup = (WIN / "setup-rag.ps1").read_text(encoding="utf-8")
    assert "Test-OllamaExposure" in setup and "Test-OpenWebUIReachesOllama" in setup
    assert "Test-OllamaExposure" in (WIN / "update.ps1").read_text(encoding="utf-8")


def test_macos_checks_an_existing_container_too() -> None:
    comp = (REPO / "installers" / "components" / "50-llm-rag.sh").read_text(encoding="utf-8")
    assert "docker port open-webui 8080" in comp
    assert "grep -v '^127\\.0\\.0\\.1:'" in comp
    assert comp.count("-p 127.0.0.1:3000:8080") == 1


# ---------------------------------------------------------------------------
# #5 content folders
# ---------------------------------------------------------------------------

def _ignored(rel: str) -> bool:
    return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", rel],
                          check=False).returncode == 0


@pytest.mark.parametrize("folder", CONTENT_FOLDERS)
def test_every_content_folder_ignores_a_note_but_keeps_its_structure(
        folder: str, allow_subprocess) -> None:
    assert _ignored(f"{folder}/A Real Person.md")
    assert _ignored(f"{folder}/sub/deeper/note.md")
    assert not _ignored(f"{folder}/.gitkeep")
    assert not _ignored(f"{folder}/sub/.gitkeep")


def test_the_content_folder_list_matches_the_vault(allow_subprocess) -> None:
    """A new top-level folder holding only a .gitkeep is a content folder and
    needs its ignore lines; this list is what the test above covers."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], check=True,
                             capture_output=True, text=True).stdout.splitlines()
    roots = {Path(t).parts[0] for t in tracked if t.endswith("/.gitkeep") and "/" in t}
    assert roots - {"Templates"} <= set(CONTENT_FOLDERS), sorted(roots - set(CONTENT_FOLDERS))


def test_no_tracked_file_is_ignored(allow_subprocess) -> None:
    """The files meant to ship from content folders are re-included by name."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-ci", "--exclude-standard"],
                         check=True, capture_output=True, text=True).stdout.split()
    assert out == []


# ---------------------------------------------------------------------------
# #4 security suite interpreter and the integrity check
# ---------------------------------------------------------------------------

def test_the_suite_has_no_bare_python3_call() -> None:
    body = CHECKS.read_text(encoding="utf-8")
    after = body.split('CONTROL_PY="${SECURITY_CONTROL_PYTHON:-$CONTROL_PY}"', 1)[1]
    calls = [l.strip() for l in after.splitlines()
             if re.search(r'(?<![\w/."$])python3\b', l) and not l.strip().startswith("#")]
    assert calls == []
    assert "/usr/bin/python3 Templates/Scripts/integrity_monitor.py" not in after


needs_bash_suite = pytest.mark.skipif(
    platform.system() == "Windows" or not shutil.which("bash") or not shutil.which("git"),
    reason="runs security-checks.sh under bash")


@needs_bash_suite
def test_a_broken_control_interpreter_fails_the_integrity_check(tmp_path, allow_subprocess) -> None:
    """The check used to PASS here: a baseline that failed silently made the
    next run exit non-zero, which it read as 'drift detected'."""
    r = subprocess.run(["bash", str(CHECKS), "--dast", "--artifacts", str(tmp_path)],
                       cwd=REPO, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "SECURITY_CONTROL_PYTHON": "/usr/bin/false"})
    assert "dast     FAIL    integrity check could not record its sandbox baseline (exit 1)" in r.stdout
    assert "integrity monitor fails closed on drift" not in r.stdout
    assert r.returncode != 0


@needs_bash_suite
def test_a_python_override_that_does_not_work_is_named(tmp_path, allow_subprocess) -> None:
    r = subprocess.run(["bash", str(CHECKS), "--fast", "--artifacts", str(tmp_path)],
                       cwd=REPO, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHON": str(tmp_path / "no-such-python")})
    assert f"python:    NONE - PYTHON={tmp_path / 'no-such-python'} is not a working Python 3.10+" in r.stdout
    assert "RESULT: FAIL" in r.stdout


# ---------------------------------------------------------------------------
# #3 the unmatched-meeting digest runs on this interpreter
# ---------------------------------------------------------------------------

def test_the_digest_runs_on_the_running_interpreter(monkeypatch, tmp_path) -> None:
    """Every Windows pre-populate run ended 'Digest refresh failed:
    [WinError 2]': the digest was started with a hardcoded /usr/bin/python3."""
    import sys as _sys
    import meeting_prepopulate as mp
    (tmp_path / "meeting_group_backfill.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(mp, "SCRIPTS_DIR", tmp_path)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "done\n", "")
    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp.refresh_unmatched_digest(dry_run=False)
    assert seen["argv"][0] == _sys.executable
    assert seen["argv"][1:] == [str(tmp_path / "meeting_group_backfill.py"), "--digest", "--apply"]


# ---------------------------------------------------------------------------
# #6 the retired-plugin detector reads notes, not the template's docs
# ---------------------------------------------------------------------------

def _plugin_in_use():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "plugin_in_use", REPO / "installers" / "lib" / "plugin_in_use.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_template_docs_do_not_count_as_dataview_notes(tmp_path) -> None:
    """update.ps1 reported '2 note(s) still use Dataview queries' -- both were
    the template's own docs quoting a query, so Dataview could never go."""
    piu = _plugin_in_use()
    for rel in ("docs/Data-Classification.md", "installers/notes.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("an inline `= this.file.name` query\n", encoding="utf-8")
    assert piu.dataview_uses(tmp_path) == 0
    # A user's own folder of that name is a note, and still counts.
    (tmp_path / "Knowledge" / "docs").mkdir(parents=True)
    (tmp_path / "Knowledge" / "docs" / "Mine.md").write_text("`= this.file.name`\n", encoding="utf-8")
    assert piu.dataview_uses(tmp_path) == 1


# ---------------------------------------------------------------------------
# Every Windows task has a time limit sized to its job (2026-10-11)
# ---------------------------------------------------------------------------

def _windows_jobs():
    text = (WIN / "schedules.psd1").read_text(encoding="utf-8")
    jobs = {}
    for line in text.splitlines():
        m = re.search(r"Name='([^']+)'.*Trigger=@\{ Type='(\w+)'(?:; Minutes=(\d+))?", line)
        if m and not line.lstrip().startswith("#"):
            lim = re.search(r"TimeLimitMinutes=(\d+)", line)
            jobs[m.group(1)] = (m.group(2), int(m.group(3) or 0), int(lim.group(1)) if lim else None)
    return jobs


def _limit(kind, minutes, override):
    """The rule in Register-Tasks.ps1's Get-TimeLimitMinutes, mirrored."""
    if override:
        return override
    return max(2 * minutes, 15) if kind == "MinuteInterval" else 120


def test_registration_sets_and_verifies_a_time_limit():
    """The default 72 hours let one hung tagger run block every 30-minute
    start for over a day: the next start is skipped while one runs."""
    reg = (WIN / "Register-Tasks.ps1").read_text(encoding="utf-8")
    assert "-ExecutionTimeLimit $limit" in reg
    assert "$liveLimit -eq $limit" in reg          # read back, not assumed


def test_every_job_gets_a_limit_far_below_three_days():
    jobs = _windows_jobs()
    assert len(jobs) >= 14
    limits = {name: _limit(*spec) for name, spec in jobs.items()}
    assert limits["tag-clippings"] == 60
    assert limits["strip-ads"] == 15
    assert limits["podcast-watch"] == 180 and limits["rag-sync"] == 240
    assert max(limits.values()) <= 240


# ---------------------------------------------------------------------------
# Scripts parse on the oldest interpreter that runs them (2026-10-13)
# ---------------------------------------------------------------------------

APPLE_PY = Path("/usr/bin/python3")


@pytest.mark.skipif(platform.system() != "Darwin" or not APPLE_PY.exists(),
                    reason="Apple's /usr/bin/python3 only exists on macOS")
def test_every_script_parses_on_apples_python(allow_subprocess):
    """The dashboard and the security controls run on /usr/bin/python3 (3.9).
    A nested-quote f-string, legal only from 3.12, passed the whole suite on
    Homebrew 3.14 and broke the deployed dashboard; this parses every script
    with the interpreter that actually runs them. Z_attachments/ is still
    parsed: the group-photo scripts ran from there until 2026-10-20, and a
    vault may carry an old copy."""
    scripts = sorted([*(REPO / "Templates" / "Scripts").glob("*.py"),
                      *(REPO / "Z_attachments").glob("*.py")])
    assert len(scripts) > 2
    code = ("import ast, sys\n"
            "bad = []\n"
            "for f in sys.argv[1:]:\n"
            "    try:\n"
            "        ast.parse(open(f, encoding='utf-8').read(), f)\n"
            "    except SyntaxError as e:\n"
            "        bad.append('%s:%s %s' % (f, e.lineno, e.msg))\n"
            "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n")
    r = subprocess.run([str(APPLE_PY), "-c", code, *map(str, scripts)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_runtime_union_annotation_without_future_import():
    """`def f() -> dict | None` parses on 3.9 but raises TypeError when the
    module loads, so the parse test above cannot see it. tag_clippings.py
    carried three such annotations unnoticed because its plist happens to
    use the venv. A script with annotations must either avoid `X | Y` in
    them or import annotations from __future__."""
    import ast
    bad = []
    for f in sorted([*(REPO / "Templates" / "Scripts").glob("*.py"),
                     *(REPO / "Z_attachments").glob("*.py")]):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names)
               for n in tree.body):
            continue
        for n in ast.walk(tree):
            anns = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                params = a.posonlyargs + a.args + a.kwonlyargs + [
                    x for x in (a.vararg, a.kwarg) if x is not None]
                anns = [x.annotation for x in params
                        if x.annotation] + ([n.returns] if n.returns else [])
            elif isinstance(n, ast.AnnAssign):
                anns = [n.annotation]
            for ann in anns:
                if any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr)
                       for x in ast.walk(ann)):
                    bad.append("%s:%d" % (f.relative_to(REPO), ann.lineno))
    assert not bad, "runtime X | Y annotation, no __future__ import: " + ", ".join(bad)


def test_a_windows_toast_starts_powershell_without_a_console(monkeypatch):
    """Each dashboard click flashed two PowerShell windows (2026-10-13)."""
    import security_common as sc
    seen = {}
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: seen.update(k))
    sc.notify("t", "m")
    assert seen["creationflags"] == 0x08000000


def test_a_refused_button_install_does_not_report_success():
    """It printed "The buttons appear on the next dashboard render" and exited
    0 after refusing inside a packaged app (Windows, 2026-10-14)."""
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    body = common[common.index("function Register-DashboardActions"):
                  common.index("function Unregister-DashboardActions")]
    assert body.count("return $false") == 3 and body.count("return $true") == 1
    inst = (WIN / "Install-DashboardActions.ps1").read_text(encoding="utf-8")
    assert "if (-not (Register-DashboardActions" in inst and "exit 1" in inst
    for caller in ("install.ps1", "update.ps1"):
        assert "$null = Register-DashboardActions" in (WIN / caller).read_text(encoding="utf-8")


def test_the_adopt_rule_allows_the_allowlist_after_a_revet():
    for text in ((WIN / "update.ps1").read_text(encoding="utf-8"),
                 (REPO / "update.sh").read_text(encoding="utf-8")):
        assert "plugin_allowlist.json" in text and "re-vetted plugins" in text


# ---------------------------------------------------------------------------
# The timestamp plugin stamped tracked docs; a .base checked out CRLF
# (Windows, 2026-10-15)
# ---------------------------------------------------------------------------

def _timestamp_ignores():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "timestamp_ignores", REPO / "installers" / "lib" / "timestamp_ignores.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin_dir(vault: Path) -> Path:
    d = vault / ".obsidian" / "plugins" / "update-time-on-edit"
    d.mkdir(parents=True)
    return d


def test_every_tracked_note_is_on_the_timestamp_ignore_list(allow_subprocess) -> None:
    """update-time-on-edit stamps any Markdown file Obsidian sees change; a
    tracked one then shows as a local change and update.ps1 refuses."""
    ti = _timestamp_ignores()
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "*.md"], check=True,
                             capture_output=True, text=True).stdout.split("\0")
    exposed = [t for t in tracked if t and not any(t.startswith(p) for p in ti.TRACKED)]
    assert exposed == []


def test_the_ignore_merge_keeps_the_users_settings(tmp_path) -> None:
    import json
    ti = _timestamp_ignores()
    d = _plugin_dir(tmp_path)
    (d / "data.json").write_text(json.dumps({"ignoreGlobalFolder": ["Templates", "Private"],
                                             "dateFormat": "yyyy", "fileHashMap": {"a.md": "x"}}))
    assert ti.main([str(tmp_path)]) == "ADDED:%d" % (len(ti.TRACKED) - 1)
    got = json.loads((d / "data.json").read_text())
    assert got["ignoreGlobalFolder"][:2] == ["Templates", "Private"]
    assert set(ti.TRACKED) <= set(got["ignoreGlobalFolder"])
    assert got["dateFormat"] == "yyyy" and got["fileHashMap"] == {"a.md": "x"}
    assert b"\r\n" not in (d / "data.json").read_bytes()
    assert ti.main([str(tmp_path)]) == "ALREADY"
    assert not (d / "data.json.tmp").exists()


def test_the_ignore_merge_handles_a_missing_or_old_settings_file(tmp_path) -> None:
    import json
    ti = _timestamp_ignores()
    assert ti.main([str(tmp_path)]) == "NO_PLUGIN"
    d = _plugin_dir(tmp_path)
    assert ti.main([str(tmp_path)]) == "ADDED:%d" % len(ti.TRACKED)
    assert json.loads((d / "data.json").read_text()) == {"ignoreGlobalFolder": ti.TRACKED}
    (d / "data.json").write_text(json.dumps({"ignoreGlobalFolder": "Templates"}))
    assert ti.main([str(tmp_path)]).startswith("ADDED:")
    assert json.loads((d / "data.json").read_text())["ignoreGlobalFolder"][0] == "Templates"
    (d / "data.json").write_text("{not json")
    assert ti.main([str(tmp_path)]) == "UNREADABLE"
    assert (d / "data.json").read_text() == "{not json"


def test_install_and_every_update_set_the_timestamp_ignores() -> None:
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    assert "function Invoke-TimestampIgnores" in common
    assert "'installers\\lib\\timestamp_ignores.py'" in common
    for caller in ("install.ps1", "update.ps1"):
        assert "Invoke-TimestampIgnores -Vault $vault -Python" in (WIN / caller).read_text(encoding="utf-8")
    assert "--only 32-timestamp-ignores" in (REPO / "update.sh").read_text(encoding="utf-8")
    comp = REPO / "installers" / "components" / "32-timestamp-ignores.sh"
    assert os.access(comp, os.X_OK) and "timestamp_ignores.py" in comp.read_text(encoding="utf-8")


def test_bases_files_check_out_with_lf(allow_subprocess) -> None:
    out = subprocess.run(["git", "-C", str(REPO), "check-attr", "eol", "--",
                          "Templates/Bases/Notes.base"], check=True,
                         capture_output=True, text=True).stdout
    assert out.strip().endswith("eol: lf"), out


def test_the_adopt_rule_allows_a_deliberate_venv_rebuild():
    for text in ((WIN / "update.ps1").read_text(encoding="utf-8"),
                 (REPO / "update.sh").read_text(encoding="utf-8")):
        assert "rebuilt the venv" in text and "pip check" in text


# ---------------------------------------------------------------------------
# Meta Bind ships (2026-10-17): its tracked settings are part of the posture
# ---------------------------------------------------------------------------

def test_meta_bind_ships_pinned_with_javascript_off_and_outside_text_excluded() -> None:
    """Meta Bind draws the Classification Review page's tier dropdown and
    Reject toggle. It renders INPUT[/VIEW[/BUTTON[ fields from note text and
    can run JavaScript, so the template ships it with JS off and excluded from
    every folder a pipeline writes outside text into; the ingest guard is the
    second line."""
    import json
    pid = "obsidian-meta-bind-plugin"
    enabled = json.loads((REPO / ".obsidian" / "community-plugins.json").read_text())
    pins = {p["id"]: p for p in json.loads((REPO / "installers" / "plugin-pins.json").read_text())}
    assert pid in enabled and pid in pins and pins[pid]["kind"] == "release"
    cfg = json.loads((REPO / ".obsidian" / "plugins" / pid / "data.json").read_text())
    assert cfg["enableJs"] is False and cfg["devMode"] is False
    assert cfg["ignoreCodeBlockRestrictions"] is False
    assert not cfg["inputFieldTemplates"] and not cfg["buttonTemplates"]
    outside_text = {"Clippings", "Creations", "Meetings", "People", "Groups",
                    "Knowledge", "Templates", "Z_archive", "Z_attachments", "Z_dashboards"}
    assert outside_text <= set(cfg["excludedFolders"])
    assert "Actions" not in cfg["excludedFolders"]      # the review page lives there


# ---------------------------------------------------------------------------
# Windows tester, 2026-10-22: 102 errors from test ids over the 32,767-char
# environment-variable cap (PYTEST_CURRENT_TEST), and a read-only .git that
# tmp cleanup could not remove. The guards, checked on any platform.
# ---------------------------------------------------------------------------

def test_a_long_parameter_gets_a_short_stable_id() -> None:
    import conftest
    short = conftest.short_param_id("[[" * 250000)
    assert short is not None and len(short) < 60
    assert short == conftest.short_param_id("[[" * 250000)       # stable
    assert short != conftest.short_param_id("[[" * 249999)       # distinct
    assert "len500000-" in short
    assert conftest.short_param_id(b"\n" * 5000).startswith("\\n\\n")
    assert conftest.short_param_id("short") is None              # pytest's own id
    assert conftest.short_param_id(12345) is None


def test_a_long_node_id_is_caught_at_collection() -> None:
    import conftest

    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid
    long = "t.py::test_x[" + "a" * 40000 + "]"
    assert conftest.long_node_ids([Item("t.py::ok"), Item(long)]) == [long]


def test_rmtree_force_removes_what_a_plain_rmtree_cannot(tmp_path: Path) -> None:
    from platform_caps import rmtree_force
    obj = tmp_path / ".git" / "objects" / "ab" / "cdef"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"x")
    obj.chmod(0o444)              # Windows: the read-only attribute git sets
    obj.parent.chmod(0o555)       # POSIX: a folder that refuses the delete
    with pytest.raises(PermissionError):
        shutil.rmtree(tmp_path / ".git")
    rmtree_force(tmp_path / ".git")
    assert not (tmp_path / ".git").exists()
