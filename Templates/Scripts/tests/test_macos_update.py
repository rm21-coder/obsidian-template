"""
test_macos_update.py -- ./update.sh, the one-command update for a macOS install.

Re-running install.sh is not an update: it records nothing about what a person
declined, so --auto installs every component (the local LLM stack included)
and interactive mode re-asks every question. update.sh changes only what this
machine already has. These run its decision logic, and the script itself in
--dry-run, against a scratch HOME and a scratch repository: nothing here
touches the real LaunchAgents.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from platform_caps import macos_only

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "installers" / "lib"
UPDATE = (REPO / "update.sh").read_text(encoding="utf-8")


def _bash(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = (f'set -euo pipefail; source "{LIB}/common.sh"; '
              f'source "{LIB}/plist.sh"; source "{LIB}/update.sh"; {snippet}')
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def _render(label: str, user: str = "tester") -> str:
    tpl = (REPO / "Templates" / "Scripts" / f"{label}.plist").read_text(encoding="utf-8")
    return tpl.replace("YOUR_USERNAME", user)


# ---------------------------------------------------------------------------
# The decisions.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,component", [
    ("com.tag-clippings", "40-tagger"),
    ("com.obsidian.classify", "58-classification"),        # a loose match said 20-secrets
    ("com.obsidian.meeting-pull", "54-meeting-pull"),
    ("com.obsidian.security.integrity", "49-security-controls"),
    ("com.obsidian.claude-auth-check", ""),                 # installed by hand
])
def test_each_job_maps_to_the_component_that_installs_it(label, component, allow_subprocess):
    r = _bash(f'component_for_label "{REPO}" {label}')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == component


def test_the_vault_may_be_a_symlink_to_the_repo(tmp_path, allow_subprocess):
    link = tmp_path / "Obsidian"
    link.symlink_to(REPO)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _bash(f'vault_is_repo "{REPO}" "{link}"').returncode == 0
    assert _bash(f'vault_is_repo "{REPO}" "{other}"').returncode != 0
    assert _bash(f'vault_is_repo "{REPO}" "{tmp_path}/missing"').returncode != 0


def test_only_installed_jobs_are_candidates_and_identical_ones_are_left_alone(
        tmp_path, allow_subprocess):
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.tag-clippings.plist").write_text(_render("com.tag-clippings"), encoding="utf-8")
    (la / "com.voice-cleanup.plist").write_text(
        _render("com.voice-cleanup").replace("<true/>", "<false/>", 1), encoding="utf-8")
    r = _bash(f'plan_agents "{REPO}" "{la}"', env={"USER": "tester"})
    assert r.returncode == 0, r.stderr
    plan = {line.split()[1]: line.split()[0] for line in r.stdout.splitlines()}
    assert plan["com.tag-clippings"] == "unchanged"
    assert plan["com.voice-cleanup"] == "changed"
    assert plan["com.obsidian-rag-sync"] == "not-installed"   # never added by an update
    assert set(plan.values()) == {"unchanged", "changed", "not-installed"}


def test_plugins_are_reinstalled_only_when_the_pins_moved(tmp_path, allow_subprocess):
    repo = tmp_path / "r"
    (repo / "installers").mkdir(parents=True)

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    git("init", "-q")
    (repo / "installers" / "plugin-pins.json").write_text("{}\n")
    (repo / "README").write_text("a\n")
    git("add", "-A"); git("commit", "-qm", "one")
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (repo / "README").write_text("b\n")
    git("commit", "-qam", "unrelated")
    assert _bash(f'pins_changed "{repo}" {first}').returncode != 0
    (repo / "installers" / "plugin-pins.json").write_text('{"x": 1}\n')
    git("commit", "-qam", "pins")
    assert _bash(f'pins_changed "{repo}" {first}').returncode == 0


# ---------------------------------------------------------------------------
# The script, end to end, in a scratch HOME.
# ---------------------------------------------------------------------------

@pytest.fixture
def scratch(tmp_path):
    """HOME with ~/Obsidian as a small git repo carrying the real update
    machinery and plist templates, a stand-in venv, and two installed jobs."""
    home = tmp_path / "home"
    vault = home / "Obsidian"
    for rel in ("update.sh", "install.sh"):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, vault / rel)
    shutil.copytree(REPO / "installers" / "lib", vault / "installers" / "lib")
    shutil.copytree(REPO / "installers" / "components", vault / "installers" / "components")
    (vault / "installers" / "plugin-pins.json").write_text("{}\n")
    scripts = vault / "Templates" / "Scripts"
    scripts.mkdir(parents=True)
    for p in (REPO / "Templates" / "Scripts").glob("*.plist"):
        shutil.copy2(p, scripts / p.name)
    for name in ("requirements.txt", "requirements.lock"):
        shutil.copy2(REPO / "Templates" / "Scripts" / name, scripts / name)
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "types.json").write_text("{}\n")
    env = {**os.environ, "HOME": str(home), "USER": "tester",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=vault, check=True, env=env)
    venv = scripts / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").symlink_to(sys.executable)
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "com.tag-clippings.plist").write_text(_render("com.tag-clippings"), encoding="utf-8")
    (la / "com.voice-cleanup.plist").write_text(
        _render("com.voice-cleanup").replace("<true/>", "<false/>", 1), encoding="utf-8")
    return home, vault, la, env


def _digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


@macos_only
def test_a_dry_run_reports_the_plan_and_changes_nothing(scratch, allow_subprocess):
    home, vault, la, env = scratch
    before = _digest(la)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True, text=True).stdout
    r = subprocess.run(["/bin/bash", str(vault / "update.sh"), "--dry-run"],
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "dry run: not pulling" in out
    assert "com.voice-cleanup: definition changed" in out
    assert "com.tag-clippings: definition changed" not in out
    assert "com.obsidian.classify  add with: ./install.sh --only 58-classification" in out
    assert "Dry run at" in out and "nothing was changed" in out
    assert _digest(la) == before, "a dry run modified an installed job"
    assert not (home / "Library" / "Logs" / "obsidian-template-update").exists()
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True,
                          text=True).stdout == head


@macos_only
def test_local_changes_stop_the_update_before_the_pull(scratch, allow_subprocess):
    home, vault, la, env = scratch
    (vault / ".obsidian" / "types.json").write_text('{"x": "text"}\n')
    r = subprocess.run(["/bin/bash", str(vault / "update.sh")],
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert "Local changes to tracked files; not updating:" in out
    assert "checkout -- .obsidian/types.json" in out
    assert "== 2/6" not in out


@macos_only
def test_it_refuses_a_vault_that_is_not_its_repository(scratch, tmp_path, allow_subprocess):
    home, vault, la, env = scratch
    elsewhere = tmp_path / "copy"
    shutil.copytree(vault, elsewhere, symlinks=True)
    r = subprocess.run(["/bin/bash", str(elsewhere / "update.sh"), "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "is not this repository" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Structure that the tests above cannot reach.
# ---------------------------------------------------------------------------

def test_the_script_is_parsed_whole_before_the_pull_can_rewrite_it():
    lines = [ln for ln in UPDATE.splitlines() if ln.strip()]
    assert lines[-1] == 'main "$@"'
    assert "main() {" in UPDATE


def test_the_rest_runs_from_the_pulled_copy():
    pull = UPDATE.index('git -C "$REPO_ROOT" pull --ff-only')
    reexec = UPDATE.index('exec /bin/bash "$REPO_ROOT/update.sh" --after-pull --from "$FROM"')
    assert pull < reexec


def test_it_never_adopts_a_security_baseline_itself():
    for line in UPDATE.splitlines():
        if "--update" in line and "integrity" in line or "--update" in line and "$path" in line:
            assert line.lstrip().startswith(("warn", "#", "info")), line


def test_install_and_update_share_one_requirements_step():
    boot = (REPO / "installers" / "components" / "10-vault-bootstrap.sh").read_text(encoding="utf-8")
    assert 'install_requirements "$VENV_PY"' in boot
    assert 'install_requirements "$VENV_PY"' in UPDATE
    for src in (boot, UPDATE):
        assert "-m pip install -r" not in src, "a second, separate pip step"


def test_the_meeting_pull_component_loads_the_helper_it_calls():
    src = (REPO / "installers" / "components" / "54-meeting-pull.sh").read_text(encoding="utf-8")
    assert (src.index('source "$REPO_ROOT/installers/lib/plist.sh"')
            < src.index('install_plist_and_load "Templates/'))


def test_the_prepopulate_job_watches_the_folder_the_script_reads():
    plist = (REPO / "Templates" / "Scripts" / "com.meeting-prepopulate.plist").read_text(encoding="utf-8")
    assert "<string>/Users/YOUR_USERNAME/MeetingIngest</string>" in plist
    assert "HandoffDrop</string>" not in plist


@macos_only
def test_an_update_rewrites_only_the_changed_job_and_keeps_the_old_copy(
        scratch, tmp_path, allow_subprocess):
    """The real write path, with launchctl and pip replaced by stand-ins that
    only record their arguments. Runs the post-pull half (--after-pull), since
    the scratch repo has no remote."""
    home, vault, la, env = scratch
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    (stubs / "launchctl").write_text(f'#!/bin/sh\necho "launchctl $*" >> "{calls}"\n')
    (stubs / "launchctl").chmod(0o755)
    venv_py = vault / "Templates" / "Scripts" / ".venv" / "bin" / "python3"
    venv_py.unlink()
    venv_py.write_text('#!/bin/sh\n'
                       'if [ "$1" = "-c" ]; then echo "arm64 13 gil"; exit 0; fi\n'   # install_requirements' interpreter probe
                       f'echo "venv-python $*" >> "{calls}"\n')
    venv_py.chmod(0o755)
    old_voice = (la / "com.voice-cleanup.plist").read_bytes()
    old_tag = (la / "com.tag-clippings.plist").read_bytes()

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=vault,
                          capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["/bin/bash", str(vault / "update.sh"), "--after-pull", "--from", head],
                       capture_output=True, text=True,
                       env={**env, "PATH": f"{stubs}:{env['PATH']}"})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out

    assert (la / "com.voice-cleanup.plist").read_text(encoding="utf-8") == _render("com.voice-cleanup")
    assert (la / "com.tag-clippings.plist").read_bytes() == old_tag
    [backup] = list((home / "Library" / "Logs" / "obsidian-template-update").iterdir())
    assert (backup / "com.voice-cleanup.plist").read_bytes() == old_voice
    assert not (backup / "com.tag-clippings.plist").exists()

    log = calls.read_text()
    pip = [ln for ln in log.splitlines() if ln.startswith("venv-python -m pip")]
    assert len(pip) == 1, pip
    assert "--require-hashes --no-deps --only-binary :all: --force-reinstall -r" in pip[0]
    assert pip[0].endswith("/Templates/Scripts/requirements.lock")
    loads = [ln for ln in log.splitlines() if ln.startswith("launchctl load")]
    assert loads == [f"launchctl load {la}/com.voice-cleanup.plist"], log
    assert not (la / "com.obsidian-rag-sync.plist").exists(), "an update installed a declined job"
    assert f"Already at {head}" in out
