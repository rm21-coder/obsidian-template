"""
test_installer_input_validation.py -- the installer treats its own data files as
input (Mythos review, batch 2, Issue 2).

A template user's clone is writable by whatever runs as that user, so the pin
manifest, the requirements lock and the plist folder are input to the
installer, not trusted code. Each was found to steer it somewhere it should not
go:

  5278126  a pinned URL of "-K<file>" made curl read a planted config
  5278122  a pinned id or file name of "../.." or "/abs" wrote outside the
           plugin folder, before the hash check, and was left behind
  5278131  the same in the Windows installer, plus any URL scheme
  5278629  uninstall.sh --plugins ran rm -rf on pin-supplied names
  5278623  a lock line "pkg @ file:///x.whl --hash=<own>" installed a wheel
           whose .pth runs at every interpreter start
  5278124  update.sh installed and loaded any plist in Templates/Scripts,
           tracked or not

The bash fixes run here against scratch folders with a stand-in curl and a
stand-in venv python: nothing touches the network, a real venv or the real
LaunchAgents. The PowerShell twins can only be checked statically on this
machine; their regexes are also run through Python's re, which agrees with
.NET on every construct they use.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "installers" / "lib"
SCRIPTS = REPO / "Templates" / "Scripts"
WIN = SCRIPTS / "windows"

needs_bash = pytest.mark.skipif(sys.platform == "win32", reason="bash installer")

LOCKS = sorted(SCRIPTS.glob("requirements*.lock"))
GOOD_HASH = "a" * 64


def _bash(snippet: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail; source "{LIB}/common.sh"; {snippet}'
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, cwd=cwd)


# ---------------------------------------------------------------------------
# plugins.sh: fetch_plugin (5278122, 5278126)
# ---------------------------------------------------------------------------

FAKE_CURL = r"""#!/bin/bash
# Records its argv, one per line, then writes $FAKE_CURL_BODY to the -o path.
printf '%s\n' "$@" >> "$CURL_LOG"
printf -- '----\n' >> "$CURL_LOG"
out=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-o" ]]; then out="$2"; shift; fi
    shift
