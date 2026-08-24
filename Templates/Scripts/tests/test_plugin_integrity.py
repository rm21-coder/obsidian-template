"""
test_plugin_integrity.py — the v1.4 HMAC envelope is the headline.

Coverage
--------
- TestKeychainHelpers   _require_hmac_key: creates on miss, reads on hit
- TestCanonicalSerialization  HMAC input is order-stable across Python runs
- TestEnvelopeRoundTrip save_allowlist → load_allowlist returns the same dict
- TestTamperDetection   mutating state, hmac, or envelope shape fires
                        ALLOWLIST_TAMPER + non-zero exit + alert log entry
- TestLegacyMigration   pre-v1.4 flat-format files load once, then re-save
                        wraps them
- TestScanPlugins       complete / missing-manifest / missing-main /
                        malformed-manifest cases
- TestDiff              NEW / REMOVED / BUNDLE_CHANGE (same version) /
                        VERSION_CHANGE / MANIFEST_DRIFT
- TestEndToEndMain      drive main() through --update → check cycle on
                        a synthetic vault, plus tamper scenario
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

import plugin_integrity_check as pic


# ---------------------------------------------------------------------------
# Keychain helpers
# ---------------------------------------------------------------------------

class TestKeychainHelpers:

    def test_creates_key_on_first_call(self, fake_keychain) -> None:
        assert fake_keychain.read() is None  # initially empty
        key = pic._require_hmac_key()
        assert isinstance(key, bytes)
        assert len(key) == 32  # 256-bit
        # And that the fake Keychain saw a write.
        assert len(fake_keychain.write_calls) == 1

    def test_reads_existing_key(self, fake_keychain) -> None:
        preset = secrets.token_bytes(32)
        fake_keychain.preset(preset)
        # Reset call counts so we count only this test's reads/writes.
        fake_keychain.read_calls.clear()
        fake_keychain.write_calls.clear()
        got = pic._require_hmac_key()
        assert got == preset
        # Should NOT have written a new key.
        assert fake_keychain.write_calls == []

    def test_short_key_treated_as_missing(self, fake_keychain) -> None:
        """A truncated stored key should not be silently used —
        regenerate. (Defense against tampering with the Keychain entry
        itself.)"""
        fake_keychain.preset(b"\x00\x01")  # 2 bytes — too short
        fake_keychain.write_calls.clear()
        key = pic._require_hmac_key()
        assert len(key) == 32
        assert len(fake_keychain.write_calls) == 1


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

class TestCanonicalSerialization:
    """The HMAC must hash a canonical, byte-stable representation.
    Otherwise two semantically-identical states could produce different
    HMACs across Python versions / platforms."""

    def test_key_order_does_not_matter(self) -> None:
        a = {"plugin-a": {"version": "1", "main_sha256": "x"}}
        # Same content but built with reversed insertion order.
        b = {}
        b["plugin-a"] = {"main_sha256": "x", "version": "1"}
        assert (pic._canonical_state_bytes(a)
                == pic._canonical_state_bytes(b))

    def test_no_whitespace_in_canonical(self) -> None:
        out = pic._canonical_state_bytes({"k": "v"})
        # separators=(",", ":") — no spaces.
        assert b" " not in out


# ---------------------------------------------------------------------------
# Envelope round-trip
# ---------------------------------------------------------------------------

class TestEnvelopeRoundTrip:

    def test_save_load_identity(self, fake_keychain, tmp_state_dir) -> None:
        original = {
            "templater-obsidian": {
                "name": "Templater", "version": "2.0.0",
                "main_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
            },
        }
        pic.save_allowlist(original)
        loaded = pic.load_allowlist()
        assert loaded == original

    def test_envelope_format_on_disk(self, fake_keychain,
                                     tmp_state_dir) -> None:
        pic.save_allowlist({"x": {"version": "1"}})
        raw = json.loads(pic.ALLOWLIST_PATH.read_text(encoding="utf-8"))
        assert "state" in raw
        assert "hmac" in raw
        assert raw.get("envelope_version") == 1

    def test_file_is_owner_only(self, fake_keychain, tmp_state_dir,
                                assert_owner_only,
                                allow_subprocess: None) -> None:
        """Defense-in-depth alongside the HMAC envelope: chmod 0600 on POSIX,
        an icacls ACL on Windows."""
        pic.save_allowlist({})
        assert_owner_only(pic.ALLOWLIST_PATH)


# ---------------------------------------------------------------------------
# Tamper detection — the v1.4 attack scenarios.
# ---------------------------------------------------------------------------

class TestTamperDetection:

    def test_mutated_state_field_fires_tamper(
            self, fake_keychain, tmp_state_dir, silent_notify) -> None:
        pic.save_allowlist({
            "evil-plugin": {"version": "1.0.0", "main_sha256": "OLD"},
        })
        # An attacker swaps the bundle hash in-place to neutralize a
        # later check.
        raw = json.loads(pic.ALLOWLIST_PATH.read_text())
        raw["state"]["evil-plugin"]["main_sha256"] = "ATTACKER_FAVORED"
        pic.ALLOWLIST_PATH.write_text(json.dumps(raw, indent=2))

        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1

        # Tamper alert fired.
        assert any("ALLOWLIST_TAMPER" in m for _, m in silent_notify)
        # And was logged.
        log = (tmp_state_dir / "alerts.log").read_text()
        assert "ALLOWLIST_TAMPER" in log

    def test_mutated_hmac_field_fires_tamper(
            self, fake_keychain, tmp_state_dir, silent_notify) -> None:
        pic.save_allowlist({"x": {"version": "1"}})
        raw = json.loads(pic.ALLOWLIST_PATH.read_text())
        # Flip one byte in the HMAC.
        raw["hmac"] = "0" * 64
        pic.ALLOWLIST_PATH.write_text(json.dumps(raw, indent=2))
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1

    def test_malformed_envelope_fires_tamper(
            self, fake_keychain, tmp_state_dir, silent_notify) -> None:
        # state present but hmac missing — envelope is malformed and
        # cannot be verified. Treat as tamper, not just legacy.
        pic.ALLOWLIST_PATH.write_text(json.dumps(
            {"state": {"x": {}}, "hmac": 12345}))  # hmac wrong type
        with pytest.raises(SystemExit):
            pic.load_allowlist()

    def test_swapped_envelope_with_unknown_key_fails(
            self, tmp_state_dir, monkeypatch, silent_notify) -> None:
        """An attacker who substitutes their own HMAC key entirely
        (and recomputes hmac under it) cannot pass — verification
        uses the Keychain key, which they don't control."""
        # Inline a fake trust-anchor lookup so we don't depend on the
        # cross-module fixture and we control the exact key in use.
        import security_common
        defender_key = secrets.token_bytes(32)
        monkeypatch.setattr(security_common, "get_or_create_hmac_key",
                            lambda service, account: defender_key)
        pic.save_allowlist({"x": {"version": "1"}})

        # Attacker re-wraps with a different key.
        import hashlib
        import hmac as hmac_mod
        attacker_key = secrets.token_bytes(32)
        forged_state = {"x": {"version": "999"}}
        forged_hmac = hmac_mod.new(
            attacker_key,
            pic._canonical_state_bytes(forged_state),
            hashlib.sha256).hexdigest()
        pic.ALLOWLIST_PATH.write_text(json.dumps(
            {"state": forged_state, "hmac": forged_hmac,
             "envelope_version": 1}))

        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:

    def test_flat_format_loads_as_legacy(
            self, fake_keychain, tmp_state_dir,
            capsys: pytest.CaptureFixture) -> None:
        """A pre-v1.4 file with no envelope must load successfully once,
        so the patch deployment doesn't break before the first --update."""
        pic.ALLOWLIST_PATH.write_text(json.dumps({
            "templater-obsidian": {"version": "2.0.0"},
        }))
        loaded = pic.load_allowlist()
        assert "templater-obsidian" in loaded
        # And we surfaced a clear stderr note about it.
        captured = capsys.readouterr()
        assert "migrating" in captured.err.lower()

    def test_save_after_legacy_load_wraps(
            self, fake_keychain, tmp_state_dir) -> None:
        pic.ALLOWLIST_PATH.write_text(json.dumps({"x": {"version": "1"}}))
        legacy = pic.load_allowlist()
        pic.save_allowlist(legacy)  # next --update path
        raw = json.loads(pic.ALLOWLIST_PATH.read_text())
        assert "hmac" in raw
        assert "state" in raw


