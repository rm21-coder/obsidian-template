"""
test_voice_cleanup.py — the always-on dictation watcher's fault tolerance.

Every test here is about one thing: this process is the always-on job
(KeepAlive in com.voice-cleanup.plist), so exiting is never the right answer
to a recoverable fault. launchd respawns it immediately, and its 10s minimum
runtime turns that into a spin that logs forever and watches nothing.

That is not hypothetical. The watcher used to resolve its API client *before*
entering the loop and sys.exit(0) when an institutional gateway's hostname did
not resolve. A laptop off the VPN is a normal, recurring state, so the job
spent days in exactly that spin: 7,152 respawns and a 19MB error log with one
warning repeated every ten seconds. An earlier era in the same log had the
same shape for a different reason -- a retired model 404ing out of
process_file, uncaught, ~6,800 more respawns -- because a raw file is only
unlinked on success, so one permanently-failing drop retried forever and
blocked every drop queued behind it.

So the invariants under test are: an unreachable gateway is a skipped cycle,
not an exit; the outage is logged on transition rather than once per cycle;
recovery resumes on its own; a failing file cannot kill the loop or wedge the
queue, and is never deleted. --once keeps its old exit-0 "skipped, not failed"
behaviour, because a one-shot run has no later cycle to recover into.

The behavioural tests drive a fake `llm_endpoint` module rather than patching
a helper inside voice_cleanup, so they describe behaviour instead of
implementation -- main() does the import itself, and both the pre-fix and
post-fix code reach it. Run against the version that resolved its client
before the loop, the watch-mode tests fail by exiting, which is the bug.

The SDK is faked and load_dotenv is stubbed for the duration of the import --
voice_cleanup imports anthropic at module scope and calls
load_dotenv(~/dev/secrets/.env) at import time, so an unguarded import would
need the SDK installed and would pull the developer's real secrets into
os.environ, letting a stray LLM_BASE_URL decide how these tests behave.
"""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

import llm_endpoint

# ---------------------------------------------------------------------------
# Guarded import of the module under test.
#
# The dotenv stub is installed only for the import and then removed, so this
# does not leak a no-op load_dotenv into the rest of the pytest session --
# other suites import modules that call it for real.
# ---------------------------------------------------------------------------

_fake_sdk = types.ModuleType("anthropic")
_fake_sdk.Anthropic = object
sys.modules.setdefault("anthropic", _fake_sdk)

_real_dotenv = sys.modules.get("dotenv")
_dotenv_stub = types.ModuleType("dotenv")
_dotenv_stub.load_dotenv = lambda *a, **kw: False
sys.modules["dotenv"] = _dotenv_stub
try:
    import voice_cleanup
finally:
    if _real_dotenv is None:
        del sys.modules["dotenv"]
    else:
        sys.modules["dotenv"] = _real_dotenv


GATEWAY = "api.ai.example.edu"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the watcher at tmp dirs.

    load_config() is replaced wholesale rather than patched field by field:
    its default watch folder resolves through source_media to a real
    ~/SourceMedia/VoiceInput, and main() mkdirs whatever it is handed -- so
    the unpatched default would create directories on the developer's machine.
    """
    inbox = tmp_path / "VoiceInput"
    vault = tmp_path / "vault"
    inbox.mkdir()
    vault.mkdir()
    cfg = {
        "claude_model": "test-model",
        "vault_path": str(vault),
        "watch_folder": str(inbox),
    }
    monkeypatch.setattr(voice_cleanup, "load_config", lambda: cfg)
    return cfg


@pytest.fixture
def inbox(isolated_config) -> Path:
    return Path(isolated_config["watch_folder"])


@pytest.fixture
def at_info(caplog):
    caplog.set_level(logging.INFO, logger="voice_cleanup")
    return caplog


class FakeEndpoint:
    """A stand-in llm_endpoint whose client() the test controls.

    Installed into sys.modules, which is what both the pre-fix and post-fix
    code paths import -- so a test written against this runs unchanged on
    either, and fails on the version with the bug.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None
        self.fail_first = 0
        # The real classes, so the code under test catches what we raise.
        self.EndpointError = llm_endpoint.EndpointError
        self.GatewayUnreachable = llm_endpoint.GatewayUnreachable

    def client(self, **kwargs):
        self.calls += 1
        if self.error is not None and (
            self.fail_first == 0 or self.calls <= self.fail_first
        ):
            raise self.error
        return object()

    def describe(self) -> str:
        return f"TEST_KEY via https://{GATEWAY}"

    # -- configuration helpers, so tests read as intent -------------------

    def off_vpn(self) -> "FakeEndpoint":
        self.error = llm_endpoint.GatewayUnreachable(GATEWAY)
        return self

    def off_vpn_for(self, cycles: int) -> "FakeEndpoint":
        self.off_vpn()
        self.fail_first = cycles
        return self

    def broken_credential(self) -> "FakeEndpoint":
        self.error = llm_endpoint.EndpointError("TEST_KEY resolved nowhere")
        return self