done
printf '%s' "$FAKE_CURL_BODY" > "$out"
"""


@pytest.fixture
def plugin_env(tmp_path):
    """A scratch repo root holding only a pin file, a vault, a stand-in curl on
    PATH, and a private TMPDIR so leftover staging folders are visible."""
    root = tmp_path / "repo"
    (root / "installers").mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / ".obsidian" / "plugins").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    log = tmp_path / "curl.log"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "CURL_LOG": str(log),
           "TMPDIR": str(tmpdir), "FAKE_CURL_BODY": "hello"}

    def run(pins, plugin_id):
        (root / "installers" / "plugin-pins.json").write_text(json.dumps(pins))
        return _bash(f'REPO_ROOT="{root}"; source "{LIB}/plugins.sh"; '
                     f'fetch_plugin "$PID" "{vault}"', env={**env, "PID": plugin_id})
    return run, vault, log, tmpdir, tmp_path


def _pin(pid, files):
    return [{"id": pid, "ref": "1.0", "files": {
        name: {"url": url, "sha256": sha} for name, (url, sha) in files.items()}}]


HELLO = hashlib.sha256(b"hello").hexdigest()
GH = "https://github.com/o/r/releases/download/1.0/"


@needs_bash
def test_a_good_pin_installs_and_curl_is_called_safely(plugin_env, allow_subprocess):
    run, vault, log, tmpdir, _ = plugin_env
    r = run(_pin("good", {"main.js": (GH + "main.js", HELLO)}), "good")
    assert r.returncode == 0, r.stderr
    assert (vault / ".obsidian" / "plugins" / "good" / "main.js").read_text() == "hello"
    argv = log.read_text().split("----\n")[0].splitlines()
    assert argv[0] == "-q"                                    # no ~/.curlrc
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"
    assert argv[argv.index("--url") + 1] == GH + "main.js"   # never an option
    assert list(tmpdir.iterdir()) == []                       # staging removed


@needs_bash
@pytest.mark.parametrize("pid", ["../../../out/ID_ESCAPE", "..", "a/b", "/abs", ".hidden", "-rf", "a..b"])
def test_an_unsafe_plugin_id_is_refused_before_any_download(plugin_env, pid, allow_subprocess):
    run, vault, log, _, tmp_path = plugin_env
    r = run(_pin(pid, {"main.js": (GH + "main.js", HELLO)}), pid)
    assert r.returncode != 0
    assert "REFUSED plugin id" in r.stderr
    assert not log.exists()
    assert not (tmp_path / "out").exists()


@needs_bash
@pytest.mark.parametrize("name", ["../../../../NAME_ESCAPE.txt", "/tmp/NAME_ESCAPE.txt", "..", "sub/main.js", ".x"])
def test_an_unsafe_file_name_is_refused_before_any_download(plugin_env, name, allow_subprocess):
    run, vault, log, _, _ = plugin_env
    # The bad name comes second: a good first entry must not be fetched either.
    pins = _pin("nameesc", {"main.js": (GH + "main.js", HELLO), name: (GH + "x", HELLO)})
    r = run(pins, "nameesc")
    assert r.returncode != 0
    assert "REFUSED file name" in r.stderr
    assert not log.exists()
    assert not (vault / ".obsidian" / "plugins" / "nameesc").exists()


@needs_bash
@pytest.mark.parametrize("url", [
    "-K/tmp/x.rc",                                   # the repro: curl config injection
    "--config=/tmp/x.rc",
    "file:///etc/passwd",
    "http://github.com/o/r/main.js",
    "https://evil.example/main.js",
    "https://github.com.evil.example/main.js",
    "https://objects.githubusercontent.com.example/x",
    "https://github.com/o/r/main.js -o /tmp/x",
])
def test_an_unsafe_url_is_refused_before_any_download(plugin_env, url, allow_subprocess):
    run, _, log, _, _ = plugin_env
    r = run(_pin("kinj", {"a": (url, HELLO)}), "kinj")
    assert r.returncode != 0
    assert "REFUSED URL for kinj/a" in r.stderr
    assert not log.exists()


@needs_bash
def test_a_raw_githubusercontent_url_is_allowed(plugin_env, allow_subprocess):
    """pin_plugins.py pins release-less plugins at a commit on this host."""
    run, vault, _, _, _ = plugin_env
    r = run(_pin("raw", {"main.js": ("https://raw.githubusercontent.com/o/r/abc/main.js", HELLO)}), "raw")
    assert r.returncode == 0, r.stderr


@needs_bash
def test_a_malformed_hash_is_refused(plugin_env, allow_subprocess):
    run, _, log, _, _ = plugin_env
    r = run(_pin("h", {"main.js": (GH + "main.js", "bad")}), "h")
    assert r.returncode != 0
    assert "REFUSED sha256 for h/main.js" in r.stderr
    assert not log.exists()


@needs_bash
def test_a_hash_mismatch_leaves_nothing_behind(plugin_env, allow_subprocess):
    run, vault, log, tmpdir, _ = plugin_env
    r = run(_pin("mm", {"main.js": (GH + "main.js", GOOD_HASH)}), "mm")
    assert r.returncode != 0
    assert "HASH MISMATCH for mm/main.js" in r.stderr
    assert log.exists()                                       # it did download
    assert not (vault / ".obsidian" / "plugins" / "mm").exists()
    assert list(tmpdir.iterdir()) == []                       # and deleted it


@needs_bash
def test_a_symlinked_plugin_folder_is_refused(plugin_env, allow_subprocess):
    run, vault, log, _, tmp_path = plugin_env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (vault / ".obsidian" / "plugins" / "sl").symlink_to(elsewhere)
    r = run(_pin("sl", {"main.js": (GH + "main.js", HELLO)}), "sl")
    assert r.returncode != 0
    assert "is a symlink or not a directory" in r.stderr
    assert list(elsewhere.iterdir()) == []


def test_every_shipped_pin_passes_the_checks():
    """The rules must not refuse the pins the repository actually ships."""
    name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    url_re = re.compile(r"^https://(github\.com|raw\.githubusercontent\.com)/\S+$")
    pins = json.loads((REPO / "installers" / "plugin-pins.json").read_text(encoding="utf-8"))
    assert pins
    for p in pins:
        assert name_re.match(p["id"]) and ".." not in p["id"], p["id"]
        for name, meta in p["files"].items():
            assert name_re.match(name) and ".." not in name, name
            assert url_re.match(meta["url"]), meta["url"]
            assert re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
    sh = (LIB / "plugins.sh").read_text(encoding="utf-8")
    assert name_re.pattern in sh and url_re.pattern.replace(r"\S", "[^[:space:]]") in sh


# ---------------------------------------------------------------------------
# uninstall.sh --plugins: rm_plugin_file (5278629)
# ---------------------------------------------------------------------------

def _rm_plugin_file_source() -> str:
    text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
    m = re.search(r"^rm_plugin_file\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "rm_plugin_file not found in uninstall.sh"
    return m.group(0)


def test_uninstall_routes_every_pin_entry_through_the_check():
    text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
    assert 'rm_plugin_file "$PLUGINS" "$PLUGINS_REAL" "$pid" "$fname"' in text
    assert 'rm_path "$PLUGINS/$pid/$fname"' not in text
    assert 'source "$REPO_ROOT/installers/lib/plugins.sh"' in text


@needs_bash
def test_uninstall_removes_only_regular_files_inside_the_plugin_folder(tmp_path, allow_subprocess):
    plugins = tmp_path / "plugins"
    a = plugins / "a"
    (a / "sub").mkdir(parents=True)
    (a / "main.js").write_text("x")
    (a / "data.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (outside / "main.js").write_text("keep")
    (a / "link").symlink_to(outside / "keep.txt")
    (plugins / "evil").symlink_to(outside)
    calls = [("a", "main.js"), ("a", "../../outside/keep.txt"), ("a", "sub"), ("a", "link"),
             ("evil", "main.js"), ("..", "outside"), ("a", "/abs")]
    snippet = (f'REPO_ROOT="{REPO}"; source "{LIB}/plugins.sh"; DRY_RUN=0; '
               + _rm_plugin_file_source()
               + f'\nP="{plugins}"; PR="$(cd "$P" && pwd -P)"\n'
               + "".join(f'rm_plugin_file "$P" "$PR" "{i}" "{f}"\n' for i, f in calls))
    r = _bash(snippet, env={"DRY_RUN": "0"})
    assert r.returncode == 0, r.stderr
    assert not (a / "main.js").exists()
    assert "removed: " in r.stderr
    assert (a / "data.json").exists()
    assert (a / "sub").is_dir()
    assert (a / "link").is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"
    assert (outside / "main.js").read_text() == "keep"
    assert r.stderr.count("not a plain plugin id and file name") == 3
    assert "REFUSED to remove " + str(a / "sub") + ": not a regular file" in r.stderr
    assert "REFUSED to remove " + str(a / "link") + ": not a regular file" in r.stderr
    assert "its folder does not resolve to" in r.stderr


# ---------------------------------------------------------------------------
# Lock validation: install_requirements and Install-Requirements (5278623)
# ---------------------------------------------------------------------------

REPRO_LINE = ("fakepkg @ file:///tmp/wheels/fakepkg-1.0-py3-none-any.whl "
              "--hash=sha256:637d147f09c6d79ce6d9e167f5390fd0bb7ed30e229f4256b84cf9963de81a98")

BAD_LOCKS = [
    (REPRO_LINE, "a direct-URL requirement"),
    ("fakepkg@file:///tmp/x.whl", "a direct-URL requirement"),
    ("fakepkg==1.0 ; python_version > '3' and extra == 'x@y'", "a direct-URL requirement"),
    ("https://evil.example/fakepkg-1.0-py3-none-any.whl", "a direct-URL requirement"),
    ("-i https://evil.example/simple", "a pip option line"),
    ("--index-url=https://evil.example/simple", "a pip option line"),
    ("--extra-index-url https://evil.example/simple", "a pip option line"),
    ("   -r other.txt", "a pip option line"),
    ("-e .", "a pip option line"),
    ("--find-links /tmp/wheels", "a pip option line"),
    ("-c constraints.txt", "a pip option line"),
    ("--hash=sha256:" + GOOD_HASH + " --index-url x", "a --hash line that is not exactly"),
    ("--hash=md5:abc", "a --hash line that is not exactly"),
    ("fakepkg==1.0 --config-settings x=y \\", "a pip option on a requirement line"),
    ("fakepkg-1.0-py3-none-any.whl \\", "not a name==version requirement"),
    ("./fakepkg-1.0-py3-none-any.whl", "not a name==version requirement"),
    ("fakepkg>=1.0 \\", "not a name==version requirement"),
    ("fakepkg==${VERSION} \\", "not a name==version requirement"),
    ("fakepkg \\", "not a name==version requirement"),
]


def _write_lock(path: Path, bad: str) -> Path:
    good = LOCKS[0].read_text(encoding="utf-8")
    path.write_text(good + bad + "\n    --hash=sha256:" + GOOD_HASH + "\n", encoding="utf-8")
    return path


@needs_bash
@pytest.mark.parametrize("lock", LOCKS, ids=lambda p: p.name)
def test_every_shipped_lock_is_plain(lock, tmp_path, allow_subprocess):
    r = _bash(f'lock_is_plain "{lock}"')
    assert r.returncode == 0, r.stderr
    crlf = tmp_path / lock.name                       # a Windows checkout
    crlf.write_bytes(lock.read_bytes().replace(b"\n", b"\r\n"))
    r = _bash(f'lock_is_plain "{crlf}"')
    assert r.returncode == 0, r.stderr


@needs_bash
@pytest.mark.parametrize("bad,why", BAD_LOCKS)
def test_a_lock_line_that_could_redirect_pip_is_refused(bad, why, tmp_path, allow_subprocess):
    lock = _write_lock(tmp_path / "requirements.lock", bad)
    n = len(LOCKS[0].read_text(encoding="utf-8").splitlines()) + 1
    r = _bash(f'lock_is_plain "{lock}"')
    assert r.returncode == 1
    assert f"REFUSED {lock} line {n}: {why}" in r.stderr


@needs_bash
def test_install_requirements_refuses_the_repro_before_running_any_python(tmp_path, allow_subprocess):
    """The check runs before the venv's interpreter is even probed, so pip
    never sees the lock."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    _write_lock(scripts / "requirements.lock", REPRO_LINE)
    marker = tmp_path / "python-ran"
    py = tmp_path / "python3"
    py.write_text(f'#!/bin/bash\ntouch "{marker}"\n')
    py.chmod(0o755)
    r = _bash(f'install_requirements "{py}" "{scripts}/requirements.txt"')
    assert r.returncode != 0
    assert "a direct-URL requirement" in r.stderr
    assert not marker.exists()


