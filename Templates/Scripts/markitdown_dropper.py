#!/usr/bin/env python3
"""
Markitdown Dropper (PySide6, in-process markitdown)
---------------------------------------------------
A small always-on-top window. Drag files onto it; each file is converted
to Markdown using the markitdown Python API, run through a cleanup pass
(~/Obsidian/Templates/Scripts/markitdown_cleanup.py — extracts inline base64
images, normalizes bullet markers, promotes strict heading patterns,
generates frontmatter),
and saved into your Obsidian Creations folder.

Extracted images land in <vault>/Z_attachments/ (derived from the destination
folder's parent), referenced in the markdown via Obsidian wiki-links.

First run prompts for the destination folder and stores the choice at
~/.markitdown_dropper.json. Originals are left where they are. If the
output filename already exists, a timestamp is appended.

If markitdown_cleanup is not importable, a conversion is refused rather than
written raw: the cleanup pass is what gives the note the pipeline's own
frontmatter and tier, and without it a document's own frontmatter block would
become the note's (review, 2026-10-04).

A zip container (.zip/.docx/.xlsx/.pptx) is checked by archive_limits.py
BEFORE MarkItDown opens it, and refused when its declared member sizes or
count are over the limits: MarkItDown reads members whole, so a
small-but-inflating archive would otherwise exhaust memory inside convert().
Any other file over MAX_SOURCE_BYTES is refused, and every conversion runs in a
child process killed after CONVERT_TIMEOUT_SEC (finding 5278110): a hostile PDF
could otherwise keep pdfminer, and the dropper's worker with it, busy without
bound, since a thread cannot be stopped from outside.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QObject, Signal
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    sys.stderr.write(
        "PySide6 not installed.\n"
        "Re-run the installer: it builds this venv from requirements-dropper.lock.\n"
    )
    sys.exit(1)

try:
    from markitdown import MarkItDown
except ImportError:
    sys.stderr.write(
        "markitdown not installed.\n"
        "Re-run the installer: it builds this venv from requirements-dropper.lock.\n"
    )
    sys.exit(1)

# Make the per-vault Templates/Scripts/ importable so we can pick up
# markitdown_cleanup.py. If the import fails, conversions are refused (see the
# module docstring). Vault path defaults to ~/Obsidian and can be overridden
# with OBSIDIAN_VAULT for testing.
_vault = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "Obsidian"))).expanduser()
sys.path.insert(0, str(_vault / "Templates" / "Scripts"))
import templater_guard  # noqa: E402  -- outside text must not run as Templater code
import output_sanitizer  # noqa: E402  -- nor load remote images, HTML or other schemes on open
import archive_limits  # noqa: E402  -- refuse an inflating archive before convert()

try:
    from markitdown_cleanup import clean as cleanup_clean
except ImportError:
    cleanup_clean = None


CONFIG_PATH = Path.home() / ".markitdown_dropper.json"


# ---------- config -----------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------- conversion -------------------------------------------------------

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


def unique_destination(dest_dir: Path, base_name: str) -> Path:
    out = dest_dir / f"{base_name}.md"
    if not out.exists():
        return out
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return dest_dir / f"{base_name}-{stamp}.md"


def _embed_names(summary: dict) -> list:
    """The image files cleanup extracted and embedded as ![[name]]."""
    return [p.name for p in list(summary.get("images_extracted") or [])
            + list(summary.get("source_images") or [])]


def convert_one(md: MarkItDown, src_path: str, dest_dir: Path) -> tuple[bool, str]:
    src = Path(src_path)
    if not src.exists():
        return False, f"Not found: {src}"
    if src.is_dir():
        return False, f"Skipped folder: {src.name}"
    refused = archive_limits.refusal(src)
    if refused:
        return False, f"{src.name}: {refused}"
    try:
        size = src.stat().st_size
    except OSError as e:
        return False, f"{src.name}: {type(e).__name__}: {e}"
    if size > MAX_SOURCE_BYTES:
        return False, (f"{src.name}: refused: {size:,} bytes is over the "
                       f"{MAX_SOURCE_BYTES:,}-byte limit; not converted")

    out = unique_destination(dest_dir, src.stem)
    try:
        result = md.convert(str(src))
    except Exception as e:
        return False, f"{src.name}: {type(e).__name__}: {e}"

    text = getattr(result, "text_content", None) or getattr(result, "markdown", None) or ""
    title = getattr(result, "title", None)

    body = text
    if title:
        body = f"# {title}\n\n{text}"

    # Cleanup pass — extract base64 images, recover source-archive images for
    # any Markitdown stubs, normalize bullets, promote strict heading patterns,
    # ensure frontmatter. Attachments live alongside the destination folder
    # (typically <vault>/Z_attachments).
    cleanup_msg = ""
    if cleanup_clean is None:
        return False, (f"{src.name}: not converted: markitdown_cleanup is unavailable, "
                       "and without it the note would carry no pipeline frontmatter or tier")
    if cleanup_clean is not None:
        attachments_dir = dest_dir.parent / "Z_attachments"
        try:
            body, summary = cleanup_clean(body, src, attachments_dir)
            parts: list[str] = []
            total_imgs = len(summary["images_extracted"]) + len(summary["source_images"])
            if total_imgs:
                parts.append(f"{total_imgs} img")
            if summary["stubs_replaced"]:
                parts.append(f"{summary['stubs_replaced']} stubs→img")
            if summary["stubs_placeheld"]:
                parts.append(f"{summary['stubs_placeheld']} stubs→placeholder")
            if summary["bullets_normalized"]:
                parts.append(f"{summary['bullets_normalized']} bullets")
            if summary["headings_promoted"]:
                parts.append(f"{summary['headings_promoted']} headings")
            if parts:
                cleanup_msg = "  [cleaned: " + ", ".join(parts) + "]"
        except Exception as e:
            return False, f"{src.name}: cleanup failed: {type(e).__name__}: {e}"

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        # A document's remote images, HTML and non-web links would act when
        # the note opens; only the embeds cleanup wrote stay (F-20261020 5278628).
        body, _counts = output_sanitizer.sanitize_keeping_embeds(body, _embed_names(summary))
        out.write_text(templater_guard.neutralize(body), encoding="utf-8")
    except Exception as e:
        return False, f"{src.name}: write failed: {e}"

    size = out.stat().st_size
    return True, f"{src.name}  →  {out.name}  ({size:,} bytes){cleanup_msg}"


# ---------- worker (cross-thread signaling) ----------------------------------

class Worker(QObject):
    log = Signal(str)
    finished = Signal(int, int)  # ok, total

    def __init__(self, md: MarkItDown) -> None:
        super().__init__()
        self.md = md

    def submit(self, paths: list[str], dest_dir: Path) -> None:
        threading.Thread(
            target=self._run, args=(paths, dest_dir), daemon=True
        ).start()

    def _run(self, paths: list[str], dest_dir: Path) -> None:
        ok = 0
        try:
            for p in paths:
                self.log.emit(f"… working on {Path(p).name}")
                success, msg = convert_one(self.md, p, dest_dir)
                self.log.emit(("✓ " if success else "✗ ") + msg)
                ok += int(success)
        except Exception:
            self.log.emit("✗ Worker crashed:\n" + traceback.format_exc())
        finally:
            self.finished.emit(ok, len(paths))


# ---------- drop zone --------------------------------------------------------

DROP_BASE_QSS = (
    "QLabel { background-color: #f4f1ea; border: 2px dashed #999; "
    "border-radius: 8px; color: #3a3a3a; padding: 18px; }"
)
DROP_HOVER_QSS = (
    "QLabel { background-color: #e6dfc8; border: 2px dashed #5a7a3a; "
    "border-radius: 8px; color: #2a2a2a; padding: 18px; }"
)


class DropZone(QLabel):
    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__("⬇   Drop files here   ⬇")
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(DROP_BASE_QSS)
        self.setMinimumHeight(140)
        self.setFont(QFont("Helvetica", 15))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(DROP_HOVER_QSS)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet(DROP_BASE_QSS)

    def dropEvent(self, event) -> None:
        paths = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.isLocalFile()
        ]
        self.setStyleSheet(DROP_BASE_QSS)
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()


# ---------- main window ------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Markitdown Dropper")
        self.resize(500, 460)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # MarkItDown runs in a child process with a per-file time limit
        # (BoundedMarkItDown); it loads its models on the first drop.
        self.md = BoundedMarkItDown()

        self.dest = self._init_destination()
        if not self.dest:
            QMessageBox.warning(
                self, "No destination",
                "A destination folder is required. Exiting.",
            )
            sys.exit(0)

        self._build_ui()

        self.worker = Worker(self.md)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_finished)

        self._log(f"Ready. Output → {self.dest}")

    # ----- destination handling -----

    def _init_destination(self) -> str | None:
        cfg = load_config()
        d = cfg.get("destination")
        if d and Path(d).is_dir():
            return d
        d = self._prompt_destination()
        if d:
            cfg["destination"] = d
            save_config(cfg)
        return d

    def _prompt_destination(self) -> str | None:
        d = QFileDialog.getExistingDirectory(
            self, "Pick your Obsidian 'Creations' folder"
        )
        return d or None

    # ----- UI -----

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)

        title = QLabel("Drop files to convert with markitdown")
        title.setFont(QFont("Helvetica", 14, QFont.Bold))
        lay.addWidget(title)

        self.dest_label = QLabel(f"→  {self.dest}")
        self.dest_label.setStyleSheet("color: #666;")
        self.dest_label.setWordWrap(True)
        lay.addWidget(self.dest_label)

        self.drop = DropZone()
        self.drop.dropped.connect(self.on_dropped)
        lay.addWidget(self.drop, 1)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            "QTextEdit { background-color: #0f1115; color: #d8d8d8; "
            "font-family: Menlo, monospace; font-size: 12px; "
            "border-radius: 6px; }"
        )
        self.log.setFixedHeight(160)
        lay.addWidget(self.log)

        bar = QHBoxLayout()
        change_btn = QPushButton("Change folder…")
        change_btn.clicked.connect(self.change_dest)
        bar.addWidget(change_btn)

        open_btn = QPushButton("Open destination")
        open_btn.clicked.connect(self.open_dest)
        bar.addWidget(open_btn)

        bar.addStretch(1)

        self.topmost_cb = QCheckBox("Always on top")
        self.topmost_cb.setChecked(True)
        self.topmost_cb.toggled.connect(self.toggle_topmost)
        bar.addWidget(self.topmost_cb)

        lay.addLayout(bar)

    # ----- actions -----

    def _log(self, msg: str) -> None:
        self.log.append(msg)

    def toggle_topmost(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()  # required after changing window flags

    def change_dest(self) -> None:
        new = self._prompt_destination()
        if not new:
            return
        cfg = load_config()
        cfg["destination"] = new
        save_config(cfg)
        self.dest = new
        self.dest_label.setText(f"→  {new}")
        self._log(f"Destination changed to: {new}")

    def open_dest(self) -> None:
        try:
            if sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", self.dest], check=False)
            elif os.name == "nt":
                os.startfile(self.dest)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.run(["xdg-open", self.dest], check=False)
        except Exception as e:
            self._log(f"Could not open folder: {e}")

    def on_dropped(self, paths: list[str]) -> None:
        self._log(f"--- {len(paths)} item(s) at {datetime.now():%H:%M:%S} ---")
        self.worker.submit(paths, Path(self.dest))

    def _on_finished(self, ok: int, total: int) -> None:
        self._log(f"Done: {ok}/{total} converted.\n")


# ---------- entry point ------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
