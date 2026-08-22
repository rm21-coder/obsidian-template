"""secret_store: resolution order, keystore backends, CLI.

The autouse block_unmocked_subprocess fixture guarantees none of these
tests can touch a real Keychain — every subprocess.run is monkeypatched.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

import secret_store
import security_common


@pytest.fixture
def linux_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Force the plain-file backend into a temp dir (fake non-mac platform)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(security_common, "state_dir", lambda: tmp_path)
    return tmp_path


class FakeSecurity:
    """Records /usr/bin/security invocations and scripts their results."""

    def __init__(self):
        self.calls = []
        self.store = {}

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ["/usr/bin/security", "find-generic-password"]:
            service = args[args.index("-s") + 1]
            if service in self.store:
                return subprocess.CompletedProcess(
                    args, 0, stdout=self.store[service] + "\n", stderr="")
            return subprocess.CompletedProcess(args, 44, stdout="", stderr="")
        if args[:2] == ["/usr/bin/security", "add-generic-password"]:
            svc = args[args.index("-s") + 1]
            self.store[svc] = args[args.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["/usr/bin/security", "delete-generic-password"]:
            service = args[args.index("-s") + 1]
            rc = 0 if self.store.pop(service, None) is not None else 44
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="")
        raise AssertionError(f"unexpected security call: {args}")


@pytest.fixture
def fake_security(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeSecurity()
    monkeypatch.setattr(secret_store.subprocess, "run", fake.run)
    return fake


# ---------- resolution order -------------------------------------------------

def test_env_wins(monkeypatch, fake_security):
    fake_security.store["some_key"] = "keychain-value"
    monkeypatch.setenv("SOME_KEY", "env-value")
    assert secret_store.get_secret("SOME_KEY") == "env-value"


def test_blank_env_falls_through_to_keystore(monkeypatch, fake_security):
    fake_security.store["some_key"] = "keychain-value"
    monkeypatch.setenv("SOME_KEY", "   ")
    assert secret_store.get_secret("SOME_KEY") == "keychain-value"


def test_use_env_false_skips_environment(monkeypatch, fake_security):
    fake_security.store["some_key"] = "keychain-value"
    monkeypatch.setenv("SOME_KEY", "env-value")
    assert secret_store.get_secret("SOME_KEY", use_env=False) == "keychain-value"


def test_missing_everywhere_is_none(monkeypatch, fake_security):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert secret_store.get_secret("NOPE_KEY") is None


# ---------- macOS keychain backend --------------------------------------------

def test_keychain_roundtrip(monkeypatch, fake_security):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert secret_store.set_secret("MY_TOKEN", "s3cret")
    assert secret_store.get_secret("MY_TOKEN") == "s3cret"
    assert fake_security.store["my_token"] == "s3cret"  # service is lowercased


def test_keychain_set_uses_direct_argv_not_interactive_mode(
        monkeypatch, fake_security):
    """Pins two deliberate choices (both verified live 2026-08-18): argv
    over `security -i` (interactive mode hangs on realistic key lengths),
    and delete-then-add over `-U` (updating an existing item can raise a
    GUI consent prompt, which under launchd wedges silently). If someone
    'improves' either back, this test is the tripwire."""
    secret_store.set_secret("MY_TOKEN", "hunter2")
    ops = [args[1] for args, _ in fake_security.calls]
    assert ops == ["delete-generic-password", "add-generic-password"]
    add = fake_security.calls[-1][0]
    assert "hunter2" in add          # value travels as argv, by design
    assert "-i" not in add
    assert "-U" not in add


def test_keychain_set_preauthorizes_security_binary(monkeypatch, fake_security):
    secret_store.set_secret("MY_TOKEN", "v")
    add = next(args for args, _ in fake_security.calls
               if args[1] == "add-generic-password")
    t_target = add[add.index("-T") + 1]
    assert t_target == "/usr/bin/security"


def test_keychain_value_with_quotes_survives(monkeypatch, fake_security):
    tricky = 'pa"ss\\word'
    assert secret_store.set_secret("Q_KEY", tricky)
    assert secret_store.get_secret("Q_KEY") == tricky


def test_keychain_delete(monkeypatch, fake_security):
    secret_store.set_secret("GONE_KEY", "v")
    assert secret_store.delete_secret("GONE_KEY")
    assert secret_store.get_secret("GONE_KEY") is None


# ---------- hard timeout on every security call --------------------------------
#
# A Keychain call that raises a consent dialog never returns: under launchd
# (or `install.sh --auto`) there is nobody to answer it. Worse, retrying queues
# a SECOND dialog, and a stack of them blocks every later Keychain operation on
# the machine — git-over-HTTPS through the osxkeychain helper included. So each
# call is bounded, a timeout is reported as a failure, and nothing retries.

class HangingSecurity:
    """FakeSecurity that hangs on one verb, recording every attempt — so a
    test can prove no retry followed the hang."""

    def __init__(self, inner: FakeSecurity, hang_verb: str):
        self.inner = inner
        self.hang_verb = hang_verb
        self.attempts: list[str] = []

    def run(self, args, **kwargs):
        self.attempts.append(args[1])
        if args[1] == self.hang_verb:
            raise subprocess.TimeoutExpired(cmd=args,
                                            timeout=kwargs.get("timeout"))
        return self.inner.run(args, **kwargs)


@pytest.fixture
def hanging_security(monkeypatch: pytest.MonkeyPatch, fake_security: FakeSecurity):
    def _install(hang_verb: str) -> HangingSecurity:
        hung = HangingSecurity(fake_security, hang_verb)
        monkeypatch.setattr(secret_store.subprocess, "run", hung.run)
        return hung
    return _install


def _timeout_marker(verb: str) -> str:
    return f"{verb} timed out after {secret_store.KEYCHAIN_TIMEOUT_SECONDS}s"


def test_every_security_call_carries_a_hard_timeout(monkeypatch, fake_security):
    """No unbounded /usr/bin/security call may exist on any code path."""
    monkeypatch.delenv("MY_TOKEN", raising=False)
    secret_store.set_secret("MY_TOKEN", "v")
    secret_store.get_secret("MY_TOKEN", use_env=False)
    secret_store.delete_secret("MY_TOKEN")
    assert fake_security.calls, "no security calls recorded — fixture wiring?"
    for args, kwargs in fake_security.calls:
        assert kwargs.get("timeout") == secret_store.KEYCHAIN_TIMEOUT_SECONDS, (
            f"security {args[1]} ran without the hard timeout; a consent "
            "dialog on this call would hang the caller forever")


def test_set_aborts_on_delete_timeout_without_issuing_the_add(
        hanging_security, capsys):
    """The delete hanging means the Keychain is already wedged. Issuing the
    add anyway would queue a second dialog, so the write stops there."""
    hung = hanging_security("delete-generic-password")
    assert secret_store.set_secret("MY_TOKEN", "v") is False
    assert hung.attempts == ["delete-generic-password"], (
        f"expected the hung delete and nothing after it, got {hung.attempts}")
    assert _timeout_marker("delete-generic-password") in capsys.readouterr().err


def test_set_reports_failure_when_the_add_times_out(hanging_security, capsys):
    hung = hanging_security("add-generic-password")
    assert secret_store.set_secret("MY_TOKEN", "v") is False
    assert hung.attempts == ["delete-generic-password",
                             "add-generic-password"], (
        f"the add must be attempted exactly once, got {hung.attempts}")
    assert _timeout_marker("add-generic-password") in capsys.readouterr().err


def test_get_is_none_on_timeout_rather_than_raising(monkeypatch,
                                                    hanging_security, capsys):
    """A wedged read resolves to 'not found', so callers fall through to
    .env instead of dying — and says so on stderr."""
    monkeypatch.delenv("MY_TOKEN", raising=False)
    hung = hanging_security("find-generic-password")
    assert secret_store.get_secret("MY_TOKEN") is None
    assert hung.attempts == ["find-generic-password"]
    assert _timeout_marker("find-generic-password") in capsys.readouterr().err


def test_delete_is_false_on_timeout(hanging_security, capsys):
    hung = hanging_security("delete-generic-password")
    assert secret_store.delete_secret("GONE_KEY") is False
    assert hung.attempts == ["delete-generic-password"]
    assert _timeout_marker("delete-generic-password") in capsys.readouterr().err


# ---------- file backend (non-mac, non-windows) --------------------------------

def test_plainfile_roundtrip_and_mode(monkeypatch, linux_files):
    monkeypatch.delenv("PF_KEY", raising=False)
    assert secret_store.set_secret("PF_KEY", "file-value")
    assert secret_store.get_secret("PF_KEY") == "file-value"
    path = linux_files / "secrets" / "pf_key.txt"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_plainfile_delete(monkeypatch, linux_files):
    secret_store.set_secret("PF_KEY", "v")
    assert secret_store.delete_secret("PF_KEY")
    assert secret_store.get_secret("PF_KEY") is None


def test_empty_value_is_not_stored(monkeypatch, linux_files):
    assert not secret_store.set_secret("PF_KEY", "")


# ---------- CLI -----------------------------------------------------------------

def test_cli_set_reads_stdin_not_argv(monkeypatch, linux_files):
    monkeypatch.delenv("CLI_KEY", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("from-stdin\n"))
    assert secret_store._cli(["set", "CLI_KEY"]) == 0
    assert secret_store.get_secret("CLI_KEY") == "from-stdin"


def test_cli_get_missing_is_nonzero(monkeypatch, linux_files, capsys):
    monkeypatch.delenv("CLI_MISSING", raising=False)
    assert secret_store._cli(["get", "CLI_MISSING"]) == 1


def test_cli_bad_usage(monkeypatch):
    assert secret_store._cli(["frobnicate"]) == 2
