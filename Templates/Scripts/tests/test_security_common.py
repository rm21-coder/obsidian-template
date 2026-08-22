"""
test_security_common.py — the shared, platform-aware helpers every security
control now delegates to.

This module had no dedicated test file: it was extracted as the common
Keychain/DPAPI/notify/permissions layer, and the controls' own suites only
exercised it indirectly, through fixtures that monkeypatch it out. That left
the Windows branches — DPAPI protect/unprotect, the icacls hardening —
covered nowhere at all.

Platform coverage: the trust-anchor tests are per-platform by necessity (each
branch calls into an OS API that exists nowhere else), so each is marked for
the platform it applies to and skips elsewhere. `restrict_file` and
`state_dir` are asserted on every platform.

Isolation: state_dir() is redirected at the module level for every test that
could otherwise write to the real ~/.local/share/obsidian-security or
%LOCALAPPDATA%\\obsidian-security. Nothing here touches the real Keychain —
the macOS-only tests mock subprocess.run — and nothing fires a real
notification.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import security_common as sc


# ---------------------------------------------------------------------------
# state_dir
# ---------------------------------------------------------------------------

class TestStateDir:

    def test_is_platform_appropriate(self) -> None:
        d = sc.state_dir()
        assert d.name == "obsidian-security"
        if sys.platform == "win32":
            # Under LOCALAPPDATA, which is per-user and not world-readable.
            local = os.environ.get("LOCALAPPDATA")
            if local:
                assert str(d).lower().startswith(local.lower())
        else:
            assert ".local/share" in d.as_posix()

    def test_honours_localappdata(self, monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
        if sys.platform != "win32":
            pytest.skip("LOCALAPPDATA is only consulted on Windows")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert sc.state_dir() == tmp_path / "obsidian-security"

    def test_falls_back_when_localappdata_unset(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        if sys.platform != "win32":
            pytest.skip("Windows-only fallback path")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        # Must still land somewhere sane rather than raising or returning a
        # relative path a scheduled task would resolve unpredictably.
        d = sc.state_dir()
        assert d.is_absolute()
        assert d.name == "obsidian-security"


# ---------------------------------------------------------------------------
# restrict_file — the icacls / chmod hardening
# ---------------------------------------------------------------------------

class TestRestrictFile:

    def test_restricts_a_real_file(self, tmp_path: Path, assert_owner_only,
                                   allow_subprocess: None) -> None:
        f = tmp_path / "state.json"
        f.write_text('{"a": 1}', encoding="utf-8")
        assert sc.restrict_file(f) is True
        assert_owner_only(f)

    def test_owner_can_still_read_and_write(self, tmp_path: Path,
                                            allow_subprocess: None) -> None:
        """Hardening must not lock the controls out of their own state -- they
        rewrite these files on every run."""
        f = tmp_path / "state.json"
        f.write_text("first", encoding="utf-8")
        sc.restrict_file(f)
        assert f.read_text(encoding="utf-8") == "first"
        f.write_text("second", encoding="utf-8")
        assert f.read_text(encoding="utf-8") == "second"

    def test_missing_file_returns_false_not_raise(
            self, tmp_path: Path, allow_subprocess: None) -> None:
        """Callers treat this as best-effort defense-in-depth and do not guard
        the call, so it must never raise."""
        assert sc.restrict_file(tmp_path / "nope.json") is False

    def test_degrades_quietly_when_the_backend_fails(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No allow_subprocess here on purpose: on Windows the autouse guard
        makes subprocess.run raise, which is exactly the failure mode this
        must swallow. On POSIX, force the equivalent by breaking chmod."""
        f = tmp_path / "state.json"
        f.write_text("x", encoding="utf-8")
        if sys.platform != "win32":
            def boom(*a, **k):
                raise OSError("chmod unavailable")
            monkeypatch.setattr(sc.os, "chmod", boom)
        assert sc.restrict_file(f) is False

    def test_returns_false_without_a_username(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
            allow_subprocess: None) -> None:
        if sys.platform != "win32":
            pytest.skip("USERNAME is only consulted on the Windows branch")
        f = tmp_path / "state.json"
        f.write_text("x", encoding="utf-8")
        monkeypatch.delenv("USERNAME", raising=False)
        assert sc.restrict_file(f) is False

    def test_system_is_referenced_by_sid_not_display_name(self) -> None:
        """The display name 'NT AUTHORITY\\SYSTEM' is localized and icacls
        matches the localized string, so a literal breaks on non-English
        Windows. Pin the well-known SID instead."""
        assert sc._SYSTEM_SID == "*S-1-5-18"


