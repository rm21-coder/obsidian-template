"""
test_rag_status.py -- the RAG parts appear only where RAG is set up.

Most installs never run the optional local-LLM RAG layer. Until 2026-10-03 the
dashboard showed them a Refresh RAG index button and a "No sync reports
found" card, and on Windows the rag-sync job shipped enabled for everyone and
failed every night unseen (found on the ARM test laptop: exit 2 at 03:14).
rag_status.configured() is the one test every part now asks.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

import rag_status as R

SCRIPTS = Path(R.__file__).resolve().parent
WIN = SCRIPTS / "windows"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    monkeypatch.delenv(R.KEY, raising=False)
    f = tmp_path / ".env"
    return f


@pytest.mark.parametrize("text,expected", [
    ("OBSIDIAN_COLLECTION_ID=0c2f7e51-9d8b-4b3a-a1e7-2b89df1a0e9c\n", True),
    ('export OBSIDIAN_COLLECTION_ID="0c2f7e51-9d8b"\n', True),
    ("OBSIDIAN_COLLECTION_ID=abc123  # the Obsidian collection\n", True),
    ("OBSIDIAN_COLLECTION_ID=\n", False),                     # what both installers' stubs write
    ('OBSIDIAN_COLLECTION_ID=""\n', False),
    ("OBSIDIAN_COLLECTION_ID=<id>\n", False),                 # a placeholder
    ("# OBSIDIAN_COLLECTION_ID=abc123\n", False),             # commented out
    ("OPEN_WEBUI_API_KEY=sk-123\n", False),                   # a key alone is not set up
    ("", False),
])
def test_configured_reads_the_collection_id(env_file, text, expected) -> None:
    env_file.write_text(text, encoding="utf-8")
    assert R.configured(env_file) is expected


def test_a_missing_file_is_not_set_up_and_the_environment_counts(env_file, monkeypatch) -> None:
    assert R.configured(env_file) is False
    monkeypatch.setenv(R.KEY, "abc123")
    assert R.configured(env_file) is True


def test_the_cli_exit_code_says_which(tmp_path, allow_subprocess) -> None:
    f = tmp_path / ".env"
    env = {k: v for k, v in os.environ.items() if k != R.KEY}
    for text, rc in (("OBSIDIAN_COLLECTION_ID=\n", 1), ("OBSIDIAN_COLLECTION_ID=abc\n", 0)):
        f.write_text(text, encoding="utf-8")
        p = subprocess.run(["/usr/bin/python3" if Path("/usr/bin/python3").exists() else "python3",
                            str(SCRIPTS / "rag_status.py")],
                           env={**env, "RAG_STATUS_SECRETS_ENV": str(f)}, capture_output=True, text=True)
        assert p.returncode == rc, p.stdout + p.stderr


# ---- the dashboard ---------------------------------------------------------

def _render(rag_enabled: bool, rag=None) -> str:
    import morning_dashboard as md
    return md.render(_dt.date(2026, 10, 3), [], [], [], rag, [],
                     show_actions=True, rag_enabled=rag_enabled)


def test_without_rag_the_dashboard_has_no_rag_button_or_card() -> None:
    out = _render(False)
    assert 'href="obsidian-dashboard://run/refresh-rag"' not in out
    assert 'href="obsidian-dashboard://run/pull-meetings"' in out
    assert 'href="obsidian-dashboard://run/refresh-dashboard"' in out
    assert 'class="subsection section-rag"' not in out
    assert "<h2>RAG sync</h2>" not in out and "No sync reports found" not in out


def test_with_rag_both_appear() -> None:
    out = _render(True)
    assert 'href="obsidian-dashboard://run/refresh-rag"' in out
    assert "<h2>RAG sync</h2>" in out and "No sync reports found" in out


def test_the_dashboard_asks_rag_status_by_default(monkeypatch) -> None:
    import morning_dashboard as md
    monkeypatch.setattr(R, "configured", lambda *a, **k: False)
    out = md.render(_dt.date(2026, 10, 3), [], [], [], None, [], show_actions=True)
    assert 'href="obsidian-dashboard://run/refresh-rag"' not in out
    monkeypatch.setattr(R, "configured", lambda *a, **k: True)
    out = md.render(_dt.date(2026, 10, 3), [], [], [], None, [], show_actions=True)
    assert 'href="obsidian-dashboard://run/refresh-rag"' in out


# ---- the handlers ----------------------------------------------------------

def test_windows_handler_refuses_rag_where_it_is_not_set_up(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("dashboard_action", WIN / "dashboard_action.py")
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    notes, asked = [], []
    monkeypatch.setattr(A, "log", lambda m: None)
    monkeypatch.setattr(A, "notify", lambda t, m: notes.append(m))
    monkeypatch.setattr(A, "task_state", lambda t: asked.append(t) or "Ready")
    monkeypatch.setattr(A, "run_job", lambda *a: pytest.fail("ran"))
    monkeypatch.setattr(A, "rag_set_up", lambda: False)
    assert A.main(["obsidian-dashboard://run/refresh-rag"]) == 3
    assert asked == []
    assert "Not set up on this machine: the local RAG layer is not configured." in notes


def test_macos_dispatcher_refuses_rag_where_it_is_not_set_up(tmp_path, allow_subprocess) -> None:
    f = tmp_path / ".env"
    f.write_text("OBSIDIAN_COLLECTION_ID=\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != R.KEY}
    env.update(RAG_STATUS_SECRETS_ENV=str(f), HOME=str(tmp_path))
    p = subprocess.run(["/bin/bash", str(SCRIPTS / "dashboard_actions.sh"), "refresh-rag"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 3
    assert "refresh-rag refused: RAG is not set up on this machine" in p.stderr


# ---- the Windows scheduled job ---------------------------------------------

def test_rag_sync_is_the_one_job_that_requires_rag() -> None:
    psd = (WIN / "schedules.psd1").read_text(encoding="utf-8")
    marked = re.findall(r"Name='([\w-]+)';[^\n]*RequiresRag=\$true", psd)
    assert marked == ["rag-sync"]


def test_register_tasks_enables_it_exactly_when_rag_is_set_up() -> None:
    reg = (WIN / "Register-Tasks.ps1").read_text(encoding="utf-8")
    assert "if ($job.RequiresRag) { $enable = $ragSetUp }" in reg     # overrides "kept"
    check = reg[reg.index("$ragSetUp = $false"):reg.index("foreach ($job in $manifest.Jobs)")]
    assert "Get-VenvPython" in check and "Get-VenvPythonW" not in check  # waits for the exit code
    assert "'rag_status.py'" in check and "$LASTEXITCODE -eq 0" in check
    assert reg.index("$ragSetUp = $false") < reg.index("if ($job.RequiresRag)")
    reg.encode("ascii")
