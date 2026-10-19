#!/usr/bin/env python3
"""semgrep_complete.py -- a semgrep scan counts only if it finished.

Semgrep's taint engine gives up on a function when its fixpoint runs past a
wall-clock limit. It records that as a WARNING under time.fixpoint_timeouts,
not under errors, exits 0, and simply returns fewer findings. The limit has no
CLI flag and no rule option, and it is load-dependent: the same tree gives
33-49 timeouts across 15-21 files at the default parallelism, and none at all
when those files are rescanned with -j 1.

On 2026-10-19 one of those timeouts landed in source_mail_pull.py on our own
secret-value-reaches-output-sink rule, right after a 3-minute pytest run. The
suite saw 1 finding where the suppression counts 3 and blamed the code; an
immediate rerun found all 3. The same blind spot cuts the other way: a
timeout in a file with NO suppression drops a real new finding and the pass
reports clean (on 2026-10-18 the rule timed out in two such files).

So: any file with a fixpoint timeout or a semgrep error is rescanned serially,
the serial results replace the parallel ones for that file, and if anything is
still incomplete the pass FAILS as incomplete. A count is never judged on an
analysis that did not finish.

    semgrep_complete.py retry-paths <parallel.json>
        print each file to rescan, one per line
    semgrep_complete.py merge <parallel.json> <serial.json> <out.json>
        write the merged results; exit 0 if complete, 1 if not (reasons on
        stderr), 2 if the parallel results are unusable. <serial.json> may be
        absent when nothing needed a retry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


class Unusable(Exception):
    pass


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unusable(f"{path.name}: no usable semgrep JSON ({exc})")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise Unusable(f"{path.name}: no 'results' list")
    if not isinstance((data.get("time") or {}).get("fixpoint_timeouts"), list):
        # Without this list a timeout is invisible, so completeness cannot be
        # shown. Semgrep 1.157 always writes it; a version that moves it must
        # be looked at, not trusted.
        raise Unusable(f"{path.name}: no time.fixpoint_timeouts -- cannot tell whether "
                       f"taint analysis finished (semgrep {data.get('version', '?')})")
    return data


def _timeouts(data: dict) -> list[dict]:
    return data["time"]["fixpoint_timeouts"]


def _err_path(err: dict) -> str | None:
    p = err.get("path")
    if not p:
        spans = err.get("spans") or []
        p = spans[0].get("file") if spans and isinstance(spans[0], dict) else None
    return p or None


def _describe(item: dict) -> str:
    msg = str(item.get("message", "")).strip().splitlines()
    kind = item.get("error_type") or item.get("type") or "error"
    if isinstance(kind, list):
        kind = kind[0]
    return f"{kind}: {msg[0] if msg else '(no message)'}"


def incomplete(data: dict) -> tuple[set[str], list[str]]:
    """(files to rescan, problems no rescan can fix)."""
    paths, fatal = set(), []
    for t in _timeouts(data):
        p = (t.get("location") or {}).get("path")
        if p:
            paths.add(p)
        else:
            fatal.append(_describe(t))
    for e in data.get("errors") or []:
        p = _err_path(e)
        if p:
            paths.add(p)
        else:
            fatal.append(_describe(e))        # a rule or config error: no file to retry
    return paths, fatal


def merge(parallel: dict, serial: dict | None) -> tuple[dict, list[str]]:
    """Merged scan, and every reason it is still incomplete."""
    retried, fatal = incomplete(parallel)
    problems = [f"parallel scan: {d}" for d in fatal]
    keep = lambda items, path_of: [i for i in items if path_of(i) not in retried]
    out = dict(parallel)
    out["results"] = keep(parallel["results"], lambda r: r.get("path"))
    out["errors"] = keep(parallel.get("errors") or [], _err_path)
    out["time"] = dict(parallel["time"])
    out["time"]["fixpoint_timeouts"] = keep(_timeouts(parallel),
                                            lambda t: (t.get("location") or {}).get("path"))
    out["serial_retry"] = sorted(retried)
    if retried and serial is None:
        problems.extend(f"{p}: timed out or errored, and the serial rescan produced no results"
                        for p in sorted(retried))
        return out, problems
    if serial is not None:
        # Only the retried files' results are taken: anything else in the
        # serial file would be a second copy.
        out["results"] += [r for r in serial["results"] if r.get("path") in retried]
        out["errors"] += serial.get("errors") or []
        out["time"]["fixpoint_timeouts"] += _timeouts(serial)
        still, sfatal = incomplete(serial)
        problems.extend(f"serial rescan: {d}" for d in sfatal)
        for t in _timeouts(serial):
            p = (t.get("location") or {}).get("path")
            if p in still:
                problems.append(f"{p}: still incomplete with -j 1 -- {_describe(t)}")
        for e in serial.get("errors") or []:
            p = _err_path(e)
            if p in still:
                problems.append(f"{p}: still incomplete with -j 1 -- {_describe(e)}")
    return out, problems


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "retry-paths":
            paths, _ = incomplete(load(Path(argv[1])))
            for p in sorted(paths):
                print(p)
            return 0
        if len(argv) == 4 and argv[0] == "merge":
            parallel = load(Path(argv[1]))
            serial_path = Path(argv[2])
            serial = load(serial_path) if serial_path.exists() else None
            out, problems = merge(parallel, serial)
            Path(argv[3]).write_text(json.dumps(out), encoding="utf-8")
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1 if problems else 0
    except Unusable as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
