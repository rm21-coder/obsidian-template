"""What the shipped configuration and scripts point at must exist.

2026-10-06: uninstall.sh --demo passed --remove-all, a flag the seeder never
had, so demo removal failed on every run; and the daily-notes setting named a
template that does not ship."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_every_template_an_obsidian_setting_names_exists() -> None:
    missing = []
    for cfg in sorted((REPO / ".obsidian").glob("*.json")):
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("template"), str) and data["template"]:
            path = REPO / (data["template"] + ("" if data["template"].endswith(".md") else ".md"))
            if not path.is_file():
                missing.append(f"{cfg.name}: {data['template']}")
    assert not missing, missing


def test_the_uninstallers_demo_flags_are_ones_the_seeder_accepts(tmp_path, allow_subprocess) -> None:
    text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
    flags = re.search(r"demo_args=\(([^)]*)\)", text).group(1).split()
    vault = tmp_path / "vault"
    vault.mkdir()
    p = subprocess.run([sys.executable, str(REPO / "Templates" / "Scripts" / "seed_demo_content.py"),
                        *flags, "--dry-run"], capture_output=True, text=True,
                       env={"OBSIDIAN_VAULT": str(vault), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert "unrecognized arguments" not in p.stderr, p.stderr
    assert p.returncode == 0, p.stderr


def test_the_uninstaller_removes_every_launchagent_the_template_ships() -> None:
    """2026-10-06: com.obsidian.claude-auth-check shipped but was not in the
    uninstaller's list, so an uninstall left it loaded."""
    import plistlib
    text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
    block = re.search(r"^LABELS=\((.*?)^\)", text, re.M | re.S).group(1)
    listed = {ln.strip() for ln in block.splitlines()
              if ln.strip() and not ln.strip().startswith("#")}
    shipped = {plistlib.loads(p.read_bytes())["Label"]
               for p in (REPO / "Templates" / "Scripts").glob("com.*.plist")}
    assert shipped <= listed, sorted(shipped - listed)


def test_the_uninstaller_removes_every_log_a_shipped_agent_writes() -> None:
    """2026-10-06, found by the first drift review: claude-auth-check was
    added to LABELS but not LOGS, so an uninstall left its log behind."""
    import plistlib
    text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
    block = re.search(r"^LOGS=\((.*?)^\)", text, re.M | re.S).group(1)
    listed = {w for ln in block.splitlines() if not ln.strip().startswith("#")
              for w in ln.split()}
    written = set()
    for p in (REPO / "Templates" / "Scripts").glob("com.*.plist"):
        plist = plistlib.loads(p.read_bytes())
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = plist.get(key, "")
            if "/Library/Logs/" in path:
                written.add(Path(path).stem)
    assert written <= listed, sorted(written - listed)
