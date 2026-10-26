"""
test_tree_gate.py -- the pre-execution tree gate (findings 5278120 / 5278133).

In the documented install the vault IS the git checkout and is synced by
Obsidian Sync, so a paired device can write into installers/ or
Templates/Scripts/ and the next install or update runs what it wrote. The
entry scripts now refuse, before anything else under installers/ is sourced,
when git reports a modified, untracked or hidden file there.

The bash half runs the real entry scripts in a throwaway git repository with
a scratch HOME (--list / --dry-run modes, which run no component). The
PowerShell half is structural: there is no pwsh here; behaviour on Windows is
verified on a test machine.

Residual, stated here so no test pretends otherwise: the gate cannot stop an
edit to the entry script itself (install.sh, update.sh, uninstall.sh,
install.ps1, update.ps1). What protects those is the Obsidian Sync setting the
install prints at the end.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from platform_caps import macos_shell, rmtree_force

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "installers" / "lib"
WIN = REPO / "Templates" / "Scripts" / "windows"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

REFUSED = "TREE GATE: REFUSED. The installer code in this tree differs from the git checkout."


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


@pytest.fixture
def tree(tmp_path):
    """A throwaway checkout with the real entry scripts, installer libs and
    components, the real .gitignore, and one Templates/Scripts file."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "vault"
    repo.mkdir()
    for rel in ("install.sh", "update.sh", "uninstall.sh", ".gitignore", ".gitattributes"):
        shutil.copy2(REPO / rel, repo / rel)
    shutil.copytree(LIB, repo / "installers" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__", "*.local"))
    shutil.copytree(REPO / "installers" / "components", repo / "installers" / "components")
    (repo / "installers" / "profiles").mkdir()
    shutil.copy2(REPO / "installers" / "profiles" / "gateway.env.example",
                 repo / "installers" / "profiles" / "gateway.env.example")
    scripts = repo / "Templates" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO / "Templates" / "Scripts" / "secret_store.py", scripts / "secret_store.py")
    _git(repo, "init", "-q")
    _git(repo, "config", "gc.auto", "0")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    env = {**os.environ, **GIT_ENV, "HOME": str(home), "USER": "tester"}
    return repo, home, env


def _run(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(script), *args], capture_output=True, text=True,
                          env=env, cwd=str(script.parent))


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------

@macos_shell
def test_a_clean_tree_runs(tree, allow_subprocess):
    repo, home, env = tree
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "tree gate: installer code matches the git checkout (HEAD " in r.stdout
    assert "  90-verify" in r.stdout


@macos_shell
def test_a_modified_tracked_installer_file_refuses_before_common_sh_runs(tree, allow_subprocess):
    repo, home, env = tree
    marker = home / "planted-ran"
    with open(repo / "installers" / "lib" / "common.sh", "a", encoding="utf-8") as fh:
        fh.write(f'\ntouch "{marker}"\n')
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 1
    assert REFUSED in r.stderr
    assert " M installers/lib/common.sh" in r.stderr
    assert 'status --untracked-files=all' in r.stderr and " diff" in r.stderr
    assert "--allow-local-changes" in r.stderr
    assert not marker.exists(), "the planted code ran before the gate refused"
    assert "  90-verify" not in r.stdout


@macos_shell
def test_an_untracked_file_in_installers_refuses(tree, allow_subprocess):
    repo, home, env = tree
    (repo / "installers" / "components" / "99-planted.sh").write_text("echo planted\n")
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 1
    assert REFUSED in r.stderr
    assert "?? installers/components/99-planted.sh" in r.stderr


@macos_shell
def test_an_untracked_file_in_templates_scripts_refuses(tree, allow_subprocess):
    repo, home, env = tree
    (repo / "Templates" / "Scripts" / "com.planted.plist").write_text("<plist/>\n")
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 1
    assert "?? Templates/Scripts/com.planted.plist" in r.stderr


