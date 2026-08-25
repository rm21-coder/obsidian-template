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


def test_estimate_pins_absolute_list_prices() -> None:
    """The relative tests above (haiku < sonnet, reads are cheap) all still
    passed while Opus sat at a stale $15/$75 — three times its real rate.
    Pin the actual per-MTok numbers so a price change has to be deliberate."""
    for model, dollars_per_mtok_in in (("claude-opus-5", 5.00),
                                       ("claude-fable-5", 10.00),
                                       ("claude-sonnet-5", 3.00),
                                       ("claude-haiku-4-5", 1.00),
                                       ("claude-3-opus-20240229", 15.00)):
        usd = usage_log.estimate_usd({
            "model": model, "input_tokens": 1_000_000, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        assert usd == pytest.approx(dollars_per_mtok_in), model


def test_legacy_opus_3_does_not_match_the_opus_family() -> None:
    """Substring matching is order-sensitive: "claude-3-opus" has to be
    tested before "opus" or Opus 3 silently reprices to a third of its rate."""
    legacy = usage_log.estimate_usd({
        "model": "claude-3-opus-20240229", "input_tokens": 1_000_000,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0})
    current = usage_log.estimate_usd({
        "model": "claude-opus-5", "input_tokens": 1_000_000,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0})
    assert legacy == pytest.approx(3 * current)


def test_cache_writes_priced_by_ttl() -> None:
    """A 1-hour write costs 2x input, a 5-minute write 1.25x."""
    base = {"model": "claude-haiku-4-5", "input_tokens": 0,
            "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1_000_000}
    short = usage_log.estimate_usd({**base,
                                    "cache_creation_5m_input_tokens": 1_000_000,
                                    "cache_creation_1h_input_tokens": 0})
    long = usage_log.estimate_usd({**base,
                                   "cache_creation_5m_input_tokens": 0,
                                   "cache_creation_1h_input_tokens": 1_000_000})
    assert short == pytest.approx(1.25)
    assert long == pytest.approx(2.00)


def test_write_with_no_ttl_split_is_not_free() -> None:
    """Regression: record() writes both TTL fields unconditionally, so a row
    from an SDK that reports no usage.cache_creation arrives as 0/0 rather
    than absent. Keying the fallback on absence priced those writes at zero
    and silently dropped the whole write line from the estimate."""
    usd = usage_log.estimate_usd({
        "model": "claude-haiku-4-5", "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0})
    assert usd == pytest.approx(2.00)  # unattributed remainder -> 1h multiple


def test_partial_ttl_split_prices_the_remainder() -> None:
    usd = usage_log.estimate_usd({
        "model": "claude-haiku-4-5", "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation_5m_input_tokens": 400_000,
        "cache_creation_1h_input_tokens": 0})
    # 400k at 1.25x + the unattributed 600k at 2x
    assert usd == pytest.approx(0.4 * 1.25 + 0.6 * 2.00)


def test_billable_excludes_cache_reads_and_total_includes_them() -> None:
    row = {"input_tokens": 10, "output_tokens": 20,
           "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40_000}
    assert usage_log.billable_tokens(row) == 60
    assert usage_log.total_tokens(row) == 40_060


def test_billable_ignores_the_ttl_breakdown() -> None:
    """The TTL fields are a breakdown OF cache_creation_input_tokens, not an
    addition to it — counting them again would double the write line."""
    row = {"input_tokens": 0, "output_tokens": 0,
           "cache_creation_input_tokens": 1_000,
           "cache_creation_5m_input_tokens": 400,
           "cache_creation_1h_input_tokens": 600,
           "cache_read_input_tokens": 0}
    assert usage_log.billable_tokens(row) == 1_000


def test_cache_read_usd_is_the_read_slice_of_the_estimate() -> None:
    row = {"model": "claude-opus-5", "input_tokens": 1_000,
           "output_tokens": 1_000, "cache_creation_input_tokens": 1_000,
           "cache_read_input_tokens": 1_000_000}
    assert usage_log.cache_read_usd(row) == pytest.approx(0.50)  # 5.00 * 0.1
    assert usage_log.cache_read_usd(row) < usage_log.estimate_usd(row)


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
