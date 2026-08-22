"""usage_log: recording, aggregation, price estimation, failure isolation."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import usage_log


@pytest.fixture
def log_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("USAGE_LOG_PATH", str(path))
    return path


def _usage(inp=100, out=20, cw=0, cr=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw,
                           cache_read_input_tokens=cr)


def test_record_appends_one_json_line(log_file: Path) -> None:
    usage_log.record("tag_clippings", "claude-haiku-4-5-20251001", _usage())
    usage_log.record("tag_clippings", "claude-haiku-4-5-20251001", _usage())
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["pipeline"] == "tag_clippings"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20


def test_record_accepts_dict_and_missing_fields(log_file: Path) -> None:
    usage_log.record("p", "m", {"input_tokens": 5})
    row = json.loads(log_file.read_text())
    assert row["input_tokens"] == 5
    assert row["cache_read_input_tokens"] == 0


def test_record_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Usage accounting must not break a pipeline, even if the log path is
    unwritable."""
    monkeypatch.setenv("USAGE_LOG_PATH", "/nonexistent-root/nope/usage.jsonl")
    usage_log.record("p", "m", _usage())  # must not raise


def test_summarize_aggregates_by_pipeline_and_model(log_file: Path) -> None:
    usage_log.record("tagger", "claude-haiku-4-5", _usage(100, 10))
    usage_log.record("tagger", "claude-haiku-4-5", _usage(200, 30))
    usage_log.record("voice", "claude-sonnet-4-6", _usage(50, 500))
    rows = usage_log.summarize(days=7)
    assert len(rows) == 2
    tagger = next(r for r in rows if r["pipeline"] == "tagger")
    assert tagger["calls"] == 2
    assert tagger["input_tokens"] == 300
    assert tagger["output_tokens"] == 40


def test_summarize_respects_window(log_file: Path) -> None:
    old = {"ts": (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=30)).isoformat(),
           "pipeline": "old", "model": "m", "input_tokens": 1,
           "output_tokens": 1, "cache_creation_input_tokens": 0,
           "cache_read_input_tokens": 0}
    log_file.write_text(json.dumps(old) + "\n")
    usage_log.record("fresh", "m", _usage())
    rows = usage_log.summarize(days=7)
    assert [r["pipeline"] for r in rows] == ["fresh"]


def test_summarize_skips_corrupt_lines(log_file: Path) -> None:
    log_file.write_text("{not json\n")
    usage_log.record("ok", "m", _usage())
    assert [r["pipeline"] for r in usage_log.summarize()] == ["ok"]


def test_estimate_prices_haiku_below_sonnet() -> None:
    row = {"input_tokens": 1_000_000, "output_tokens": 0,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    haiku = usage_log.estimate_usd({**row, "model": "claude-haiku-4-5"})
    sonnet = usage_log.estimate_usd({**row, "model": "claude-sonnet-4-6"})
    assert 0 < haiku < sonnet


def test_estimate_cache_reads_are_cheap() -> None:
    fresh = usage_log.estimate_usd({
        "model": "claude-haiku-4-5", "input_tokens": 1_000_000,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0})
    cached = usage_log.estimate_usd({
        "model": "claude-haiku-4-5", "input_tokens": 0,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1_000_000})
    assert cached < fresh / 5


def test_unknown_model_gets_default_pricing() -> None:
    usd = usage_log.estimate_usd({
        "model": "my-gateway-alias", "input_tokens": 1_000_000,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0})
    assert usd > 0


# ---------------------------------------------------------------------------
# summarize_claude_code — local transcript reader
# ---------------------------------------------------------------------------

def _entry(msg_id="m1", req_id="r1", model="claude-opus-5", ts=None,
           inp=100, out=10):
    ts = ts or dt.datetime.now(dt.timezone.utc).isoformat()
    return json.dumps({
        "type": "assistant", "timestamp": ts, "requestId": req_id,
        "message": {"id": msg_id, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}}})


@pytest.fixture
def transcripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "projects" / "-Users-x"
    root.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_TRANSCRIPTS_DIR", str(tmp_path / "projects"))
    return root


def test_cc_aggregates_and_counts_sessions(transcripts: Path) -> None:
    (transcripts / "s1.jsonl").write_text(
        _entry("m1", "r1") + "\n" + _entry("m2", "r2") + "\n")
    (transcripts / "s2.jsonl").write_text(_entry("m3", "r3", inp=50) + "\n")
    cc = usage_log.summarize_claude_code(days=30)
    assert cc["calls"] == 3
    assert cc["sessions"] == 2
    assert cc["input_tokens"] == 250


def test_cc_dedups_resumed_session_copies(transcripts: Path) -> None:
    """A resumed session copies earlier messages into its new file; the same
    (message.id, requestId) must count once."""
    (transcripts / "orig.jsonl").write_text(_entry("m1", "r1") + "\n")
    (transcripts / "resumed.jsonl").write_text(
        _entry("m1", "r1") + "\n" + _entry("m2", "r2") + "\n")
    cc = usage_log.summarize_claude_code(days=30)
    assert cc["calls"] == 2
    assert cc["input_tokens"] == 200


def test_cc_window_excludes_old_entries(transcripts: Path) -> None:
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=45)).isoformat()
    (transcripts / "s.jsonl").write_text(
        _entry("m1", "r1", ts=old) + "\n" + _entry("m2", "r2") + "\n")
    cc = usage_log.summarize_claude_code(days=30)
    assert cc["calls"] == 1


def test_cc_skips_zero_token_placeholders(transcripts: Path) -> None:
    row = json.loads(_entry("m1", "r1"))
    row["message"]["model"] = "<synthetic>"
    for f in row["message"]["usage"]:
        row["message"]["usage"][f] = 0
    (transcripts / "s.jsonl").write_text(
        json.dumps(row) + "\n" + _entry("m2", "r2") + "\n")
    cc = usage_log.summarize_claude_code(days=30)
    assert cc["calls"] == 1
    assert all(b["model"] != "<synthetic>" for b in cc["by_model"])


def test_cc_none_when_no_transcripts(monkeypatch: pytest.MonkeyPatch,
                                     tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_TRANSCRIPTS_DIR", str(tmp_path / "nope"))
    assert usage_log.summarize_claude_code(days=30) is None


def test_cc_survives_corrupt_lines(transcripts: Path) -> None:
    (transcripts / "s.jsonl").write_text(
        '{"usage" broken\n' + _entry("m1", "r1") + "\n")
    assert usage_log.summarize_claude_code(days=30)["calls"] == 1