@macos_shell
def test_a_self_ignoring_gitignore_cannot_hide_a_planted_component(tree, allow_subprocess):
    """`*` in a nested .gitignore ignores the .gitignore too, so plain
    `git status` shows nothing; the ignored-files pass still sees it."""
    repo, home, env = tree
    comp = repo / "installers" / "components"
    (comp / ".gitignore").write_text("99-*\n.gitignore\n")
    (comp / "99-planted.sh").write_text("echo planted\n")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True).stdout
    assert status == "", "precondition: plain git status should be blind to it"
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 1
    assert "!! installers/components/.gitignore" in r.stderr
    assert "!! installers/components/99-planted.sh" in r.stderr


@macos_shell
def test_a_modified_gate_helper_is_not_sourced(tree, allow_subprocess):
    repo, home, env = tree
    marker = home / "gate-ran"
    gate = repo / "installers" / "lib" / "tree_gate.sh"
    gate.write_text(f'touch "{marker}"\ntree_gate_check() {{ return 0; }}\n'
                    'tree_gate_sync_notice() { :; }\n')
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 1
    assert "TREE GATE: REFUSED. installers/lib/tree_gate.sh differs from HEAD; not sourcing it." in r.stderr
    assert not marker.exists()


@macos_shell
def test_the_override_runs_and_is_logged(tree, allow_subprocess):
    repo, home, env = tree
    with open(repo / "installers" / "lib" / "common.sh", "a", encoding="utf-8") as fh:
        fh.write("\n# a maintainer's edit\n")
    r = _run(repo / "install.sh", "--list", "--allow-local-changes", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TREE GATE OVERRIDE: --allow-local-changes given." in r.stdout
    assert " M installers/lib/common.sh" in r.stdout
    assert "  90-verify" in r.stdout
    log = (home / "Library" / "Logs" / "obsidian-template-tree-gate.log").read_text()
    assert "OVERRIDE install" in log and "installers/lib/common.sh" in log
    assert (home / "Library" / "Logs" / "obsidian-template-tree-gate.log").stat().st_mode & 0o077 == 0


@macos_shell
def test_a_tree_that_is_not_a_git_checkout_warns_and_continues(tree, tmp_path, allow_subprocess):
    repo, home, env = tree
    rmtree_force(repo / ".git")
    r = _run(repo / "install.sh", "--list", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING: " in r.stdout and "is not a git checkout" in r.stdout
    assert "  90-verify" in r.stdout
    strict = _run(repo / "install.sh", "--list", "--strict-tree", env=env)
    assert strict.returncode == 1
    assert "This run requires a checkout (--strict-tree, or an update, which pulls)." in strict.stderr


@macos_shell
def test_a_gitignored_profile_is_allowed_and_its_hash_printed(tree, allow_subprocess):
    repo, home, env = tree
    prof = repo / "installers" / "profiles" / "site.env"
    prof.write_text("# site.env\n# A test profile\nPROFILE_DESCRIPTION='test'\n")
    r = _run(repo / "install.sh", "--dry-run", "--profile", "site", env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "installer code matches the git checkout" in out
    assert f"profile sha256: {hashlib.sha256(prof.read_bytes()).hexdigest()}" in out
    assert f"profile file:   {prof.resolve()}" in out


@macos_shell
def test_a_profile_outside_the_vault_is_allowed(tree, tmp_path, allow_subprocess):
    repo, home, env = tree
    prof = tmp_path / "Downloads" / "ours.env"
    prof.parent.mkdir()
    prof.write_text("PROFILE_DESCRIPTION='handed over'\n")
    r = _run(repo / "install.sh", "--dry-run", "--profile", str(prof), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert hashlib.sha256(prof.read_bytes()).hexdigest() in r.stdout


@macos_shell
@pytest.mark.parametrize("where", ["ours.env", "Templates/ours.env"])
def test_a_profile_elsewhere_in_the_vault_is_refused(tree, where, allow_subprocess):
    repo, home, env = tree
    marker = home / "profile-ran"
    prof = repo / where
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(f'touch "{marker}"\n')
    r = _run(repo / "install.sh", "--dry-run", "--profile", str(prof), env=env)
    assert r.returncode == 1
    assert "is inside the vault but not in" in r.stderr
    assert not marker.exists()


def test_a_full_install_ends_with_the_sync_notice():
    src = (REPO / "install.sh").read_text(encoding="utf-8")
    end = src[src.index('section "install.sh complete"'):]
    assert "tree_gate_sync_notice" in end
    notice = (LIB / "tree_gate.sh").read_text(encoding="utf-8")
    body = notice[notice.index("tree_gate_sync_notice() {"):]
    assert 'turn OFF "Sync all other types"' in body
    assert "Excluded folders > add Templates/Scripts" in body
    assert body.isascii()


# ---------------------------------------------------------------------------
# update.sh and uninstall.sh
# ---------------------------------------------------------------------------

@macos_shell
def test_update_refuses_an_untracked_installer_file(tree, allow_subprocess):
    """It used to check tracked files only (--untracked-files=no)."""
    repo, home, env = tree
    (repo / "installers" / "lib" / "planted.sh").write_text("echo planted\n")
    for args in ([], ["--dry-run"], ["--after-pull", "--from", "HEAD"]):
        r = _run(repo / "update.sh", *args, env=env)
        assert r.returncode == 1, args
        assert REFUSED in r.stderr, args
        assert "?? installers/lib/planted.sh" in r.stderr


@macos_shell
def test_update_refuses_a_tree_git_cannot_vouch_for(tree, allow_subprocess):
    repo, home, env = tree
    rmtree_force(repo / ".git")
    r = _run(repo / "update.sh", "--dry-run", env=env)
    assert r.returncode == 1
    assert "This run requires a checkout (--strict-tree, or an update, which pulls)." in r.stderr


@macos_shell
def test_uninstall_refuses_before_sourcing_its_libraries(tree, allow_subprocess):
    repo, home, env = tree
    marker = home / "planted-ran"
    with open(repo / "installers" / "lib" / "secrets.sh", "a", encoding="utf-8") as fh:
        fh.write(f'\ntouch "{marker}"\n')
    r = _run(repo / "uninstall.sh", "--dry-run", "--yes", env=env)
    assert r.returncode == 1
    assert REFUSED in r.stderr
    assert not marker.exists()


@pytest.mark.parametrize("name", ["install.sh", "update.sh", "uninstall.sh"])
def test_the_gate_comes_before_any_other_installer_source(name):
    src = (REPO / name).read_text(encoding="utf-8")
    gate = src.index('source "$REPO_ROOT/installers/lib/tree_gate.sh"')
    check = src.index("tree_gate_check ")
    first_other = min(i for i in (src.find('source "$REPO_ROOT/installers/lib/common.sh"'),) if i >= 0)
    assert gate < check < first_other
    assert src.index("hash-object -- installers/lib/tree_gate.sh") < gate


def test_update_has_one_tree_check():
    src = (REPO / "update.sh").read_text(encoding="utf-8")
    assert "--untracked-files=no" not in src, "a second, separate dirty check"
    pull = src[src.index('info "== 1/6 pull =="'):]
    assert pull.index("obsidian_running") < pull.index('tree_gate_check "$REPO_ROOT" update')
    assert pull.index('tree_gate_check "$REPO_ROOT" update') < pull.index("pull --ff-only")
    # The override reaches every install.sh step and the post-pull pass.
    for line in src.splitlines():
        if '"$REPO_ROOT/install.sh" --auto --only' in line or "--after-pull --from" in line and "exec" in line:
            assert '${gate_args[@]+"${gate_args[@]}"}' in line, line


# ---------------------------------------------------------------------------
# Windows: structure only (no pwsh here).
# ---------------------------------------------------------------------------

GATE_PS = LIB / "tree_gate.ps1"


def test_the_powershell_gate_is_ascii_and_crlf():
    raw = GATE_PS.read_bytes()
    assert raw.isascii()
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")


# uninstall.ps1 since review R3 (2026-10-26): uninstall.sh was gated, its
# Windows twin was not, and it deletes paths its libraries compute.
@pytest.mark.parametrize("name", ["install.ps1", "update.ps1", "uninstall.ps1"])
def test_the_powershell_gate_runs_before_common_ps1(name):
    src = (WIN / name).read_text(encoding="utf-8")
    verify = src.index("hash-object -- 'installers/lib/tree_gate.ps1'")
    load = src.index(". $treeGate")
    check = src.index("Test-TreeGate -Root $treeRoot -Mode ")
    common = src.index('. "$PSScriptRoot\\common.ps1"')
    assert verify < load < check < common
    assert "[switch]$AllowLocalChanges" in src


def test_uninstall_ps1_gates_in_uninstall_mode_and_the_gate_accepts_it():
    src = (WIN / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Test-TreeGate -Root $treeRoot -Mode uninstall" in src
    assert "[switch]$StrictTree" in src
    assert "[ValidateSet('install', 'update', 'uninstall')]" in GATE_PS.read_text(encoding="utf-8")


def test_update_ps1_has_one_tree_check_and_passes_the_override_on():
    src = (WIN / "update.ps1").read_text(encoding="utf-8")
    assert "--untracked-files=no" not in src
    first = src[src.index("if (-not $AfterPull) {"):src.index("Write-Host '== 2/3 requirements =='")]
    gate = first.index("Test-TreeGate -Root $treeRoot -Mode update")
    assert first.index("Get-Process -Name Obsidian") < gate < first.index("git -C $vault pull --ff-only")
    assert "-AfterPull -From $from -AllowLocalChanges" in first


def test_install_ps1_checks_the_profile_and_prints_the_sync_notice():
    src = (WIN / "install.ps1").read_text(encoding="utf-8")
    assert src.index("Test-TreeGateProfile -Root $treeRoot -Path $profPath") < \
        src.index("$prof = Import-InstallProfile -Spec $InstallProfile")
    assert src.rstrip().endswith("Show-TreeGateSyncNotice")
    assert "[switch]$StrictTree" in src


def test_both_gates_check_the_same_paths_and_allow_the_same_ignored_files():
    sh = (LIB / "tree_gate.sh").read_text(encoding="utf-8")
    ps = GATE_PS.read_text(encoding="utf-8")
    sh_paths = sh[sh.index("TREE_GATE_PATHS=("):].split(")", 1)[0]
    for p in ("installers", "Templates/Scripts", "install.sh", "update.sh", "uninstall.sh",
              ".gitignore", ".gitattributes"):
        assert p in sh_paths and f"'{p}'" in ps
    for allowed in ("installers/profiles/*.env", "installers/lib/__pycache__/",
                    "installers/lib/identity-denylist.local",
                    "installers/lib/identity-allowed-identities.local"):
        assert allowed in sh and allowed in ps
    for marker in ("--untracked-files=all", "--ignored", "--directory", "core.fsmonitor=false",
                   "TREE GATE OVERRIDE", "Local changes to tracked files; not updating:"):
        assert marker in sh and marker in ps, marker


def test_powershell_gate_has_no_pwsh_only_syntax():
    src = GATE_PS.read_text(encoding="utf-8")
    for bad in ("??", "?.", " ? ", "-AsHashtable", "Join-Path -AdditionalChildPath"):
        assert bad not in src, bad


# N5 (2026-10-21): git reports an ignored __pycache__/ as one entry, so the
# gate cannot see a .pyc planted inside it, and installer helpers run without
# -I import their siblings. After the gate, no Python the run starts may read
# bytecode from the tree.
@macos_shell
def test_after_the_gate_python_reads_no_bytecode_from_the_tree(tree, allow_subprocess):
    repo, home, env = tree
    probe = ('source installers/lib/tree_gate.sh; '
             'tree_gate_check "$PWD" install 0 0 >/dev/null || exit 9; '
             'python3 -c "import sys; print(sys.flags.dont_write_bytecode, sys.pycache_prefix)"')
    r = subprocess.run(["/bin/bash", "-c", probe], capture_output=True, text=True,
                       env=env, cwd=str(repo))
    assert r.returncode == 0, r.stdout + r.stderr
    flag, prefix = r.stdout.split()
    assert flag == "1"
    assert prefix.endswith("/Library/Caches/obsidian-template/pycache")
    assert not prefix.startswith(str(repo))


def test_the_powershell_gate_also_stops_tree_bytecode():
    src = (REPO / "installers" / "lib" / "tree_gate.ps1").read_text(encoding="ascii")
    body = src[src.index("function Test-TreeGate {"):src.index("function Test-TreeGateProfile")]
    assert "$env:PYTHONDONTWRITEBYTECODE = '1'" in body
    assert "$env:PYTHONPYCACHEPREFIX = Join-Path $env:LOCALAPPDATA 'obsidian-template\\pycache'" in body


# N9 (2026-10-22): on Windows a tracked doc that changed only in its line
# endings made update refuse. Outside the installer code that is noted, not
# refused; a real content change is still refused, and installer code is
# still checked byte for byte.
def _update_gate(repo: Path, env: dict) -> subprocess.CompletedProcess:
    probe = 'source installers/lib/tree_gate.sh; tree_gate_check "$PWD" update 0 1'
    return subprocess.run(["/bin/bash", "-c", probe], capture_output=True, text=True,
                          env=env, cwd=str(repo))


def _tracked_doc(repo: Path) -> Path:
    doc = repo / "docs" / "Guide.md"
    doc.parent.mkdir()
    doc.write_bytes(b"# Guide\n\nOne line.\nTwo lines.\n")
    _git(repo, "add", "docs/Guide.md")
    _git(repo, "commit", "-qm", "doc")
    return doc


@macos_shell
def test_update_notes_a_line_ending_only_change_outside_installer_code(tree, allow_subprocess):
    repo, home, env = tree
    doc = _tracked_doc(repo)
    doc.write_bytes(doc.read_bytes().replace(b"\n", b"\r\n"))
    r = _update_gate(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "line endings only, not refused" in r.stdout
    assert "docs/Guide.md" in r.stdout


@macos_shell
def test_update_still_refuses_a_content_change_outside_installer_code(tree, allow_subprocess):
    repo, home, env = tree
    doc = _tracked_doc(repo)
    doc.write_bytes(doc.read_bytes().replace(b"One line.", b"One changed line."))
    r = _update_gate(repo, env)
    assert r.returncode == 1
    assert "Local changes to tracked files; not updating:" in r.stderr
    assert "docs/Guide.md" in r.stderr


@macos_shell
def test_a_line_ending_change_to_installer_code_is_still_refused(tree, allow_subprocess):
    repo, home, env = tree
    lib = repo / "installers" / "lib" / "common.sh"
    lib.write_bytes(lib.read_bytes().replace(b"\n", b"\r\n"))
    r = _update_gate(repo, env)
    assert r.returncode == 1
    assert "TREE GATE: REFUSED. The installer code in this tree differs" in r.stderr
    assert "installers/lib/common.sh" in r.stderr


def test_the_powershell_gate_has_the_same_line_ending_rule():
    src = (REPO / "installers" / "lib" / "tree_gate.ps1").read_text(encoding="ascii")
    assert "diff --ignore-cr-at-eol --quiet -- $path" in src
    assert "tree gate: line endings only, not refused" in src
    assert "$l.StartsWith(' M ')" in src
