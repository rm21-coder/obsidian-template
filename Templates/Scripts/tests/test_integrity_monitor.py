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
from pathlib import Path

import pytest

import integrity_monitor as im


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
