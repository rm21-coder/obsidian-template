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

import os
import stat
from pathlib import Path
from typing import Any

import pytest

import script_lock
from platform_caps import requires_posix_modes, requires_symlinks


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
        """meeting_prepopulate resolves its lock dir through
        MEETING_PREPOP_SCRIPTS_DIR. Two installs under different roots
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
        monkeypatch.setattr(script_lock.os, "open", boom)
        said: list[str] = []
        assert script_lock.acquire("job", dir=tmp_path, warn=said.append) is None
        assert any("could not open lock file" in m for m in said), said


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


@pytest.fixture
def loose_umask():
    """The default umask a LaunchAgent without Umask runs under: 022."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@requires_posix_modes
class TestOwnerOnly:
    """5278107: lock files were 0644 in a 0755 directory, so another local
    account could open one and hold the flock -- every scheduled run would
    then read "already running" and exit 0, doing nothing."""

    def test_a_new_lock_file_is_0600_in_a_0700_directory(
            self, tmp_path: Path, loose_umask) -> None:
        locks = tmp_path / ".locks"
        handle = script_lock.acquire("job", dir=locks)
        assert handle is not None
        handle.close()
        assert stat.S_IMODE((locks / "job.lock").stat().st_mode) == 0o600
        assert stat.S_IMODE(locks.stat().st_mode) == 0o700

    def test_an_existing_install_is_tightened(
            self, tmp_path: Path, loose_umask) -> None:
        locks = tmp_path / ".locks"
        locks.mkdir(mode=0o755)
        os.chmod(locks, 0o755)
        (locks / "job.lock").write_text("sentinel\n", encoding="utf-8")
        os.chmod(locks / "job.lock", 0o644)
        handle = script_lock.acquire("job", dir=locks)
        assert handle is not None
        handle.close()
        assert stat.S_IMODE((locks / "job.lock").stat().st_mode) == 0o600
        assert stat.S_IMODE(locks.stat().st_mode) == 0o700
        # Still never truncated.
        assert (locks / "job.lock").read_text(encoding="utf-8") == "sentinel\n"

    @requires_symlinks
    def test_a_symlink_planted_at_the_lock_path_is_refused(
            self, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        os.chmod(victim, 0o644)
        locks = tmp_path / ".locks"
        locks.mkdir()
        (locks / "job.lock").symlink_to(victim)
        handle, error = script_lock.try_acquire("job", dir=locks)
        assert handle is None
        assert error is not None and error.startswith("could not open lock file"), error
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644
        assert victim.read_text(encoding="utf-8") == "keep\n"