# ---------------------------------------------------------------------------
# Plugin scanner
# ---------------------------------------------------------------------------

class TestScanPlugins:

    def test_complete_plugin_recorded(self, sample_vault: Path) -> None:
        plugins_dir = sample_vault / ".obsidian" / "plugins"
        out = pic.scan_plugins(plugins_dir)
        assert "templater-obsidian" in out
        record = out["templater-obsidian"]
        assert record["version"] == "2.0.0"
        assert record["main_sha256"]
        assert record["manifest_sha256"]

    def test_missing_main_js_marks_incomplete(self, tmp_path: Path,
                                              write_plugin) -> None:
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        write_plugin(plugins, plugin_id="halfdone")
        # Remove main.js after construction.
        (plugins / "halfdone" / "main.js").unlink()
        out = pic.scan_plugins(plugins)
        assert out["halfdone"].get("incomplete")

    def test_malformed_manifest_recorded(self, tmp_path: Path) -> None:
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        broken = plugins / "broken-plugin"
        broken.mkdir()
        (broken / "manifest.json").write_text("{ this is not json")
        (broken / "main.js").write_text("// noop\n")
        out = pic.scan_plugins(plugins)
        assert "broken-plugin" in out
        assert "manifest_error" in out["broken-plugin"]

    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        out = pic.scan_plugins(tmp_path / "no-such-dir")
        assert out == {}


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------

