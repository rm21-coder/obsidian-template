"""
test_plugin_drift.py -- plugin pin changes reach existing installs.

Before 2026-10-03 a Windows update never reinstalled plugins, so a re-pin --
a plugin's security fix included -- never reached an existing Windows install;
the maintainer's own Mac vault had drifted ten plugins behind its pins. Both
updaters now reinstall when the pull moved the pins OR an installed plugin is
not its pinned copy (installers/lib/plugin_drift.py, by manifest hash).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "installers" / "lib" / "plugin_drift.py"
WINDOWS = REPO / "Templates" / "Scripts" / "windows"
COMMON = (WINDOWS / "common.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
UPDATE = (WINDOWS / "update.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
INSTALL = (WINDOWS / "install.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")

sys.path.insert(0, str(HELPER.parent))
import plugin_drift  # noqa: E402

PINNED = b'{"id": "a", "version": "2.0.0"}'


def _vault(tmp_path: Path, enabled, installed: dict[str, bytes | None]) -> Path:
    v = tmp_path / "vault"
    (v / "installers").mkdir(parents=True)
    pins = [{"id": pid, "ref": "2.0.0", "files": {
        "manifest.json": {"url": "https://example.invalid", "sha256": hashlib.sha256(PINNED).hexdigest()},
        "main.js": {"url": "https://example.invalid", "sha256": "0" * 64}}} for pid in ("a", "b", "c")]
    (v / "installers" / "plugin-pins.json").write_text(json.dumps(pins))
    (v / ".obsidian").mkdir()
    (v / ".obsidian" / "community-plugins.json").write_text(json.dumps(enabled))
    for pid, body in installed.items():
        d = v / ".obsidian" / "plugins" / pid
        d.mkdir(parents=True)
        if body is not None:
            (d / "manifest.json").write_bytes(body)
        (d / "main.js").write_text("patched locally")      # never compared
    return v


def test_only_plugins_whose_manifest_differs_from_the_pin_are_listed(tmp_path) -> None:
    v = _vault(tmp_path, ["a", "b", "c", "unpinned", 7],
               {"a": PINNED, "b": b'{"id": "b", "version": "1.0.0"}', "c": None})
    # a: at its pin (main.js differing is the QuickAdd patch, not drift);
    # b: older; c: manifest missing; unpinned and non-string entries: left alone.
    assert plugin_drift.drifted(v) == ["b", "c"]


def test_a_plugin_that_is_not_installed_at_all_is_drift(tmp_path) -> None:
    assert plugin_drift.drifted(_vault(tmp_path, ["a"], {})) == ["a"]


def test_a_disabled_plugin_is_not_checked(tmp_path) -> None:
    assert plugin_drift.drifted(_vault(tmp_path, ["a"], {"a": PINNED, "b": b"old"})) == []


def test_unreadable_pins_exit_2_with_a_message(tmp_path, allow_subprocess) -> None:
    v = _vault(tmp_path, ["a"], {"a": PINNED})
    (v / "installers" / "plugin-pins.json").write_text("{not json")
    r = subprocess.run([sys.executable, str(HELPER), str(v)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "plugin_drift: cannot read pins or plugin list" in r.stderr
    assert r.stdout == ""


def test_the_cli_prints_one_id_per_line(tmp_path, allow_subprocess) -> None:
    v = _vault(tmp_path, ["a", "b"], {"a": b"old", "b": b"old"})
    r = subprocess.run([sys.executable, str(HELPER), str(v)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.split() == ["a", "b"]


def test_mac_plugins_drifted_reports_the_reason(tmp_path, allow_subprocess) -> None:
    def run(v: Path):
        return subprocess.run(["bash", "-c", f'source "{REPO}/installers/lib/common.sh"; '
                               f'source "{REPO}/installers/lib/update.sh"; plugins_drifted "{v}"'],
                              capture_output=True, text=True)
    v = _vault(tmp_path, ["a", "b"], {"a": b"old", "b": PINNED})
    (v / "installers" / "lib").mkdir()
    (v / "installers" / "lib" / "plugin_drift.py").write_bytes(HELPER.read_bytes())
    r = run(v)
    assert r.returncode == 0 and r.stdout.strip() == "not at their pins: a"
    (v / ".obsidian" / "plugins" / "a" / "manifest.json").write_bytes(PINNED)
    assert run(v).returncode == 1
    (v / "installers" / "plugin-pins.json").write_text("{not json")
    r = run(v)
    assert r.returncode == 0 and r.stdout.strip() == "the plugin drift check failed"


# ---- Windows update.ps1: structure (behaviour is verified on the laptop) ----

def _after_pull() -> str:
    return UPDATE[UPDATE.index("Write-Host '== 2/3 requirements =='"):]


def test_windows_update_reinstalls_plugins_after_requirements_and_before_tasks() -> None:
    rest = _after_pull()
    req = rest.index("Install-Requirements -VenvPython $venvPy")
    reason = rest.index("Get-PluginReinstallReason -Vault $vault -From $From -VenvPython $venvPy")
    install = rest.index("& (Join-Path $PSScriptRoot 'Install-Plugins.ps1')")
    patch = rest.index("Invoke-QuickAddPatch -Vault $vault -Python @($venvPy)")
    tasks = rest.index("Write-Host '== 3/3 scheduled tasks =='")
    assert req < reason < install < patch < tasks


def test_windows_reason_covers_a_pin_change_and_drift() -> None:
    body = COMMON[COMMON.index("function Get-PluginReinstallReason"):]
    body = body[:body.index("\n}\n")]
    assert "git -C $Vault diff --quiet $From HEAD -- installers/plugin-pins.json" in body
    assert "installers\\lib\\plugin_drift.py" in body
    assert "return 'the plugin drift check failed'" in body       # fails toward reinstalling
    assert 'return "not at their pins: $($ids -join \', \')"' in body


def test_a_plugin_failure_does_not_pass_as_a_clean_update() -> None:
    rest = _after_pull()
    block = rest[rest.index("if ($pluginReason) {"):rest.index("Remove-RetiredPlugins -Vault $vault")]
    assert "} catch {" in block and "$pluginFailure = \"$_\"" in block
    tail = rest[rest.index("if ($pluginFailure) {"):]
    assert tail.index("Update INCOMPLETE") < tail.index("exit 1") < tail.index("Updated $From -> $to.")


def test_install_and_update_share_one_quickadd_patch_step() -> None:
    assert "function Invoke-QuickAddPatch" in COMMON
    assert "Invoke-QuickAddPatch -Vault $vault -Python" in INSTALL
    for src, name in ((INSTALL, "install.ps1"), (UPDATE, "update.ps1")):
        assert "'installers\\lib\\quickadd_patch.py'" not in src, f"{name} carries its own copy of the patch step"
