"""
conftest.py — shared pytest fixtures for the security-controls test suite.

The modules under test live one directory up, at `Templates/Scripts/`
(integrity_monitor.py, plugin_integrity_check.py,
youtube_summarize.py) — resolved relative to this file so the suite runs
straight out of a repo checkout, no vault install required. We add that
directory to sys.path so tests can `import integrity_monitor` etc.

Note: the retired process-audit control (see docs/Security-Harness.md) —
it's a newer addition (with its own Windows port) and hasn't had tests
written for it. See test_static.py for the invariants that ARE covered.

Tests run fully mocked by default — they NEVER touch:
  - the real macOS Keychain (subprocess.run for /usr/bin/security is
    monkeypatched to a dict-backed fake)
  - the real ~/.local/share/obsidian-security/ (each test gets its own
    tmp dir that we use to override STATE_DIR via monkeypatch)
  - the real Obsidian vault (sample plugin dirs are synthesized in tmp)
  - the real network (url_safety security tests use a local httpd
    bound to 127.0.0.1 on a random port and is_safe_url tests
    monkeypatch socket.getaddrinfo). This is now ENFORCED, not just
    intended: block_external_dns is autouse and fails any test that
    resolves a real hostname. It was added after seven tests were found
    depending on live DNS.
  - the macOS unified log (`log show` output is mocked via
    subprocess.run patches)

This means the suite is safe to run repeatedly during development and
in CI-like contexts without polluting state.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Locate the runtime scripts and inject them onto sys.path so tests can
# import them as modules. Resolved relative to this file (Templates/Scripts/
# tests/conftest.py -> Templates/Scripts/), so the suite works from any repo
# checkout without requiring a live vault install first.
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

if not SCRIPTS_DIR.is_dir():
    raise RuntimeError(f"Templates/Scripts/ not found at {SCRIPTS_DIR}.")

sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Network isolation: no test may resolve a real hostname.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def block_external_dns(request: pytest.FixtureRequest,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast when a test depends on live DNS.

    This file promises the suite never touches the real network, but
    is_safe_url grew a socket.getaddrinfo call in v1.7 and two tests quietly
    started depending on real resolution. They passed on a developer laptop
    and failed offline and on any sandboxed CI runner -- the worst kind of
    flake, because the failure looks like a code regression and appears only
    where nobody is watching.

    A test that needs resolution monkeypatches getaddrinfo itself (several
    already do, mapping a template hostname to a public IP). This turns
    forgetting into an immediate, legible failure instead of an
    environment-dependent one.

    Loopback names and IP literals are passed through: they need no resolver,
    and the local-httpd security tests bind 127.0.0.1.
    """
    if "allow_external_dns" in request.fixturenames:
        return

    real_getaddrinfo = socket.getaddrinfo
    passthrough = {"localhost", "ip6-localhost", "127.0.0.1", "::1", ""}

    def guarded(host, port, *args, **kwargs):
        name = str(host or "").lower()
        if name in passthrough:
            return real_getaddrinfo(host, port, *args, **kwargs)
        try:
            ipaddress.ip_address(name)          # literal: no resolver needed
        except ValueError:
            raise AssertionError(
                f"test resolved the real hostname {name!r} via "
                "socket.getaddrinfo. The suite must not depend on DNS -- "
                "monkeypatch socket.getaddrinfo to a fixed address (see "
                "test_accepts_public_hostname for the pattern), or request "
                "the allow_external_dns fixture if a live lookup is truly "
                "the thing under test."
            ) from None
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> str:
    """Resolve every hostname to one fixed public address.

    For tests that need is_safe_url to reach its post-resolution logic: the
    point under test is "a public hostname is accepted", not "this particular
    domain exists today". Patching the shared socket module covers every
    caller, including the duplicate predicate in url_safety, which is what the
    parity tests need.

    Returns the address so a test can assert against it.
    """
    addr = "93.184.216.34"  # public, documentation-range-adjacent, not internal

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return addr


@pytest.fixture
def allow_external_dns() -> None:
    """Opt out of block_external_dns. Nothing uses this today; it exists so
    the escape hatch is explicit and greppable rather than improvised."""
    return None


# ---------------------------------------------------------------------------
# Path fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    """Templates/Scripts/ — runtime location of the scripts and plists under test."""
    return SCRIPTS_DIR