class TestDiff:

    def test_new_plugin(self) -> None:
        cur = {"a": {"version": "1", "main_sha256": "h",
                     "manifest_sha256": "m"}}
        out = pic.diff(cur, allowlist={})
        assert any(f["kind"] == "NEW" for f in out)

    def test_removed_plugin(self) -> None:
        out = pic.diff(current={},
                       allowlist={"a": {"version": "1"}})
        assert any(f["kind"] == "REMOVED" for f in out)

    def test_version_change(self) -> None:
        cur = {"a": {"version": "2", "main_sha256": "h",
                     "manifest_sha256": "m"}}
        old = {"a": {"version": "1", "main_sha256": "h",
                     "manifest_sha256": "m"}}
        out = pic.diff(cur, old)
        assert any(f["kind"] == "VERSION_CHANGE" for f in out)

    def test_bundle_change_same_version_is_strongest_signal(self) -> None:
        """Bundle hash flip with no version bump = supply-chain
        compromise pattern. This must produce BUNDLE_CHANGE."""
        cur = {"a": {"version": "1", "main_sha256": "NEW",
                     "manifest_sha256": "m"}}
        old = {"a": {"version": "1", "main_sha256": "OLD",
                     "manifest_sha256": "m"}}
        out = pic.diff(cur, old)
        kinds = {f["kind"] for f in out}
        assert "BUNDLE_CHANGE" in kinds

    def test_manifest_drift(self) -> None:
        cur = {"a": {"version": "1", "main_sha256": "h",
                     "manifest_sha256": "NEW"}}
        old = {"a": {"version": "1", "main_sha256": "h",
                     "manifest_sha256": "OLD"}}
        out = pic.diff(cur, old)
        assert any(f["kind"] == "MANIFEST_DRIFT" for f in out)


# ---------------------------------------------------------------------------
# End-to-end main()
# ---------------------------------------------------------------------------

class TestEndToEndMain:

    def _argv(self, vault: Path, *extra: str) -> list[str]:
        return ["--vault", str(vault), *extra]

    def test_no_baseline_returns_2(self, sample_vault: Path,
                                    fake_keychain, tmp_state_dir,
                                    silent_notify) -> None:
        rc = pic.main(self._argv(sample_vault))
        assert rc == 2

    def test_update_then_clean_check(self, sample_vault: Path,
                                      fake_keychain, tmp_state_dir,
                                      silent_notify) -> None:
        assert pic.main(self._argv(sample_vault, "--update")) == 0
        rc = pic.main(self._argv(sample_vault))
        assert rc == 0

    def test_update_refreshes_the_jobs_recorded_status(
            self, sample_vault: Path, fake_keychain, tmp_state_dir,
            silent_notify, silent_kickstart: list) -> None:
        # Adopting by hand leaves the scheduler reporting the drift run that
        # prompted it, so the dashboard shows this control failing while the
        # allowlist is in fact clean.
        assert pic.main(self._argv(sample_vault, "--update")) == 0
        assert [c[0] for c in silent_kickstart] == [pic.AGENT_LABEL]

    def test_a_plain_check_never_kickstarts(
            self, sample_vault: Path, fake_keychain, tmp_state_dir,
            silent_notify, silent_kickstart: list) -> None:
        # The triggered run carries no --update, so it must not trigger
        # another. A check that kickstarted would spin the job forever.
        assert pic.main(self._argv(sample_vault, "--update")) == 0
        silent_kickstart.clear()
        pic.main(self._argv(sample_vault))
        assert silent_kickstart == []

    def test_bundle_change_after_update_fires(
            self, sample_vault: Path, fake_keychain, tmp_state_dir,
            silent_notify) -> None:
        assert pic.main(self._argv(sample_vault, "--update")) == 0
        # Tamper with a plugin bundle without changing version.
        plugins = sample_vault / ".obsidian" / "plugins"
        (plugins / "templater-obsidian" / "main.js").write_text(
            "// MALICIOUS BUNDLE swap\n")
        rc = pic.main(self._argv(sample_vault))
        assert rc == 1
        assert any("templater" in m for _, m in silent_notify)
