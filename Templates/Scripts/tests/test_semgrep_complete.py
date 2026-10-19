"""
test_semgrep_complete.py -- a semgrep count is judged only on a scan that finished.

On 2026-10-19 a --full run reported source_mail_pull.py with 1 of its 3
counted findings; the rerun found 3. Semgrep's taint engine had hit its
wall-clock fixpoint limit under load -- recorded as a warning, exit 0, fewer
findings. installers/lib/semgrep_complete.py rescans such files with -j 1 and
fails the pass as incomplete when they still do not finish.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "installers" / "lib" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("semgrep_complete")
S = _load("suppressions")

SMP = "Templates/Scripts/source_mail_pull.py"
RULE = "installers.lib.semgrep-rules.secret-value-reaches-output-sink"


def _hit(path, line, rule=RULE):
    return {"path": path, "check_id": rule, "start": {"line": line}}


def _timeout(path, line):
    return {"error_type": "Fixpoint timeout", "severity": "warn",
            "message": f"Fixpoint timeout while performing taint analysis at {path}:{line}:4 "
                       f"[rules: 2, first: {RULE}]",
            "location": {"path": path, "start": {"line": line}}}


def _scan(results, timeouts=(), errors=()):
    return {"version": "1.157.0", "results": list(results), "errors": list(errors),
            "paths": {"scanned": []}, "time": {"fixpoint_timeouts": list(timeouts)}}


# The 2026-10-19 parallel scan, reduced: one timeout in source_mail_pull.py
# cost it two of its three findings; another file finished normally.
INCIDENT = _scan([_hit(SMP, 717), _hit("b.py", 5, "other-rule")], [_timeout(SMP, 534)])
SERIAL_OK = _scan([_hit(SMP, 717), _hit(SMP, 729), _hit(SMP, 745)])


def test_a_clean_parallel_scan_needs_no_retry() -> None:
    scan = _scan([_hit("b.py", 5)])
    assert C.incomplete(scan) == (set(), [])
    out, problems = C.merge(scan, None)
    assert problems == [] and out["results"] == scan["results"]


def test_a_timed_out_file_is_retried_and_its_serial_results_replace_the_parallel_ones() -> None:
    assert C.incomplete(INCIDENT) == ({SMP}, [])
    out, problems = C.merge(INCIDENT, SERIAL_OK)
    assert problems == []
    lines = sorted(r["start"]["line"] for r in out["results"] if r["path"] == SMP)
    assert lines == [717, 729, 745]                      # not 717 twice, not 717 alone
    assert any(r["path"] == "b.py" for r in out["results"])
    assert out["time"]["fixpoint_timeouts"] == [] and out["serial_retry"] == [SMP]


def test_the_incident_now_meets_its_suppression_count(tmp_path) -> None:
    sup = tmp_path / "sup.txt"
    sup.write_text(f"semgrep  {SMP}  secret-value-reaches-output-sink  count=3  # x. re-check 2099-01-01\n",
                   encoding="utf-8")
    entries, _ = S.parse(sup)
    out, _ = C.merge(INCIDENT, SERIAL_OK)
    findings = [(r["path"], r["check_id"].split(".")[-1], r["start"]["line"])
                for r in out["results"] if r["path"] == SMP]
    assert S.live_findings("semgrep", findings, entries) == []


def test_a_file_that_still_times_out_serially_is_incomplete() -> None:
    serial = _scan([_hit(SMP, 717)], [_timeout(SMP, 534)])
    _, problems = C.merge(INCIDENT, serial)
    assert len(problems) == 1 and f"{SMP}: still incomplete with -j 1 -- Fixpoint timeout" in problems[0]


def test_a_retry_that_produced_nothing_is_incomplete() -> None:
    _, problems = C.merge(INCIDENT, None)
    assert problems == [f"{SMP}: timed out or errored, and the serial rescan produced no results"]


def test_a_file_error_is_retried_and_a_persistent_one_fails() -> None:
    err = {"code": 3, "level": "warn", "type": ["PartialParsing", []], "message": "Syntax error", "path": "c.py"}
    scan = _scan([], errors=[err])
    assert C.incomplete(scan) == ({"c.py"}, [])
    _, problems = C.merge(scan, _scan([], errors=[err]))
    assert problems == ["c.py: still incomplete with -j 1 -- PartialParsing: Syntax error"]


def test_an_error_with_no_file_cannot_be_retried_and_fails() -> None:
    scan = _scan([], errors=[{"level": "error", "type": "SemgrepError", "message": "Invalid scanning root: x"}])
    _, problems = C.merge(scan, None)
    assert problems == ["parallel scan: SemgrepError: Invalid scanning root: x"]


def _main(*argv):
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = C.main(list(argv))
    return rc, err.getvalue()


def test_json_without_the_timeout_list_is_unusable_not_clean(tmp_path) -> None:
    p = tmp_path / "parallel.json"
    p.write_text(json.dumps({"results": [], "errors": []}), encoding="utf-8")
    rc, err = _main("merge", str(p), str(tmp_path / "none.json"), str(tmp_path / "out.json"))
    assert rc == 2 and "no time.fixpoint_timeouts -- cannot tell whether taint analysis finished" in err


def test_a_missing_parallel_scan_is_unusable(tmp_path) -> None:
    rc, err = _main("merge", str(tmp_path / "absent.json"), str(tmp_path / "s.json"), str(tmp_path / "o.json"))
    assert rc == 2 and "absent.json: no usable semgrep JSON" in err


def test_the_cli_merges_and_exits_by_completeness(tmp_path) -> None:
    par, ser, out = tmp_path / "p.json", tmp_path / "s.json", tmp_path / "o.json"
    par.write_text(json.dumps(INCIDENT), encoding="utf-8")
    ser.write_text(json.dumps(SERIAL_OK), encoding="utf-8")
    assert _main("merge", str(par), str(ser), str(out)) == (0, "")
    assert len(json.loads(out.read_text(encoding="utf-8"))["results"]) == 4
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        C.main(["retry-paths", str(par)])
    assert buf.getvalue() == SMP + "\n"


def test_the_suite_retries_serially_and_fails_incomplete_before_judging_counts() -> None:
    src = (REPO / "installers" / "lib" / "security-checks.sh").read_text(encoding="utf-8")
    assert "semgrep_complete.py retry-paths" in src and "semgrep_complete.py merge" in src
    assert '-j 1 --json -o "$OUT/semgrep-serial.json"' in src
    i_incomplete = src.index("semgrep: analysis incomplete")
    i_count = src.index('record sast FAIL "semgrep: $n unsuppressed finding(s)"')
    assert i_incomplete < i_count                        # incomplete wins over a short count
