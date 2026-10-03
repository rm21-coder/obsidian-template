"""
test_windows_update.py -- update.ps1, the one-command update for a Windows install.

The update is three steps whose order matters (pull, requirements, re-register)
and which colleagues kept getting partly right by hand: a pull without the
requirements step left the meeting pipeline without tzdata; a pull without
re-registering left the old task definitions (and their console windows)
running. Behaviour on Windows is verified on a test machine; these pin the
structure so a refactor cannot quietly reorder or drop a step.
"""
from __future__ import annotations

import re
from pathlib import Path

WINDOWS = Path(__file__).resolve().parent.parent / "windows"
UPDATE = (WINDOWS / "update.ps1").read_text(encoding="utf-8")
INSTALL = (WINDOWS / "install.ps1").read_text(encoding="utf-8")
COMMON = (WINDOWS / "common.ps1").read_text(encoding="utf-8")


def _first_pass() -> str:
    return UPDATE[UPDATE.index("if (-not $AfterPull) {"):UPDATE.index("Write-Host '== 2/3 requirements =='")]


def _after_pull() -> str:
    return UPDATE[UPDATE.index("Write-Host '== 2/3 requirements =='"):]


def test_it_refuses_to_pull_over_local_changes_to_tracked_files() -> None:
    first = _first_pass()
    check = first.index("git -C $vault status --porcelain --untracked-files=no")
    refuse = first.index("exit 1", check)
    pull = first.index("git -C $vault pull --ff-only")
    assert check < refuse < pull


def test_the_pull_is_fast_forward_only() -> None:
    calls = [ln.strip() for ln in UPDATE.splitlines()
             if re.match(r"\s*git -C \$vault pull\b", ln)]
    assert calls == ["git -C $vault pull --ff-only"], calls


def test_the_rest_runs_from_the_version_just_pulled() -> None:
    # Without the re-exec, steps 2 and 3 would run as written in the OLD file.
    first = _first_pass()
    pull = first.index("pull --ff-only")
    reexec = first.index("-File $PSCommandPath -AfterPull -From $from")
    assert pull < reexec
    assert "exit $LASTEXITCODE" in first[reexec:]
    assert "Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'" in first


def test_requirements_then_registration_in_that_order() -> None:
    rest = _after_pull()
    req = rest.index("Install-Requirements -VenvPython $venvPy -ScriptsDir $scriptsDir")
    reg = rest.index("& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')")
    assert req < reg


def test_a_failed_registration_fails_the_update() -> None:
    rest = _after_pull()
    reg = rest.index("& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')")
    after = rest[reg:reg + 600]
    assert "if ($LASTEXITCODE -ne 0) {" in after
    assert "Update INCOMPLETE" in after
    assert "exit 1" in after


def test_it_never_adopts_a_new_integrity_baseline_itself() -> None:
    # The --update command may be PRINTED for the person to run; it must never
    # be executed by the script.
    for line in UPDATE.splitlines():
        if "--update" in line:
            assert line.lstrip().startswith(("Write-Host", "#", ".")), line


def test_install_and_update_share_one_requirements_step() -> None:
    assert "function Install-Requirements" in COMMON
    assert "Install-Requirements -VenvPython $venvPy -ScriptsDir $scriptsDir" in INSTALL
    for src, name in ((INSTALL, "install.ps1"), (UPDATE, "update.ps1")):
        assert "pip install -r" not in src, (
            f"{name} runs its own pip line again; the two can drift apart")
    body = COMMON[COMMON.index("function Install-Requirements"):]
    body = body[:body.index("\n}\n")]
    assert "--upgrade pip" not in body
    assert "--require-hashes --no-deps --only-binary ':all:' @reinstall -r $lock" in body


def test_a_no_op_update_does_not_claim_to_have_updated() -> None:
    tail = UPDATE[UPDATE.index("if ($From -eq $to) {"):]
    assert tail.index("Already at $to") < tail.index("Updated $From -> $to.")


def test_settings_left_crlf_by_an_older_checkout_are_rewritten_after_the_pull() -> None:
    # Clones made before .gitattributes held settings JSON as CRLF. Obsidian
    # re-saved them as LF and git then called them modified with no diff, so
    # the following update refused (ARM test laptop, 2026-10-03).
    rest = _after_pull()
    assert "Repair-SettingsLineEndings -Vault $vault" in rest
    assert rest.index("Repair-SettingsLineEndings") < rest.index("== 3/3 scheduled tasks ==")
    body = COMMON[COMMON.index("function Repair-SettingsLineEndings"):]
    body = body[:body.index("\n}\n")]
    # Only a file whose content matches git apart from line endings is touched,
    # and it is deleted before the checkout, which otherwise skips it.
    guard = body.index("diff --quiet --ignore-cr-at-eol -- $rel")
    assert guard < body.index("$redo += $rel")
    assert body.index("Remove-Item -LiteralPath") < body.index("checkout -- @redo")
    assert "catch {" in body and "Write-Warning" in body    # a failure warns, never ends the update
