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
