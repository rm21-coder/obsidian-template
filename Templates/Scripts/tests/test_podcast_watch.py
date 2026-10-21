"""
test_podcast_watch.py — log routing for the podcast drop-folder watcher.

The LaunchAgent writes stdout to podcast-watch.log and stderr to
podcast-watch.err, and the installer tells you to check the .log. Because
logging defaults to a single stderr stream, every line went to .err and the
advertised .log stayed at zero bytes for the life of the install — so the
file the operator was told to check was the one that could never have
anything in it, and a failing job looked like a job that never ran.

These pin the split: routine progress on stdout, anything at WARNING or
above on stderr, each record emitted exactly once.
"""
from __future__ import annotations

import io
import logging
import sys

import pytest

import podcast_watch as pw


@pytest.fixture
def streams(monkeypatch):
    """Re-point logging at capture buffers, restoring the root logger after.

    _configure_logging() binds sys.stdout/sys.stderr at call time, so the
    swap has to happen before it runs.
    """
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    root = logging.getLogger()
    saved = list(root.handlers)
    pw._configure_logging()
    try:
        yield out, err
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)


def test_routine_progress_goes_to_stdout(streams):
    out, err = streams
    pw.log.info("podcast-drop.txt done in 31s")
    assert "done in 31s" in out.getvalue()
    assert err.getvalue() == ""


@pytest.mark.parametrize("level,text", [
    ("warning", "another run holds the lock"),
    ("error", "podcast-drop.txt failed (exit 1)"),
])
def test_problems_go_to_stderr(streams, level, text):
    out, err = streams
    getattr(pw.log, level)(text)
    assert text in err.getvalue()
    assert out.getvalue() == ""


def test_each_record_is_emitted_once(streams):
    """Two handlers on one logger is the easy way to double every line."""
    out, err = streams
    pw.log.info("tick")
    pw.log.error("boom")
    assert out.getvalue().count("tick") == 1
    assert err.getvalue().count("boom") == 1


def test_debug_is_below_the_threshold(streams):
    out, err = streams
    pw.log.debug("noisy internal detail")
    assert out.getvalue() == ""
    assert err.getvalue() == ""


# ---------------------------------------------------------------------------
# A run stopped from outside never leaves its drop in the queue (5278625).
#
# The Windows task's run_logged limit (10,680 s) fell before the per-episode
# timeout (14,400 s), so a long episode was killed before _retire ran, stayed
# in the inbox, and was picked up -- and killed -- on every tick after.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402


@pytest.fixture
def inbox(tmp_path):
    box = tmp_path / "PodcastInput"
    box.mkdir()
    return box


def _drop(inbox, name="episode.mp3"):
    p = inbox / name
    p.write_bytes(b"audio")
    return p


def test_the_drop_is_in_processing_while_it_transcribes(inbox, tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["source"] = cmd[2]
        seen["inbox"] = sorted(p.name for p in inbox.iterdir())
        seen["processing"] = sorted(p.name for p in (inbox / "processing").iterdir())
        return subprocess.CompletedProcess(cmd, 0, "out.md\n", "")

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    item = _drop(inbox)
    assert pw.process_item(item, out_dir=tmp_path / "out", model=None,
                           python="py", timeout=60)
    assert seen["processing"] == ["episode.mp3"]
    assert seen["inbox"] == ["processing"]                 # nothing left to requeue
    assert seen["source"] == str(inbox / "processing" / "episode.mp3")
    assert (inbox / "done" / "episode.mp3").is_file()
    assert list((inbox / "processing").iterdir()) == []


def test_a_link_drop_is_claimed_too(inbox, tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["source"] = cmd[2]
        seen["processing"] = sorted(p.name for p in (inbox / "processing").iterdir())
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    link = inbox / "ep.txt"
    link.write_text("https://example.com/ep.mp3\n", encoding="utf-8")
    assert not pw.process_item(link, out_dir=tmp_path / "out", model=None,
                               python="py", timeout=60)
    assert seen == {"source": "https://example.com/ep.mp3", "processing": ["ep.txt"]}
    assert (inbox / "failed" / "ep.txt").is_file()
    assert "boom" in (inbox / "failed" / "ep.txt.error.log").read_text(encoding="utf-8")


def test_a_timed_out_episode_goes_to_failed_with_the_limit(inbox, tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    item = _drop(inbox)
    assert not pw.process_item(item, out_dir=tmp_path / "out", model=None,
                               python="py", timeout=10200)
    log = (inbox / "failed" / "episode.mp3.error.log").read_text(encoding="utf-8")
    assert "Transcription exceeded the 10200s limit." in log
    assert list((inbox / "processing").iterdir()) == []


def test_a_drop_left_in_processing_goes_to_failed_not_the_queue(inbox, tmp_path, monkeypatch):
    """What a killed run leaves behind. The next run must not transcribe it
    again: it would be killed at the same point, forever."""
    (inbox / "processing").mkdir()
    (inbox / "processing" / "long.mp3").write_bytes(b"audio")

    def fail(*a, **k):
        raise AssertionError("re-ran an interrupted drop")

    monkeypatch.setattr(pw.subprocess, "run", fail)
    done = pw.run_once(inbox, out_dir=tmp_path / "out", model=None, python="py",
                       max_per_run=1, timeout=60, min_age=0)
    assert done == 0
    assert (inbox / "failed" / "long.mp3").is_file()
    log = (inbox / "failed" / "long.mp3.error.log").read_text(encoding="utf-8")
    assert log.startswith("Interrupted: the run transcribing this was stopped before it finished")
    assert list((inbox / "processing").iterdir()) == []


def test_recovery_then_the_queue_carries_on(inbox, tmp_path, monkeypatch):
    (inbox / "processing").mkdir()
    (inbox / "processing" / "old.mp3").write_bytes(b"audio")
    new = _drop(inbox, "new.mp3")
    import os
    os.utime(new, (0, 0))
    ran = []
    monkeypatch.setattr(pw.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd[2]) or
                        subprocess.CompletedProcess(cmd, 0, "x.md\n", ""))
    assert pw.run_once(inbox, out_dir=tmp_path / "out", model=None, python="py",
                       max_per_run=1, timeout=60, min_age=0) == 1
    assert ran == [str(inbox / "processing" / "new.mp3")]
    assert (inbox / "failed" / "old.mp3").is_file()
    assert (inbox / "done" / "new.mp3").is_file()


def test_processing_is_not_itself_queued(inbox):
    (inbox / "processing").mkdir()
    (inbox / "processing" / "x.mp3").write_bytes(b"a")
    assert pw.pending_items(inbox, min_age=0) == []


def test_the_default_timeout_is_parsed_from_the_constant(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(pw, "run_once", lambda inbox, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(pw, "acquire_lock", lambda: type("L", (), {"close": lambda self: None})())
    pw.main(["--once", "--inbox", str(tmp_path / "in"), "--out", str(tmp_path / "out")])
    assert seen["timeout"] == pw.DEFAULT_TIMEOUT == 14400
    pw.main(["--once", "--inbox", str(tmp_path / "in"), "--out", str(tmp_path / "out"),
             "--timeout", "10200"])
    assert seen["timeout"] == 10200