@pytest.fixture
def endpoint(monkeypatch) -> FakeEndpoint:
    fake = FakeEndpoint()
    module = types.ModuleType("llm_endpoint")
    module.client = fake.client
    module.describe = fake.describe
    module.EndpointError = fake.EndpointError
    module.GatewayUnreachable = fake.GatewayUnreachable
    monkeypatch.setitem(sys.modules, "llm_endpoint", module)
    return fake


def run_watch(monkeypatch, *, cycles: int):
    """Run main() in watch mode for `cycles` sleeps, then stop it.

    The loop is `while True` by design, so the test ends it the way an
    operator does -- time.sleep raises KeyboardInterrupt, which main()
    already catches and returns on. Anything else escaping main() means the
    always-on process died, which is the failure this module is about.
    """
    seen = {"n": 0}

    def fake_sleep(_seconds):
        seen["n"] += 1
        if seen["n"] >= cycles:
            raise KeyboardInterrupt

    monkeypatch.setattr(voice_cleanup.time, "sleep", fake_sleep)
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py"])
    try:
        voice_cleanup.main()
    except SystemExit as exc:
        pytest.fail(
            f"the watcher exited with code {exc.code} instead of staying in "
            f"its loop (managed {seen['n']} of {cycles} cycles); under "
            "KeepAlive that exit is the crash loop this module prevents"
        )
    except Exception as exc:
        pytest.fail(
            f"{type(exc).__name__} escaped main() and killed the always-on "
            f"process after {seen['n']} of {cycles} cycles: {exc}"
        )
    return seen["n"]


