#!/usr/bin/env python3
"""security_common.py — shared, platform-aware helpers for the vault security
controls (integrity_monitor, plugin_integrity_check).

Centralizes the macOS/Windows/Linux forks so each control stays platform-neutral:
  - state_dir()             runtime state location
  - notify()                best-effort desktop notification
  - get_or_create_hmac_key() the trust-anchor key for signing an allowlist/baseline

Trust-anchor key store, by platform:
  - macOS   : Keychain (via /usr/bin/security), as before.
  - Windows : a DPAPI-encrypted file under state_dir(). CryptUnprotectData only
              succeeds for the logged-in user, giving parity with Keychain's
              user-scoped protection (a plain 0600 file wouldn't — any process
              running as the user could read AND forge it).
  - other   : a 0600 file (best-effort).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# ---------- state dir --------------------------------------------------------

def state_dir() -> Path:
    """Runtime state dir for the security controls."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "obsidian-security"
    return Path.home() / ".local" / "share" / "obsidian-security"


# ---------- file permission hardening ----------------------------------------

# SYSTEM's well-known SID. Referenced numerically because the display name
# ("NT AUTHORITY\SYSTEM") is localized and icacls matches on the localized
# string, so the literal breaks on a non-English Windows.
_SYSTEM_SID = "*S-1-5-18"


def restrict_file(path: Path) -> bool:
    """Restrict a state file to its owner. Best-effort: returns True if the
    restriction was applied, False if it could not be. Never raises.

    POSIX: chmod 0600, as before.

    Windows: os.chmod is nearly a no-op there -- it toggles the read-only
    attribute and nothing else, so a 0600 call leaves the file carrying
    whatever ACL it inherited and stat() still reports 0o666. Use icacls to
    drop inheritance and grant only the current user plus SYSTEM.

    Granting SYSTEM is deliberate, not a loosening: it is the closest parallel
    to macOS, where root reads a 0600 file freely. Administrators are dropped
    -- they can still take ownership, but that is a deliberate, auditable act
    rather than ambient read access.
    """
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False

    user = os.environ.get("USERNAME")
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{user}" if domain else user
    try:
        p = subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{principal}:(F)",
             "/grant:r", f"{_SYSTEM_SID}:(F)"],
            check=False, capture_output=True, text=True, timeout=15)
        return p.returncode == 0
    except Exception:
        # icacls missing, blocked, or (inside the test suite) the
        # block_unmocked_subprocess guard. Callers treat this as
        # defense-in-depth, so degrade quietly rather than failing the write.
        return False


# ---------- notifications ----------------------------------------------------

def notify(title: str, message: str) -> None:
    """Best-effort desktop notification. Never raises."""
    try:
        if sys.platform == "darwin":
            script = (
                f"display notification {json.dumps(message)} "
                f"with title {json.dumps(title)} sound name \"Submarine\""
            )
            subprocess.run(["osascript", "-e", script],
                           check=False, capture_output=True, timeout=5)
        elif sys.platform == "win32":
            ps1 = Path(__file__).resolve().parent / "windows" / "Send-Notification.ps1"
            if ps1.exists():
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                     "-Title", title, "-Message", message],
                    check=False, capture_output=True, timeout=15)
    except Exception:
        pass


# ---------- HMAC trust-anchor key --------------------------------------------

def get_or_create_hmac_key(service: str, account: str) -> bytes | None:
    """Return a >=16-byte key for HMAC-signing a trust anchor, creating it on
    first use. Returns None if a key can neither be read nor persisted."""
    if sys.platform == "darwin":
        return _keychain_get_or_create(service, account)
    if sys.platform == "win32":
        return _dpapi_get_or_create(service)
    return _file_get_or_create(service)