@needs_bash
def test_the_lock_check_is_fast_on_a_pumped_line(tmp_path, allow_subprocess):
    lock = tmp_path / "requirements.lock"
    # A UTF-8 locale: there, ${s%<suffix>} is quadratic (1.8 s on this line).
    lock.write_bytes(("a" * 50000 + "==1.0 ; " + "(" * 50000 + " \\  \r\n").encode())
    t = time.monotonic()
    r = _bash(f'lock_is_plain "{lock}"', env={"LC_ALL": "en_US.UTF-8"})
    assert time.monotonic() - t < 1.0
    assert r.returncode == 0, r.stderr


@needs_bash
def test_the_plugin_checks_are_fast_on_a_pumped_value(allow_subprocess):
    long = "a" * 50000
    t = time.monotonic()
    r = _bash(f'REPO_ROOT=/nonexistent; source "{LIB}/plugins.sh"; '
              f'plugin_name_ok "{long}-" ; plugin_url_ok "https://github.com/{long}"; '
              f'! plugin_name_ok "{long}.." ', env={"LC_ALL": "en_US.UTF-8"})
    assert time.monotonic() - t < 1.0
    assert r.returncode == 0, r.stderr


def test_lock_extras_runs_isolated():
    text = (LIB / "common.sh").read_text(encoding="utf-8")
    assert '"$venv_py" -I "$(dirname "$lock")/lock_extras.py"' in text
    body = text[text.index("install_requirements() {"):]
    assert body.index('lock_is_plain "$lock" || return 1') < body.index("-m pip install")