# ---------------------------------------------------------------------------
# State-directory isolation.
#
# Both integrity_monitor.py and plugin_integrity_check.py write to the
# module-level STATE_DIR (~/.local/share/obsidian-security/). We replace
# that with a tmp dir for every test so we don't pollute real state.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh tmp state directory: STATE_DIR / ALLOWLIST_PATH / STATE_PATH
    monkeypatched on both control modules, and security_common.state_dir
    redirected so the shared alert log lands here too."""
    state = tmp_path / "obsidian-security"
    state.mkdir(parents=True, exist_ok=True)

    # plugin_integrity_check
    import plugin_integrity_check as pic
    monkeypatch.setattr(pic, "STATE_DIR", state)
    monkeypatch.setattr(pic, "ALLOWLIST_PATH", state / "plugin_allowlist.json")

    # integrity_monitor
    import integrity_monitor as im
    monkeypatch.setattr(im, "STATE_DIR", state)
    monkeypatch.setattr(im, "STATE_PATH", state / "integrity_state.json")

    # append_alert is shared now (security_common, size-capped): redirect its
    # state_dir so alert writes land here, never in the real state dir.
    import security_common as sc
    monkeypatch.setattr(sc, "state_dir", lambda: state)

    return state


# ---------------------------------------------------------------------------
# Fake Keychain.
#
# We replace _keychain_read_key / _keychain_write_key in
# plugin_integrity_check with dict-backed fakes so tests never call out
# to /usr/bin/security and never read or write the real Keychain.
# ---------------------------------------------------------------------------

class FakeKeychain:
    """Dict-backed in-memory Keychain. Use to track what was written."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.read_calls: list[None] = []
        self.write_calls: list[bytes] = []

    def read(self) -> bytes | None:
        self.read_calls.append(None)
        return self._store.get("hmac_key")

    def write(self, key: bytes) -> bool:
        self.write_calls.append(key)
        self._store["hmac_key"] = key
        return True

    def preset(self, key: bytes) -> None:
        """Pre-seed a key as if a previous run had created it."""
        self._store["hmac_key"] = key


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    """Replace security_common.get_or_create_hmac_key — the shared,
    platform-aware trust-anchor lookup used by plugin_integrity_check (and
    integrity_monitor) — with an in-memory fake. Returns the fake so tests
    can inspect calls or pre-seed a key, mirroring the real function's
    get-or-create-with-validation contract (a too-short existing key is
    treated as missing and regenerated)."""
    import secrets as secrets_mod
    import security_common
    fake = FakeKeychain()

    def _fake_get_or_create(service: str, account: str) -> bytes:
        existing = fake.read()
        if existing is not None and len(existing) >= 16:
            return existing
        new_key = secrets_mod.token_bytes(32)
        fake.write(new_key)
        return new_key

    monkeypatch.setattr(security_common, "get_or_create_hmac_key",
                        _fake_get_or_create)
    return fake


# ---------------------------------------------------------------------------
# Silent notify — replace osascript shell-out with a recorder.
# ---------------------------------------------------------------------------