def messages(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def explode(message: str):
    """A process_file that always raises `message`."""
    def fail(*a, **kw):
        raise RuntimeError(message)
    return fail


# ---------------------------------------------------------------------------
# An unreachable gateway is a skipped cycle, not an exit.
# ---------------------------------------------------------------------------

def test_watch_mode_rides_out_an_unreachable_gateway(endpoint, monkeypatch,
                                                     at_info):
    """The crash-loop regression, stated directly."""
    endpoint.off_vpn()

    assert run_watch(monkeypatch, cycles=5) == 5


def test_outage_is_logged_on_transition_not_once_per_cycle(endpoint,
                                                           monkeypatch,
                                                           at_info):
    """One line for the outage, however long it lasts.

    The condition persists for as long as the VPN is down. A line per cycle
    is what grew this job's error log to 19MB.
    """
    endpoint.off_vpn()

    run_watch(monkeypatch, cycles=6)

    assert len(messages(at_info, "Paused")) == 1


def test_the_outage_line_names_the_host_and_the_fix(endpoint, monkeypatch,
                                                    at_info):
    endpoint.off_vpn()

    run_watch(monkeypatch, cycles=2)

    (line,) = messages(at_info, "Paused")
    assert GATEWAY in line
    assert "VPN" in line


def test_nothing_is_processed_while_the_endpoint_is_down(endpoint, monkeypatch,
                                                         at_info, inbox):
    """A drop that arrives during an outage waits; it is not consumed."""
    endpoint.off_vpn()
    drop = inbox / "note.txt"
    drop.write_text("raw dictation", encoding="utf-8")
    monkeypatch.setattr(voice_cleanup, "process_file",
                        lambda *a, **kw: pytest.fail("processed while paused"))

    run_watch(monkeypatch, cycles=4)

    assert drop.read_text(encoding="utf-8") == "raw dictation"


def test_a_broken_credential_also_pauses_rather_than_exiting(endpoint,
                                                             monkeypatch,
                                                             at_info):
    """GatewayUnreachable is not the only EndpointError worth surviving.

    A resident job that exits on a bad credential is the same crash loop with
    a different first line; one clear log line and a wait is the useful
    behaviour.
    """
    endpoint.broken_credential()

    assert run_watch(monkeypatch, cycles=3) == 3
    assert len(messages(at_info, "Paused")) == 1


# ---------------------------------------------------------------------------
# Recovery.
# ---------------------------------------------------------------------------

def test_watcher_resumes_when_the_endpoint_comes_back(endpoint, monkeypatch,
                                                      at_info, inbox):
    """The VPN reconnects and the watcher picks up without being restarted."""
    endpoint.off_vpn_for(2)
    processed: list[str] = []
    monkeypatch.setattr(voice_cleanup, "process_file",
                        lambda f, client, cfg: processed.append(f.name))
    (inbox / "note.txt").write_text("raw", encoding="utf-8")

    run_watch(monkeypatch, cycles=4)

    assert len(messages(at_info, "Paused")) == 1
    assert len(messages(at_info, "resuming")) == 1
    assert "note.txt" in processed


def test_a_healthy_watcher_resolves_the_endpoint_once(endpoint, monkeypatch,
                                                      at_info):
    """Cached across cycles, so a working watcher is not re-resolving."""
    run_watch(monkeypatch, cycles=5)

    assert endpoint.calls == 1


# ---------------------------------------------------------------------------
# A failing file must not kill the loop or wedge the queue.
# ---------------------------------------------------------------------------

def test_a_failing_file_does_not_kill_the_watcher(endpoint, monkeypatch,
                                                  at_info, inbox):
    """process_file used to raise straight out of main().

    A retired model 404ing mid-run took the whole process down with it, and
    launchd respawned it into the same failure.
    """
    monkeypatch.setattr(voice_cleanup, "process_file",
                        explode("404 model not found"))
    (inbox / "note.txt").write_text("raw", encoding="utf-8")

    assert run_watch(monkeypatch, cycles=2) == 2


def test_a_transient_failure_does_not_quarantine_immediately(endpoint,
                                                             monkeypatch,
                                                             at_info, inbox):
    """A rate limit or a blip must not cost the user their dictation."""
    monkeypatch.setattr(voice_cleanup, "process_file",
                        explode("503 upstream"))
    drop = inbox / "note.txt"
    drop.write_text("raw", encoding="utf-8")

    # One attempt short of the quarantine threshold.
    run_watch(monkeypatch, cycles=voice_cleanup.MAX_FILE_ATTEMPTS - 1)

    assert drop.exists()
    assert not list(inbox.glob("*.failed"))


def test_a_permanent_failure_is_quarantined_and_kept(endpoint, monkeypatch,
                                                     at_info, inbox):
    """Set aside so it stops blocking, kept so nothing dictated is lost."""
    monkeypatch.setattr(voice_cleanup, "process_file",
                        explode("404 model not found"))
    drop = inbox / "note.txt"
    drop.write_text("irreplaceable dictation", encoding="utf-8")

    run_watch(monkeypatch, cycles=voice_cleanup.MAX_FILE_ATTEMPTS + 1)

    assert not drop.exists()
    assert (inbox / "note.txt.failed").read_text(
        encoding="utf-8") == "irreplaceable dictation"


def test_a_bad_file_does_not_block_the_drops_behind_it(endpoint, monkeypatch,
                                                       at_info, inbox):
    """The queue keeps moving once the poison drop is set aside."""
    processed: list[str] = []

    def only_the_bad_one_fails(f, client, cfg):
        if f.name == "bad.txt":
            raise RuntimeError("404 model not found")
        processed.append(f.name)
        f.unlink()

    monkeypatch.setattr(voice_cleanup, "process_file", only_the_bad_one_fails)
    (inbox / "bad.txt").write_text("poison", encoding="utf-8")
    (inbox / "good.txt").write_text("fine", encoding="utf-8")

    run_watch(monkeypatch, cycles=voice_cleanup.MAX_FILE_ATTEMPTS + 1)

    assert "good.txt" in processed
    assert (inbox / "bad.txt.failed").exists()


def test_quarantine_stops_the_retries(endpoint, monkeypatch, at_info, inbox):
    """Once set aside, the file stops costing an API call every cycle."""
    attempts = {"n": 0}

    def always_fails(*a, **kw):
        attempts["n"] += 1
        raise RuntimeError("404 model not found")

    monkeypatch.setattr(voice_cleanup, "process_file", always_fails)
    (inbox / "note.txt").write_text("raw", encoding="utf-8")

    run_watch(monkeypatch, cycles=voice_cleanup.MAX_FILE_ATTEMPTS + 5)

    assert attempts["n"] == voice_cleanup.MAX_FILE_ATTEMPTS


# ---------------------------------------------------------------------------
# --once keeps its old contract.
# ---------------------------------------------------------------------------

def test_once_mode_skips_cleanly_when_the_gateway_is_down(endpoint, monkeypatch,
                                                          inbox, at_info):
    """Exit 0: skipped, not failed.

    A one-shot run has no later cycle to recover into, and a scheduled job
    that reports failure every time the user is off the VPN trains people to
    ignore the dashboard.
    """
    endpoint.off_vpn()
    (inbox / "note.txt").write_text("raw", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py", "--once"])

    with pytest.raises(SystemExit) as exc:
        voice_cleanup.main()

    assert exc.value.code == 0


def test_once_mode_reports_a_broken_credential_as_failure(endpoint, monkeypatch,
                                                          inbox, at_info):
    """A credential that resolves nowhere is a failure worth a non-zero exit."""
    endpoint.broken_credential()
    (inbox / "note.txt").write_text("raw", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py", "--once"])

    with pytest.raises(SystemExit) as exc:
        voice_cleanup.main()

    assert exc.value.code == 1


def test_once_mode_does_not_resolve_an_endpoint_it_does_not_need(endpoint,
                                                                 monkeypatch,
                                                                 at_info):
    """An empty inbox should not touch the gateway, or the keystore.

    This is what keeps the scheduled runs free when there is nothing to do,
    and why an empty-inbox run is silent off the VPN.
    """
    endpoint.off_vpn()
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py", "--once"])

    voice_cleanup.main()

    assert endpoint.calls == 0


def test_once_mode_processes_pending_files(endpoint, monkeypatch, inbox,
                                           at_info):
    """The happy path still works, so the tests above cannot pass vacuously."""
    processed: list[str] = []
    monkeypatch.setattr(voice_cleanup, "process_file",
                        lambda f, client, cfg: processed.append(f.name))
    (inbox / "note.txt").write_text("raw", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py", "--once"])

    voice_cleanup.main()

    assert processed == ["note.txt"]


# ---------------------------------------------------------------------------
# --once handles each file on its own (Windows schedules voice-cleanup as
# --once every five minutes, so this is that platform's only mode).
# ---------------------------------------------------------------------------

def run_once(monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["voice_cleanup.py", "--once"])
    try:
        voice_cleanup.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_once_mode_a_bad_file_does_not_stop_the_drops_behind_it(
        endpoint, monkeypatch, at_info, inbox):
    """process_file used to raise straight out of the --once loop, so every
    drop sorted after a poison one waited on it, run after run."""
    processed: list[str] = []

    def only_the_bad_one_fails(f, client, cfg):
        if f.name == "a-bad.txt":
            raise RuntimeError("404 model not found")
        processed.append(f.name)
        f.unlink()

    monkeypatch.setattr(voice_cleanup, "process_file", only_the_bad_one_fails)
    (inbox / "a-bad.txt").write_text("poison", encoding="utf-8")
    (inbox / "b-good.txt").write_text("fine", encoding="utf-8")

    assert run_once(monkeypatch) == 1, "a failed file is still a failed run"
    assert processed == ["b-good.txt"]
    assert messages(at_info, "Failed (1/3): a-bad.txt")
    assert messages(at_info, "processed 1 file(s), 1 failed")


def test_once_mode_quarantines_after_max_attempts_across_runs(
        endpoint, monkeypatch, at_info, inbox):
    """Each --once run is a new process, so the count must outlive it."""
    monkeypatch.setattr(voice_cleanup, "process_file",
                        explode("404 model not found"))
    drop = inbox / "note.txt"
    drop.write_text("irreplaceable dictation", encoding="utf-8")

    for _ in range(voice_cleanup.MAX_FILE_ATTEMPTS - 1):
        assert run_once(monkeypatch) == 1
        assert drop.exists()
    assert run_once(monkeypatch) == 1
    assert not drop.exists()
    assert (inbox / "note.txt.failed").read_text(
        encoding="utf-8") == "irreplaceable dictation"
    assert messages(at_info, "Quarantined after 3 attempts: note.txt.failed")
    assert not (inbox / voice_cleanup.ATTEMPTS_FILE).exists(), (
        "the count should be cleared once nothing is failing")


def test_once_mode_success_clears_a_prior_failure_count(
        endpoint, monkeypatch, at_info, inbox):
    (inbox / "note.txt").write_text("raw", encoding="utf-8")
    monkeypatch.setattr(voice_cleanup, "process_file", explode("503 upstream"))
    assert run_once(monkeypatch) == 1

    monkeypatch.setattr(voice_cleanup, "process_file",
                        lambda f, client, cfg: f.unlink())
    assert run_once(monkeypatch) == 0
    assert not (inbox / voice_cleanup.ATTEMPTS_FILE).exists()


def test_quarantine_never_overwrites_an_earlier_failed_drop(
        endpoint, monkeypatch, at_info, inbox):
    """rename() clobbers on POSIX: a second bad drop with the same name
    used to destroy the first dictation set aside."""
    (inbox / "note.txt.failed").write_text("first dictation", encoding="utf-8")
    (inbox / "note.txt").write_text("second dictation", encoding="utf-8")
    monkeypatch.setattr(voice_cleanup, "process_file",
                        explode("404 model not found"))

    for _ in range(voice_cleanup.MAX_FILE_ATTEMPTS):
        run_once(monkeypatch)

    assert (inbox / "note.txt.failed").read_text(encoding="utf-8") == "first dictation"
    assert (inbox / "note.txt.2.failed").read_text(encoding="utf-8") == "second dictation"
    assert messages(at_info, "Quarantined after 3 attempts: note.txt.2.failed")


def test_a_failing_set_aside_neither_escapes_nor_loses_the_count(
        endpoint, monkeypatch, at_info, inbox):
    """On Windows rename raises when the target exists. Raised inside the
    except block, it escaped before the counts were saved, so every --once
    run retried the file from zero and failed the same way, forever."""
    calls = {"n": 0}

    def always_fails(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("404 model not found")

    real_rename = Path.rename

    def refuse(self, target):
        raise FileExistsError(183, "Cannot create a file when that file already exists")

    monkeypatch.setattr(voice_cleanup, "process_file", always_fails)
    monkeypatch.setattr(Path, "rename", refuse)
    drop = inbox / "note.txt"
    drop.write_text("raw", encoding="utf-8")

    for _ in range(voice_cleanup.MAX_FILE_ATTEMPTS + 1):
        assert run_once(monkeypatch) == 1
    assert drop.exists()
    assert messages(at_info, "Could not set aside note.txt after 3 attempts; will retry")
    assert calls["n"] == voice_cleanup.MAX_FILE_ATTEMPTS, (
        "a file due to be set aside should not cost another API call")

    monkeypatch.setattr(Path, "rename", real_rename)
    run_once(monkeypatch)
    assert not drop.exists()
    assert (inbox / "note.txt.failed").read_text(encoding="utf-8") == "raw"


def test_counts_for_files_no_longer_in_the_inbox_are_pruned(
        endpoint, monkeypatch, at_info, inbox):
    import json
    (inbox / voice_cleanup.ATTEMPTS_FILE).write_text(
        json.dumps({"gone.txt": 2}), encoding="utf-8")
    (inbox / "note.txt").write_text("raw", encoding="utf-8")
    monkeypatch.setattr(voice_cleanup, "process_file", explode("503 upstream"))

    run_once(monkeypatch)

    assert json.loads((inbox / voice_cleanup.ATTEMPTS_FILE).read_text(
        encoding="utf-8")) == {"note.txt": 1}
