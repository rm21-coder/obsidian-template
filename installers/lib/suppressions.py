#!/usr/bin/env python3
"""suppressions.py -- read installers/lib/security-suppressions.txt, strictly.

Each accepted finding is one line:

    <tool>  <path>  <rule>  count=<N>  # rationale ... re-check YYYY-MM-DD

and is held to three things, so an accepted risk stays an argued one:

  * count -- the exact number of findings it covers. A suppression used to
    match on (tool, path, rule) alone, so it covered every future finding of
    that rule in that file too: a new secret-to-log flow added to
    source_mail_pull.py after its two deliberate ones were accepted would
    have passed unseen. More findings than the count fail; fewer fail too,
    because the line no longer describes the code (re-argue, then fix it).
  * a re-check date -- past it, the entry FAILS the run until someone
    re-reads the code and re-argues it. Nothing enforced this before: two
    entries sat 8 days past their date, still suppressing, until a person
    happened to read the file (2026-10-03). Within WARN_DAYS it is a note.
  * a rationale -- the text after the '#'. Required.

    suppressions.py apply <tool> <results.json>    print the number of LIVE findings
                                                   (unsuppressed, or beyond/short of a
                                                   count); details on stderr
    suppressions.py dates [--today YYYY-MM-DD]     exit 1 on any expired, undated,
                                                   uncounted or unexplained entry
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SUPPRESSIONS = Path(__file__).resolve().parent / "security-suppressions.txt"
WARN_DAYS = 14
_DATE = re.compile(r"re-check (\d{4}-\d{2}-\d{2})")
_COUNT = re.compile(r"\Acount=(\d+)\Z")


class Entry:
    def __init__(self, lineno: int, tool: str, path: str, rule: str,
                 count: int | None, recheck: dt.date | None, rationale: str) -> None:
        self.lineno, self.tool, self.path, self.rule = lineno, tool, path, rule
        self.count, self.recheck, self.rationale = count, recheck, rationale

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.rule)

    def where(self) -> str:
        return f"line {self.lineno}: {self.tool} {self.path} {self.rule}"


def parse(path: Path | None = None) -> tuple[list[Entry], list[str]]:
    """Entries, and problems with lines that could not be read as one."""
    path = path or SUPPRESSIONS                  # looked up now, not at import
    entries, problems = [], []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"cannot read {path}: {exc}"]
    for n, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        body, _, comment = raw.partition("#")
        fields = body.split()
        if len(fields) < 3:
            problems.append(f"line {n}: not '<tool> <path> <rule> count=<N> # ...'")
            continue
        tool, fpath, rule, extra = fields[0], fields[1], fields[2], fields[3:]
        count = None
        for tok in extra:
            m = _COUNT.match(tok)
            if m:
                count = int(m.group(1))
            else:
                problems.append(f"line {n}: unexpected field {tok!r}")
        dm = _DATE.search(comment)
        recheck = None
        if dm:
            try:
                recheck = dt.date.fromisoformat(dm.group(1))
            except ValueError:
                problems.append(f"line {n}: bad re-check date {dm.group(1)!r}")
        entries.append(Entry(n, tool, fpath, rule, count, recheck, comment.strip()))
    return entries, problems


def check_dates(entries: list[Entry], problems: list[str], today: dt.date) -> tuple[list[str], list[str]]:
    """(failures, notes)."""
    fails, notes = list(problems), []
    for e in entries:
        if e.count is None:
            fails.append(f"{e.where()}: no count=<N> (an uncounted line covers every future finding)")
        if not e.rationale:
            fails.append(f"{e.where()}: no rationale")
        if e.recheck is None:
            fails.append(f"{e.where()}: no 're-check YYYY-MM-DD'")
        elif e.recheck < today:
            fails.append(f"{e.where()}: re-check date {e.recheck} has passed -- re-argue it")
        elif (e.recheck - today).days <= WARN_DAYS:
            notes.append(f"{e.where()}: re-check due {e.recheck}")
    return fails, notes


def _findings(tool: str, results_path: Path) -> list[tuple[str, str, int]]:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    root = os.getcwd() + os.sep
    out = []
    for r in data.get("results", []):
        if tool == "semgrep":
            out.append((r["path"], r["check_id"].split(".")[-1], r["start"]["line"]))
        else:
            out.append((r["filename"].replace(root, ""), r["test_id"], r.get("line_number", 0)))
    return out


def live_findings(tool: str, findings: list[tuple[str, str, int]],
                  entries: list[Entry]) -> list[str]:
    """Descriptions of every finding that counts against the run."""
    sup = {e.key: e for e in entries if e.tool == tool}
    by_key: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    live = []
    for fpath, rule, line in findings:
        if (fpath, rule) in sup:
            by_key[(fpath, rule)].append(line)
        else:
            live.append(f"{fpath}:{line} {rule} (not suppressed)")
    for key, e in sup.items():
        got = len(by_key.get(key, []))
        if e.count is None or got == e.count:
            continue
        where = ", ".join(str(n) for n in sorted(by_key.get(key, []))) or "none"
        if got > e.count:
            live.extend(f"{key[0]} {key[1]}: {got} findings, suppression allows {e.count} "
                        f"(lines {where}) -- a new one appeared" for _ in range(got - e.count))
        else:
            live.append(f"{key[0]} {key[1]}: {got} findings, suppression expects {e.count} "
                        f"(lines {where}) -- re-argue and correct the count")
    return live


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "apply":
        entries, _ = parse()
        live = live_findings(argv[1], _findings(argv[1], Path(argv[2])), entries)
        for d in live:
            print(f"  {d}", file=sys.stderr)
        print(len(live))
        return 0
    if argv and argv[0] == "dates":
        today = dt.date.today()
        if len(argv) == 3 and argv[1] == "--today":
            today = dt.date.fromisoformat(argv[2])
        entries, problems = parse()
        fails, notes = check_dates(entries, problems, today)
        for n in notes:
            print(f"NOTE: {n}")
        for f in fails:
            print(f"FAIL: {f}")
        if not fails:
            print(f"OK: {len(entries)} suppressions, each counted, explained and in date")
        return 1 if fails else 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
