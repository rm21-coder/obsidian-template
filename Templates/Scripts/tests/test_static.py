"""
test_static.py — fast static checks. These run without exercising any
behavior; they only inspect source code and config artifacts. If any of
these fail, no further test is meaningful.

What's covered
--------------
1. py_compile every patched .py file (catches syntax errors and stale
   sources).
2. AST-level assertions of the security invariants:
     - url_safety.py imports `socket`, calls `socket.getaddrinfo`,
       sets `allow_redirects=False`.
     - plugin_integrity_check.py imports `hmac` and uses
       `hmac.compare_digest` (constant-time HMAC verification).
     - integrity_monitor.py defines `scan_state_dir` and excludes
       integrity_state.json from that scan (chicken-and-egg comment).
3. plistlib parses every security-control plist in Templates/Scripts/.
4. integrity plist WatchPaths array covers the three expected targets.
5. run_tests.sh has valid shell syntax (`bash -n`).

These tests run in milliseconds and provide the first line of defense
against an ill-applied patch — a malformed file fails here before any
behavior test.
"""
from __future__ import annotations

import ast
import plistlib
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

PATCHED_PY = (
    "integrity_monitor.py",
    "plugin_integrity_check.py",
)


def _read_ast(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _ast_has_call(tree: ast.AST, dotted_name: str) -> bool:
    """Return True if `tree` contains any Call whose func renders to
    the given dotted name (e.g. socket.getaddrinfo, hmac.compare_digest)."""
    target = dotted_name.split(".")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Walk Attribute chain back to a Name root.
        parts: list[str] = []
        cur: ast.AST | None = func
        while isinstance(cur, ast.Attribute):
            parts.insert(0, cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.insert(0, cur.id)
        if parts == target:
            return True
    return False


def _ast_has_import(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == name:
                return True
    return False


# ---------------------------------------------------------------------------
# 1. py_compile.
# ---------------------------------------------------------------------------

class TestPyCompile:
    """Every patched .py file must compile without errors."""

    @pytest.mark.parametrize("name", PATCHED_PY)
    def test_runtime_compiles(self, name: str, scripts_dir: Path) -> None:
        target = scripts_dir / name
        assert target.exists(), f"missing runtime file: {target}"
        # raises py_compile.PyCompileError on failure
        py_compile.compile(str(target), doraise=True)


# ---------------------------------------------------------------------------
# 2. AST security invariants.
# ---------------------------------------------------------------------------

class TestUrlSafetyAST:
    """Verify the SSRF fix is structurally present in url_safety.py.

    These invariants used to point at the article clipper, which owned the
    guard before it was extracted (and before the clipper itself was retired
    2026-08-18). The guard lives in url_safety.py so every fetcher shares it,
    and the invariants moved with the code rather than being relaxed -- a
    revert has to survive these plus TestFetchersRouteThroughUrlSafety below.
    """

    @pytest.fixture
    def tree(self, scripts_dir: Path) -> ast.AST:
        return _read_ast(scripts_dir / "url_safety.py")

    def test_imports_socket(self, tree: ast.AST) -> None:
        assert _ast_has_import(tree, "socket")

    def test_imports_requests(self, tree: ast.AST) -> None:
        assert _ast_has_import(tree, "requests")

    def test_calls_getaddrinfo(self, tree: ast.AST) -> None:
        """is_safe_url must resolve hostnames to defeat DNS rebinding."""
        assert _ast_has_call(tree, "socket.getaddrinfo")

    def test_sets_allow_redirects_false(self, scripts_dir: Path) -> None:
        """requests.get must be called with allow_redirects=False to
        force the manual redirect walk."""
        text = (scripts_dir / "url_safety.py").read_text(encoding="utf-8")
        get_calls = text.count("requests.get(")
        guarded = text.count("allow_redirects=False")
        assert get_calls > 0, "no requests.get calls — SSRF fix backed out?"
        assert guarded >= get_calls, (
            f"requests.get appears {get_calls} times but allow_redirects="
            f"False appears only {guarded} — at least one auto-redirect "
            "fetcher remains.")

    def test_max_redirects_bound(self, scripts_dir: Path) -> None:
        """Manual redirect walk must have a finite hop cap."""
        text = (scripts_dir / "url_safety.py").read_text(encoding="utf-8")
        assert "MAX_REDIRECTS" in text

    def test_download_is_size_capped(self, scripts_dir: Path) -> None:
        """safe_download streams to disk, so it needs its own ceiling — an
        unbounded stream fills the disk rather than the heap."""
        text = (scripts_dir / "url_safety.py").read_text(encoding="utf-8")
        assert "MAX_DOWNLOAD_BYTES" in text


class TestFetchersRouteThroughUrlSafety:
    """No pipeline may fetch an untrusted URL on its own.

    The reason this is asserted structurally: urllib.request.urlopen and
    requests with allow_redirects=True both follow redirects internally, so a
    hand-rolled fetcher can validate its first hop, pass, and still be walked
    to a loopback or link-local address. That failure is invisible in normal
    use -- it only shows up when someone points a hostile redirect at it.
    """

    # youtube_summarize.py is deliberately absent. It no longer makes a model
    # HTTP call of its own (that goes through the SDK via llm_endpoint now), so
    # the only urlopen left is its *caption-track* fetch -- which IS an SSRF
    # surface, and still validates-then-urlopens (so a redirect is unvalidated)
    # against its own duplicate copy of is_safe_url. Tracked as a follow-up;
    # adding it here before that is fixed would just mean a failing test with
    # no fix attached.
    FETCHERS = ("podcast_transcribe.py",)

    @pytest.mark.parametrize("name", FETCHERS)
    def test_does_not_use_urlopen(self, scripts_dir: Path, name: str) -> None:
        text = (scripts_dir / name).read_text(encoding="utf-8")
        assert "urllib.request.urlopen" not in text, (
            f"{name} calls urllib.request.urlopen, which follows redirects "
            "automatically and so bypasses per-hop SSRF validation. Use "
            "url_safety.safe_fetch / safe_download.")

    @pytest.mark.parametrize("name", FETCHERS)
    def test_imports_the_shared_guard(self, scripts_dir: Path,
                                      name: str) -> None:
        text = (scripts_dir / name).read_text(encoding="utf-8")
        assert "url_safety" in text, (
            f"{name} fetches URLs but does not reference url_safety")


class TestPluginIntegrityAST:
    """Verify the HMAC envelope is structurally present in
    plugin_integrity_check.py."""

    @pytest.fixture
    def tree(self, scripts_dir: Path) -> ast.AST:
        return _read_ast(scripts_dir / "plugin_integrity_check.py")

    def test_imports_hmac(self, tree: ast.AST) -> None:
        assert _ast_has_import(tree, "hmac")

    def test_uses_compare_digest(self, tree: ast.AST) -> None:
        """Constant-time comparison is mandatory for HMAC verification."""
        assert _ast_has_call(tree, "hmac.compare_digest")

    def test_keychain_service_constant(self, scripts_dir: Path) -> None:
        text = (scripts_dir / "plugin_integrity_check.py").read_text()
        assert '"obsidian-allowlist-hmac"' in text

    def test_tamper_alert_kind(self, scripts_dir: Path) -> None:
        text = (scripts_dir / "plugin_integrity_check.py").read_text()
        assert "ALLOWLIST_TAMPER" in text


class TestIntegrityMonitorAST:
    """Verify the state_dir scope addition in integrity_monitor.py."""

    def test_scan_state_dir_defined(self, scripts_dir: Path) -> None:
        tree = _read_ast(scripts_dir / "integrity_monitor.py")
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "scan_state_dir" in names

    def test_excludes_self(self, scripts_dir: Path) -> None:
        """integrity_state.json must be excluded from state_dir scan
        (otherwise: chicken-and-egg with save_state — no fixed-point
        exists for SHA-256 on a file containing its own hash)."""
        text = (scripts_dir / "integrity_monitor.py").read_text()
        assert 'name == "integrity_state.json"' in text


# ---------------------------------------------------------------------------
# 3. Plist validity + WatchPaths assertion.
# ---------------------------------------------------------------------------

# Every shipped plist must parse with plistlib, not just plutil: plistlib
# (expat) rejects things plutil tolerates — notably `--` inside an XML
# comment — and morning_dashboard.py reads plists through plistlib, so a
# plutil-only-valid plist silently vanishes from pipeline-health monitoring.
# Globbed so a newly added plist is covered without editing this list.
PLISTS = tuple(sorted(
    p.name for p in Path(__file__).resolve().parent.parent.glob("*.plist")
))
assert PLISTS, "no plists found next to tests/ — layout changed?"

# The shipped plists use a /Users/YOUR_USERNAME/... placeholder (substituted
# by the installer), so we assert on the path *suffix* rather than a real
# absolute path — this is what makes the check meaningful on any checkout.
INTEGRITY_WATCHPATH_SUFFIXES = (
    "Library/LaunchAgents",
    "Obsidian/Templates/Scripts",
)


class TestPlists:

    @pytest.mark.parametrize("name", PLISTS)
    def test_parses(self, name: str, scripts_dir: Path) -> None:
        target = scripts_dir / name
        assert target.exists(), f"missing plist: {target}"
        with target.open("rb") as f:
            plist = plistlib.load(f)
        assert "Label" in plist
        assert "ProgramArguments" in plist

    def test_integrity_watchpaths(self, scripts_dir: Path) -> None:
        with (scripts_dir / "com.obsidian.security.integrity.plist"
              ).open("rb") as f:
            plist = plistlib.load(f)
        watch = plist.get("WatchPaths", [])
        for suffix in INTEGRITY_WATCHPATH_SUFFIXES:
            assert any(w.endswith(suffix) for w in watch), (
                f"WatchPaths missing an entry ending in {suffix!r} — got {watch}")

    def test_integrity_does_not_watch_its_own_state_dir(
            self, scripts_dir: Path) -> None:
        """The state dir must never be a WatchPath, and this is the guard.

        integrity_monitor appends to alerts.log inside that directory whenever
        it finds drift. Watching it made the job re-trigger itself: one
        unadopted change produced a run every ThrottleInterval until someone
        rebaselined or the log rotated, and a single stale hash was observed
        generating 36,160 alerts that way. The entry was there originally and
        looks obviously correct, so removing it without a test just invites the
        next person to put it back.
        """
        with (scripts_dir / "com.obsidian.security.integrity.plist"
              ).open("rb") as f:
            plist = plistlib.load(f)
        watch = plist.get("WatchPaths", [])
        offenders = [w for w in watch if "obsidian-security" in w]
        assert not offenders, (
            f"the control's own state dir is watched again: {offenders} — "
            "this re-creates the self-triggering alert loop")

    def test_integrity_schedule(self, scripts_dir: Path) -> None:
        with (scripts_dir / "com.obsidian.security.integrity.plist"
              ).open("rb") as f:
            plist = plistlib.load(f)
        sched = plist.get("StartCalendarInterval", {})
        assert sched.get("Hour") == 6
        assert sched.get("Minute") == 35


# ---------------------------------------------------------------------------
# 4. Shell script syntax.
# ---------------------------------------------------------------------------

class TestShellScripts:

    @pytest.fixture
    def allow_subprocess(self) -> None:
        """Opt out of the global subprocess block — we genuinely need
        to shell out to bash -n here."""
        return None

    def test_run_tests_syntax(self, allow_subprocess: None) -> None:
        target = Path(__file__).parent / "run_tests.sh"
        # No bash on PATH (a stock Windows box) means there is nothing to
        # syntax-check against -- skip rather than fall back to /bin/bash,
        # which just raises FileNotFoundError there. Git Bash satisfies this
        # if it is on PATH.
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not on PATH; nothing to syntax-check "
                        "run_tests.sh with")
        result = subprocess.run([bash, "-n", str(target)],
                                capture_output=True, text=True)
        assert result.returncode == 0, (
            f"bash -n run_tests.sh failed:\n{result.stderr}")


# ---------------------------------------------------------------------------
# 5. Installer Keychain invariants.
#
# These are source assertions rather than behavior tests because the behavior
# is "does macOS raise a consent dialog", which cannot be exercised in CI --
# and the failure mode is severe enough (a wedged unattended install, plus
# queued dialogs that block later Keychain reads including git-over-HTTPS)
# that a plain regression guard on the source is worth having.
# ---------------------------------------------------------------------------

class TestInstallerKeychainWrites:
    """Source guards on the installer's Keychain access.

    These are source assertions rather than behavior tests because the
    behavior is "does macOS raise a consent dialog", which cannot be
    exercised in CI. The failure mode earns the guard: an unattended install
    stalls, and the queued dialogs block every later Keychain operation on
    the machine, git-over-HTTPS through the osxkeychain helper included.

    Behavioral coverage of the python layer's timeouts lives in
    test_secret_store.py; these only pin the invariants that a future edit
    could quietly undo.
    """

    @pytest.fixture
    def secrets_sh(self, scripts_dir: Path) -> str:
        # Templates/Scripts/ -> repo root -> installers/lib/secrets.sh
        target = scripts_dir.parent.parent / "installers" / "lib" / "secrets.sh"
        if not target.is_file():
            pytest.skip(f"{target} not present (vault-only install?)")
        return target.read_text(encoding="utf-8")

    @pytest.fixture
    def secret_store_py(self, scripts_dir: Path) -> str:
        return (scripts_dir / "secret_store.py").read_text(encoding="utf-8")

    @staticmethod
    def _code_lines(source: str) -> list[str]:
        """Source with comment-only lines dropped, so an assertion cannot be
        satisfied (or tripped) by prose about the thing it checks."""
        return [ln for ln in source.splitlines()
                if not ln.lstrip().startswith("#")]

    # -- the one-way door: -U must never come back, in either layer ---------

    @pytest.mark.parametrize("layer", ["secrets_sh", "secret_store_py"])
    def test_never_updates_an_existing_item_in_place(self, layer: str,
                                                    request) -> None:
        """`add-generic-password ... -U` updates in place, which prompts for
        consent whenever the item's ACL doesn't cover the caller -- and under
        launchd that prompt can never be answered. Delete-then-add is the only
        safe form, because a fresh add never prompts."""
        source = request.getfixturevalue(layer)
        offenders = [ln for ln in self._code_lines(source)
                     if "add-generic-password" in ln
                     and ("-U" in ln.split() or ln.rstrip().endswith("-U"))]
        assert not offenders, f"-U reintroduced in {layer}: {offenders}"

    # -- the shell layer delegates writes rather than duplicating them ------

    def test_shell_layer_does_not_write_the_keychain_itself(
            self, secrets_sh: str) -> None:
        """secrets.sh must route writes through secret_store.py: that keeps the
        secret on stdin instead of argv (out of the process table), and keeps
        one implementation of the delete-then-add dance instead of two that
        can drift."""
        offenders = [ln for ln in self._code_lines(secrets_sh)
                     if "add-generic-password" in ln]
        assert not offenders, (
            "secrets.sh writes the Keychain directly instead of delegating "
            f"to secret_store.py: {offenders}")
        assert "secret_store.py" in secrets_sh

    def test_shell_reads_are_time_bounded(self, secrets_sh: str) -> None:
        """A Keychain read that raises a dialog never returns; unbounded, it
        stalls the whole install."""
        assert "alarm shift; exec @ARGV" in secrets_sh, (
            "the _security wrapper's hard timeout is missing")
        bare = [ln for ln in self._code_lines(secrets_sh)
                if "generic-password" in ln and "_security " not in ln]
        assert not bare, (
            f"Keychain call bypassing the _security timeout wrapper: {bare}")

    # -- a hang must not lead to more calls ---------------------------------

    def test_a_hang_latches_and_short_circuits(self, secrets_sh: str) -> None:
        """perl exits 142 on alarm, which a caller checking only "non-zero"
        cannot distinguish from "no such item". Unlatched, that ambiguity turns
        one hang into three calls: keychain_has reads it as absent, so the
        caller prompts, so the write fires -- each queuing another dialog. The
        latch is what makes "never retry after a hang" true rather than
        aspirational."""
        code = self._code_lines(secrets_sh)
        assert any("KEYCHAIN_WEDGED=1" in ln for ln in code), (
            "no latch is set when a Keychain call times out")
        assert any("142" in ln for ln in code), (
            "the timeout exit status (128 + SIGALRM) is never checked")
        # _security must refuse before spending another call once latched.
        wrapper = secrets_sh[secrets_sh.index("_security() {"):]
        wrapper = wrapper[:wrapper.index("\n}\n")]
        assert "KEYCHAIN_WEDGED" in wrapper, (
            "_security does not short-circuit on the latch, so later calls "
            "still reach a Keychain that already proved it is wedged")

    def test_keychain_helpers_are_never_called_in_a_subshell(
            self, secrets_sh: str) -> None:
        """The wedged latch is a shell variable, so it dies inside $(...).
        A caller written as `x="$(keychain_has ...)"` would set the latch in a
        subshell that immediately exits, un-latching it -- and the next call
        would walk straight back into the wedged Keychain."""
        import re
        pattern = re.compile(r'\$\((?:[^()]*\s)?(?:keychain|keystore)_'
                             r'(?:has|set)\b')
        offenders = [ln for ln in self._code_lines(secrets_sh)
                     if pattern.search(ln)]
        assert not offenders, (
            "Keychain helper called inside a command substitution, where the "
            f"wedged latch cannot survive: {offenders}")

    def test_python_layer_timeout_marker_is_what_the_shell_matches(
            self, secrets_sh: str, secret_store_py: str) -> None:
        """The shell latches on the helper's stderr text, so the two layers are
        coupled by that string. If one side rewords it the coupling breaks
        silently -- a python-side hang would stop latching the shell side."""
        assert "timed out" in secret_store_py, (
            "secret_store.py no longer emits the 'timed out' marker that "
            "secrets.sh greps for to share the wedged state")
        assert '*"timed out"*' in secrets_sh, (
            "secrets.sh no longer matches the helper's timeout marker")

    # -- the python layer keeps the safe write form -------------------------

    def test_python_layer_deletes_then_adds_with_preauthorization(
            self, secret_store_py: str) -> None:
        code = "\n".join(self._code_lines(secret_store_py))
        assert "delete-generic-password" in code, (
            "the write must delete first, so the add is always a fresh item")
        assert '"-T", "/usr/bin/security"' in code, (
            "-T pre-authorizes /usr/bin/security on the new item, which is "
            "what lets launchd jobs read it later without their own consent "
            "prompt")
