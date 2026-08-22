#!/usr/bin/env python3
"""secret_store.py — one place to read and write workflow secrets.

Every script that needs an API key, app password, or token resolves it
through get_secret(), in this order:

  1. The environment (after the caller's own load_dotenv). An explicitly
     set env var always wins, so .env files, CI, and one-off overrides
     keep working exactly as before.
  2. The platform keystore:
       macOS   — Keychain generic password (service = the secret's name
                 lowercased, account = the current user), read via
                 /usr/bin/security.
       Windows — a DPAPI-encrypted file under security_common.state_dir().
                 CryptUnprotectData only succeeds for the logged-in user.
       other   — a 0600 file under state_dir() (best effort, plainly a
                 weaker fallback).

Nothing here is required: an install that keeps everything in .env never
touches the keystore. Moving a secret out of .env and into the keystore is
what removes the plaintext from disk.

Why the macOS reads shell out to /usr/bin/security instead of a native
Keychain binding: Keychain ACLs bind to the *calling binary*. Homebrew's
python is ad-hoc signed, so every `brew upgrade python` would orphan the
grant, and under launchd the re-consent prompt can never be shown — the
job would hang. /usr/bin/security is Apple-signed and stable across python
upgrades. The tradeoff: the item is readable by anything that can execute
`security` as you. That is still strictly better than a plaintext .env —
reading requires code execution as the user, not a mere file read.

The service name is the secret's name lowercased, which deliberately
matches the manual convention the installer has always used (a lowercased
name, e.g. `anthropic_api_key`), so an install that already keeps a key in the
Keychain by hand needs no migration.

CLI (for setup and the installer):
  python3 secret_store.py set NAME     value read from stdin or a hidden
                                       prompt — never from argv
  python3 secret_store.py get NAME     prints the value (careful in shells
                                       with history expansion of output)
  python3 secret_store.py rm NAME
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

# security_common owns the platform forks for state_dir, DPAPI, and file
# permission hardening; reuse rather than re-implement.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import security_common  # noqa: E402


def _service(name: str) -> str:
    return name.strip().lower()


def _account() -> str:
    return getpass.getuser()


def _secrets_dir() -> Path:
    d = security_common.state_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- macOS (Keychain via /usr/bin/security) ---------------------------

# Hard ceiling on every /usr/bin/security call. A Keychain op that has not
# returned by now is not slow, it is wedged: something is waiting on a GUI
# consent dialog that an unattended run (launchd, --auto, CI) can never
# answer. Bound it, report failure, let the caller fall back to .env — and
# never retry, because every retry queues another dialog onto a stack that
# blocks all later Keychain ops on the machine, git-over-HTTPS through the
# osxkeychain helper included.
KEYCHAIN_TIMEOUT_SECONDS = 15


def _security(*args: str) -> subprocess.CompletedProcess | None:
    """Run /usr/bin/security under a hard timeout. None means it hung (and
    was killed) — a distinct outcome from a non-zero exit, and never a
    reason to try again."""
    try:
        return subprocess.run(
            ["/usr/bin/security", *args],
            check=False, capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"secret_store: /usr/bin/security {args[0]} timed out after "
              f"{KEYCHAIN_TIMEOUT_SECONDS}s; Keychain appears wedged behind a "
              "consent prompt. Not retrying.", file=sys.stderr)
        return None


def _keychain_get(name: str) -> str | None:
    p = _security("find-generic-password",
                  "-a", _account(), "-s", _service(name), "-w")
    if p is None or p.returncode != 0:
        return None
    value = p.stdout.rstrip("\n")
    return value if value else None


def _keychain_set(name: str, value: str) -> bool:
    # Two hard-won constraints (both verified live, 2026-08-18):
    #   1. Direct argv, NOT `security -i` — interactive mode hangs on
    #      realistic key lengths. Cost: the value is visible in the process
    #      table for the milliseconds the command runs; acceptable on a
    #      single-user machine, and how the installer's keychain_set has
    #      always worked.
    #   2. Delete-then-add, NOT `-U` — updating an existing item raises a
    #      GUI consent prompt whenever the item's ACL doesn't already cover
    #      the caller, and under launchd (or any unattended run) that prompt
    #      can never be answered: the call wedges silently. A fresh add
    #      never prompts. Deleting first loses nothing — we are about to
    #      write the authoritative value.
    #   3. Every call is bounded by KEYCHAIN_TIMEOUT_SECONDS. If the delete
    #      hangs we stop there rather than issuing the add: a wedged Keychain
    #      answers nothing, and a second call only queues a second dialog.
    # -T pre-authorizes /usr/bin/security itself so later reads by launchd
    # jobs (which all shell out to it) need no consent.
    if _security("delete-generic-password",
                 "-a", _account(), "-s", _service(name)) is None:
        return False
    p = _security("add-generic-password",
                  "-a", _account(), "-s", _service(name), "-w", value,
                  "-T", "/usr/bin/security")
    return p is not None and p.returncode == 0


def _keychain_delete(name: str) -> bool:
    p = _security("delete-generic-password",
                  "-a", _account(), "-s", _service(name))
    return p is not None and p.returncode == 0


# ---------- Windows (DPAPI file) / other (0600 file) --------------------------

def _file_path(name: str, *, dpapi: bool) -> Path:
    suffix = ".dpapi" if dpapi else ".txt"
    return _secrets_dir() / (_service(name) + suffix)


def _dpapi_get(name: str) -> str | None:
    path = _file_path(name, dpapi=True)
    if not path.exists():
        return None
    try:
        raw = security_common._dpapi_crypt(path.read_bytes(), protect=False)
        return raw.decode("utf-8") or None
    except Exception:
        return None


def _dpapi_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=True)
    try:
        blob = security_common._dpapi_crypt(value.encode("utf-8"), protect=True)
        path.write_bytes(blob)
        security_common.restrict_file(path)
        return True
    except Exception:
        return False


def _plainfile_get(name: str) -> str | None:
    path = _file_path(name, dpapi=False)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _plainfile_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=False)
    try:
        path.write_text(value + "\n", encoding="utf-8")
        security_common.restrict_file(path)
        return True
    except OSError:
        return False


# ---------- public API --------------------------------------------------------

def get_secret(name: str, *, use_env: bool = True) -> str | None:
    """Resolve a secret by NAME. Environment first (unless use_env=False),
    then the platform keystore. Returns None when nowhere has it."""
    if use_env:
        env = os.environ.get(name, "").strip()
        if env:
            return env
    if sys.platform == "darwin":
        return _keychain_get(name)
    if sys.platform == "win32":
        return _dpapi_get(name)
    return _plainfile_get(name)


def set_secret(name: str, value: str) -> bool:
    """Store a secret in the platform keystore. Returns False on failure —
    callers should treat that as 'keep using .env', not an exception."""
    if not value:
        return False
    if sys.platform == "darwin":
        return _keychain_set(name, value)
    if sys.platform == "win32":
        return _dpapi_set(name, value)
    return _plainfile_set(name, value)


def delete_secret(name: str) -> bool:
    if sys.platform == "darwin":
        return _keychain_delete(name)
    path = _file_path(name, dpapi=(sys.platform == "win32"))
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---------- CLI ----------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("set", "get", "rm"):
        print(__doc__.split("CLI", 1)[1], file=sys.stderr)
        return 2
    op, name = argv
    if op == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"Value for {name}: ").strip()
        else:
            value = sys.stdin.readline().strip()
        if not value:
            print("empty value; nothing stored", file=sys.stderr)
            return 1
        ok = set_secret(name, value)
        print("stored" if ok else "FAILED to store", name,
              file=sys.stderr)
        return 0 if ok else 1
    if op == "get":
        value = get_secret(name)
        if value is None:
            print(f"{name}: not found", file=sys.stderr)
            return 1
        print(value)
        return 0
    ok = delete_secret(name)
    print("removed" if ok else "nothing removed", name, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