# The PowerShell twin, statically and through Python's re.

COMMON_PS = (WIN / "common.ps1").read_text(encoding="ascii")


def _ps_single_quoted(text: str, var: str) -> str:
    m = re.search(r"\$" + var + r"\s*=\s*'((?:[^']|'')*)'", text)
    assert m, f"${var} not found"
    return m.group(1).replace("''", "'")


def _ps_lock_why(s: str) -> str:
    """Assert-PlainLock's decision, transcribed; the regexes are its own."""
    hash_re = _ps_single_quoted(COMMON_PS, "hashRe")
    req_re = _ps_single_quoted(COMMON_PS, "reqRe")
    s = s.strip()
    if not s or s.startswith("#"):
        return ""
    if s.startswith("--hash="):
        return "" if re.search(hash_re, s) else "a --hash line that is not exactly"
    if s.startswith("-"):
        return "a pip option line"
    if "@" in s or "://" in s:
        return "a direct-URL requirement"
    if re.search(r"\s-", s):
        return "a pip option on a requirement line"
    return "" if re.search(req_re, s) else "not a name==version requirement"


def test_install_requirements_ps_checks_the_lock_before_pip():
    body = COMMON_PS[COMMON_PS.index("function Install-Requirements {"):]
    assert body.index("Assert-PlainLock -Lock $lock") < body.index("-m pip install")
    fn = COMMON_PS[COMMON_PS.index("function Assert-PlainLock {"):COMMON_PS.index("function Install-Requirements {")]
    for marker in ("a pip option line", "a direct-URL requirement", "a pip option on a requirement line",
                   "not a name==version requirement", "a --hash line that is not exactly",
                   "throw \"REFUSED $Lock line ${n}: $why."):
        assert marker in fn, marker