# ---------------------------------------------------------------------------
# Trust-anchor key store
# ---------------------------------------------------------------------------

class TestDpapiKeyStore:
    """Windows: a DPAPI-encrypted key file under state_dir()."""

    pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                    reason="DPAPI is Windows-only")

    @pytest.fixture(autouse=True)
    def _isolated_state(self, monkeypatch: pytest.MonkeyPatch,
                        tmp_path: Path) -> None:
        monkeypatch.setattr(sc, "state_dir", lambda: tmp_path)

    def test_round_trips_through_dpapi(self, allow_subprocess: None) -> None:
        secret = b"\x00\x01binary\xff payload"
        blob = sc._dpapi_crypt(secret, protect=True)
        assert blob != secret, "payload was not actually encrypted"
        assert sc._dpapi_crypt(blob, protect=False) == secret

    def test_creates_then_reuses_a_stable_key(self,
                                              allow_subprocess: None) -> None:
        first = sc.get_or_create_hmac_key("test-service", "test-account")
        assert first is not None and len(first) >= 16
        second = sc.get_or_create_hmac_key("test-service", "test-account")
        assert second == first, "key changed between runs; every previously " \
                               "signed envelope would fail verification"

    def test_key_file_is_not_plaintext(self, allow_subprocess: None,
                                       tmp_path: Path) -> None:
        key = sc.get_or_create_hmac_key("test-service", "test-account")
        blob = (tmp_path / "test-service.key.dpapi").read_bytes()
        assert key is not None
        assert key not in blob, "raw key material found in the file on disk"

    def test_key_file_is_owner_only(self, assert_owner_only,
                                    allow_subprocess: None,
                                    tmp_path: Path) -> None:
        sc.get_or_create_hmac_key("test-service", "test-account")
        assert_owner_only(tmp_path / "test-service.key.dpapi")

    def test_corrupt_blob_regenerates_rather_than_crashing(
            self, allow_subprocess: None, tmp_path: Path) -> None:
        sc.get_or_create_hmac_key("test-service", "test-account")
        key_path = tmp_path / "test-service.key.dpapi"
        key_path.write_bytes(b"not a dpapi blob")
        regenerated = sc.get_or_create_hmac_key("test-service", "test-account")
        assert regenerated is not None and len(regenerated) >= 16

    def test_short_key_is_treated_as_missing(self, allow_subprocess: None,
                                             tmp_path: Path) -> None:
        """A sub-128-bit key must not be accepted -- mirrors the trust-anchor
        rule the plugin_integrity_check suite asserts."""
        key_path = tmp_path / "test-service.key.dpapi"
        key_path.write_bytes(sc._dpapi_crypt(b"tooshort", protect=True))
        key = sc.get_or_create_hmac_key("test-service", "test-account")
        assert key is not None and len(key) >= 16
        assert key != b"tooshort"


