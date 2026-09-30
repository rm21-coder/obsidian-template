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
        assert """$argString = '"{0}" {1} "{2}"' -f $runner, $job.Name, $scriptPath""" in src, (
            "Register-Tasks.ps1 no longer routes the task through run_logged.py; "
            "Task Scheduler will discard the job's output again")
        assert "New-ScheduledTaskAction -Execute $python -Argument $argString" in src

    def test_every_manifest_name_is_a_valid_log_name(self) -> None:
        names = _manifest_names()
        assert len(names) == 15, names
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