@pytest.mark.parametrize("lock", LOCKS, ids=lambda p: p.name)
def test_the_ps_lock_rules_accept_every_shipped_lock(lock):
    for line in lock.read_text(encoding="utf-8").splitlines():
        assert _ps_lock_why(line) == "", line


@pytest.mark.parametrize("bad,why", BAD_LOCKS)
def test_the_ps_lock_rules_refuse_what_bash_refuses(bad, why):
    assert _ps_lock_why(bad).startswith(why)


# ---------------------------------------------------------------------------
# Install-Plugins.ps1 (5278131), statically
# ---------------------------------------------------------------------------

INSTALL_PLUGINS_PS = (WIN / "Install-Plugins.ps1").read_text(encoding="ascii")


def test_install_plugins_ps_validates_before_downloading():
    fn = INSTALL_PLUGINS_PS[INSTALL_PLUGINS_PS.index("function Install-OnePlugin {"):]
    first_fetch = fn.index("Invoke-WebRequest")
    for check in ("if (-not (Test-PluginName $Id))", "if (-not (Test-PluginName $name))",
                  "if (-not (Test-PluginUrl $url))",
                  "$target.StartsWith($dirFull, [System.StringComparison]::OrdinalIgnoreCase)",
                  "[IO.FileAttributes]::ReparsePoint"):
        assert check in fn, check
        assert fn.index(check) < first_fetch, check
    assert "foreach ($prop in $files)" in fn             # only the checked list is fetched


