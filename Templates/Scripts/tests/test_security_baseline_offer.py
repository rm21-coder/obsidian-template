"""
test_security_baseline_offer.py -- where, and whether, an install records the
two security baselines.

A baseline adopts the machine's current state as trusted. Recorded before the
installer finished, it went stale at once: on macOS it was offered in
49-security-controls, ahead of components 50-58 that install more agents, so
the first scheduled integrity run reported the installer's own later steps as
drift. On Windows it was never offered at all, so both controls reported "no
baseline" every day and protected nothing (found on a colleague's machine,
2026-10-01). These pin: last, asked, plugin allowlist before integrity.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
COMPONENTS = REPO / "installers" / "components"
OFFER = (COMPONENTS / "88-security-baselines.sh").read_text(encoding="utf-8")
WIN = (REPO / "Templates" / "Scripts" / "windows" / "install.ps1").read_text(encoding="utf-8")


def test_macos_offers_baselines_after_every_component_that_installs_an_agent() -> None:
    installs_agents = sorted(
        p.name for p in COMPONENTS.glob("[0-9][0-9]-*.sh")
        if re.search(r"install_plist_and_load|launchctl_reload", p.read_text(encoding="utf-8")))
    assert installs_agents, "found no agent-installing components"
    assert all(name < "88-security-baselines.sh" for name in installs_agents), installs_agents


def test_macos_no_longer_baselines_inside_the_security_component() -> None:
    src = (COMPONENTS / "49-security-controls.sh").read_text(encoding="utf-8")
    assert "--update" not in src


def test_macos_asks_and_records_the_plugin_allowlist_first() -> None:
    ask = OFFER.index('confirm "Record the security baselines now?" Y')
    plugin = OFFER.index('plugin_integrity_check.py" --update')
    integ = OFFER.index('integrity_monitor.py"      --update')
    assert ask < plugin < integ
    auto = OFFER[OFFER.index('info "  --auto: not recording baselines.'):]
    assert '" --update ||' not in auto, "--auto records a baseline"


def _win_step() -> str:
    return WIN[WIN.index("Write-Host '== 85 security baselines =='"):WIN.index("Write-Host '== 90 status =='")]


def test_windows_offers_baselines_after_the_tasks_are_registered() -> None:
    assert WIN.index("& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')") \
        < WIN.index("Write-Host '== 85 security baselines =='") \
        < WIN.index("Write-Host '== 90 status =='")


def test_windows_records_only_on_yes_and_the_plugin_allowlist_first() -> None:
    step = _win_step()
    yes = step.index("} elseif ($ans -match '^(y|yes)$') {")
    plugin = step.index("& $venvPy $pluginCheck --update")
    integ = step.index("& $venvPy $integrity --update")
    assert yes < plugin < integ
    # Every other branch only prints the commands.
    before = step[:yes]
    assert "& $venvPy $pluginCheck --update" not in before
    assert "& $venvPy $integrity --update" not in before


def test_windows_leaves_existing_baselines_and_skip_tasks_alone() -> None:
    step = _win_step()
    assert step.index("if ($haveBaselines) {") < step.index("} elseif ($SkipTasks) {") \
        < step.index("$ans = Read-Answer")
