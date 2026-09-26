"""platform_caps.py -- what this machine can do, measured, for honest skips.

The Windows ARM64 run of 2026-09-25 found tests that failed rather than
skipped because they needed something the platform lacks: symlinks (Windows
needs Developer Mode or elevation), FIFOs, POSIX modes, SIGALRM, launchd. A
failure there says "broken" when the truth is "not verified here", and the
two must never be confused in a security packet -- so each skip reason says
which control goes unverified on this machine.

Capabilities are PROBED where they can be, not inferred from the OS name: a
Windows account with Developer Mode can create symlinks, and then those tests
should run.
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
from pathlib import Path

import pytest


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(Path(d) / "target", Path(d) / "link")
            return True
        except (OSError, NotImplementedError):
            return False


CAN_SYMLINK = _can_symlink()
CAN_FIFO = hasattr(os, "mkfifo")
HAS_SIGALRM = hasattr(signal, "SIGALRM")
POSIX_MODES = os.name == "posix"
ON_MACOS = sys.platform == "darwin"

requires_symlinks = pytest.mark.skipif(
    not CAN_SYMLINK,
    reason="cannot create symlinks here (Windows: needs Developer Mode or "
           "elevation) -- the symlink defence this test proves is NOT verified "
           "on this machine")
requires_fifo = pytest.mark.skipif(
    not CAN_FIFO,
    reason="no os.mkfifo on this platform -- the FIFO defence is NOT verified here")
requires_sigalrm = pytest.mark.skipif(
    not HAS_SIGALRM,
    reason="no SIGALRM on this platform; the run deadline degrades to the socket "
           "timeout by design (source_mail_pull._arm_deadline)")
requires_posix_modes = pytest.mark.skipif(
    not POSIX_MODES,
    reason="POSIX file modes are not represented on this platform; the Windows "
           "ACL path (restrict_file / icacls) is covered separately")
macos_only = pytest.mark.skipif(not ON_MACOS, reason="launchd is macOS-only")


def make_cli_stub(directory: Path, name: str, source: str) -> Path:
    """An executable stand-in for a CLI (e.g. `claude`) that runs `source`.

    POSIX: a script with a shebang naming this interpreter. Windows has no
    shebang handling, and a .cmd wrapper would mangle arguments -- cmd.exe
    truncates at a newline, and meeting_pull passes a multi-line prompt. So on
    Windows this builds a real .exe the way pip builds console scripts: pip's
    own launcher (t64 / t64-arm), then a shebang line, then a zip holding
    __main__.py. CreateProcess then passes every argument through intact.
    """
    if os.name != "nt":
        p = directory / name
        p.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        p.chmod(0o755)
        return p
    import io
    import platform
    import zipfile
    try:
        from pip._vendor import distlib
    except ImportError:
        pytest.skip("pip's launcher templates are unavailable; cannot build a "
                    "Windows CLI stub, so this CLI-invoking test is NOT run here")
    arm = platform.machine().lower() in ("arm64", "aarch64")
    launcher = (Path(distlib.__file__).parent / ("t64-arm.exe" if arm else "t64.exe")).read_bytes()
    exe = sys.executable
    if " " in exe:
        exe = f'"{exe}"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", source)
    p = directory / f"{name}.exe"
    p.write_bytes(launcher + b"#!" + exe.encode("utf-8") + b"\n" + buf.getvalue())
    return p