@pytest.mark.parametrize("value,ok", [
    ("cmdr", True), ("obsidian-tasks-plugin", True), ("main.js", True),
    ("../x", False), ("..", False), ("a..b", False), ("C:\\x", False), ("/abs", False),
    ("a\\b", False), (".x", False), ("x.", False),
])
def test_install_plugins_ps_name_rule(value, ok):
    name_re = _ps_single_quoted(INSTALL_PLUGINS_PS, "PluginNameRe")
    got = bool(re.search(name_re, value)) and ".." not in value and not value.endswith(".")
    assert got is ok
    assert "-notlike '*..*'" in INSTALL_PLUGINS_PS and "-not $Name.EndsWith('.')" in INSTALL_PLUGINS_PS


@pytest.mark.parametrize("url,ok", [
    (GH + "main.js", True), ("https://raw.githubusercontent.com/o/r/abc/main.js", True),
    ("http://github.com/o/r/main.js", False), ("file:///C:/x", False),
    ("https://github.com.evil.example/x", False), ("\\\\server\\share\\x", False),
])
def test_install_plugins_ps_url_rule(url, ok):
    assert bool(re.search(_ps_single_quoted(INSTALL_PLUGINS_PS, "PluginUrlRe"), url)) is ok


@pytest.mark.parametrize("rel", ["windows/Install-Plugins.ps1", "windows/common.ps1"])
def test_changed_powershell_stays_ascii_crlf(rel):
    raw = (SCRIPTS / rel).read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == raw.count(b"\r\n")


# ---------------------------------------------------------------------------
# update.sh: plan_agents acts only on tracked plists with a component (5278124)
# ---------------------------------------------------------------------------

@pytest.fixture
def plist_repo(tmp_path):
    repo = tmp_path / "r"
    shutil.copytree(REPO / "installers" / "components", repo / "installers" / "components")
    scripts = repo / "Templates" / "Scripts"
    scripts.mkdir(parents=True)
    for p in SCRIPTS.glob("*.plist"):
        shutil.copy2(p, scripts / p.name)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, env=env, capture_output=True)
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    return repo, scripts, la


def _plan(repo, la):
    r = _bash(f'source "{LIB}/plist.sh"; source "{LIB}/update.sh"; plan_agents "{repo}" "{la}"',
              env={"USER": "tester"})
    assert r.returncode == 0, r.stderr
    return {line.split()[1]: line.split()[0] for line in r.stdout.splitlines()}, r.stderr


@needs_bash
def test_an_untracked_plist_is_never_planned(plist_repo, allow_subprocess):
    repo, scripts, la = plist_repo
    planted = '<?xml version="1.0"?><plist><dict><key>Label</key><string>com.planted</string></dict></plist>\n'
    (scripts / "com.planted.plist").write_text(planted)
    (la / "com.planted.plist").write_text("old\n")             # installed, and different
    plan, err = _plan(repo, la)
    assert "com.planted" not in plan
    assert "Templates/Scripts/com.planted.plist is not tracked by git: ignored" in err
    assert plan["com.tag-clippings"] == "not-installed"         # tracked ones still planned


@needs_bash
def test_a_changed_agent_without_a_component_is_refused(plist_repo, allow_subprocess):
    repo, scripts, la = plist_repo
    (la / "com.obsidian.claude-auth-check.plist").write_text("hand-installed, older\n")
    (la / "com.tag-clippings.plist").write_text("older\n")
    plan, err = _plan(repo, la)
    assert plan["com.obsidian.claude-auth-check"] == "refused"
    assert ("Templates/Scripts/com.obsidian.claude-auth-check.plist changed, but no installer "
            "component installs it") in err
    assert plan["com.tag-clippings"] == "changed"               # has a component


@needs_bash
def test_outside_a_git_checkout_nothing_is_planned(tmp_path, allow_subprocess):
    repo = tmp_path / "notgit"
    shutil.copytree(REPO / "installers" / "components", repo / "installers" / "components")
    (repo / "Templates" / "Scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "com.tag-clippings.plist", repo / "Templates" / "Scripts")
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.tag-clippings.plist").write_text("older\n")
    plan, err = _plan(repo, la)
    assert plan == {}
    assert "is not a git checkout: not planning any job update" in err
