"""
test_windows_encoding.py -- files the PowerShell layer writes must not carry a BOM.

Windows PowerShell 5.1's `Set-Content -Encoding utf8` always prepends a UTF-8
byte-order mark. The installer wrote meeting_pull.json, meeting_prepopulate.json
and the secrets .env that way, and Python's json.loads rejects the mark, so
meeting_pull failed on every run and meeting_prepopulate silently ignored its
config (found on a colleague's machine, 2026-10-01). Every writer now goes
through Set-Utf8Content.
"""
from __future__ import annotations

import re
from pathlib import Path

WINDOWS = Path(__file__).resolve().parent.parent / "windows"


def _ps1() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(WINDOWS.glob("*.ps1"))}


def test_no_shipped_script_writes_utf8_with_a_bom() -> None:
    bad = []
    for name, src in _ps1().items():
        for n, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(Set-Content|Add-Content|Out-File)\b.*-Encoding\s+utf8\b", code, re.I):
                bad.append(f"{name}:{n}: {line.strip()}")
    assert not bad, "UTF-8 with a BOM under PowerShell 5.1:\n" + "\n".join(bad)


def test_the_shared_writer_omits_the_bom() -> None:
    common = _ps1()["common.ps1"]
    body = common[common.index("function Set-Utf8Content"):]
    body = body[:body.index("\n}\n")]
    assert "New-Object System.Text.UTF8Encoding $false" in body
    assert "[System.IO.File]::WriteAllText(" in body


def test_every_config_and_secrets_writer_uses_it() -> None:
    src = _ps1()
    merge = src["common.ps1"][src["common.ps1"].index("function Merge-JsonConfig"):]
    assert "Set-Utf8Content -LiteralPath $Path -Value (ConvertTo-Json" in merge[:merge.index("\n}\n")]
    env = src["common.ps1"][src["common.ps1"].index("function Set-EnvValue"):]
    assert "Set-Utf8Content -LiteralPath $Path -Value $out" in env[:env.index("\n}\n")]
    assert "Set-Utf8Content -LiteralPath $secrets -Value $stub" in src["install.ps1"]
