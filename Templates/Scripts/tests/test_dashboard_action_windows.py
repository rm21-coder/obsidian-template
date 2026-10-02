"""
test_dashboard_action_windows.py -- the Windows handler for the dashboard's
three buttons.

The Morning Dashboard drew its buttons on macOS only: no Windows handler for
the obsidian-dashboard:// scheme existed (2026-10-02). A registered scheme can
be fired by ANY web page, so these pin, above all, that the handler accepts
exactly three fixed actions, reads nothing else from the URL, never adopts a
security baseline, and runs each job only the way its scheduled task does.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
WIN = SCRIPTS / "windows"
_spec = importlib.util.spec_from_file_location("dashboard_action", WIN / "dashboard_action.py")
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

COMMON_PS = (WIN / "common.ps1").read_text(encoding="utf-8")
INSTALL_PS = (WIN / "install.ps1").read_text(encoding="utf-8")
UPDATE_PS = (WIN / "update.ps1").read_text(encoding="utf-8")
UNINSTALL_PS = (WIN / "uninstall.ps1").read_text(encoding="utf-8")
SCHEDULES = (WIN / "schedules.psd1").read_text(encoding="utf-8")


# ---- the URL allowlist -----------------------------------------------------

@pytest.mark.parametrize("url,action", [
    ("obsidian-dashboard://run/pull-meetings", "pull-meetings"),
    ("obsidian-dashboard://run/refresh-dashboard", "refresh-dashboard"),
    ("obsidian-dashboard://run/refresh-rag", "refresh-rag"),
    ("obsidian-dashboard://run/refresh-rag/", "refresh-rag"),        # some browsers add a slash
    ("OBSIDIAN-DASHBOARD://run/Pull-Meetings", "pull-meetings"),
])
def test_the_three_buttons_are_accepted(url: str, action: str) -> None:
    assert A.action_from_url(url) == action


@pytest.mark.parametrize("url", [
    "",
    "obsidian-dashboard://run/rebaseline-security",
    "obsidian-dashboard://run/update",
    "obsidian-dashboard://run/pull-meetings/extra",
    "obsidian-dashboard://run/pull-meetings?arg=--update",
    "obsidian-dashboard://run/pull-meetings#x",
    "obsidian-dashboard://run/pull-meetings%2F..%2F",
    "obsidian-dashboard://run/pull-meetings\n",
    " obsidian-dashboard://run/pull-meetings",
    "obsidian-dashboard://run//pull-meetings",
    "obsidian-dashboard://open/pull-meetings",
    "other-scheme://run/pull-meetings",
    "obsidian-dashboard://run/" + "a" * 500,
    "ob\u017fidian-dashboard://run/refresh-rag",           # long s folds to s without re.ASCII
    "obs\u0131dian-dashboard://run/refresh-rag",           # dotless i
    "obsidian-dashboard://run/refre\u017fh-rag",
])
def test_anything_else_is_refused(url: str) -> None:
    assert A.action_from_url(url) is None


def test_exactly_three_actions_and_none_touches_a_baseline() -> None:
    assert set(A.ACTIONS) == {"pull-meetings", "refresh-dashboard", "refresh-rag"}
    src = (WIN / "dashboard_action.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)                  # docstrings may say why
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    assert "--update" not in code and "rebaseline" not in code


def test_each_action_runs_a_scheduled_jobs_own_script() -> None:
    for task, script, args, _ in A.ACTIONS.values():
        assert re.search(rf"Name='{re.escape(task)}';\s+Script='{re.escape(script)}'", SCHEDULES), task
        assert (SCRIPTS / script).is_file(), script
        assert "--skip-if-fresh" not in args                     # a click means "pull now"


# ---- main(): what each task state leads to ---------------------------------

@pytest.fixture
def harness(monkeypatch, tmp_path):
    import script_lock
    calls = {"notify": [], "run": [], "state_asked": []}
    monkeypatch.setattr(A, "log", lambda m: None)
    monkeypatch.setattr(A, "notify", lambda t, m: calls["notify"].append((t, m)))
    monkeypatch.setattr(script_lock, "LOCK_DIR", tmp_path / ".locks")
    state = {"value": "Ready", "rc": 0}

    def fake_state(task):
        calls["state_asked"].append(task)
        return state["value"]

    def fake_run(task, script, args, timeout):
        calls["run"].append((task, script, args))
        calls.setdefault("timeout", []).append(timeout)
        if state.get("raise"):
            raise state["raise"]
        return state["rc"]

    monkeypatch.setattr(A, "task_state", fake_state)
    monkeypatch.setattr(A, "run_job", fake_run)
    monkeypatch.setattr(A, "pull_refused_today", lambda: state.get("refused"))
    return calls, state


def test_a_refused_link_runs_nothing_and_reads_no_task(harness) -> None:
    calls, _ = harness
    assert A.main(["obsidian-dashboard://run/rebaseline-security"]) == 2
    assert calls["run"] == [] and calls["state_asked"] == []
    assert "Refused" in calls["notify"][0][1]


@pytest.mark.parametrize("state,rc,marker", [
    (None, 3, "Not installed here: there is no meeting-pull scheduled task"),
    ("Disabled", 3, "Not set up on this machine: the meeting-pull job is disabled"),
    ("Running", 0, "Already running as a scheduled job"),
])
def test_a_task_that_cannot_run_now_is_reported_not_run(harness, state, rc, marker) -> None:
    calls, st = harness
    st["value"] = state
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == rc
    assert calls["run"] == []
    assert any(marker in m for _, m in calls["notify"]), calls["notify"]


def test_a_ready_task_runs_its_job_and_reports_the_result(harness) -> None:
    calls, st = harness
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == 0
    assert calls["run"] == [("meeting-pull", "meeting_pull.py", [])]
    assert [m for _, m in calls["notify"]] == ["Started.", "Finished."]

    calls["notify"].clear()
    st["rc"] = 1
    assert A.main(["obsidian-dashboard://run/refresh-rag"]) == 1
    assert calls["run"][-1] == ("rag-sync", "obsidian-rag-sync.py", [])
    assert "Failed (exit 1). See %LOCALAPPDATA%\\obsidian-logs\\rag-sync.log" in calls["notify"][-1][1]


def test_a_second_click_while_the_first_runs_is_reported(harness, tmp_path) -> None:
    import script_lock
    calls, _ = harness
    import run_logged
    held = script_lock.acquire(run_logged.job_lock_name("morning-dashboard"))
    try:
        assert A.main(["obsidian-dashboard://run/refresh-dashboard"]) == 0
    finally:
        held.close()
    assert calls["run"] == []
    assert calls["notify"][-1][1] == "Already running."


def test_messages_carry_no_double_quote() -> None:
    """Windows PowerShell 5.1 splits a -File argument containing one."""
    src = (WIN / "dashboard_action.py").read_text(encoding="utf-8")
    for msg in re.findall(r'notify\(\w+, f?"([^"]*)"', src):
        assert '"' not in msg


# ---- how state is read and a job is started --------------------------------

def test_task_state_reads_the_enum_through_powershell(monkeypatch) -> None:
    seen = {}

    class P:
        stdout = "Disabled\r\n"

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return P()

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    assert A.task_state("meeting-pull") == "Disabled"
    script = seen["cmd"][-1]
    assert "Get-ScheduledTask -TaskPath '\\Obsidian\\' -TaskName 'meeting-pull'" in script
    assert '"' not in script                                     # survives PS 5.1 argument passing
    assert seen["cmd"][0].lower().replace("/", "\\").endswith("windowspowershell\\v1.0\\powershell.exe")
    assert seen["kw"]["creationflags"] == A.CREATE_NO_WINDOW

    P.stdout = ""
    assert A.task_state("nope") is None

    def boom(cmd, **kw):
        raise OSError("no powershell")
    monkeypatch.setattr(A.subprocess, "run", boom)
    assert A.task_state("meeting-pull") is None


def test_a_job_runs_through_run_logged_with_no_window(monkeypatch) -> None:
    seen = {}

    class P:
        returncode = 7

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return P()

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    assert A.run_job("rag-sync", "obsidian-rag-sync.py", [], 99) == 7
    assert seen["cmd"] == [sys.executable, str(SCRIPTS / "run_logged.py"), "rag-sync", "obsidian-rag-sync.py"]
    assert seen["kw"]["creationflags"] == A.CREATE_NO_WINDOW
    assert seen["kw"]["cwd"] == str(SCRIPTS)
    assert seen["kw"]["env"]["RUN_LOGGED_TIMEOUT"] == "99"          # run_logged enforces it
    assert seen["kw"]["timeout"] == 99 + 300                          # only a backstop here
    assert seen["kw"]["env"]["RUN_LOGGED_LOCK_HELD"] == "rag-sync"   # it holds run_logged's lock


# ---- the registry handler and its wiring -----------------------------------

def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    return src[start:src.index("\n}\n", start)]


def test_the_handler_key_runs_the_venv_pythonw_with_the_url_only() -> None:
    assert "$DashboardSchemeKey = 'HKCU:\\Software\\Classes\\obsidian-dashboard'" in COMMON_PS
    reg = _fn(COMMON_PS, "Register-DashboardActions")
    assert "'.venv\\Scripts\\pythonw.exe'" in reg
    assert "'windows\\dashboard_action.py'" in reg
    assert """$command = '"{0}" "{1}" "%1"' -f $pyw, $handler""" in reg
    assert "-Name 'URL Protocol'" in reg
    assert "try {" in reg and "Write-Warning" in reg               # optional: never ends an update


def test_install_asks_or_follows_the_profile_like_macos_component_57() -> None:
    step = INSTALL_PS[INSTALL_PS.index("== 82 dashboard buttons =="):]
    step = step[:step.index("== 85 security baselines ==")]
    assert "Get-ProfileFlag $prof 'DASHBOARD_ACTIONS'" in step
    assert "Confirm-Optional" in step                              # unattended runs decline
    assert "if ($wantDash) {\n    Register-DashboardActions -ScriptsDir $scriptsDir" in step
    assert "macOS-only (URL-scheme handler app)" not in INSTALL_PS
    # after the venv exists (its pythonw is the handler's interpreter)
    assert INSTALL_PS.index("== 40 venv + deps ==") < INSTALL_PS.index("== 82 dashboard buttons ==")


def test_update_refreshes_only_a_handler_already_registered() -> None:
    block = UPDATE_PS[UPDATE_PS.index("if (Test-Path -LiteralPath $DashboardSchemeKey) {"):]
    block = block[:block.index("\n}\n")]
    assert "Register-DashboardActions -ScriptsDir $scriptsDir" in block
    assert UPDATE_PS.count("Register-DashboardActions") == 1       # never added unasked
    assert UPDATE_PS.index("Update INCOMPLETE") < UPDATE_PS.index("Register-DashboardActions")


def test_opting_in_later_and_uninstalling_use_one_script() -> None:
    src = (WIN / "Install-DashboardActions.ps1").read_text(encoding="utf-8")
    assert "if ($Remove) { Unregister-DashboardActions; return }" in src
    assert "Register-DashboardActions -ScriptsDir (Get-ScriptsDir)" in src
    assert "& (Join-Path $PSScriptRoot 'Install-DashboardActions.ps1') -Remove" in UNINSTALL_PS


def test_powershell_files_stay_ascii() -> None:
    for text in (COMMON_PS, INSTALL_PS, UPDATE_PS, UNINSTALL_PS,
                 (WIN / "Install-DashboardActions.ps1").read_text(encoding="utf-8")):
        text.encode("ascii")


# ---- the dashboard draws the buttons only when the handler exists ----------

def _fake_winreg(command: str | None):
    m = types.ModuleType("winreg")
    m.HKEY_CURRENT_USER = object()

    class Key:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def open_key(root, path):
        assert path == r"Software\Classes\obsidian-dashboard\shell\open\command"
        if command is None:
            raise OSError("no key")
        return Key()

    m.OpenKey = open_key
    m.QueryValueEx = lambda k, name: (command, 1)
    return m


def test_dashboard_finds_only_this_vaults_handler(monkeypatch, tmp_path) -> None:
    import morning_dashboard as md
    ours = SCRIPTS / "windows" / "dashboard_action.py"
    pyw = tmp_path / "pythonw.exe"
    pyw.write_text("", encoding="utf-8")
    other = tmp_path / "windows" / "dashboard_action.py"            # another clone's
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")

    def key(cmd):
        monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(cmd))

    key(f'"{pyw}" "{ours}" "%1"')
    assert md.dashboard_actions_available() is True
    key(f'"{pyw}" "{other}" "%1"')
    assert md.dashboard_actions_available() is False             # a second clone's handler
    key(f'"{tmp_path / "gone.exe"}" "{ours}" "%1"')
    assert md.dashboard_actions_available() is False             # interpreter is gone
    key(f'"{ours}" "%1"')
    assert md.dashboard_actions_available() is False             # not interpreter + script
    key(None)
    assert md.dashboard_actions_available() is False


# ---- review round 1 (2026-10-02) -------------------------------------------

def test_more_than_one_argument_is_refused(harness) -> None:
    """Quotes in a link split it on the way to argv; the parts are refused,
    not read past the first."""
    calls, _ = harness
    assert A.main(["obsidian-dashboard://run/refresh-rag", "-c", "import os"]) == 2
    assert calls["run"] == [] and calls["state_asked"] == []


def test_a_second_click_within_the_cooldown_does_not_run(harness, monkeypatch) -> None:
    calls, _ = harness
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(A.time, "time", lambda: clock["t"])
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == 0
    clock["t"] += 60
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == 0
    assert len(calls["run"]) == 1
    assert "Ran moments ago; try again in 9 min." in calls["notify"][-1][1]
    clock["t"] += A.COOLDOWN_SEC["pull-meetings"]
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == 0
    assert len(calls["run"]) == 2


def test_a_pull_refused_today_is_not_retried_from_the_dashboard(harness) -> None:
    calls, st = harness
    st["refused"] = "org-policy"
    assert A.main(["obsidian-dashboard://run/pull-meetings"]) == 3
    assert calls["run"] == []
    assert "Not retrying: the Claude sign-in was refused earlier today" in calls["notify"][-1][1]


def test_a_run_past_its_time_limit_is_reported(harness) -> None:
    calls, st = harness
    st["rc"] = 124                                               # run_logged stopped it
    assert A.main(["obsidian-dashboard://run/refresh-dashboard"]) == 124
    assert calls["timeout"] == [A.TIMEOUT_SEC["refresh-dashboard"]]
    assert "Stopped: it ran past 15 min" in calls["notify"][-1][1]


def test_an_internal_failure_is_logged_and_toasted(harness, monkeypatch) -> None:
    calls, st = harness
    logged = []
    monkeypatch.setattr(A, "log", logged.append)
    st["raise"] = OSError("cannot start pythonw")
    assert A.main(["obsidian-dashboard://run/refresh-rag"]) == 1
    assert any("handler failed" in m and "cannot start pythonw" in m for m in logged)
    assert "Failed (internal error)" in calls["notify"][-1][1]


def test_run_logged_skips_a_job_whose_lock_is_held(tmp_path, monkeypatch, allow_subprocess) -> None:
    import run_logged
    import script_lock
    monkeypatch.setattr(script_lock, "LOCK_DIR", tmp_path / ".locks")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(run_logged.LOCK_HELD_ENV, raising=False)
    job = tmp_path / "job.py"
    marker = tmp_path / "ran"
    job.write_text(f"open(r'{marker}', 'w').write('x')\n", encoding="utf-8")

    held = script_lock.acquire(run_logged.job_lock_name("demo"))
    try:
        assert run_logged.main(["demo", str(job)]) == 0
        assert not marker.exists()                                   # a scheduled run stands aside
        assert "demo is already running; not starting a second copy" in \
            run_logged.log_path("demo").read_text(encoding="utf-8")
        monkeypatch.setenv(run_logged.LOCK_HELD_ENV, "demo")         # the click that holds it
        assert run_logged.main(["demo", str(job)]) == 0
        assert marker.exists()
    finally:
        held.close()


def test_registering_starts_from_an_empty_key_and_removal_never_aborts() -> None:
    reg = _fn(COMMON_PS, "Register-DashboardActions")
    assert reg.index("Remove-Item -LiteralPath $DashboardSchemeKey -Recurse") < \
        reg.index('New-Item -Path "$DashboardSchemeKey\\shell\\open\\command"')
    unreg = _fn(COMMON_PS, "Unregister-DashboardActions")
    assert "try {" in unreg and "Write-Warning" in unreg


# ---- review round 2 (2026-10-02) -------------------------------------------

def test_try_acquire_tells_busy_from_broken(tmp_path) -> None:
    import script_lock
    held = script_lock.try_acquire("x", dir=tmp_path / "ok")
    assert held[0] is not None and held[1] is None
    try:
        assert script_lock.try_acquire("x", dir=tmp_path / "ok") == (None, None)   # busy
    finally:
        held[0].close()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    handle, error = script_lock.try_acquire("x", dir=blocker)
    assert handle is None and "could not create lock directory" in error


def test_a_broken_lock_directory_does_not_stop_a_job(tmp_path, monkeypatch, allow_subprocess) -> None:
    """Every Windows job runs through run_logged: a lock it cannot even try
    must not read as "already running" and skip them all with exit 0."""
    import run_logged
    import script_lock
    blocker = tmp_path / ".locks"
    blocker.write_text("", encoding="utf-8")                     # a file where the folder goes
    monkeypatch.setattr(script_lock, "LOCK_DIR", blocker)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(run_logged.LOCK_HELD_ENV, raising=False)
    job = tmp_path / "job.py"
    marker = tmp_path / "ran"
    job.write_text(f"open(r'{marker}', 'w').write('x')\n", encoding="utf-8")
    assert run_logged.main(["demo", str(job)]) == 0
    assert marker.exists()                                       # it ran
    text = run_logged.log_path("demo").read_text(encoding="utf-8")
    assert "running demo without its single-run guard" in text
    assert "already running" not in text


def test_a_time_limit_stops_the_job_and_its_children(tmp_path, monkeypatch, allow_subprocess, capsys) -> None:
    import time as _time
    import run_logged
    import script_lock
    monkeypatch.setattr(script_lock, "LOCK_DIR", tmp_path / ".locks")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv(run_logged.TIMEOUT_ENV, "1")
    monkeypatch.delenv(run_logged.LOCK_HELD_ENV, raising=False)
    late = tmp_path / "grandchild-finished"
    job = tmp_path / "job.py"
    job.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(3); open(r'{late}', 'w')\"])\n"
        "time.sleep(30)\n", encoding="utf-8")
    t0 = _time.time()
    assert run_logged.main(["demo", str(job)]) == run_logged.TIMED_OUT
    assert _time.time() - t0 < 20
    _time.sleep(4)
    assert not late.exists()                                     # the grandchild was stopped too
    assert "demo ran past 1s; stopped it and its children" in capsys.readouterr().err
    assert "demo ran past 1s; stopped it and its children" in \
        run_logged.log_path("demo").read_text(encoding="utf-8")       # where the toast points
    assert run_logged.TASKKILL_EXE.lower().replace("/", "\\").endswith("system32\\taskkill.exe")


def test_a_click_refuses_when_its_lock_cannot_be_tried(harness, monkeypatch) -> None:
    import script_lock
    calls, _ = harness
    monkeypatch.setattr(script_lock, "try_acquire", lambda name, **kw: (None, "could not open lock file X"))
    assert A.main(["obsidian-dashboard://run/refresh-rag"]) == 1
    assert calls["run"] == []
    assert "Failed: could not take its run lock" in calls["notify"][-1][1]
