"""
test_windows_notifications.py -- Send-Notification.ps1, the Windows toast path.

These toasts carry the security alerts (integrity drift, plugin tampering)
and the Claude sign-in expiry. Until 2026-10-01 they rendered only when the
optional BurntToast module was installed, which install.ps1 never did, so on
a standard install every alert reached a log file and nobody. Whether a toast
actually appears can only be seen on Windows; these pin the code paths.
"""
from __future__ import annotations

from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "windows" / "Send-Notification.ps1"
       ).read_text(encoding="utf-8")


def test_without_burnttoast_the_native_api_shows_the_toast() -> None:
    branch = SRC[SRC.index("if (Get-Module -ListAvailable -Name BurntToast)"):]
    other = branch[branch.index("} else {"):branch.index("} catch {")]
    assert "Show-NativeToast $Title $Message" in other, (
        "no toast without BurntToast: security alerts go to the log only")
    assert "ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)" in SRC


def test_the_toast_text_is_escaped_before_it_becomes_xml() -> None:
    fn = SRC[SRC.index("function Show-NativeToast"):]
    fn = fn[:fn.index("\n}\n")]
    assert "$t = [System.Security.SecurityElement]::Escape($Title)" in fn
    assert "$m = [System.Security.SecurityElement]::Escape($Message)" in fn
    loadxml = next(ln for ln in fn.splitlines() if "LoadXml(" in ln)
    assert "$Title" not in loadxml and "$Message" not in loadxml


def test_every_message_is_logged_before_any_toast_is_tried() -> None:
    assert SRC.index("Add-Content -Path $log -Value \"[$stamp] $Title -- $Message\"") \
        < SRC.index("try {")


def test_a_toast_that_cannot_be_shown_is_recorded_not_thrown() -> None:
    catch = SRC[SRC.index("} catch {"):]
    assert "toast not shown: " in catch
    assert "throw" not in catch