class TestKeychainKeyStore:
    """macOS: /usr/bin/security. Fully mocked -- never touches a real Keychain."""

    pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                    reason="Keychain is macOS-only")

    def test_returns_existing_key(self, monkeypatch: pytest.MonkeyPatch,
                                  allow_subprocess: None) -> None:
        from types import SimpleNamespace
        existing = bytes(range(32))
        monkeypatch.setattr(
            sc.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0,
                                            stdout=existing.hex() + "\n",
                                            stderr=""))
        assert sc.get_or_create_hmac_key("svc", "acct") == existing

    def test_short_existing_key_is_replaced(
            self, monkeypatch: pytest.MonkeyPatch,
            allow_subprocess: None) -> None:
        from types import SimpleNamespace
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if "find-generic-password" in cmd:
                return SimpleNamespace(returncode=0, stdout="00ff\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sc.subprocess, "run", fake_run)
        key = sc.get_or_create_hmac_key("svc", "acct")
        assert key is not None and len(key) >= 16
        assert any("add-generic-password" in c for c in calls)

    def test_returns_none_when_it_cannot_persist(
            self, monkeypatch: pytest.MonkeyPatch,
            allow_subprocess: None) -> None:
        from types import SimpleNamespace
        monkeypatch.setattr(
            sc.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
        assert sc.get_or_create_hmac_key("svc", "acct") is None


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------

class TestNotify:

    def test_never_raises_when_the_backend_explodes(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """notify is called from alert paths -- if it raised, a genuine
        integrity alert would turn into a traceback instead of a warning."""
        def boom(*a, **k):
            raise OSError("no notification backend")
        monkeypatch.setattr(sc.subprocess, "run", boom)
        sc.notify("title", "message")  # must not raise

    def test_is_a_noop_on_unsupported_platforms(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[object] = []
        monkeypatch.setattr(sc.subprocess, "run",
                            lambda *a, **k: called.append(a))
        monkeypatch.setattr(sc.sys, "platform", "linux")
        sc.notify("title", "message")
        assert called == []


# ---------------------------------------------------------------------------
# append_alert — shared, size-capped alert log
# ---------------------------------------------------------------------------
# Rotation is the regression this pins: a pre-baseline install re-alerted the
# same drift on every run and grew alerts.log to 342 MB over 3.5 months.

class TestAppendAlert:

    @pytest.fixture
    def alert_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setattr(sc, "state_dir", lambda: tmp_path)
        return tmp_path

    def test_appends_json_line_with_timestamp(self, alert_dir: Path) -> None:
        sc.append_alert({"control": "t", "summary": "x"})
        lines = (alert_dir / "alerts.log").read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["summary"] == "x"
        assert row["ts"]  # stamped by append_alert

    def test_rotates_at_cap_and_gzips(self, monkeypatch: pytest.MonkeyPatch,
                                      alert_dir: Path) -> None:
        import gzip
        monkeypatch.setattr(sc, "ALERT_LOG_MAX_BYTES", 200)
        log = alert_dir / "alerts.log"
        log.write_text("x" * 250 + "\n")
        sc.append_alert({"summary": "after rotation"})
        archives = list(alert_dir.glob("alerts-*.log.gz"))
        assert len(archives) == 1
        assert gzip.open(archives[0], "rt").read().startswith("x" * 250)
        # live file restarted: only the new record
        rows = log.read_text().splitlines()
        assert len(rows) == 1
        assert json.loads(rows[0])["summary"] == "after rotation"

    def test_prunes_to_keep_count(self, monkeypatch: pytest.MonkeyPatch,
                                  alert_dir: Path) -> None:
        monkeypatch.setattr(sc, "ALERT_LOG_MAX_BYTES", 1)
        monkeypatch.setattr(sc, "ALERT_LOG_KEEP", 2)
        for i in range(5):
            (alert_dir / f"alerts-2026010{i}-000000.log.gz").write_bytes(b"old")
        (alert_dir / "alerts.log").write_text("overflow\n")
        sc.append_alert({"summary": "s"})
        archives = sorted(p.name for p in alert_dir.glob("alerts-*.log.gz"))
        assert len(archives) == 2
        # newest survive: the two most recent names sort last
        assert archives[0] > "alerts-20260102"

    def test_archives_invisible_to_json_stateddir_scan(
            self, monkeypatch: pytest.MonkeyPatch, alert_dir: Path) -> None:
        """The integrity monitor hashes *.json in the state dir; rotation
        artifacts must never match that glob or they'd read as drift."""
        monkeypatch.setattr(sc, "ALERT_LOG_MAX_BYTES", 1)
        (alert_dir / "alerts.log").write_text("overflow\n")
        sc.append_alert({"summary": "s"})
        assert list(alert_dir.glob("*.json")) == []

    def test_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
        monkeypatch.setattr(sc, "state_dir",
                            lambda: tmp_path / "missing" / "\0bad")
        sc.append_alert({"summary": "s"})  # must not raise
