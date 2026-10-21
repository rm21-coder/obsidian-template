"""markitdown_bounded.py -- MarkItDown with a size cap and a time limit.

Shared by the Markitdown Dropper app and the markitdown_convert.py command
line (findings 5278110 and its CLI twin). A document usually came from someone
else: MarkItDown and pdfminer have no bound of their own, so a hostile PDF
could hold a conversion forever or exhaust memory. Every file over
MAX_SOURCE_BYTES is refused before conversion, and each conversion runs in a
long-lived child process that is killed after CONVERT_TIMEOUT_SEC; the next
file starts a fresh child. Standard library only; the child imports
MarkItDown from the interpreter that runs this module.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


# A dropped file usually came from someone else. Zip containers have their own
# member limits (archive_limits); this bounds every file, a PDF included, by
# size before conversion and by wall clock during it.
MAX_SOURCE_BYTES = 100 * 1024 * 1024
CONVERT_TIMEOUT_SEC = 600

# The converter child, run by the dropper venv's own interpreter under -I. It
# loads MarkItDown once (its models take about a second warm, ten cold) and
# then answers one request per line: {"path": ...} in, {"text", "title"} or
# {"error"} out, all ASCII JSON so no console code page is involved. Anything
# a library prints to fd 1 is sent to fd 2, so it cannot corrupt a reply.
_CHILD_CODE = (
    "import json, os, sys\n"
    "reply = os.fdopen(os.dup(1), 'w')\n"
    "os.dup2(2, 1)\n"
    "sys.stdout = sys.stderr\n"
    "from markitdown import MarkItDown\n"
    "md = MarkItDown()\n"
    "for line in sys.stdin:\n"
    "    try:\n"
    "        r = md.convert(json.loads(line)['path'])\n"
    "        text = getattr(r, 'text_content', None) or getattr(r, 'markdown', None) or ''\n"
    "        title = getattr(r, 'title', None)\n"
    "        out = {'text': str(text), 'title': None if title is None else str(title)}\n"
    "    except Exception as e:\n"
    "        out = {'error': (type(e).__name__ + ': ' + str(e))[:300]}\n"
    "    reply.write(json.dumps(out) + '\\n')\n"
    "    reply.flush()\n"
)


class ConversionFailed(Exception):
    """The child conversion timed out, failed, or printed no result."""


class _Converted:
    def __init__(self, text: str, title: str | None) -> None:
        self.text_content = text
        self.title = title


class BoundedMarkItDown:
    """MarkItDown's convert(), run in a long-lived child process with a time
    limit per file. A conversion past the limit kills the child; the next
    file starts a fresh one."""

    def __init__(self, timeout: float = CONVERT_TIMEOUT_SEC) -> None:
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue | None = None
        self._lock = threading.Lock()

    def _start(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _CHILD_CODE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        replies: queue.Queue = queue.Queue()

        def _read() -> None:
            for line in proc.stdout:
                replies.put(line)
            replies.put(None)            # the child exited

        threading.Thread(target=_read, daemon=True).start()
        self._proc, self._replies = proc, replies

    def _stop(self) -> None:
        proc, self._proc, self._replies = self._proc, None, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass

    def convert(self, path: str) -> _Converted:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._stop()
                self._start()
            try:
                self._proc.stdin.write(
                    (json.dumps({"path": path}) + "\n").encode("ascii"))
                self._proc.stdin.flush()
                line = self._replies.get(timeout=self.timeout)
            except queue.Empty:
                self._stop()
                raise ConversionFailed(
                    f"conversion took longer than {self.timeout:g} s and was stopped")
            except OSError:
                self._stop()
                raise ConversionFailed("the converter process is not running")
            if line is None:
                self._stop()
                raise ConversionFailed("the converter process exited")
            try:
                out = json.loads(line.decode("ascii"))
            except ValueError:
                self._stop()
                raise ConversionFailed("the converter replied with something unreadable")
            if not isinstance(out, dict) or "error" in out or "text" not in out:
                raise ConversionFailed(str(out.get("error") if isinstance(out, dict)
                                           else out)[:300])
            title = out.get("title")
            return _Converted(str(out["text"]),
                              None if title is None else str(title))


def size_refusal(path) -> str | None:
    """Why `path` must not be converted for size, or None."""
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        return f"cannot read its size: {exc}"
    if size > MAX_SOURCE_BYTES:
        return (f"{size:,} bytes is over the {MAX_SOURCE_BYTES:,}-byte limit; "
                "not converted")
    return None
