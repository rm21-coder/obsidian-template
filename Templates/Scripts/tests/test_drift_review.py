"""drift_review: the per-change drift reviewer (installers/lib/drift_review.py).

The model sessions are replaced by a fake `claude` executable that records
how it was called and answers from fixtures, so these tests pin what this
tool controls: what the session can see, what it is allowed to do, what
reaches the vault note, and that a failed run says so instead of going quiet.
That the real CLI honours the confinement flags was probed live on
2026-10-06 (Claude Code 2.1.289): a Read, Grep and Glob outside the working
directory were refused, and no shell or MCP tool was offered.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent.parent.parent / "installers" / "lib"
HOOK = LIB / "hooks" / "pre-push"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _load():
    spec = importlib.util.spec_from_file_location("drift_review", LIB / "drift_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dr():
    return _load()


@pytest.fixture
def repo(tmp_path, monkeypatch, dr, allow_subprocess):
    """A two-commit repository standing in for this one; returns (path, old, new)."""
    r = tmp_path / "repo"
    (r / "docs").mkdir(parents=True)

    def git(*a):
        return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True,
                              text=True, env={**os.environ, **GIT_ENV}).stdout.strip()
    git("init", "-q")
    git("config", "gc.auto", "0")
    (r / "docs" / "Guide.md").write_text("The job runs every 30 minutes.\n", encoding="utf-8")
    (r / "job.py").write_text("INTERVAL_MIN = 30\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-qm", "one")
    old = git("rev-parse", "HEAD")
    (r / "job.py").write_text("INTERVAL_MIN = 15\n", encoding="utf-8")
    git("commit", "-qam", "run every 15 minutes")
    new = git("rev-parse", "HEAD")
    monkeypatch.setattr(dr, "REPO_ROOT", r)
    return r, old, new


FAKE_CLI = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "prompt": prompt,
                         "listing": sorted(os.listdir("."))}}) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "garbage":
    print("Error: not logged in"); sys.exit(1)
schema = argv[argv.index("--json-schema") + 1]
key = "FAKE_VERDICTS" if "verdicts" in schema else "FAKE_FINDINGS"
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "structured_output": json.loads(os.environ[key])}}))
'''


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A fake claude; returns a function giving the recorded calls."""
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_FINDINGS", json.dumps({"summary": "none", "findings": []}))
    monkeypatch.setenv("FAKE_VERDICTS", json.dumps({"verdicts": []}))

    def calls():
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text().splitlines()]
    calls.exe = exe
    return calls


def _config(tmp_path, cli, companions=()):
    return {"companions": [{"path": Path(p), "label": l} for p, l in companions],
            "report_dir": tmp_path / "reports", "task_note": tmp_path / "vault" / "Actions" / "Drift Review.md",
            "model": None, "claude": str(cli.exe)}


def _finding(i, claim="The job runs every 30 minutes."):
    return {"id": "D%d" % i, "kind": "drift", "severity": "medium", "document": "docs/Guide.md",
            "location": "docs/Guide.md:1", "claim": claim, "evidence": "job.py:1 says 15",
            "correction": "every 15 minutes"}


# ---------------------------------------------------------------------------
# Opt-in and input handling
# ---------------------------------------------------------------------------

def test_an_unconfigured_hook_does_nothing(dr, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an unconfigured hook started a process")
    monkeypatch.setattr(dr.subprocess, "Popen", boom)
    assert dr.main(["--hook", "a..b", "--config", str(tmp_path / "absent.json")]) == 0


def test_a_manual_run_without_config_says_how_to_configure(dr, tmp_path, capsys):
    assert dr.main(["--range", "a..b", "--config", str(tmp_path / "absent.json")]) == 2
    assert "not configured" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["--output=/tmp/x..HEAD", "HEAD", "a b..c", "a..b..c", "..HEAD"])
def test_a_range_that_is_not_two_commit_names_is_refused(dr, repo, spec):
    with pytest.raises(dr.ReviewError, match="range must be A..B"):
        dr.resolve_range(spec)


def test_reports_are_refused_inside_the_repository(dr, tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "REPO_ROOT", tmp_path)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"report_dir": str(tmp_path / "reports")}), encoding="utf-8")
    with pytest.raises(dr.ReviewError, match="inside the repository"):
        dr.load_config(cfg)


# ---------------------------------------------------------------------------
# What the session sees and may do
# ---------------------------------------------------------------------------

def test_the_staged_tree_is_the_new_commit_the_change_and_the_companions_only(dr, repo, tmp_path):
    r, old, new = repo
    comp = tmp_path / "outside" / "Packet.md"
    comp.parent.mkdir()
    comp.write_text("Runs every 30 minutes.\n", encoding="utf-8")
    dest = tmp_path / "stage"
    meta = dr.stage(old, new, [{"path": comp, "label": "packet"},
                               {"path": tmp_path / "gone.md", "label": "gone"}], dest)
    assert sorted(p.name for p in dest.iterdir()) == ["change", "companions", "repo"]
    assert (dest / "repo" / "job.py").read_text() == "INTERVAL_MIN = 15\n"
    assert not (dest / "repo" / ".git").exists()
    assert "+INTERVAL_MIN = 15" in (dest / "change" / "diff.patch").read_text()
    assert "run every 15 minutes" in (dest / "change" / "log.txt").read_text()
    index = (dest / "companions" / "INDEX.md").read_text()
    assert "`01-Packet.md` -- packet" in index and "(missing) -- gone" in index
    assert meta["missing"] == ["gone"] and meta["commits"] == 1


