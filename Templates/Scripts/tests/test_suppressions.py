"""
test_suppressions.py -- an accepted finding stays argued: counted, explained, dated.

Until 2026-10-03 a line in security-suppressions.txt matched on (tool, path,
rule) alone, so it also covered every FUTURE finding of that rule in that file,
and its re-check date was a comment nothing read: two lines sat 8 days past
theirs, still suppressing. installers/lib/suppressions.py now enforces both.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("suppressions", REPO / "installers" / "lib" / "suppressions.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

TODAY = dt.date(2026, 10, 3)
OK = "semgrep  a.py  rule-x  count=2   # two deliberate sites, lines 1 and 2. re-check 2026-12-31\n"


def _file(tmp_path, text):
    f = tmp_path / "sup.txt"
    f.write_text("# header\n\n" + text, encoding="utf-8")
    return f


def _fails(tmp_path, text, today=TODAY):
    entries, problems = S.parse(_file(tmp_path, text))
    return S.check_dates(entries, problems, today)


def test_a_counted_explained_dated_line_passes(tmp_path) -> None:
    assert _fails(tmp_path, OK) == ([], [])


@pytest.mark.parametrize("text,marker", [
    (OK.replace("2026-12-31", "2026-09-25"), "re-check date 2026-09-25 has passed -- re-argue it"),
    (OK.replace(" re-check 2026-12-31", ""), "no 're-check YYYY-MM-DD'"),
    (OK.replace("count=2   ", ""), "no count=<N>"),
    ("semgrep  a.py  rule-x  count=2\n", "no rationale"),
    (OK.replace("count=2", "lines=1,2"), "unexpected field 'lines=1,2'"),
    ("semgrep  a.py   # too short\n", "not '<tool> <path> <rule> count=<N>"),
])
def test_a_line_that_is_not_fully_argued_fails(tmp_path, text, marker) -> None:
    fails, _ = _fails(tmp_path, text)
    assert any(marker in f for f in fails), fails


def test_a_date_inside_the_warning_window_is_a_note(tmp_path) -> None:
    fails, notes = _fails(tmp_path, OK.replace("2026-12-31", "2026-10-10"))
    assert fails == [] and any("re-check due 2026-10-10" in n for n in notes)


def _live(tmp_path, text, findings):
    entries, _ = S.parse(_file(tmp_path, text))
    return S.live_findings("semgrep", findings, entries)


def test_exactly_the_counted_findings_are_suppressed(tmp_path) -> None:
    assert _live(tmp_path, OK, [("a.py", "rule-x", 1), ("a.py", "rule-x", 2)]) == []


def test_a_new_finding_in_a_suppressed_file_fails(tmp_path) -> None:
    live = _live(tmp_path, OK, [("a.py", "rule-x", 1), ("a.py", "rule-x", 2), ("a.py", "rule-x", 9)])
    assert len(live) == 1 and "3 findings, suppression allows 2" in live[0] and "a new one appeared" in live[0]


def test_a_count_the_code_no_longer_matches_fails(tmp_path) -> None:
    live = _live(tmp_path, OK, [("a.py", "rule-x", 1)])
    assert len(live) == 1 and "1 findings, suppression expects 2" in live[0]


def test_an_unsuppressed_finding_fails(tmp_path) -> None:
    live = _live(tmp_path, OK, [("a.py", "rule-x", 1), ("a.py", "rule-x", 2), ("b.py", "rule-x", 4)])
    assert live == ["b.py:4 rule-x (not suppressed)"]


def test_the_cli_reads_scanner_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(S, "SUPPRESSIONS", _file(tmp_path, OK))
    res = tmp_path / "semgrep.json"
    res.write_text(json.dumps({"results": [
        {"path": "a.py", "check_id": "x.y.rule-x", "start": {"line": n}} for n in (1, 2, 3)]}),
        encoding="utf-8")
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        S.main(["apply", "semgrep", str(res)])
    assert out.getvalue().strip() == "1"


def test_the_real_file_is_counted_explained_and_in_date_today() -> None:
    entries, problems = S.parse()
    fails, _ = S.check_dates(entries, problems, dt.date.today())
    assert fails == [], fails
    assert len(entries) >= 10 and all(e.count for e in entries)


def test_the_suite_uses_it_for_both_scanners_and_the_dates() -> None:
    src = (REPO / "installers" / "lib" / "security-checks.sh").read_text(encoding="utf-8")
    assert "suppressions.py apply semgrep" in src and "suppressions.py apply bandit" in src
    assert "suppressions.py dates" in src
    assert "security-suppressions\\.txt" in src            # a change to it triggers SAST
