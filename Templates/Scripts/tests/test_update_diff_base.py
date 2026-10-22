"""N8 (2026-10-22): when the tree was already at the new commit before the
update ran (pulled by hand), both updaters printed `git diff X X` as the check
for integrity drift, which compares nothing. They now record the commit each
completed update ended at and diff from it in that case.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_update_sh_diffs_from_the_last_completed_update():
    src = (REPO / "update.sh").read_text(encoding="utf-8")
    assert 'UPDATE_RECORD="$HOME/.local/share/obsidian-template/last-update-commit"' in src
    assert 'report_control "integrity monitor" integrity_monitor.py "$diff_from" "$TO"' in src
    assert 'diff_from="$last_update"' in src
    # Recorded only on a real, completed run: after the plugin-failure return.
    end = src.index('printf \'%s\\n\' "$TO" > "$UPDATE_RECORD"')
    assert src.index("Don't adopt a new plugin baseline until an update completes.") < end
    assert 'if [[ "$DRY_RUN" -eq 0 ]]; then' in src[end - 200:end]


def test_update_ps1_diffs_from_the_last_completed_update():
    src = (REPO / "Templates" / "Scripts" / "windows" / "update.ps1").read_text(encoding="ascii")
    assert "Join-Path $env:LOCALAPPDATA 'obsidian-template\\last-update-commit'" in src
    assert 'diff --name-status $diffFrom $to' in src
    assert '$diffFrom = $lastUpdate' in src
    write = src.index("Set-Content -LiteralPath $updateRecord -Value $to")
    assert src.index("Don't adopt a new plugin baseline until an update completes.") < write
    raw = (REPO / "Templates" / "Scripts" / "windows" / "update.ps1").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
