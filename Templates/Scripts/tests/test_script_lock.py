"""
test_script_lock.py — the single-instance lock shared by every scheduled job.

Worth testing carefully despite being ~60 lines: five scheduled jobs depend on
it, and both of its failure directions are silent. A lock that never excludes
lets two runs duplicate work (double-clipped articles, double-transcribed
episodes); a lock that never releases wedges a pipeline until someone deletes a
file by hand, and the job keeps exiting 0 while doing nothing.

Real files and real flock, not mocks: the whole point of the module is the OS
behaviour, so faking it would test nothing that matters.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import script_lock


class TestAcquire:

    def test_acquires_then_excludes_then_releases(self, tmp_path: Path) -> None:
        first = script_lock.acquire("job", dir=tmp_path)
        assert first is not None
        try:
            assert script_lock.acquire("job", dir=tmp_path) is None
        finally:
            first.close()
        # Release must actually free it, or one crashed run wedges the job.
        again = script_lock.acquire("job", dir=tmp_path)
        assert again is not None
        again.close()

    def test_distinct_names_do_not_block_each_other(self,
                                                    tmp_path: Path) -> None:
        """Every job shares one .locks/ directory, so a bug collapsing names to
        one file would silently serialize unrelated pipelines."""
        a = script_lock.acquire("job-a", dir=tmp_path)
        b = script_lock.acquire("job-b", dir=tmp_path)
        assert a is not None and b is not None
        a.close()
        b.close()

    def test_dir_override_keeps_relocated_installs_separate(
            self, tmp_path: Path) -> None:
        """meeting_prepopulate and handoff_blob_pull resolve their lock dir
        through MEETING_PREPOP_SCRIPTS_DIR. Two installs under different roots
        must not contend, and one install must not split across two locks."""
        other = tmp_path / "other"
        other.mkdir()
        a = script_lock.acquire("job", dir=tmp_path)
        b = script_lock.acquire("job", dir=other)
        assert a is not None and b is not None
        a.close()
        b.close()

    def test_creates_missing_lock_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / ".locks"
        handle = script_lock.acquire("job", dir=target)
        assert handle is not None
        handle.close()
        assert (target / "job.lock").exists()

    def test_does_not_truncate_an_existing_lock_file(self,
                                                     tmp_path: Path) -> None:
        """The "a+" open is the fix for two scripts that used 'w'. Truncating a
        file another process holds raises PermissionError on Windows before any
        locking call runs, turning "already running" into a crash."""
        (tmp_path / "job.lock").write_text("sentinel\n", encoding="utf-8")
        handle = script_lock.acquire("job", dir=tmp_path)
        assert handle is not None
        handle.close()
        assert (tmp_path / "job.lock").read_text(encoding="utf-8") == "sentinel\n"

    def test_warns_only_when_there_is_no_guard_at_all(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Contention is normal and must stay quiet — the caller phrases it.
        Having no locking primitive is the one case worth a warning, because
        the caller then proceeds genuinely unprotected."""
        quiet: list[str] = []
        h = script_lock.acquire("job", dir=tmp_path, warn=quiet.append)
        blocked = script_lock.acquire("job", dir=tmp_path, warn=quiet.append)
        assert blocked is None
        assert quiet == []
        h.close()

        monkeypatch.setattr(script_lock, "_fcntl", None)
        monkeypatch.setattr(script_lock, "_msvcrt", None)
        said: list[str] = []
        unguarded = script_lock.acquire("job2", dir=tmp_path, warn=said.append)
        assert unguarded is not None
        unguarded.close()
        assert any("without a single-instance guard" in m for m in said)

    def test_unopenable_lock_reports_contention_rather_than_running(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the lock file can't be opened we must not run unguarded — two
        copies of a job both discovering a broken directory is worse than
        neither running."""
        def boom(*a: Any, **kw: Any) -> None:
            raise OSError("nope")
        monkeypatch.setattr("builtins.open", boom)
        said: list[str] = []
        assert script_lock.acquire("job", dir=tmp_path, warn=said.append) is None
        assert said


class TestAcquireOrExit:

    def test_exits_zero_when_held(self, tmp_path: Path) -> None:
        """Exit 0, not non-zero: declining to run twice is the guard working,
        and a scheduler reports a non-zero exit as a failed job."""
        holder = script_lock.acquire("job", dir=tmp_path)
        assert holder is not None
        said: list[str] = []
        with pytest.raises(SystemExit) as excinfo:
            script_lock.acquire_or_exit("job", dir=tmp_path, warn=said.append)
        assert excinfo.value.code == 0
        assert any("another job instance is running" in m for m in said)
        holder.close()

    def test_returns_handle_when_free(self, tmp_path: Path) -> None:
        handle = script_lock.acquire_or_exit("job", dir=tmp_path)
        assert handle is not None
        handle.close()
