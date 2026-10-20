#!/usr/bin/env python3
"""Run a scheduled job with a stack dump armed in case it hangs.

    python job_boot.py <dump-after-seconds> <script.py> [script args...]

run_logged.py starts every Windows scheduled job through this when the run
has a time limit. If the job is still running after <dump-after-seconds>,
faulthandler writes the stack of every thread to stderr -- which run_logged
sends to the job's log -- and the job carries on until run_logged's own limit
stops it. The run itself is unchanged: the script runs as __main__ with its
own arguments and its folder first on sys.path, and its exit code is the
process's.

Why: a Windows tagger run hung for over a day (2026-10-11) with no network
call open and nothing in its log, and with no stack-dump tool on the machine
there was no way to see where. faulthandler's watchdog is a C thread, so it
dumps even while the main thread is blocked inside a system call.

Stdlib only, and Python 3.9-compatible like the rest of the scripts' shared
layer.
"""
import faulthandler
import runpy
import sys
from pathlib import Path


def main(argv: list) -> None:
    if len(argv) < 2:
        sys.stderr.write("usage: job_boot.py <dump-after-seconds> <script.py> [args...]\n")
        raise SystemExit(2)
    after, script = float(argv[0]), argv[1]
    if after > 0:
        faulthandler.dump_traceback_later(after, repeat=False, file=sys.stderr, exit=False)
    sys.argv = [script, *argv[2:]]
    # Isolated (-I, the security tasks: 5278112), sys.path[0] is the standard
    # library, not this folder, and the job is meant to run without its own
    # folder in front of it; it finds its siblings itself.
    if not sys.flags.isolated:
        sys.path[0] = str(Path(script).resolve().parent)
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main(sys.argv[1:])