@pytest.fixture
def silent_notify(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture notify() calls instead of firing real desktop notifications.
    Returns a list of (title, message) tuples that tests can assert on.

    Both plugin_integrity_check and integrity_monitor call the shared
    security_common.notify() rather than defining their own, so patching
    it once there covers both."""
    captured: list[tuple[str, str]] = []

    def fake_notify(title: str, message: str) -> None:
        captured.append((title, message))

    import security_common
    monkeypatch.setattr(security_common, "notify", fake_notify)
    return captured


# ---------------------------------------------------------------------------
# Sample plugin directory generator.
# ---------------------------------------------------------------------------

def _write_plugin(plugins_root: Path, *, plugin_id: str, name: str = "",
                  version: str = "1.0.0", main_body: str = "// noop\n",
                  extra_manifest: dict | None = None) -> Path:
    """Synthesize a minimal Obsidian plugin in plugins_root/<plugin_id>/."""
    p = plugins_root / plugin_id
    p.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": name or plugin_id,
        "version": version,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (p / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (p / "main.js").write_text(main_body)
    return p


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    """Build a fake Obsidian vault layout under tmp_path with two plugins
    (`templater-obsidian` and `dataview`) and some markdown files. Returns
    the vault root."""
    vault = tmp_path / "Obsidian"
    vault.mkdir(parents=True)
    plugins = vault / ".obsidian" / "plugins"
    plugins.mkdir(parents=True)
    _write_plugin(plugins, plugin_id="templater-obsidian", version="2.0.0",
                  main_body="// templater bundle v2\n")
    _write_plugin(plugins, plugin_id="dataview", version="0.5.0",
                  main_body="// dataview bundle\n")
    # A few real-shaped markdown files.
    for i in range(10):
        (vault / f"note-{i}.md").write_text(f"# Note {i}\n\nbody\n")
    # Hidden / excluded paths the integrity scan must skip.
    (vault / ".trash").mkdir()
    (vault / ".trash" / "deleted.md").write_text("# trash\n")
    (vault / ".obsidian" / "config.md").write_text("# config\n")
    return vault


@pytest.fixture
def write_plugin():
    """Allow tests to add plugins on demand to a chosen vault."""
    return _write_plugin


# ---------------------------------------------------------------------------
# Subprocess gating — global guard.
#
# A safety net: any subprocess.run call that escapes the test mocks
# raises so we surface accidental real shell-outs immediately.
# Tests that legitimately need subprocess.run can scope a more
# permissive monkeypatch in the test itself.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def block_unmocked_subprocess(request: pytest.FixtureRequest,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real subprocess.run unless the test opts out by requesting
    the `allow_subprocess` fixture."""
    if "allow_subprocess" in request.fixturenames:
        return
    real_run = subprocess.run

    def gated(*args, **kwargs):
        # Allow trivial safe commands that some tests use (none right
        # now, but keep the structure so we can extend later).
        cmd = args[0] if args else kwargs.get("args", [])
        raise RuntimeError(
            f"Unmocked subprocess.run({cmd!r}) called inside a test. "
            "Either use the fake_keychain / silent_notify fixtures, "
            "or request the allow_subprocess fixture for tests that "
            "really need to shell out.")

    monkeypatch.setattr(subprocess, "run", gated)


@pytest.fixture
def allow_subprocess() -> None:
    """Marker fixture: tests that include this in their args opt out
    of the subprocess block."""
    return None


# ---------------------------------------------------------------------------
# Owner-only permission assertion.
#
# The controls express "owner only" differently per platform -- chmod 0600 on
# POSIX, an icacls ACL on Windows -- so tests assert the intent rather than
# the POSIX mode bits, which Windows does not enforce (os.chmod there only
# toggles the read-only attribute; stat() reports 0o666 no matter what).
# ---------------------------------------------------------------------------

def _win_aces(path: Path) -> list[str]:
    """Explicit ACEs on a file, as icacls prints them ('PRINCIPAL:(F)')."""
    out = subprocess.run(["icacls", str(path)],
                         capture_output=True, text=True).stdout
    aces = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        # The first line is prefixed with the path; strip it so only the ACE
        # remains. Trailing summary lines carry no ":(" and drop out here.
        line = line.replace(str(path), "", 1).strip()
        if ":(" in line:
            aces.append(line)
    return aces


@pytest.fixture
def assert_owner_only():
    """Assert a state file is restricted to its owner.

    Windows tests using this must also request `allow_subprocess`: both the
    icacls call under test and this verification shell out, and the autouse
    guard would otherwise block them.
    """
    def _assert(path: Path) -> None:
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600, (
                f"{path} is not 0600")
            return

        aces = _win_aces(path)
        # Inheritance must be broken -- an inherited ACE prints an (I) flag,
        # and inherited entries are exactly what we are dropping.
        inherited = [a for a in aces if "(I)" in a]
        assert not inherited, f"{path} still carries inherited ACEs: {inherited}"
        # Only the current user and SYSTEM survive. Compared case-insensitively
        # against the account name rather than a localized display string.
        user = os.environ.get("USERNAME", "")
        allowed_markers = [user.lower(), "system", "s-1-5-18"]
        for ace in aces:
            principal = ace.split(":(")[0].strip().lower()
            assert any(m and m in principal for m in allowed_markers), (
                f"{path} grants access to an unexpected principal: {ace}")
        assert aces, f"{path} has no explicit ACEs at all"

    return _assert