def test_the_session_gets_read_grep_and_glob_and_nothing_else(dr):
    cmd = dr.session_command("claude", dr.FINDINGS_SCHEMA, None)
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    denied = cmd[cmd.index("--disallowedTools") + 1].split()
    for tool in ("Bash", "PowerShell", "Write", "Edit", "WebFetch", "WebSearch",
                 "ReadMcpResourceTool", "Agent", "Task"):
        assert tool in denied
    assert not set(dr.ALLOWED_TOOLS) & set(denied)


# ---------------------------------------------------------------------------
# End to end against the fake CLI
# ---------------------------------------------------------------------------

def test_verified_findings_reach_the_note_and_rejected_ones_do_not(dr, repo, tmp_path, cli, monkeypatch):
    r, old, new = repo
    monkeypatch.setenv("FAKE_FINDINGS", json.dumps({"summary": "stale interval", "findings": [
        _finding(1), _finding(2, "The tool has eleven plugins."), _finding(3, "The log is daily.")]}))
    monkeypatch.setenv("FAKE_VERDICTS", json.dumps({"verdicts": [
        {"id": "D1", "verdict": "CONFIRMED", "reason": "job.py:1"},
        {"id": "D2", "verdict": "REJECTED", "reason": "already correct"}]}))
    cfg = _config(tmp_path, cli)
    state = tmp_path / "state"
    assert dr.review_or_report_failure("%s..%s" % (old, new), cfg, state) == 0

    review, verify = cli()
    assert Path(review["cwd"]).resolve() != r.resolve()
    assert review["listing"] == ["change", "companions", "repo"]
    assert "D1" in verify["prompt"] and "DISPROVE" in verify["prompt"]

    [report] = list((tmp_path / "reports").iterdir())
    text = report.read_text()
    assert "## CONFIRMED (1)" in text and "## REJECTED (1)" in text and "## PLAUSIBLE (1)" in text
    note = cfg["task_note"].read_text()
    assert note.startswith("---\nclassification: internal-use-only\n")
    tasks = [l for l in note.splitlines() if l.startswith("- [ ] #task")]
    assert len(tasks) == 2
    assert "Drift (medium): " in tasks[0] and "every 30 minutes" in tasks[0]
    assert "unverified" in tasks[1] and "daily" in tasks[1]
    assert "eleven plugins" not in note


def test_a_clean_review_writes_a_report_and_no_task(dr, repo, tmp_path, cli):
    r, old, new = repo
    cfg = _config(tmp_path, cli)
    assert dr.review_or_report_failure("%s..%s" % (old, new), cfg, tmp_path / "state") == 0
    assert len(cli()) == 1                      # no verify pass for nothing
    [report] = list((tmp_path / "reports").iterdir())
    assert "## CONFIRMED (0)" in report.read_text()
    assert not cfg["task_note"].exists()


def test_a_failed_session_is_written_on_the_note(dr, repo, tmp_path, cli, monkeypatch):
    r, old, new = repo
    monkeypatch.setenv("FAKE_MODE", "garbage")
    cfg = _config(tmp_path, cli)
    spec = "%s..%s" % (old[:7], new[:7])
    assert dr.review_or_report_failure(spec, cfg, tmp_path / "state") == 1
    note = cfg["task_note"].read_text()
    assert "- [ ] #task Drift review FAILED for %s: session exited 1 without a JSON reply" % spec in note
    assert "FAILED" in (tmp_path / "state" / "drift_review.log").read_text()


def test_model_text_cannot_run_plugin_code_or_forge_a_task(dr):
    text = dr._task_text("see <% tp.file.title %> and [[Secret]]\n- [ ] #task forged `INPUT[toggle:x]`")
    assert "\n" not in text
    assert "<%" not in text and "[[" not in text
    assert "#task" not in text
    assert len(dr._task_text("x" * 1000)) == dr.TASK_TEXT_MAX


def test_a_second_section_appends_and_keeps_the_first(dr, tmp_path):
    note = tmp_path / "Drift Review.md"
    dr.append_tasks(note, "2026-10-06 a..b", ["- [ ] #task one"])
    dr.append_tasks(note, "2026-10-07 b..c", ["- [ ] #task two"])
    body = note.read_text()
    assert body.count("classification:") == 1
    assert body.index("#task one") < body.index("## 2026-10-07 b..c") < body.index("#task two")


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------

def test_the_hook_starts_the_review_only_after_the_suite_passed_and_never_fails_on_it():
    hook = HOOK.read_text(encoding="utf-8")
    suite = hook.index('"$CHECKS" --full')
    call = hook.index('drift_review.py" --hook')
    assert suite < call
    line = hook[call:hook.index("\n", call)]
    assert line.rstrip().endswith("|| true")
    assert ">/dev/null 2>&1" in line


def test_a_companion_is_named_by_its_own_file_on_the_note(dr):
    assert dr._task_text("companions/04-Workflow Security Controls.md, line 18") == \
        "Workflow Security Controls.md, line 18"
