"""
test_windows_powershell_json.py -- JSON arrays must be enumerated the 5.1 way.

Windows PowerShell 5.1's ConvertFrom-Json writes a parsed JSON array to the
pipeline as ONE object (PowerShell 7 enumerates it). So `@(... |
ConvertFrom-Json)` on 5.1 is a one-element array whose only element is the
whole list: a foreach sees one item, and a [string] cast of it joins every
element with spaces. That is N12 (2026-10-23): Test-OpenWebUIHardening read
the container's env as one garbled key, warning about CORS on every hardened
container and never reporting WEBUI_AUTH=False; Install-Plugins.ps1 would have
refused every plugin id as one space-joined name. The safe form, on 5.1 and
7, is to assign the result first and enumerate it with foreach.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SHIPPED = sorted([*(REPO / "Templates" / "Scripts" / "windows").glob("*.ps1"),
                  *(REPO / "installers" / "lib").glob("*.ps1")])

# @( ... ConvertFrom-Json ... ) on one line, one level of inner parentheses.
WRAPPED = re.compile(r"@\((?:[^()]|\([^()]*\))*\bConvertFrom-Json\b", re.I)


def test_the_scan_covers_the_powershell_layer() -> None:
    names = {p.name for p in SHIPPED}
    assert {"common.ps1", "Install-Plugins.ps1", "update.ps1", "tree_gate.ps1"} <= names


def test_the_pattern_catches_the_n12_forms() -> None:
    assert WRAPPED.search("foreach ($item in @($raw | ConvertFrom-Json)) {")
    assert WRAPPED.search("$ids = @(Get-Content $listFile -Raw | ConvertFrom-Json)")
    assert WRAPPED.search("$x = @((Get-Content $f -Raw) | ConvertFrom-Json)")
    assert not WRAPPED.search("$parsed = $raw | ConvertFrom-Json; foreach ($item in $parsed) {")
    assert not WRAPPED.search("$ids = @(foreach ($i in $parsedIds) { $i })")


def test_no_shipped_script_wraps_convertfrom_json_in_an_array_subexpression() -> None:
    bad = []
    for p in SHIPPED:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if WRAPPED.search(code):
                bad.append(f"{p.relative_to(REPO)}:{n}: {line.strip()}")
    assert not bad, ("@(... | ConvertFrom-Json) is one element on Windows PowerShell 5.1; "
                     "assign, then foreach:\n" + "\n".join(bad))


def test_the_hardening_check_enumerates_the_parsed_env() -> None:
    common = (REPO / "Templates" / "Scripts" / "windows" / "common.ps1").read_text(encoding="utf-8")
    body = common[common.index("function Test-OpenWebUIHardening"):]
    body = body[:body.index("\n}\n")]
    assert "$parsed = $raw | ConvertFrom-Json; foreach ($item in $parsed) {" in body


# N17 (2026-10-23): Windows PowerShell 5.1 passes a double quote inside a
# native command's argument unescaped, so `docker ... --format '{{join .X " "}}'`
# reached docker as `{{join .X  }}`, the template failed, and the Open WebUI pin
# check reported every container, the pinned one included, as off-pin.
_NATIVE_QUOTED = re.compile(r"""\b(docker|git|icacls|schtasks)\b[^#\n]*'[^'\n]*"[^'\n]*'""")


def test_no_native_command_gets_a_double_quote_inside_an_argument():
    offenders = []
    for path in SHIPPED:
        for n, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or s.startswith("Write-"):
                continue
            if _NATIVE_QUOTED.search(line):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, offenders


def test_the_rule_catches_the_n17_form():
    assert _NATIVE_QUOTED.search("""docker image inspect $id --format '{{join .RepoDigests " "}}' 2>$null""")
    assert not _NATIVE_QUOTED.search("docker image inspect $id --format '{{range .RepoDigests}}{{println .}}{{end}}'")
