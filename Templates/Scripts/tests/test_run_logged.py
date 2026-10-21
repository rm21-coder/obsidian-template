"""
test_run_logged.py -- the wrapper every Windows scheduled task runs through.

Task Scheduler discards a job's stdout and stderr, so before run_logged.py no
Windows job left a log anywhere. These tests run the wrapper for real (a child
python process) against a throwaway LOCALAPPDATA, because the behaviour under
test is exactly what lands in the file.
"""
from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

import run_logged as rl

SCRIPTS = Path(rl.__file__).resolve().parent
WINDOWS = SCRIPTS / "windows"


@pytest.fixture(autouse=True)
def _restore_bytecode_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() points this process's bytecode cache outside the vault; undo it
    after each test so the rest of the suite is unaffected."""
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(sys, "pycache_prefix", sys.pycache_prefix)


@pytest.fixture
def localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "AppData" / "Local"
    base.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    return base


def _job(tmp_path: Path, body: str) -> str:
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(script)


class TestTheJobsOutputIsKept:

    def test_stdout_and_stderr_land_in_the_job_log_in_order(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, """\
            import sys
            print("first, on stdout")
            print("second, on stderr", file=sys.stderr)
            print("third, on stdout")
        """)
        assert rl.run("demo", script, []) == 0
        text = (localappdata / "obsidian-logs" / "demo.log").read_text(encoding="utf-8")
        # Unbuffered child output is what keeps these in write order; with
        # block-buffered stdout the stderr line would arrive first.
        assert text.splitlines() == ["first, on stdout", "second, on stderr",
                                     "third, on stdout"]

    def test_runs_append_rather_than_overwrite(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, 'print("tick")\n')
        rl.run("demo", script, [])
        rl.run("demo", script, [])
        assert (localappdata / "obsidian-logs" / "demo.log").read_text(
            encoding="utf-8").splitlines() == ["tick", "tick"]

    def test_the_jobs_own_args_are_passed_through(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, 'import sys; print("argv:", sys.argv[1:])\n')
        rl.main(["demo", script, "--once", "--exit-zero"])
        assert "argv: ['--once', '--exit-zero']" in (
            localappdata / "obsidian-logs" / "demo.log").read_text(encoding="utf-8")

    def test_non_ascii_output_is_written_as_utf8(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        # On Windows a file-redirected stdout defaults to the ANSI code page,
        # where this print raises UnicodeEncodeError and kills the job.
        script = _job(tmp_path, 'print("caf\\u00e9 \\u2014 \\u2713")\n')
        assert rl.run("demo", script, []) == 0
        raw = (localappdata / "obsidian-logs" / "demo.log").read_bytes()
        assert raw.decode("utf-8").strip() == "caf\u00e9 \u2014 \u2713"

    def test_child_is_told_to_write_utf8_unbuffered(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        env = rl.child_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_an_explicit_encoding_setting_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8:backslashreplace")
        assert rl.child_env()["PYTHONIOENCODING"] == "utf-8:backslashreplace"


class TestTheExitCodeIsTheJobs:
    """LastTaskResult, and the dashboard's pipeline health, read this."""

    def test_a_failure_is_passed_through_and_dated_in_the_log(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, """\
            import sys
            print("about to fail", file=sys.stderr)
            sys.exit(3)
        """)
        assert rl.run("demo", script, []) == 3
        lines = (localappdata / "obsidian-logs" / "demo.log").read_text(
            encoding="utf-8").splitlines()
        assert lines[0] == "about to fail"
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d run_logged: demo exited with code 3",
            lines[1]), lines

    def test_a_crash_is_passed_through(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, 'raise RuntimeError("boom in the job")\n')
        assert rl.run("demo", script, []) == 1
        text = (localappdata / "obsidian-logs" / "demo.log").read_text(encoding="utf-8")
        assert "RuntimeError: boom in the job" in text
        assert "run_logged: demo exited with code 1" in text

    def test_a_clean_run_adds_nothing_of_its_own(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        script = _job(tmp_path, 'print("done")\n')
        assert rl.run("demo", script, []) == 0
        assert (localappdata / "obsidian-logs" / "demo.log").read_text(
            encoding="utf-8") == "done\n"

    def test_a_windows_crash_code_survives_sys_exit(self, monkeypatch) -> None:
        # 0xC0000005, as GetExitCodeProcess reports it: unsigned.
        monkeypatch.setattr(rl.sys, "platform", "win32")
        assert rl.exit_status(0xC0000005) == -1073741819
        assert rl.exit_status(-1073741819) & 0xFFFFFFFF == 0xC0000005
        assert rl.exit_status(2) == 2

    def test_posix_codes_are_untouched(self, monkeypatch) -> None:
        monkeypatch.setattr(rl.sys, "platform", "darwin")
        assert rl.exit_status(-9) == -9
        assert rl.exit_status(0xC0000005) == 0xC0000005


class TestTheLogNeverStopsTheJob:

    def test_an_unopenable_log_still_runs_the_job(
            self, tmp_path, localappdata, allow_subprocess, capsys) -> None:
        # A file where the log directory should be: mkdir fails.
        (localappdata / "obsidian-logs").write_text("not a directory")
        ran = tmp_path / "ran"
        script = _job(tmp_path, f"""\
            import sys
            open({str(ran)!r}, "w").write("yes")
            sys.exit(4)
        """)
        assert rl.run("demo", script, []) == 4
        assert ran.read_text() == "yes"
        err = capsys.readouterr().err
        assert "run_logged: cannot open" in err
        assert "running demo without a log" in err


class TestRotation:

    def test_a_full_log_moves_aside_before_the_run(
            self, tmp_path, localappdata, allow_subprocess, monkeypatch) -> None:
        monkeypatch.setattr(rl, "LOG_MAX_BYTES", 100)
        log = localappdata / "obsidian-logs" / "demo.log"
        log.parent.mkdir()
        log.write_text("old\n" * 50)
        rl.run("demo", _job(tmp_path, 'print("new")\n'), [])
        assert log.read_text() == "new\n"
        assert (log.parent / "demo.log.1").read_text() == "old\n" * 50

    def test_only_one_old_generation_is_kept(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(rl, "LOG_MAX_BYTES", 10)
        log = tmp_path / "demo.log"
        (tmp_path / "demo.log.1").write_text("oldest")
        log.write_text("x" * 20)
        rl.rotate(log)
        assert (tmp_path / "demo.log.1").read_text() == "x" * 20
        assert not log.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.log.1"]

    def test_a_log_under_the_cap_is_left_alone(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(rl, "LOG_MAX_BYTES", 100)
        log = tmp_path / "demo.log"
        log.write_text("small")
        rl.rotate(log)
        assert log.read_text() == "small"
        assert not (tmp_path / "demo.log.1").exists()

    def test_a_missing_log_is_not_an_error(self, tmp_path) -> None:
        rl.rotate(tmp_path / "never-written.log")     # must not raise


class TestWhereTheLogGoes:

    def test_it_is_under_localappdata(self, localappdata) -> None:
        assert rl.log_path("tag-clippings") == (
            localappdata / "obsidian-logs" / "tag-clippings.log")

    def test_it_is_not_inside_the_watched_security_dir(self, localappdata) -> None:
        # The integrity monitor watches obsidian-security; a log growing there
        # would raise a drift alert on every run.
        assert "obsidian-security" not in rl.log_path("security-integrity").parts

    def test_the_display_form_names_the_same_file(self) -> None:
        assert rl.display_path("vault-lint") == r"%LOCALAPPDATA%\obsidian-logs\vault-lint.log"

    @pytest.mark.parametrize("bad", ["../escape", "..", "a/b", "a\\b", "", "-x"])
    def test_a_job_name_that_is_not_a_plain_filename_is_refused(
            self, bad, localappdata, capsys) -> None:
        assert rl.main([bad, "job.py"]) == 2
        assert f"run_logged: refusing job name {bad!r}" in capsys.readouterr().err
        assert not (localappdata / "obsidian-logs").exists()

    def test_too_few_arguments_is_a_usage_error(self, capsys) -> None:
        assert rl.main(["demo"]) == 2
        assert "usage: run_logged.py <job-name> <script.py>" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The PowerShell side. There is no PowerShell in the suite, so these are source
# guards, like the ones in test_static.py.
# ---------------------------------------------------------------------------

def _manifest_names() -> list[str]:
    text = (WINDOWS / "schedules.psd1").read_text(encoding="utf-8")
    return re.findall(r"Name='([^']+)'", text)


class TestEveryTaskRunsThroughTheWrapper:

    def test_the_task_action_invokes_run_logged_with_the_task_name(self) -> None:
        src = (WINDOWS / "Register-Tasks.ps1").read_text(encoding="utf-8")
        assert "$runner     = Join-Path $scriptsDir 'run_logged.py'" in src
        # The security tasks use the installed run_logged.py instead (5278112).
        assert "$jobRunner  = $runner" in src
        assert """$argString = '{0}"{1}" --timeout {2} {3} "{4}"' -f $jobFlags, $jobRunner, ([Math]::Max(60, $limitMin * 60 - 120)), $job.Name, $scriptPath""" in src, (
            "Register-Tasks.ps1 no longer routes the task through run_logged.py; "
            "Task Scheduler will discard the job's output again")
        assert "New-ScheduledTaskAction -Execute $jobPython -Argument $argString" in src

    def test_every_manifest_name_is_a_valid_log_name(self) -> None:
        names = _manifest_names()
        assert len(names) == 16, names  # handoff-blob-pull removed 2026-09-30; email-tasks, classify added 2026-10-18
        bad = [n for n in names if not rl._JOB_NAME.match(n)]
        assert not bad, f"run_logged.py would refuse these task names: {bad}"

    def test_a_refresh_never_disables_an_enabled_task(self) -> None:
        # Re-running Register-Tasks.ps1 is how an existing install picks up the
        # wrapper. It used to re-disable every Enabled=$false job on the way,
        # including meeting-pull on a machine where it had been turned on.
        src = (WINDOWS / "Register-Tasks.ps1").read_text(encoding="utf-8")
        assert "$enable   = $job.Enabled -or ($existing -and $existing.State -ne 'Disabled')" in src
        lines = [ln.strip() for ln in src.splitlines()]
        guard = lines[lines.index(next(ln for ln in lines
                                       if ln.startswith("Disable-ScheduledTask"))) - 1]
        assert guard == "if (-not $enable) {", (
            f"Disable-ScheduledTask is guarded by {guard!r}, not by the "
            "refresh-aware $enable")

    def test_uninstall_removes_the_logs(self) -> None:
        src = (WINDOWS / "uninstall.ps1").read_text(encoding="utf-8")
        line = next(ln for ln in src.splitlines()
                    if ln.startswith("foreach ($d in 'obsidian-security'"))
        assert "'obsidian-logs'" in line


class TestNoConsoleWindow:
    """A job firing must not open a window in the user's session.

    The windowless behaviour itself can only be seen on Windows; these pin the
    three things that produce it, so a refactor cannot quietly undo one.
    """

    @staticmethod
    def _capture_run(monkeypatch) -> list[dict]:
        calls: list[dict] = []

        class _Done:
            returncode = 0

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            return _Done()
        monkeypatch.setattr(rl.subprocess, "run", fake_run)
        return calls

    def test_on_windows_the_job_starts_under_a_hidden_console(
            self, tmp_path, localappdata, monkeypatch) -> None:
        calls = self._capture_run(monkeypatch)
        monkeypatch.setattr(rl.sys, "platform", "win32")
        rl.run("demo", "job.py", [])
        assert calls[0]["creationflags"] == 0x08000000   # CREATE_NO_WINDOW

    def test_even_the_no_log_fallback_starts_it_hidden(
            self, tmp_path, monkeypatch) -> None:
        calls = self._capture_run(monkeypatch)
        monkeypatch.setattr(rl.sys, "platform", "win32")
        blocker = tmp_path / "file-not-dir"
        blocker.write_text("x")
        monkeypatch.setenv("LOCALAPPDATA", str(blocker))   # log dir cannot exist
        rl.run("demo", "job.py", [])
        assert calls[0]["creationflags"] == 0x08000000
        assert "stdout" not in calls[0]

    def test_elsewhere_no_windows_only_flag_is_passed(
            self, localappdata, monkeypatch) -> None:
        calls = self._capture_run(monkeypatch)
        monkeypatch.setattr(rl.sys, "platform", "darwin")
        rl.run("demo", "job.py", [])
        assert "creationflags" not in calls[0]

    def test_under_pythonw_the_job_gets_the_console_interpreter(
            self, tmp_path, monkeypatch) -> None:
        # A job under pythonw.exe has no console, so every console program it
        # starts (claude CLI, PowerShell) would open a visible window instead
        # of inheriting a hidden one.
        venv = tmp_path / "Scripts"
        venv.mkdir()
        (venv / "pythonw.exe").write_text("")
        (venv / "python.exe").write_text("")
        monkeypatch.setattr(rl.sys, "platform", "win32")
        monkeypatch.setattr(rl.sys, "executable", str(venv / "pythonw.exe"))
        assert rl.child_command("job.py", ["--once"]) == [
            str(venv / "python.exe"), "job.py", "--once"]

    def test_without_a_console_interpreter_pythonw_still_runs_the_job(
            self, tmp_path, monkeypatch) -> None:
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(rl.sys, "platform", "win32")
        monkeypatch.setattr(rl.sys, "executable", str(tmp_path / "pythonw.exe"))
        assert rl.child_command("job.py", [])[0] == str(tmp_path / "pythonw.exe")

    def test_the_task_launches_the_windowless_interpreter(self) -> None:
        src = (WINDOWS / "Register-Tasks.ps1").read_text(encoding="utf-8")
        assert "$python     = Get-VenvPythonW" in src, (
            "Register-Tasks.ps1 launches the console python.exe again; every "
            "job firing will open a console window")
        common = (WINDOWS / "common.ps1").read_text(encoding="utf-8")
        body = common[common.index("function Get-VenvPythonW"):]
        assert ".venv\\Scripts\\pythonw.exe" in body.split("}")[0]


class TestRunnerDiagnosticsSurviveWithoutStderr:
    """Under pythonw.exe sys.stderr is None; print() to it vanishes."""

    def test_a_refused_job_name_is_recorded_in_the_fallback_file(
            self, localappdata, monkeypatch) -> None:
        monkeypatch.setattr(rl.sys, "stderr", None)
        assert rl.main(["../escape", "job.py"]) == 2
        text = (localappdata / "obsidian-logs" / "run_logged.log").read_text(
            encoding="utf-8")
        assert "run_logged: refusing job name '../escape'" in text

    def test_a_usage_error_is_recorded_in_the_fallback_file(
            self, localappdata, monkeypatch) -> None:
        monkeypatch.setattr(rl.sys, "stderr", None)
        assert rl.main(["only-one"]) == 2
        assert "usage: run_logged.py <job-name>" in (
            localappdata / "obsidian-logs" / "run_logged.log").read_text(encoding="utf-8")

    def test_with_stderr_present_nothing_is_written_to_the_file(
            self, localappdata, capsys) -> None:
        assert rl.main(["../escape", "job.py"]) == 2
        assert "refusing job name" in capsys.readouterr().err
        assert not (localappdata / "obsidian-logs" / "run_logged.log").exists()


class TestRegistrationFailuresAreLoud:
    """Register-Tasks.ps1 is how an install receives a fix. It once printed
    "registered" for 15 tasks it had been denied, and exited 0."""

    @staticmethod
    def _src() -> str:
        return (WINDOWS / "Register-Tasks.ps1").read_text(encoding="utf-8")

    def test_each_task_cmdlet_stops_on_error(self) -> None:
        # The ScheduledTasks cmdlets ignore the script's $ErrorActionPreference
        # (CDXML module scope); only a per-call -ErrorAction Stop reaches them.
        src = self._src()
        reg = src[src.index("Register-ScheduledTask -TaskName"):]
        reg = reg[:reg.index("| Out-Null")]
        assert "-ErrorAction Stop" in reg
        dis = next(ln for ln in src.splitlines()
                   if ln.strip().startswith("Disable-ScheduledTask"))
        assert "-ErrorAction Stop" in dis

    def test_success_is_printed_only_after_reading_the_task_back(self) -> None:
        src = self._src()
        readback = src.index("$live.Actions[0].Execute -eq $jobPython")
        assert "$live.Actions[0].Arguments -eq $argString" in src
        for line in ('Write-Host ("  registered (DISABLED): {0}"',
                     'Write-Host ("  registered (ENABLED):  {0}  (kept:',
                     'Write-Host ("  registered (ENABLED):  {0}" -f'):
            assert src.index(line) > readback, f"{line!r} printed before the read-back"

    def test_any_failure_ends_the_run_non_zero_with_the_elevation_hint(self) -> None:
        src = self._src()
        tail = src[src.index("if ($failed.Count -gt 0) {"):]
        failure_block = tail[:tail.index("exit 0")]
        assert "exit 1" in failure_block
        assert "Run as administrator" in tail
        assert src.rstrip().endswith("exit 0"), (
            "success must exit 0 explicitly, or a caller reads a stale $LASTEXITCODE")

    def test_the_installer_stops_when_registration_fails(self) -> None:
        src = (WINDOWS / "install.ps1").read_text(encoding="utf-8")
        call = src.index("& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')")
        after = src[call:call + 300]
        assert "if ($LASTEXITCODE -ne 0) {" in after
        assert "throw 'scheduled-task registration failed" in after


# ---------------------------------------------------------------------------
# Scheduled runs: run_logged's own limit, and a stack dump before it (2026-10-13)
# ---------------------------------------------------------------------------

class TestScheduledTimeLimit:
    """A hung Windows tagger outlived Task Scheduler's stop as orphans: that
    stop ends only this wrapper. The wrapper's own --timeout fires first,
    stops the whole tree, and the job has dumped its stacks by then."""

    def test_timeout_flag_is_parsed_and_wins(self, monkeypatch) -> None:
        seen = {}
        monkeypatch.setattr(rl, "run", lambda job, script, rest: seen.update(
            job=job, script=script, rest=rest, limit=rl._timeout()) or 0)
        monkeypatch.setattr(rl, "_ARG_TIMEOUT", [])
        monkeypatch.setenv(rl.TIMEOUT_ENV, "999")
        monkeypatch.setenv(rl.LOCK_HELD_ENV, "tag-clippings")
        assert rl.main(["--timeout", "3480", "tag-clippings", "tag_clippings.py", "--x"]) == 0
        assert seen == {"job": "tag-clippings", "script": "tag_clippings.py",
                        "rest": ["--x"], "limit": 3480.0}

    @pytest.mark.parametrize("argv", [["--timeout"], ["--timeout", "soon", "j", "s.py"],
                                      ["--timeout", "60", "only-job"]])
    def test_a_bad_timeout_is_a_usage_error(self, argv, monkeypatch) -> None:
        monkeypatch.setattr(rl, "_ARG_TIMEOUT", [])
        assert rl.main(argv) == 2

    def test_with_a_limit_the_job_starts_through_job_boot(self) -> None:
        cmd = rl.child_command("job.py", ["--once"], rl.dump_after(3480))
        assert Path(cmd[1]).name == "job_boot.py"
        assert cmd[2:] == ["3420", "job.py", "--once"]

    def test_without_a_limit_the_job_starts_directly(self) -> None:
        assert rl.child_command("job.py", ["--once"], rl.dump_after(None))[1:] == ["job.py", "--once"]


class TestIsolationIsPassedOn:
    """5278112: the Windows security tasks start run_logged.py with -I -S from
    an installed copy; the control it starts must run the same way."""

    def _in_child(self, flags: list, code: str) -> str:
        import subprocess
        scripts = Path(rl.__file__).resolve().parent
        prog = f"import sys; sys.path.append({str(scripts)!r}); " + code
        # -B: -I ignores PYTHONDONTWRITEBYTECODE, and the import would
        # otherwise leave run_logged's .pyc in the checkout's __pycache__,
        # which the integrity monitor's tests then report as drift.
        return subprocess.run([sys.executable, "-B", *flags, "-c", prog], capture_output=True,
                              text=True, check=True, timeout=60).stdout.strip()

    def test_an_isolated_runner_starts_an_isolated_job(self, allow_subprocess) -> None:
        out = self._in_child(["-I", "-S"], "import run_logged; print(run_logged.child_command('c.py', ['--x'])[1:])")
        assert out == "['-I', '-u', '-S', 'c.py', '--x']"

    def test_an_ordinary_runner_adds_nothing(self, allow_subprocess) -> None:
        out = self._in_child([], "import run_logged; print(run_logged.child_command('c.py', [])[1:])")
        assert out == "['c.py']"

    def test_job_boot_leaves_an_isolated_path_alone(self, tmp_path, allow_subprocess) -> None:
        import subprocess
        script = tmp_path / "job.py"
        script.write_text("import sys\nprint(sys.path[0])\n", encoding="utf-8")
        boot = Path(rl.__file__).resolve().with_name("job_boot.py")
        out = subprocess.run([sys.executable, "-I", str(boot), "0", str(script)],
                             capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        assert out != str(tmp_path)
        out = subprocess.run([sys.executable, str(boot), "0", str(script)],
                             capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        assert out == str(tmp_path.resolve())

    def test_a_hung_job_dumps_its_stack_into_the_log_before_it_is_stopped(
            self, tmp_path, monkeypatch, allow_subprocess) -> None:
        """The whole path: run_logged --timeout, job_boot's dump, the stop."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
        script = tmp_path / "hang.py"
        script.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n",
                          encoding="utf-8")
        monkeypatch.setattr(rl, "_ARG_TIMEOUT", [])
        monkeypatch.setattr(rl, "DUMP_LEAD_SEC", 2.0)
        monkeypatch.setenv(rl.LOCK_HELD_ENV, "hang-demo")
        import time as _time
        t0 = _time.monotonic()
        assert rl.main(["--timeout", "6", "hang-demo", str(script)]) == rl.TIMED_OUT
        assert _time.monotonic() - t0 < 30
        log = rl.log_path("hang-demo").read_text(encoding="utf-8")
        assert "started" in log
        assert "most recent call first" in log           # faulthandler's dump header
        assert "hang.py" in log
        assert "ran past 6s; stopped it and its children" in log


class TestBytecodeStaysOutOfTheVault:
    """5278114: Python reads <script dir>/__pycache__ before the source, and
    the scripts live in the synced vault. An unchecked-hash .pyc planted
    there ran in place of the reviewed .py."""

    def test_the_child_neither_writes_nor_reads_beside_the_scripts(
            self, monkeypatch) -> None:
        # A prefix pointing back into the vault must not survive.
        monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(SCRIPTS / "__pycache__"))
        monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
        env = rl.child_env()
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        prefix = Path(env["PYTHONPYCACHEPREFIX"])
        assert prefix == rl.pycache_prefix()
        assert SCRIPTS not in prefix.parents and prefix != SCRIPTS
        assert prefix.parts[-2:] == ("obsidian-template", "pycache")

    def test_on_windows_it_is_under_localappdata(self, localappdata, monkeypatch) -> None:
        monkeypatch.setattr(rl.sys, "platform", "win32")
        assert rl.pycache_prefix() == localappdata / "obsidian-template" / "pycache"

    def test_keep_bytecode_out_of_the_vault_sets_this_process(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "dont_write_bytecode", False)
        monkeypatch.setattr(sys, "pycache_prefix", None)
        rl.keep_bytecode_out_of_the_vault()
        assert sys.dont_write_bytecode is True
        assert sys.pycache_prefix == str(rl.pycache_prefix())

    def test_the_runner_itself_ignores_a_planted_script_lock_pyc(
            self, tmp_path, localappdata, allow_subprocess) -> None:
        """Task Scheduler starts run_logged.py from the vault with no PYTHON*
        settings, and it imports script_lock from there."""
        import py_compile
        import shutil
        import subprocess as sp

        vault = tmp_path / "vault"
        vault.mkdir()
        for name in ("run_logged.py", "script_lock.py", "job_boot.py"):
            shutil.copy2(SCRIPTS / name, vault / name)
        evil = tmp_path / "evil.py"
        evil.write_text("raise SystemExit(99)\n", encoding="utf-8")
        tag = sys.implementation.cache_tag
        py_compile.compile(str(evil), cfile=str(vault / "__pycache__" / f"script_lock.{tag}.pyc"),
                           doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        job = _job(tmp_path, 'print("ok")\n')
        env = {k: v for k, v in rl.child_env().items() if not k.startswith("PYTHON")}
        env["LOCALAPPDATA"] = str(localappdata)
        r = sp.run([sys.executable, str(vault / "run_logged.py"), "demo", job],
                   env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert sorted(p.name for p in (vault / "__pycache__").iterdir()) == [
            f"script_lock.{tag}.pyc"], "the runner wrote bytecode into the vault"

    def test_a_planted_pyc_is_not_loaded(
            self, tmp_path, localappdata, monkeypatch, allow_subprocess) -> None:
        import importlib.util
        import py_compile
        import subprocess as sp

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "helper.py").write_text('WHO = "reviewed source"\n', encoding="utf-8")
        evil = tmp_path / "evil.py"
        evil.write_text('WHO = "planted bytecode"\n', encoding="utf-8")
        # The plant goes where a plain interpreter looks: beside the source
        # (cache_from_source would honour this process's own prefix).
        tag = sys.implementation.cache_tag
        cfile = str(vault / "__pycache__" / f"helper.{tag}.pyc")
        py_compile.compile(str(evil), cfile=cfile, doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        job = vault / "job.py"
        job.write_text("import helper\nprint(helper.WHO)\n", encoding="utf-8")

        # Control: without the runner's environment the plant is loaded, so
        # the assertion below is about the fix and not about a dud plant.
        plain = {k: v for k, v in rl.child_env().items()
                 if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        out = sp.run([sys.executable, str(job)], env=plain, capture_output=True,
                     text=True, check=True).stdout
        assert out.strip() == "planted bytecode"

        monkeypatch.setattr(rl, "pycache_prefix", lambda: tmp_path / "cache")
        assert rl.run("demo", str(job), []) == 0
        text = (localappdata / "obsidian-logs" / "demo.log").read_text(encoding="utf-8")
        assert text.strip() == "reviewed source"
        assert not (tmp_path / "cache").exists(), "bytecode was written"
