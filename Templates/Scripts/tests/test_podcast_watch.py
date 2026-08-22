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
