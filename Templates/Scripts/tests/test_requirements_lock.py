"""
test_requirements_lock.py -- installs take every package at a locked release,
checked against its hash, and nothing else.

requirements.txt was unpinned: each install or update took whatever PyPI held
that day, dependencies of dependencies included, unchecked -- and ran
`pip install --upgrade pip` first, an unpinned fetch ahead of the rest. Hash-
pinning was a committed security item (due end of September 2026). Now
installers/lib/lock_requirements.py writes requirements.lock (and
requirements-dropper.lock for the Markitdown Dropper app), universal, fully
hashed resolves, and every pip step installs only a lock with
--require-hashes --no-deps --only-binary --force-reinstall.

Review of the first version (2026-10-01) found: --check passed a lock with
fake hashes (it seeded the "fresh" resolve from the committed lock), UV_*
variables redirected the resolve, an existing venv kept unchecked files,
two components still pip-installed unpinned, and no Python ceiling. These
pin the fixes, and the floors the lock implies (macOS 14+ on Apple Silicon,
Python 3.10-3.14, 3.12+ on Windows ARM64).
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "Templates" / "Scripts"
LOCK_TEXT = (SCRIPTS / "requirements.lock").read_text(encoding="utf-8")
DROPPER_LOCK = (SCRIPTS / "requirements-dropper.lock").read_text(encoding="utf-8")
REQ_TEXT = (SCRIPTS / "requirements.txt").read_text(encoding="utf-8")
COMMON_SH = (REPO / "installers" / "lib" / "common.sh").read_text(encoding="utf-8")
COMMON_PS = (SCRIPTS / "windows" / "common.ps1").read_text(encoding="utf-8")
INSTALL_PS = (SCRIPTS / "windows" / "install.ps1").read_text(encoding="utf-8")
COMPONENTS = REPO / "installers" / "components"
PREFLIGHT = (COMPONENTS / "00-preflight.sh").read_text(encoding="utf-8")
CHECKS = (REPO / "installers" / "lib" / "security-checks.sh").read_text(encoding="utf-8")
FLAGS = ("--require-hashes", "--no-deps", "--only-binary", "--force-reinstall")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load("lock_requirements", REPO / "installers" / "lib" / "lock_requirements.py")
X = _load("lock_extras", SCRIPTS / "lock_extras.py")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---- the locks themselves --------------------------------------------------

@pytest.mark.parametrize("text", [LOCK_TEXT, DROPPER_LOCK], ids=["main", "dropper"])
def test_every_locked_release_carries_a_hash(text: str) -> None:
    assert len(L.entries(text)) >= 20
    assert L.unhashed(text) == []


def test_the_hash_check_catches_an_unhashed_entry() -> None:
    stripped = re.sub(r"(?m)^six==1\.17\.0 \\\n(?:    --hash=\S+(?: \\)?\n)+", "six==1.17.0\n",
                      LOCK_TEXT)
    assert stripped != LOCK_TEXT
    assert L.unhashed(stripped) == ["six"]


def test_every_requirement_is_in_its_lock() -> None:
    for ls in L.LOCKSETS:
        wanted = {_norm(m.group(1)) for m in re.finditer(
            r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)", ls.req.read_text(encoding="utf-8"))}
        locked = {_norm(n) for n, _ in L.pins(ls.lock.read_text(encoding="utf-8"))}
        assert wanted and wanted <= locked, (ls.req.name, wanted - locked)


def test_the_locks_name_the_tool_that_writes_them() -> None:
    for text in (LOCK_TEXT, DROPPER_LOCK):
        assert "python3 installers/lib/lock_requirements.py" in text.splitlines()[1]


def test_entries_read_name_version_marker_and_every_hash() -> None:
    e = L.entries(LOCK_TEXT)
    av = {k: v for k, v in e.items() if k[0] == "av"}
    assert len(av) == 3 and all(k[2] for k in av)          # three marker-scoped versions
    assert all(len(h) > 1 for h in av.values())


# ---- --check compares against the index, not against itself ---------------

H = "a" * 64


def _lock(*items: tuple[str, str, str, list[str]]) -> str:
    out = []
    for name, ver, marker, hashes in items:
        out.append(f"{name}=={ver}" + (f" ; {marker}" if marker else "") + " \\")
        out += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "")
                for i, h in enumerate(hashes)]
        out.append("    # via -r requirements.txt")
    return "\n".join(out) + "\n"


def test_compare_flags_a_hash_the_index_does_not_publish() -> None:
    fresh = _lock(("idna", "3.20", "", [H, "b" * 64]))
    tampered = _lock(("idna", "3.20", "", [H, "b" * 64, "0" * 64]))
    assert any("1 not published" in p for p in L.compare(tampered, fresh))


def test_compare_flags_a_trimmed_hash_list() -> None:
    fresh = _lock(("pyyaml", "6.0.3", "", [H, "b" * 64, "c" * 64]))
    trimmed = _lock(("pyyaml", "6.0.3", "", [H]))
    assert any("2 published but not locked" in p for p in L.compare(trimmed, fresh))


def test_compare_flags_added_and_removed_requirements() -> None:
    a = _lock(("six", "1.17.0", "", [H]))
    b = _lock(("six", "1.17.0", "", [H]), ("rich", "14.0.0", "", [H]))
    assert any("required but not locked: rich" in p for p in L.compare(a, b))
    assert any("no longer required" in p and "rich" in p for p in L.compare(b, a))
    assert L.compare(a, a) == []


def test_comments_and_the_constraints_annotation_do_not_count() -> None:
    a = _lock(("six", "1.17.0", "", [H]))
    b = a.replace("    # via -r requirements.txt",
                  "    # via\n    #   -c /tmp/x/constraints.txt\n    #   -r requirements.txt")
    assert L.compare(a, b) == []
    assert "-c /tmp" not in L._clean(b)


def test_check_takes_hashes_from_a_fresh_resolve_never_a_seed() -> None:
    src = (REPO / "installers" / "lib" / "lock_requirements.py").read_text(encoding="utf-8")
    fresh = src[src.index("def fresh_resolve"):src.index("def compare")]
    assert "seed" not in fresh and "constraints=cons" in fresh
    chk = src[src.index("def check()"):src.index("def relock")]
    assert "fresh_resolve(ls, committed" in chk and "compare(committed" in chk
    rel = src[src.index("def relock"):src.index("def main")]
    assert "new.write_text(fresh" in rel          # what is written is the fresh resolve


def test_uv_runs_without_the_callers_uv_settings(monkeypatch) -> None:
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://127.0.0.1:9/simple")
    monkeypatch.setenv("UV_INDEX", "evil=https://127.0.0.1:9/simple")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "2020-01-01")
    env = L._env({"MACOSX_DEPLOYMENT_TARGET": "14.0"})
    assert not [k for k in env if k.startswith("UV_")]
    assert env["MACOSX_DEPLOYMENT_TARGET"] == "14.0" and "PATH" in env


def test_audit_sets_hold_one_version_per_package_and_cover_every_pin(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "av==17.1.0 ; python_full_version < '3.11' \\\n    --hash=sha256:aa\n"
        "av==18.1.0 ; python_full_version == '3.11.*' \\\n    --hash=sha256:bb\n"
        "av==19.0.0 ; python_full_version >= '3.12' \\\n    --hash=sha256:cc\n"
        "six==1.17.0 \\\n    --hash=sha256:dd\n", encoding="utf-8")
    dropper = tmp_path / "requirements-dropper.lock"
    dropper.write_text("pyside6==6.9.0 \\\n    --hash=sha256:ee\nsix==1.17.0 \\\n    --hash=sha256:dd\n",
                       encoding="utf-8")
    monkeypatch.setattr(L, "LOCKSETS", [L.LockSet("a", "a", "3.10", []), L.LockSet("b", "b", "3.13", [])])
    L.LOCKSETS[0].lock, L.LOCKSETS[1].lock = lock, dropper
    files = L.audit_sets(tmp_path / "sets")
    contents = [f.read_text(encoding="utf-8").split() for f in files]
    assert contents == [["av==17.1.0", "pyside6==6.9.0", "six==1.17.0"], ["av==18.1.0"], ["av==19.0.0"]]


def test_the_real_locks_split_into_audit_sets_covering_every_pin(tmp_path) -> None:
    files = L.audit_sets(tmp_path)
    got = {ln for f in files for ln in f.read_text(encoding="utf-8").split()}
    assert got == {f"{n}=={v}" for t in (LOCK_TEXT, DROPPER_LOCK) for n, v in L.pins(t)}
    for f in files:
        names = [ln.split("==")[0] for ln in f.read_text(encoding="utf-8").split()]
        assert len(names) == len(set(names)), f


# ---- lock_extras: what the lock does not name ------------------------------

def test_lock_extras_lists_installed_packages_the_lock_does_not_name(monkeypatch) -> None:
    class D:
        def __init__(self, n): self.metadata = {"Name": n}
    monkeypatch.setattr(X.metadata, "distributions",
                        lambda: [D("PyYAML"), D("pip"), D("rich"), D("Pygments")])
    assert X.extras("pyyaml==6.0.3 \\\n    --hash=sha256:aa\n") == ["pygments", "rich"]


# ---- macOS: the one shared pip step ----------------------------------------

def _run_install_requirements(tmp_path: Path, *, with_lock: bool = True,
                              probe: str = "arm64 13 gil", extras: str = "",
                              lock_name: str | None = None):
    d = tmp_path / "Scripts"
    d.mkdir()
    (d / "requirements.txt").write_text("six\n", encoding="utf-8")
    if with_lock:
        (d / (lock_name or "requirements.lock")).write_text(
            "six==1.17.0 \\\n    --hash=sha256:00\n", encoding="utf-8")
    (d / "lock_extras.py").write_text("", encoding="utf-8")
    argv = tmp_path / "argv"
    fake = tmp_path / "python"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then echo "{probe}"; exit 0; fi\n'
        f'case "$1" in *lock_extras.py) printf "%s" "{extras}"; exit 0;; esac\n'
        f'printf "%s\\n" "$@" >> "{argv}"\n', encoding="utf-8")
    fake.chmod(0o755)
    call = 'source "$1"; install_requirements "$2" "$3"' + (f' "{lock_name}"' if lock_name else "")
    p = subprocess.run(
        ["bash", "-c", call, "_", str(REPO / "installers" / "lib" / "common.sh"),
         str(fake), str(d / "requirements.txt")],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    return p, argv


def test_macos_installs_only_the_lock_with_every_flag(tmp_path, allow_subprocess) -> None:
    p, argv = _run_install_requirements(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    args = argv.read_text(encoding="utf-8").split("\n")
    assert args[:2] == ["-m", "pip"] and "install" in args
    for flag in FLAGS:
        assert flag in args, flag
    assert args[args.index("-r") + 1].endswith("/Scripts/requirements.lock")
    assert "--upgrade" not in args            # no unpinned pip self-upgrade
    assert args.count("-m") == 1              # exactly one pip invocation


def test_macos_installs_a_named_lock(tmp_path, allow_subprocess) -> None:
    p, argv = _run_install_requirements(tmp_path, lock_name="requirements-dropper.lock")
    assert p.returncode == 0, p.stdout + p.stderr
    args = argv.read_text(encoding="utf-8").split("\n")
    assert args[args.index("-r") + 1].endswith("/Scripts/requirements-dropper.lock")


def test_macos_refuses_when_the_lock_is_missing(tmp_path, allow_subprocess) -> None:
    p, argv = _run_install_requirements(tmp_path, with_lock=False)
    assert p.returncode != 0
    assert "refusing to install unpinned requirements" in p.stdout + p.stderr
    assert not argv.exists()                  # pip never ran


@pytest.mark.parametrize("probe,marker", [
    ("x86_64 13 gil", "is not an Apple Silicon interpreter (x86_64)"),
    ("arm64 15 gil", "is Python 3.15; the pinned dependencies need 3.10-3.14"),
    ("arm64 9 gil", "is Python 3.9; the pinned dependencies need 3.10-3.14"),
    ("arm64 13 nogil", "is a free-threaded (no-GIL) Python"),
    ("", "is not an Apple Silicon interpreter (unknown)"),
])
def test_macos_refuses_an_interpreter_the_lock_cannot_serve(tmp_path, allow_subprocess,
                                                           probe, marker) -> None:
    p, argv = _run_install_requirements(tmp_path, probe=probe)
    assert p.returncode != 0
    assert marker in p.stdout + p.stderr
    assert not argv.exists()                  # pip never ran


def test_macos_names_packages_the_lock_does_not_cover(tmp_path, allow_subprocess) -> None:
    p, _ = _run_install_requirements(tmp_path, extras="rich\npygments\n")
    assert p.returncode == 0
    out = p.stdout + p.stderr
    assert "installed but not in requirements.lock (never hash-checked, not audited): rich pygments" in out


def test_macos_ceiling_matches_the_lock_targets() -> None:
    assert f"LOCK_PYTHON_MAX={L.PYTHON_CEILING.split('.')[1]}" in COMMON_SH
    assert f"LOCK_PYTHON_MIN={L.PYTHON_FLOOR.split('.')[1]}" in COMMON_SH
    top = max(v for _, vs in L.TARGETS for v in vs)
    assert top == L.PYTHON_CEILING


def test_macos_preflight_refuses_old_macos_and_intel_by_hardware() -> None:
    assert "macOS 14 (Sonoma) or later is required" in PREFLIGHT
    assert re.search(r'-lt 14 \]\]; then\n.*\n\s+exit 1', PREFLIGHT)
    assert re.search(r"\(Intel\) is not supported.*\n\s+exit 1", PREFLIGHT)
    assert "sysctl -n hw.optional.arm64" in PREFLIGHT    # not the shell's uname -m


def test_the_dropper_builds_from_its_own_lock_on_python_313() -> None:
    src = (COMPONENTS / "43-markitdown-dropper.sh").read_text(encoding="utf-8")
    assert 'install_requirements "$DROPPER_VENV/bin/python3"' in src
    assert "requirements-dropper.lock" in src
    assert "PY=/opt/homebrew/bin/python3.13" in src
    dropper = [ls for ls in L.LOCKSETS if ls.lock.name == "requirements-dropper.lock"][0]
    assert dropper.targets == [("aarch64-apple-darwin", ("3.13",))]


def _pip_lines(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)            # shell line continuations
    return [ln for ln in joined.splitlines()
            if re.search(r"\bpip3?(?:\.exe)?[\"']?\s+(?:-\S+\s+)*install\s|-m pip (?:-\S+\s+)*install"
                         r"|[\"']pip[\"'],\s*[\"']install|\buv pip install|\bensurepip\b", ln)
            and not ln.lstrip().startswith("#")]


def test_no_installer_or_script_pip_installs_outside_a_lock() -> None:
    """Every pip install anywhere an install or update reaches goes through
    a lock with --require-hashes; messages only point at the installer."""
    files = [REPO / "install.sh", REPO / "update.sh", *COMPONENTS.glob("*.sh"),
             *(REPO / "installers" / "lib").glob("*.sh"),
             *(SCRIPTS / "windows").glob("*.ps1"), *SCRIPTS.glob("*.sh"), *SCRIPTS.glob("*.py")]
    offenders = []
    for f in files:
        for ln in _pip_lines(f.read_text(encoding="utf-8")):
            if "--require-hashes" not in ln or not re.search(r"\.lock\b|\$lock\b", ln):
                offenders.append(f"{f.name}: {ln.strip()}")
    # the security-checks install hints name maintainer tools, not vault deps
    offenders = [o for o in offenders if "pipx install" not in o]
    assert offenders == []


# ---- Windows: the one shared pip step --------------------------------------

def _install_requirements_ps() -> str:
    start = COMMON_PS.index("function Install-Requirements")
    return COMMON_PS[start:COMMON_PS.index("\nfunction ", start + 1)]


def test_windows_installs_only_the_lock_with_every_flag() -> None:
    body = _install_requirements_ps()
    pip = [ln for ln in body.splitlines() if "-m pip" in ln]
    assert len(pip) == 1, pip                 # no separate pip self-upgrade
    for flag in FLAGS:
        assert flag in pip[0], flag
    assert "-r $lock" in pip[0]
    assert "Join-Path $ScriptsDir 'requirements.lock'" in body
    assert "refusing to install unpinned requirements" in body
    assert "lock_extras.py" in body and "never hash-checked, not audited" in body
    assert "requirements.txt" not in body.split("function Install-Requirements", 1)[1]


def test_windows_python_floor_and_ceiling() -> None:
    assert re.search(r"if \(Test-ArmWindows\) \{ return 12 \} else \{ return 10 \}", COMMON_PS)
    assert "PROCESSOR_ARCHITEW6432 -eq 'ARM64'" in COMMON_PS
    assert f"function Get-MaxPythonMinor {{ return {L.PYTHON_CEILING.split('.')[1]} }}" in COMMON_PS
    assert "$min -ge (Get-MinPythonMinor) -and $min -le (Get-MaxPythonMinor)" in INSTALL_PS
    body = _install_requirements_ps()
    assert "Get-MinPythonMinor" in body and "Get-MaxPythonMinor" in body   # existing venv on update
    arm = dict(L.TARGETS)["aarch64-pc-windows-msvc"]
    assert min(arm) == "3.12"


def test_windows_prefers_a_supported_python_and_reads_only_a_real_version() -> None:
    fn = INSTALL_PS[INSTALL_PS.index("function Resolve-BasePython"):]
    fn = fn[:fn.index("\n}\n")]
    order = re.findall(r"@\('py','(-3(?:\.\d+)?)'\)", fn)
    assert order[:3] == ["-3.13", "-3.12", "-3.14"] and order[-1] == "-3"
    # "Requested Python version (3.13) is not installed" must not parse as 3.13
    assert "$LASTEXITCODE -eq 0 -and $out -match '^\\s*Python (\\d+)\\.(\\d+)'" in fn


def test_windows_powershell_files_stay_ascii() -> None:
    for text in (COMMON_PS, INSTALL_PS):
        text.encode("ascii")                  # PS 5.1 reads BOM-less files as ANSI


# ---- the standing check ----------------------------------------------------

def test_the_sca_pass_audits_every_pin_and_checks_the_locks() -> None:
    sca = CHECKS[CHECKS.index("if want sca; then"):CHECKS.index("# SAST")]
    assert "lock_requirements.py --audit-sets" in sca
    assert "lock_requirements.py --check" in sca
    assert "--disable-pip" in sca and "requirements.txt --strict" not in sca
    assert 'skip_missing sca uv "pipx install uv"' in sca
    trigger = re.search(r"sca\)\s+grep -qE '([^']+)'", CHECKS).group(1)
    for path in ("Templates/Scripts/requirements.txt", "Templates/Scripts/requirements.lock",
                 "Templates/Scripts/requirements-dropper.txt",
                 "Templates/Scripts/requirements-dropper.lock",
                 "installers/lib/lock_requirements.py"):
        assert re.search(trigger, path), path


def test_targets_cover_every_supported_platform() -> None:
    platforms = dict(L.TARGETS)
    assert set(platforms) == {"aarch64-apple-darwin", "x86_64-pc-windows-msvc",
                              "aarch64-pc-windows-msvc"}
    assert min(platforms["aarch64-apple-darwin"]) == L.PYTHON_FLOOR == "3.10"
    assert L.MACOS_FLOOR == "14.0"


# ---- round 2 of review (2026-10-01) ----------------------------------------

@pytest.mark.parametrize("planted", [
    "rich==15.0.0 --hash sha256:" + "a" * 64,                 # same-line, space form
    " rich==15.0.0 --hash sha256:" + "a" * 64,                # leading space
    "    --hash=sha256:" + "A" * 64,                          # upper-case extra hash
    "-r http://127.0.0.1:8765/more.txt",                      # remote include
    "--extra-index-url https://example.invalid/simple",
])
def test_compare_rejects_any_line_pip_would_act_on_that_uv_did_not_write(planted: str) -> None:
    fresh = _lock(("idna", "3.20", "", [H]), ("six", "1.17.0", "", [H]))
    tampered = fresh.replace("    # via -r requirements.txt\nsix", planted + "\n    # via -r requirements.txt\nsix", 1)
    assert tampered != fresh
    assert any("lines pip would act on differ" in p for p in L.compare(tampered, fresh))


def test_compare_rejects_a_duplicate_entry() -> None:
    fresh = _lock(("idna", "3.20", "", [H]))
    dup = _lock(("idna", "3.20", "", ["0" * 64])) + fresh
    assert any("lines pip would act on differ" in p for p in L.compare(dup, fresh))


def test_check_fetches_nothing_more_for_a_lock_that_already_differs() -> None:
    src = (REPO / "installers" / "lib" / "lock_requirements.py").read_text(encoding="utf-8")
    chk = src[src.index("def check()"):src.index("def relock")]
    assert re.search(r"if not problems:\n\s+problems \+= \[f\"no wheel", chk)


def test_lock_extras_counts_an_entry_only_where_its_marker_holds(monkeypatch) -> None:
    class D:
        def __init__(self, n): self.metadata = {"Name": n}
    monkeypatch.setattr(X.metadata, "distributions", lambda: [D("tzdata"), D("six")])
    lock = ("six==1.17.0 \\\n    --hash=sha256:aa\n"
            "tzdata==2026.4 ; sys_platform == 'nonexistent-os' \\\n    --hash=sha256:bb\n")
    assert X.extras(lock) == ["tzdata"]


def test_update_refreshes_the_dropper_only_on_python_313_and_never_stops_on_it() -> None:
    upd = (REPO / "update.sh").read_text(encoding="utf-8")
    block = upd[upd.index("Markitdown Dropper app keeps its own venv"):upd.index("# ---- 3. scheduled jobs")]
    assert "!= \"13\"" in block and "not refreshed" in block
    assert re.search(r"elif install_requirements .*\n.*requirements-dropper\.lock; then", block)
    assert "else\n                warn" in block


def test_lock_extras_failures_do_not_end_an_install() -> None:
    assert re.search(r'lock_extras\.py" "\$lock" \| tr .*\|\| true', COMMON_SH)
    assert "Invoke-Native -Warn" in _install_requirements_ps()


# ---- Windows ARM laptop run (2026-10-01) ----------------------------------

def test_windows_candidate_prefix_stays_an_array() -> None:
    """A one-item array returned from a function comes back as a string, and
    splatting a string passes nothing: `py` printed its banner instead of a
    version, and every 'py -3.x' candidate was skipped (found on the ARM
    laptop). Every call site must wrap it."""
    calls = re.findall(r"[^\n]*Get-CandPrefix \$\w+[^\n]*", INSTALL_PS)
    assert calls
    for c in calls:
        assert "@(Get-CandPrefix" in c, c


def test_windows_clears_the_files_pip_set_aside_while_in_use() -> None:
    body = _install_requirements_ps()
    pip_at = body.index("-m pip install")
    assert body.index("Remove-PipLeftovers") < pip_at            # earlier runs' leftovers
    assert body.rindex("Remove-PipLeftovers") > pip_at           # this run's, if released
    fn = COMMON_PS[COMMON_PS.index("function Remove-PipLeftovers"):]
    fn = fn[:fn.index("\n}\n")]
    assert "-Directory -Filter '~*'" in fn and "'Lib\\site-packages'" in fn