def _keychain_get_or_create(service: str, account: str) -> bytes | None:
    import secrets
    p = subprocess.run(
        ["/usr/bin/security", "find-generic-password",
         "-a", account, "-s", service, "-w"],
        check=False, capture_output=True, text=True)
    if p.returncode == 0:
        try:
            k = bytes.fromhex(p.stdout.strip())
            if len(k) >= 16:
                return k
        except ValueError:
            pass
    new = secrets.token_bytes(32)
    w = subprocess.run(
        ["/usr/bin/security", "add-generic-password",
         "-a", account, "-s", service, "-w", new.hex(), "-U"],
        check=False, capture_output=True, text=True)
    return new if w.returncode == 0 else None


def _dpapi_get_or_create(service: str) -> bytes | None:
    import secrets
    key_path = state_dir() / f"{service}.key.dpapi"
    if key_path.exists():
        try:
            k = _dpapi_crypt(key_path.read_bytes(), protect=False)
            if len(k) >= 16:
                return k
        except Exception:
            pass  # unreadable/corrupt — regenerate below
    new = secrets.token_bytes(32)
    try:
        blob = _dpapi_crypt(new, protect=True)
        state_dir().mkdir(parents=True, exist_ok=True)
        tmp = key_path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, key_path)
        # Belt and braces: the blob is already user-scoped by DPAPI, so this
        # guards against copy-off rather than local read.
        restrict_file(key_path)
        return new
    except Exception:
        return None


def _dpapi_crypt(data: bytes, *, protect: bool) -> bytes:
    """Encrypt (protect=True) or decrypt (protect=False) bytes with Windows
    DPAPI (crypt32) scoped to the current user. Raises on failure."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data),
                        ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = fn(ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return out
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _file_get_or_create(service: str) -> bytes | None:
    import secrets
    key_path = state_dir() / f"{service}.key"
    if key_path.exists():
        try:
            k = bytes.fromhex(key_path.read_text().strip())
            if len(k) >= 16:
                return k
        except (ValueError, OSError):
            pass
    new = secrets.token_bytes(32)
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        tmp = key_path.with_suffix(".tmp")
        tmp.write_text(new.hex())
        os.replace(tmp, key_path)
        restrict_file(key_path)
        return new
    except OSError:
        return None


# ---------- alert log (shared, size-capped) -----------------------------------

ALERT_LOG_MAX_BYTES = 10 * 1024 * 1024   # rotate when the live file hits 10 MB
ALERT_LOG_KEEP = 3                        # compressed generations retained


def append_alert(record: dict) -> None:
    """Append one JSON line to state_dir()/alerts.log, rotating first when
    the file exceeds ALERT_LOG_MAX_BYTES.

    Rotation exists because this file is append-only by design and, before a
    baseline is adopted, the integrity control re-alerts the same drift on
    every run — a real install accumulated 342 MB in 3.5 months that way. On
    rotation the live file is gzipped to alerts-YYYYmmdd-HHMMSS.log.gz
    (owner-only, like every state file) and restarted; only the newest
    ALERT_LOG_KEEP archives are kept. The archives are .log.gz, so the
    integrity monitor's *.json state-dir scan never sees them.

    Alerting must never break a control: any failure here is reported to
    stderr and swallowed.
    """
    import datetime as _dt
    import gzip

    sd = state_dir()
    log = sd / "alerts.log"
    try:
        sd.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size >= ALERT_LOG_MAX_BYTES:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            archive = sd / f"alerts-{stamp}.log.gz"
            with log.open("rb") as src, gzip.open(archive, "wb") as dst:
                for chunk in iter(lambda: src.read(65536), b""):
                    dst.write(chunk)
            restrict_file(archive)
            log.unlink()
            for stale in sorted(sd.glob("alerts-*.log.gz"))[:-ALERT_LOG_KEEP]:
                try:
                    stale.unlink()
                except OSError:
                    pass
    except Exception as exc:
        print(f"[security_common] alert-log rotation failed: {exc}",
              file=sys.stderr)

    record["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
    try:
        existed = log.exists()
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if not existed:
            restrict_file(log)
    except Exception as exc:
        print(f"[security_common] alert append failed: {exc}", file=sys.stderr)
