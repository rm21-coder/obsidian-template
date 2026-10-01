"""
test_plugin_integrity.py — the v1.4 HMAC envelope is the headline.

Coverage
--------
- TestKeychainHelpers   _require_hmac_key: creates on miss, reads on hit
- TestCanonicalSerialization  HMAC input is order-stable across Python runs
- TestEnvelopeRoundTrip save_allowlist → load_allowlist returns the same dict
- TestTamperDetection   mutating state, hmac, or envelope shape fires
                        ALLOWLIST_TAMPER + non-zero exit + alert log entry
- TestUnsignedAllowlistIsTamper  a file without the HMAC envelope is refused
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
from platform_caps import requires_fifo, requires_symlinks


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
# No unsigned allowlist is ever trusted (was: "legacy migration")
#
# Until 2026-09-24 a file without the HMAC envelope was accepted as a trusted
# pre-v1.4 allowlist with no verification, which let anyone who could write
# the file bypass the signature by downgrading its format. M-DASH, CWE-347.
# ---------------------------------------------------------------------------

class TestUnsignedAllowlistIsTamper:

    @pytest.mark.parametrize("flat", [
        {"templater-obsidian": {"version": "2.0.0"}},
        {"evil-plugin": {"main.js": "0" * 64}},
        {"state": {"x": {}}},          # envelope keys incomplete: no hmac
        {"hmac": "abc"},               # ... and no state
    ])
    def test_flat_or_partial_file_is_refused(
            self, fake_keychain, tmp_state_dir, flat) -> None:
        pic.ALLOWLIST_PATH.write_text(json.dumps(flat))
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1, "an unsigned allowlist must be tamper"

    def test_refusal_is_recorded_as_tamper(
            self, fake_keychain, tmp_state_dir) -> None:
        pic.ALLOWLIST_PATH.write_text(json.dumps({"x": {"version": "1"}}))
        with pytest.raises(SystemExit):
            pic.load_allowlist()
        alerts = (tmp_state_dir / "alerts.log").read_text()
        assert "ALLOWLIST_TAMPER" in alerts and "signed envelope" in alerts

    def test_update_recovers_without_reading_the_old_file(
            self, fake_keychain, tmp_state_dir) -> None:
        """A genuine pre-envelope install is not stranded: save_allowlist
        (the --update path) writes a fresh signed envelope from scratch."""
        pic.ALLOWLIST_PATH.write_text(json.dumps({"x": {"version": "1"}}))
        pic.save_allowlist({"x": {"version": "1"}})
        raw = json.loads(pic.ALLOWLIST_PATH.read_text())
        assert "hmac" in raw and "state" in raw
        assert pic.load_allowlist() == {"x": {"version": "1"}}


class TestEveryMalformedAllowlistIsTamper:
    """Adversarial review 2026-09-25: these shapes exited quietly or crashed
    with no alert -- the control stopped reporting instead of reporting."""

    @pytest.mark.parametrize("make", ["not-json", "bad-utf8", "directory", "non-ascii-hmac"])
    def test_raises_a_tamper_alert(self, fake_keychain, tmp_state_dir, make) -> None:
        p = pic.ALLOWLIST_PATH
        if make == "not-json":
            p.write_text("{not json")
        elif make == "bad-utf8":
            p.write_bytes(b"\xff\xfe{}")
        elif make == "directory":
            p.mkdir()
        else:
            p.write_text(json.dumps({"state": {}, "hmac": "\u00e9" * 64}))
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1
        assert "ALLOWLIST_TAMPER" in (tmp_state_dir / "alerts.log").read_text()


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


class TestRound2AllowlistShapes:
    """Adversarial review round 2, 2026-09-25: each of these silenced the
    control with no alert -- one by hanging it forever."""

    @requires_fifo
    def test_fifo_in_place_of_the_allowlist_is_tamper_not_a_hang(
            self, fake_keychain, tmp_state_dir) -> None:
        import os
        os.mkfifo(pic.ALLOWLIST_PATH)
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1
        assert "not a regular file" in (tmp_state_dir / "alerts.log").read_text()

    @requires_symlinks
    def test_symlink_loop_is_tamper_not_no_allowlist(
            self, fake_keychain, tmp_state_dir) -> None:
        pic.ALLOWLIST_PATH.symlink_to(pic.ALLOWLIST_PATH)
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1
        assert "ALLOWLIST_TAMPER" in (tmp_state_dir / "alerts.log").read_text()

    def test_recursion_error_is_tamper(self, fake_keychain, tmp_state_dir,
                                       monkeypatch) -> None:
        """Deep nesting raises RecursionError on the system Python 3.9 launchd
        uses (not on newer Pythons, so it is simulated here)."""
        pic.ALLOWLIST_PATH.write_text("[[[]]]")
        def deep(*a, **k): raise RecursionError("maximum recursion depth exceeded")
        monkeypatch.setattr(pic.json, "loads", deep)
        with pytest.raises(SystemExit) as exc:
            pic.load_allowlist()
        assert exc.value.code == 1

    def test_two_folders_with_one_id_are_both_recorded(self, tmp_path) -> None:
        plugins = tmp_path / "plugins"
        for folder, js in (("aaa-copy", "evil()"), ("dataview", "real()")):
            d = plugins / folder; d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps({"id": "dataview", "version": "1"}))
            (d / "main.js").write_text(js)
        out = pic.scan_plugins(plugins)
        assert len(out) == 2, out
        assert "dataview@dataview" in out or "dataview@aaa-copy" in out


# ---------------------------------------------------------------------------
# Security-relevant plugin settings, the enabled list, and robustness
# (2026-10-01). Hashing main.js proves which plugin is installed, not whether
# it has been told to run code from notes, nor whether a disabled plugin was
# switched on. Every regression case below is a bypass an adversarial review
# demonstrated against the first version of this.
# ---------------------------------------------------------------------------

import os  # noqa: E402

from conftest import _write_plugin  # noqa: E402


class TestPluginSettingsAreWatched:

    def _argv(self, vault: Path, *extra: str) -> list[str]:
        return ["--vault", str(vault), *extra]

    @staticmethod
    def _plugins(vault: Path) -> Path:
        return vault / ".obsidian" / "plugins"

    def _enable(self, vault: Path, *ids) -> None:
        (vault / ".obsidian" / "community-plugins.json").write_text(json.dumps(list(ids)))

    def _baseline(self, vault: Path, *enabled) -> None:
        self._enable(vault, *(enabled or ("dataview", "templater-obsidian")))
        assert pic.main(self._argv(vault, "--update")) == 0
        assert pic.main(self._argv(vault)) == 0

    def _drift(self, vault, notify) -> str:
        assert pic._run(self._argv(vault)) == 1, "no finding"
        return " | ".join(m for _, m in notify)

    # -- recording ----------------------------------------------------------

    def test_presence_and_value_are_recorded_separately(self, sample_vault: Path) -> None:
        (self._plugins(sample_vault) / "dataview" / "data.json").write_text(
            json.dumps({"enableDataviewJs": False, "refreshInterval": 2500}))
        rec = pic.scan_plugins(self._plugins(sample_vault))["dataview"]["settings"]
        assert rec["file"] == "ok"
        assert rec["keys"]["enableDataviewJs"]["set"] is True
        assert rec["keys"]["enableInlineDataviewJs"] == {"set": False}
        assert "refreshInterval" not in rec["keys"]          # curated, not all

    def test_an_unwatched_plugin_records_no_settings(self, sample_vault: Path) -> None:
        _write_plugin(self._plugins(sample_vault), plugin_id="recent-files-obsidian")
        assert "settings" not in pic.scan_plugins(self._plugins(sample_vault))["recent-files-obsidian"]

    # -- detection ----------------------------------------------------------

    def test_turning_on_javascript_raises_an_alert(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        self._baseline(sample_vault)
        (self._plugins(sample_vault) / "dataview" / "data.json").write_text(
            json.dumps({"enableDataviewJs": True}))
        assert "dataview security setting changed" in self._drift(sample_vault, silent_notify)

    def test_a_placeholder_string_cannot_impersonate_not_set(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        """The first version recorded an unset key as the string "<absent>",
        so writing that string as the value -- truthy to Dataview, so JS on --
        compared equal to the baseline."""
        data = self._plugins(sample_vault) / "dataview" / "data.json"
        data.write_text(json.dumps({"refreshInterval": 2500}))
        self._baseline(sample_vault)
        data.write_text(json.dumps({"refreshInterval": 2500, "enableDataviewJs": "<absent>"}))
        out = self._drift(sample_vault, silent_notify)
        assert "changed: enableDataviewJs" in out, out

    def test_a_version_change_does_not_hide_a_settings_change(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        self._baseline(sample_vault)
        _write_plugin(self._plugins(sample_vault), plugin_id="dataview", version="0.5.1",
                      main_body="// dataview bundle v0.5.1\n")
        (self._plugins(sample_vault) / "dataview" / "data.json").write_text(
            json.dumps({"enableDataviewJs": True}))
        out = self._drift(sample_vault, silent_notify)
        assert "0.5.0 -> 0.5.1" in out and "security setting changed" in out, out

    def test_switching_on_a_disabled_plugin_raises_an_alert(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        self._baseline(sample_vault, "dataview")
        self._enable(sample_vault, "dataview", "templater-obsidian")
        assert "templater-obsidian ENABLED" in self._drift(sample_vault, silent_notify)

    def test_an_enabled_entry_is_read_as_obsidian_coerces_it(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        """Obsidian keys plugins by String(entry): ["templater-obsidian"]
        loads templater-obsidian. Python used to drop non-strings."""
        self._baseline(sample_vault, "dataview")
        self._enable(sample_vault, "dataview", ["templater-obsidian"])
        assert "templater-obsidian ENABLED" in self._drift(sample_vault, silent_notify)

    def test_a_startup_macro_in_quickadd_raises_an_alert(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        _write_plugin(self._plugins(sample_vault), plugin_id="quickadd")
        self._baseline(sample_vault, "dataview", "templater-obsidian", "quickadd")
        (self._plugins(sample_vault) / "quickadd" / "data.json").write_text(json.dumps({"choices": [
            {"type": "Macro", "runOnStartup": True,
             "macro": {"commands": [{"type": "UserScript", "path": "x.js"}]}}]}))
        out = self._drift(sample_vault, silent_notify)
        assert "quickadd security setting changed" in out and "choices" in out, out

    def test_a_second_config_folder_raises_an_alert(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        """Obsidian can be pointed at any ".name" config folder from outside
        the vault; this control reads .obsidian only."""
        self._baseline(sample_vault)
        alt = sample_vault / ".obsidian-alt"
        (alt / "plugins").mkdir(parents=True)
        (alt / "community-plugins.json").write_text('["dataview"]')
        assert "NEW Obsidian config folder: .obsidian-alt" in self._drift(sample_vault, silent_notify)

    def test_deleting_the_plugins_folder_is_not_silence(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        import shutil
        self._baseline(sample_vault)
        shutil.rmtree(self._plugins(sample_vault))
        assert "REMOVED" in self._drift(sample_vault, silent_notify)

    def test_an_older_signed_baseline_is_not_trusted_for_new_fields(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        self._enable(sample_vault, "dataview")
        current = pic.scan_plugins(self._plugins(sample_vault))
        pic.save_allowlist({pid: {k: v for k, v in rec.items() if k not in ("enabled", "settings")}
                            for pid, rec in current.items()})
        assert "not yet baselined" in self._drift(sample_vault, silent_notify)
        assert pic.main(self._argv(sample_vault, "--update")) == 0
        assert pic.main(self._argv(sample_vault)) == 0

    # -- the check cannot be crashed or hung into silence ---------------------

    @pytest.mark.parametrize("where,content", [
        ("data", '{"enableDataviewJs": true, "pad": ' + "[" * 100000 + "]" * 100000 + "}"),
        ("enabled", '["dataview", "templater-obsidian", ' + "[" * 100000 + "]" * 100000 + "]"),
        ("manifest-bytes", None),
        ("manifest-id-list", None),
    ])
    def test_malformed_input_is_a_finding_not_a_crash(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify,
            where, content) -> None:
        self._baseline(sample_vault)
        plugins = self._plugins(sample_vault)
        if where == "data":
            (plugins / "dataview" / "data.json").write_text(content)
        elif where == "enabled":
            (sample_vault / ".obsidian" / "community-plugins.json").write_text(content)
        elif where == "manifest-bytes":
            m = plugins / "dataview" / "manifest.json"
            m.write_bytes(m.read_bytes().replace(b'"dataview"', b'"dataview", "description": "\xff"', 1))
            (plugins / "dataview" / "main.js").write_text("// swapped\n")
        else:
            (plugins / "dataview" / "manifest.json").write_text(
                json.dumps({"id": ["dataview"], "name": "Dataview", "version": "0.5.0"}))
            (plugins / "dataview" / "main.js").write_text("// swapped\n")
        rc = pic._run(self._argv(sample_vault))
        assert rc in (1, 3), rc                     # a finding, or the control's own alert
        assert silent_notify, "nothing was reported"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
    @pytest.mark.parametrize("which", ["data", "enabled", "main"])
    def test_a_fifo_never_hangs_the_check(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify, which) -> None:
        self._baseline(sample_vault)
        target = {"data": self._plugins(sample_vault) / "dataview" / "data.json",
                  "enabled": sample_vault / ".obsidian" / "community-plugins.json",
                  "main": self._plugins(sample_vault) / "dataview" / "main.js"}[which]
        if target.exists():
            target.unlink()
        os.mkfifo(target)
        assert self._drift(sample_vault, silent_notify)

    def test_an_unexpected_failure_raises_the_controls_own_alert(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify, monkeypatch) -> None:
        self._baseline(sample_vault)

        def boom(*a, **k):
            raise RuntimeError("synthetic")
        monkeypatch.setattr(pic, "scan_plugins", boom)
        assert pic._run(self._argv(sample_vault)) == 3
        assert any("CONTROL_ERROR" in m for _, m in silent_notify)


class TestPluginSettingsRound2:
    """Bypasses the second adversarial round demonstrated (2026-10-01)."""

    _argv = TestPluginSettingsAreWatched._argv
    _plugins = staticmethod(TestPluginSettingsAreWatched._plugins)
    _enable = TestPluginSettingsAreWatched._enable
    _baseline = TestPluginSettingsAreWatched._baseline
    _drift = TestPluginSettingsAreWatched._drift

    @pytest.mark.parametrize("name", [".trash", ".git", ".claude", ".cfg"])
    def test_any_dot_folder_used_as_a_config_folder_is_reported(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify, name) -> None:
        self._baseline(sample_vault)
        alt = sample_vault / name
        (alt / "plugins").mkdir(parents=True, exist_ok=True)
        (alt / "community-plugins.json").write_text('["dataview"]')
        assert f"NEW Obsidian config folder: {name}" in self._drift(sample_vault, silent_notify)

    @requires_symlinks
    def test_a_symlinked_config_folder_is_reported(
            self, sample_vault, tmp_path, fake_keychain, tmp_state_dir, silent_notify) -> None:
        self._baseline(sample_vault)
        outside = tmp_path / "elsewhere"
        (outside / "plugins").mkdir(parents=True)
        (outside / "community-plugins.json").write_text('["dataview"]')
        (sample_vault / ".cfg").symlink_to(outside, target_is_directory=True)
        assert "NEW Obsidian config folder: .cfg" in self._drift(sample_vault, silent_notify)

    def test_an_invalid_byte_does_not_hide_a_setting_the_plugin_reads(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        """Node replaces invalid UTF-8 and parses the file; so must the check."""
        self._baseline(sample_vault)
        (self._plugins(sample_vault) / "dataview" / "data.json").write_bytes(
            b'{"note": "\xff", "enableDataviewJs": true}')
        rec = pic.scan_plugins(self._plugins(sample_vault))["dataview"]["settings"]
        assert rec["file"] == "ok" and rec["keys"]["enableDataviewJs"]["set"] is True
        assert "enableDataviewJs" in self._drift(sample_vault, silent_notify)

    def test_edits_inside_an_unparseable_file_are_still_seen(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        data = self._plugins(sample_vault) / "dataview" / "data.json"
        data.write_bytes(b"\xef\xbb\xbf" + json.dumps({"enableDataviewJs": False}).encode())  # BOM
        self._baseline(sample_vault)
        data.write_bytes(b"\xef\xbb\xbf" + json.dumps({"enableDataviewJs": True}).encode())
        assert "(file content)" in self._drift(sample_vault, silent_notify)

    def test_rewriting_a_vetted_startup_script_is_seen(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        _write_plugin(self._plugins(sample_vault), plugin_id="quickadd")
        (sample_vault / "Scripts").mkdir()
        (sample_vault / "Scripts" / "s.js").write_text("module.exports = () => 1;\n")
        (self._plugins(sample_vault) / "quickadd" / "data.json").write_text(json.dumps({"choices": [
            {"type": "Macro", "runOnStartup": True,
             "macro": {"commands": [{"type": "UserScript", "path": "Scripts/s.js"}]}}]}))
        self._baseline(sample_vault, "dataview", "templater-obsidian", "quickadd")
        (sample_vault / "Scripts" / "s.js").write_text("require('child_process');\n")
        assert "(referenced) Scripts/s.js" in self._drift(sample_vault, silent_notify)

    def test_editing_a_template_templater_runs_is_seen(
            self, sample_vault, fake_keychain, tmp_state_dir, silent_notify) -> None:
        (sample_vault / "Templates").mkdir()
        (sample_vault / "Templates" / "Note.md").write_text("<% tp.date.now() %>\n")
        (self._plugins(sample_vault) / "templater-obsidian" / "data.json").write_text(
            json.dumps({"templates_folder": "/Templates"}))
        self._baseline(sample_vault)
        (sample_vault / "Templates" / "Note.md").write_text("<%* require('child_process') %>\n")
        assert "(referenced) Templates/Note.md" in self._drift(sample_vault, silent_notify)

    def test_a_failing_notification_does_not_lose_the_alert_record(
            self, sample_vault, fake_keychain, tmp_state_dir, monkeypatch) -> None:
        records = []

        def boom(*a, **k):
            raise RuntimeError("synthetic")

        def no_notify(*a, **k):
            raise OSError("osascript failed")
        monkeypatch.setattr(pic, "scan_plugins", boom)
        monkeypatch.setattr(pic.security_common, "notify", no_notify)
        monkeypatch.setattr(pic, "append_alert", records.append)
        assert pic._run(["--vault", str(sample_vault)]) == 3
        assert records and records[0]["kind"] == "CONTROL_ERROR"

class TestReferencedCodeRound3:
    """Gaps the third review found in how referenced code is hashed."""

    @staticmethod
    def _refs(vault: Path, plugin_id: str, data: dict) -> dict:
        return pic._referenced_files(vault, plugin_id, data)

    @pytest.mark.parametrize("written", ["./Scripts/s.js", "Scripts\\s.js", "Scripts//s.js", "/Scripts/s.js"])
    def test_a_path_is_resolved_as_obsidian_normalises_it(self, tmp_path, written) -> None:
        (tmp_path / "Scripts").mkdir()
        (tmp_path / "Scripts" / "s.js").write_text("1")
        assert "Scripts/s.js" in self._refs(tmp_path, "quickadd", {"choices": [{"path": written}]})

    def test_code_a_script_can_require_is_hashed_too(self, tmp_path) -> None:
        (tmp_path / "Scripts" / "lib").mkdir(parents=True)
        (tmp_path / "Scripts" / "s.js").write_text("require('./lib/x')")
        (tmp_path / "Scripts" / "lib" / "x.js").write_text("1")
        refs = self._refs(tmp_path, "quickadd", {"choices": [{"path": "Scripts/s.js"}]})
        assert "Scripts/lib/x.js" in refs

    def test_running_out_of_budget_is_a_finding_every_time(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(pic, "REF_MAX_FILES", 3)
        t = tmp_path / "Templates"
        t.mkdir()
        for i in range(10):
            (t / f"t{i}.md").write_text(str(i))
        refs = self._refs(tmp_path, "templater-obsidian", {"templates_folder": "Templates"})
        assert "(overrun)" in refs
        cur = {"templater-obsidian": {"version": "1", "settings": {"file": "ok", "keys": {}, "refs": refs}}}
        assert any(f["kind"] == "REFERENCE_LIMIT" for f in pic.diff(cur, cur))

    @requires_symlinks
    def test_a_symlink_to_outside_the_vault_is_hashed_by_its_target(self, tmp_path) -> None:
        vault = tmp_path / "v"
        (vault / "Scripts").mkdir(parents=True)
        outside = tmp_path / "out.js"
        outside.write_text("one")
        (vault / "Scripts" / "s.js").symlink_to(outside)
        before = self._refs(vault, "quickadd", {"choices": [{"path": "Scripts/s.js"}]})
        outside.write_text("two")
        after = self._refs(vault, "quickadd", {"choices": [{"path": "Scripts/s.js"}]})
        assert before["Scripts/s.js"].startswith("symlink->") and before != after

    @requires_symlinks
    def test_a_directory_symlink_loop_does_not_hang_the_walk(self, tmp_path) -> None:
        t = tmp_path / "Templates"
        t.mkdir()
        (t / "a.md").write_text("x")
        (t / "loop").symlink_to(t, target_is_directory=True)
        refs = self._refs(tmp_path, "templater-obsidian", {"templates_folder": "Templates"})
        assert list(refs) == ["Templates/a.md"]

    def test_folder_names_are_not_treated_as_note_references(self, tmp_path) -> None:
        (tmp_path / "Categories").mkdir()
        (tmp_path / "Categories" / "People.md").write_text("x")
        assert self._refs(tmp_path, "obsidian-meta-bind-plugin",
                          {"excludedFolders": ["People"]}) == {}
        assert self._refs(tmp_path, "quickadd", {"choices": [{"folder": "People"}]}) == {}

def test_the_agent_reruns_when_a_watched_settings_file_changes() -> None:
    """A change inside a plugin folder does not fire the folder watch, and
    Templater and Meta Bind reload data.json live; every file the check reads
    must be watched individually, kept in step with SECURITY_SETTINGS."""
    plist = (Path(pic.__file__).parent / "com.obsidian.security.plugin-check.plist").read_text()
    base = "/Users/YOUR_USERNAME/Obsidian/.obsidian"
    assert f"<string>{base}/community-plugins.json</string>" in plist
    for pid in pic.SECURITY_SETTINGS:
        assert f"<string>{base}/plugins/{pid}/data.json</string>" in plist, pid
