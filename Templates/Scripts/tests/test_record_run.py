"""security_common.record_run: each security control records that it ran,
so the dashboard need not guess from a log written only on findings."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import security_common


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setenv(security_common.STATE_DIR_ENV, str(d))
    return d


def test_a_run_is_recorded_with_its_exit_code(state: Path) -> None:
    security_common.record_run("plugin-check", 1)
    text = (state / "last-run-plugin-check.txt").read_text()
    assert text.rstrip().endswith("rc=1")


def test_the_record_is_not_a_json_trust_anchor(state: Path) -> None:
    # The integrity monitor hashes *.json here; a per-run file must not be one.
    security_common.record_run("integrity", 0)
    assert not list(state.glob("*.json"))


def test_a_planted_link_is_not_written_through(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True)
    victim = tmp_path / "victim.txt"; victim.write_text("keep")
    (state / "last-run-plugin-check.txt.tmp").symlink_to(victim)
    security_common.record_run("plugin-check", 0)      # must not raise
    assert victim.read_text() == "keep"


def test_recording_never_raises(state: Path) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("a file where the directory should be")
    security_common.record_run("plugin-check", 0)      # swallowed


def test_both_controls_record_every_way_they_end(state: Path, monkeypatch) -> None:
    import plugin_integrity_check as pic
    import integrity_monitor as im
    monkeypatch.setattr(pic, "_run", lambda argv: (_ for _ in ()).throw(SystemExit(3)))
    assert pic._main_recording([]) == 3
    assert (state / "last-run-plugin-check.txt").read_text().rstrip().endswith("rc=3")
    monkeypatch.setattr(im, "main", lambda argv: 2)
    assert im._main_recording([]) == 2
    assert (state / "last-run-integrity.txt").read_text().rstrip().endswith("rc=2")


def test_the_bytecode_child_is_not_a_run(state: Path, monkeypatch) -> None:
    import integrity_monitor as im
    monkeypatch.setattr(im, "main", lambda argv: 0)
    im._main_recording(["--bytecode-only"])
    assert not (state / "last-run-integrity.txt").exists()
