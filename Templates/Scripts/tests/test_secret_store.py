"""secret_store: resolution order, keystore backends, CLI.

The autouse block_unmocked_subprocess fixture guarantees none of these
tests can touch a real Keychain — every subprocess.run is monkeypatched.
"""
from __future__ import annotations

import io
import os
import shlex
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


def _expand(args, kwargs):
    """`security -i` takes its command on stdin: expand it into the argv it
    stands for, so the fakes dispatch on the verb either way. Returns
    (effective args, True if it came on stdin)."""
    if list(args) == ["/usr/bin/security", "-i"]:
        line = kwargs.get("input") or ""
        assert line.endswith("\n") and line.count("\n") == 1, repr(line)
        return ["/usr/bin/security", *shlex.split(line)], True
    return list(args), False


class FakeSecurity:
    """Records /usr/bin/security invocations and scripts their results.
    `calls` holds (effective args, kwargs); `argvs` the literal argv."""

    def __init__(self):
        self.calls = []
        self.argvs = []
        self.store = {}
        self.corrupt_writes = False

    def run(self, args, **kwargs):
        self.argvs.append(list(args))
        args, _ = _expand(args, kwargs)
        self.calls.append((args, kwargs))
        if args[:2] == ["/usr/bin/security", "find-generic-password"]:
            service = args[args.index("-s") + 1]
            if service in self.store:
                return subprocess.CompletedProcess(
                    args, 0, stdout=self.store[service] + "\n", stderr="")
            return subprocess.CompletedProcess(args, 44, stdout="", stderr="")
        if args[:2] == ["/usr/bin/security", "add-generic-password"]:
            svc = args[args.index("-s") + 1]
            value = args[args.index("-w") + 1]
            self.store[svc] = value + "x" if self.corrupt_writes else value
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

def test_stored_value_wins_over_env(monkeypatch, fake_security, capsys):
    # KV-1: a planted .env line (loaded into the environment) must not
    # silently replace a stored secret. Said by name, never by value.
    fake_security.store["some_key"] = "keychain-value"
    monkeypatch.setenv("SOME_KEY", "env-value")
    assert secret_store.get_secret("SOME_KEY") == "keychain-value"
    assert secret_store.last_source("SOME_KEY") == "keystore"
    err = capsys.readouterr().err
    assert ("SOME_KEY: ignoring a different value in the environment; "
            "the stored value wins") in err
    assert "env-value" not in err and "keychain-value" not in err


def test_a_planted_dotenv_cannot_switch_the_order_back(
        monkeypatch, fake_security, tmp_path):
    """Review R1: the env-first switch lived in the same environment the
    planted .env fills, so two lines (the secret and the switch) replaced a
    stored value with no warning. There is no switch any more; this loads a
    two-line .env the way the jobs do and checks the stored value still wins."""
    from dotenv import load_dotenv
    for k in ("SOURCE_MAIL_TOKEN", "OBSIDIAN_ENV_SECRETS_FIRST",
              "OBSIDIAN_ALLOW_ENV_SECRETS"):
        monkeypatch.delenv(k, raising=False)
    planted = tmp_path / ".env"
    planted.write_text("SOURCE_MAIL_TOKEN=PLANTED\n"
                       "OBSIDIAN_ENV_SECRETS_FIRST=1\n"
                       "OBSIDIAN_ALLOW_ENV_SECRETS=1\n")
    load_dotenv(planted)
    try:
        fake_security.store["source_mail_token"] = "KEYCHAIN"
        assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "KEYCHAIN"
        assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "keystore"
    finally:
        for k in ("SOURCE_MAIL_TOKEN", "OBSIDIAN_ENV_SECRETS_FIRST",
                  "OBSIDIAN_ALLOW_ENV_SECRETS"):
            os.environ.pop(k, None)


def test_no_env_first_switch_is_left_in_the_module():
    src = Path(secret_store.__file__).read_text(encoding="utf-8")
    assert "OBSIDIAN_ENV_SECRETS_FIRST" not in src
    assert not hasattr(secret_store, "ENV_FIRST_FLAG")


def test_env_supplies_a_secret_the_keystore_lacks(monkeypatch, fake_security):
    # The documented .env-only install keeps working.
    monkeypatch.setenv("SOME_KEY", "env-value")
    assert secret_store.get_secret("SOME_KEY") == "env-value"
    assert secret_store.last_source("SOME_KEY") == "env"


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


def test_keychain_set_never_puts_the_value_in_argv(monkeypatch, fake_security):
    """F09: another local account can read any process's argv with `ps`.
    The value goes to `security -i` on stdin; delete-then-add over `-U`
    stays (updating an existing item can raise a consent prompt, which
    under launchd wedges silently); the write is read back."""
    value = "hunter2-" + "k" * 3300          # the KV certificate key's size
    assert secret_store.set_secret("MY_TOKEN", value)
    ops = [args[1] for args, _ in fake_security.calls]
    assert ops == ["delete-generic-password", "add-generic-password",
                   "find-generic-password"]
    for argv in fake_security.argvs:
        assert not any("hunter2" in a for a in argv), argv
    add_argv = fake_security.argvs[1]
    assert add_argv == ["/usr/bin/security", "-i"]
    add = fake_security.calls[1][0]
    assert add[add.index("-w") + 1] == value
    assert "-U" not in add


@pytest.mark.parametrize("value", ['has"quote', "back\\slash", "new\nline",
                                   "tab\there", "k" * 5000, "caf\u00e9", "emoji\U0001f511",
                                   "del\x7f"])
def test_keychain_set_refuses_what_it_cannot_send_safely(
        monkeypatch, fake_security, capsys, value):
    assert secret_store.set_secret("MY_TOKEN", value) is False
    assert fake_security.calls == []
    assert "value cannot be written safely through security -i" \
        in capsys.readouterr().err


def test_keychain_set_removes_a_value_that_does_not_read_back(
        monkeypatch, fake_security, capsys):
    fake_security.corrupt_writes = True
    assert secret_store.set_secret("MY_TOKEN", "v") is False
    assert "my_token" not in fake_security.store
    assert "the Keychain did not return the value that was written" \
        in capsys.readouterr().err


def test_keychain_set_preauthorizes_security_binary(monkeypatch, fake_security):
    secret_store.set_secret("MY_TOKEN", "v")
    add = next(args for args, _ in fake_security.calls
               if args[1] == "add-generic-password")
    t_target = add[add.index("-T") + 1]
    assert t_target == "/usr/bin/security"


def test_keychain_value_with_quotes_is_refused_not_mangled(
        monkeypatch, fake_security, capsys):
    # Since F09 the value goes through `security -i`, whose escaping rules
    # are undocumented, so a quote or backslash is refused rather than risk
    # storing something else. Every value the installer generates is
    # letters, digits, '-' and '_'; a hand-pasted one with a quote falls
    # back to .env (0600), as any failed Keychain write does.
    monkeypatch.delenv("Q_KEY", raising=False)
    assert secret_store.set_secret("Q_KEY", 'pa"ss\\word') is False
    assert secret_store.get_secret("Q_KEY") is None
    assert "value cannot be written safely through security -i" \
        in capsys.readouterr().err


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
        verb = _expand(args, kwargs)[0][1]
        self.attempts.append(verb)
        if verb == self.hang_verb:
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
    """A wedged read returns None rather than raising, says so on stderr,
    and does not fall through to an environment copy (review R4): during a
    wedge that copy may be planted or a rotated-out key."""
    monkeypatch.setenv("MY_TOKEN", "env-copy")
    hung = hanging_security("find-generic-password")
    assert secret_store.get_secret("MY_TOKEN") is None
    assert secret_store.last_source("MY_TOKEN") == "unavailable"
    assert hung.attempts == ["find-generic-password"]
    err = capsys.readouterr().err
    assert _timeout_marker("find-generic-password") in err
    assert ("MY_TOKEN: the keystore could not be read; not falling back to "
            "the environment") in err


@pytest.mark.parametrize("rc", [1, 36, 51])
def test_a_failed_keychain_read_does_not_fall_through_to_env(
        monkeypatch, fake_security, capsys, rc):
    """Review R4: only exit 44 means "no such item". Any other failure says
    nothing about what is stored, so the environment is not consulted."""
    fake_security.store["my_token"] = "stored"
    real = fake_security.run

    def _failing_read(args, **kw):
        if list(args)[:2] == ["/usr/bin/security", "find-generic-password"]:
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="")
        return real(args, **kw)

    monkeypatch.setattr(secret_store.subprocess, "run", _failing_read)
    monkeypatch.setenv("MY_TOKEN", "PLANTED")
    assert secret_store.get_secret("MY_TOKEN") is None
    assert secret_store.last_source("MY_TOKEN") == "unavailable"
    err = capsys.readouterr().err
    assert f"MY_TOKEN: the Keychain read failed (exit {rc})" in err
    assert "PLANTED" not in err


def test_a_missing_keychain_item_still_falls_through_to_env(monkeypatch, fake_security):
    monkeypatch.setenv("MY_TOKEN", "env-value")
    assert secret_store.get_secret("MY_TOKEN") == "env-value"
    assert secret_store.last_source("MY_TOKEN") == "env"


def test_an_undecryptable_dpapi_file_does_not_fall_through_to_env(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(security_common, "state_dir", lambda: tmp_path)

    def _bad(data, protect):
        raise OSError("CryptUnprotectData failed")

    monkeypatch.setattr(security_common, "_dpapi_crypt", _bad)
    path = secret_store._file_path("MY_TOKEN", dpapi=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a blob")
    monkeypatch.setenv("MY_TOKEN", "PLANTED")
    assert secret_store.get_secret("MY_TOKEN") is None
    assert secret_store.last_source("MY_TOKEN") == "unavailable"
    assert "MY_TOKEN: the DPAPI file could not be decrypted (OSError)" \
        in capsys.readouterr().err


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
    # The fixture fakes sys.platform == "linux" so the POSIX branch of
    # restrict_file runs, but the filesystem underneath is real. On Windows
    # os.chmod only toggles the read-only attribute and stat() still reports
    # 0o666, so the mode assertion tests the OS, not our code. Check it where
    # it means something; the roundtrip above is the portable part.
    if os.name == "nt":
        pytest.skip("POSIX file modes are not represented on Windows; "
                    "restrict_file's icacls path is covered separately")
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


# ---------- Azure Key Vault backend ---------------------------------------

import base64  # noqa: E402
import json  # noqa: E402
import urllib.parse  # noqa: E402

KV_URL = "https://example-kv.vault.azure.net/"
THUMB = "C0" * 20
FAKE_PEM = b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"


class FakeRequest:
    def __init__(self, host, method, path, body, headers):
        self.full_url = f"https://{host}{path}"
        self.method, self.data, self.headers = method, body, headers

    def get_header(self, name):
        return self.headers.get(name)


class FakeVault:
    """Scripts the Entra token endpoint and the vault's secret GETs, at the
    http.client.HTTPSConnection layer the backend really uses."""

    def __init__(self):
        self.secrets = {}
        self.requests = []
        self.connections = []
        self.vault_error = None     # (status, body) returned for secret GETs
        self.token_error = None
        self.network_down = False

    def connection(self, host, context=None, timeout=None):
        vault = self
        assert timeout, "every Key Vault call needs a timeout"
        assert context is not None, "every Key Vault call needs a TLS context"
        self.connections.append(host)

        class Conn:
            def request(self, method, path, body=None, headers=None):
                if vault.network_down:
                    raise OSError("nodename nor servname provided")
                self.req = FakeRequest(host, method, path, body, headers or {})
                vault.requests.append(self.req)

            def getresponse(self):
                status, payload = vault.answer(self.req)
                return type("Resp", (), {
                    "status": status,
                    "read": lambda _self, n=-1: json.dumps(payload).encode()})()

            def close(self):
                pass

        return Conn()

    def answer(self, req):
        url = req.full_url
        if url.startswith("https://login.microsoftonline.com/"):
            if self.token_error:
                return self.token_error
            return 200, {"access_token": "tok-123", "expires_in": 3599}
        assert url.startswith(KV_URL.rstrip("/") + "/secrets/")
        if self.vault_error:
            return self.vault_error
        name = url.split("/secrets/", 1)[1].split("?", 1)[0]
        if name not in self.secrets:
            return 404, {"error": {"code": "SecretNotFound"}}
        return 200, {"value": self.secrets[name], "id": url}

    def token_calls(self):
        return [r for r in self.requests
                if r.full_url.startswith("https://login.microsoftonline.com/")]


@pytest.fixture
def keyvault(monkeypatch: pytest.MonkeyPatch, fake_security: FakeSecurity):
    """Vault on, certificate key in the (fake) Keychain, openssl faked."""
    for k, v in (("KEYVAULT_URL", KV_URL), ("KEYVAULT_TENANT_ID", "tenant-1"),
                 ("KEYVAULT_CLIENT_ID", "client-1"),
                 ("KEYVAULT_CERT_THUMBPRINT", THUMB)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(secret_store, "_kv_token", {})
    monkeypatch.setattr(secret_store, "_kv_cache", {})
    monkeypatch.setattr(secret_store, "_last_source", {})
    fake_security.store["azure_kv_client_key"] = base64.b64encode(FAKE_PEM).decode()
    vault = FakeVault()
    monkeypatch.setattr(secret_store.http.client, "HTTPSConnection", vault.connection)
    vault.openssl_calls = []
    security_run = fake_security.run

    def run(args, **kwargs):
        if args and args[0] == "/usr/bin/openssl":
            fd = kwargs["pass_fds"][0]
            vault.openssl_calls.append({"args": args, "kwargs": kwargs,
                                        "key": os.read(fd, 65536)})
            return subprocess.CompletedProcess(args, 0, stdout=b"SIG", stderr=b"")
        return security_run(args, **kwargs)

    monkeypatch.setattr(secret_store.subprocess, "run", run)
    return vault


def test_keyvault_off_without_url_uses_keychain(monkeypatch, fake_security):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    fake_security.store["my_token"] = "from-keychain"
    assert secret_store.get_secret("MY_TOKEN") == "from-keychain"
    assert secret_store.last_source("MY_TOKEN") == "keystore"


def test_keyvault_resolves_with_dashed_name(monkeypatch, keyvault, fake_security):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.secrets["source-mail-token"] = "vault-value"
    fake_security.store["source_mail_token"] = "stale-keychain-value"
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "vault-value"
    assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "keyvault"
    get = keyvault.requests[-1]
    assert get.full_url == (KV_URL + "secrets/source-mail-token?api-version="
                            + secret_store.KEYVAULT_API_VERSION)
    assert get.get_header("Authorization") == "Bearer tok-123"


def test_keyvault_client_assertion_is_bound_to_the_certificate(monkeypatch, keyvault):
    monkeypatch.delenv("OPEN_WEBUI_API_KEY", raising=False)
    keyvault.secrets["open-webui-api-key"] = "v"
    secret_store.get_secret("OPEN_WEBUI_API_KEY")
    (tok,) = keyvault.token_calls()
    assert tok.full_url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    form = urllib.parse.parse_qs(tok.data.decode())
    assert form["client_id"] == ["client-1"]
    assert form["scope"] == ["https://vault.azure.net/.default"]
    header_b64, claims_b64, sig_b64 = form["client_assertion"][0].split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    header = json.loads(base64.urlsafe_b64decode(pad(header_b64)))
    claims = json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
    assert header["alg"] == "RS256"
    assert base64.urlsafe_b64decode(pad(header["x5t"])) == bytes.fromhex(THUMB)
    assert claims["aud"] == tok.full_url
    assert claims["iss"] == claims["sub"] == "client-1"
    assert claims["exp"] - claims["nbf"] <= 900
    assert base64.urlsafe_b64decode(pad(sig_b64)) == b"SIG"


def test_keyvault_private_key_reaches_openssl_on_a_pipe_not_argv(monkeypatch, keyvault):
    monkeypatch.delenv("OPEN_WEBUI_API_KEY", raising=False)
    keyvault.secrets["open-webui-api-key"] = "v"
    secret_store.get_secret("OPEN_WEBUI_API_KEY")
    (call,) = keyvault.openssl_calls
    assert call["key"] == FAKE_PEM
    assert call["args"][-1] == f"/dev/fd/{call['kwargs']['pass_fds'][0]}"
    assert not any("PRIVATE KEY" in a for a in call["args"])
    assert call["kwargs"]["timeout"]


def test_keyvault_token_is_reused_within_a_process(monkeypatch, keyvault):
    for n in ("SOURCE_MAIL_TOKEN", "OPEN_WEBUI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    keyvault.secrets.update({"source-mail-token": "a", "open-webui-api-key": "b"})
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "a"
    assert secret_store.get_secret("OPEN_WEBUI_API_KEY") == "b"
    assert len(keyvault.token_calls()) == 1


def test_keyvault_value_wins_over_env(monkeypatch, keyvault):
    monkeypatch.setenv("SOURCE_MAIL_TOKEN", "env-value")
    keyvault.secrets["source-mail-token"] = "vault-value"
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "vault-value"
    assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "keyvault"


def test_keyvault_miss_never_falls_back_to_env(monkeypatch, keyvault, capsys):
    # The vault is authoritative: with it on, an env copy is not a fallback.
    monkeypatch.setenv("SOURCE_MAIL_TOKEN", "env-value")
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "an environment copy exists but is not used while Key Vault is on" \
        in capsys.readouterr().err


def test_keyvault_old_env_first_switch_has_no_effect(monkeypatch, keyvault):
    monkeypatch.setenv("SOURCE_MAIL_TOKEN", "env-value")
    monkeypatch.setenv("OBSIDIAN_ENV_SECRETS_FIRST", "1")
    keyvault.secrets["source-mail-token"] = "vault-value"
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "vault-value"


def test_keyvault_fallback_mode_still_never_uses_env(
        monkeypatch, keyvault, fake_security, capsys):
    """Review R5: with KEYVAULT_KEYCHAIN_FALLBACK on, a name the vault and
    the Keychain both lack used to come from the environment."""
    monkeypatch.setenv("KEYVAULT_KEYCHAIN_FALLBACK", "1")
    monkeypatch.setenv("SOURCE_MAIL_TOKEN", "PLANTED")
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "missing"
    assert ("SOURCE_MAIL_TOKEN: an environment copy exists but is not used "
            "while Key Vault is on") in capsys.readouterr().err


def test_keyvault_firewall_refusal_is_named_and_does_not_fall_back(
        monkeypatch, keyvault, fake_security, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    fake_security.store["source_mail_token"] = "keychain-value"
    keyvault.vault_error = (403, {"error": {"code": "Forbidden", "innererror": {
        "code": "ForbiddenByFirewall"}}})
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    err = capsys.readouterr().err
    assert "SOURCE_MAIL_TOKEN: Key Vault refused the connection (ForbiddenByFirewall): off VPN?" in err
    assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "missing"


def test_keyvault_unreachable_is_named(monkeypatch, keyvault, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.network_down = True
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "Key Vault unreachable (nodename nor servname provided): off VPN?" in capsys.readouterr().err


def test_keyvault_token_refusal_names_the_entra_code(monkeypatch, keyvault, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.token_error = (401, {"error": "invalid_client", "error_codes": [700027],
                                  "error_description": "cert mismatch"})
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "Key Vault HTTP 401 invalid_client AADSTS700027" in capsys.readouterr().err


def test_keyvault_not_found_does_not_consult_the_keychain(
        monkeypatch, keyvault, fake_security, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    fake_security.store["source_mail_token"] = "keychain-value"
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "SOURCE_MAIL_TOKEN: not in Key Vault" in capsys.readouterr().err
    looked_up = [a[a.index("-s") + 1] for a, _ in fake_security.calls if "-s" in a]
    assert "source_mail_token" not in looked_up


def test_keyvault_fallback_flag_uses_the_keychain(
        monkeypatch, keyvault, fake_security, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    monkeypatch.setenv("KEYVAULT_KEYCHAIN_FALLBACK", "1")
    fake_security.store["source_mail_token"] = "keychain-value"
    keyvault.network_down = True
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "keychain-value"
    assert secret_store.last_source("SOURCE_MAIL_TOKEN") == "keystore"
    assert "falling back to the Keychain (KEYVAULT_KEYCHAIN_FALLBACK)" in capsys.readouterr().err


def test_keyvault_missing_config_is_named(monkeypatch, keyvault, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    monkeypatch.delenv("KEYVAULT_CLIENT_ID")
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "Key Vault is configured but missing KEYVAULT_CLIENT_ID" in capsys.readouterr().err
    assert keyvault.requests == []


@pytest.mark.parametrize("thumb", ["XYZ", "C0" * 19, "C0" * 32])
def test_keyvault_bad_thumbprint_is_refused(monkeypatch, keyvault, capsys, thumb):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    monkeypatch.setenv("KEYVAULT_CERT_THUMBPRINT", thumb)
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "KEYVAULT_CERT_THUMBPRINT must be a 40-hex SHA-1" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://example-kv.vault.azure.net/",
                                 "https://example-kv.vault.azure.net.evil.example/",
                                 "https://evil.example/"])
def test_keyvault_url_must_be_an_https_vault(monkeypatch, keyvault, capsys, url):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    monkeypatch.setenv("KEYVAULT_URL", url)
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "KEYVAULT_URL must be https://<name>.vault.azure.net" in capsys.readouterr().err
    assert keyvault.requests == []


def test_keyvault_name_outside_its_charset_is_refused(monkeypatch, keyvault, capsys):
    monkeypatch.delenv("BAD.NAME", raising=False)
    assert secret_store.get_secret("BAD.NAME") is None
    assert "cannot be a Key Vault secret name" in capsys.readouterr().err
    assert keyvault.requests == []


def test_keyvault_missing_certificate_key_is_named(monkeypatch, keyvault, fake_security, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    del fake_security.store["azure_kv_client_key"]
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "certificate key azure_kv_client_key is not in the Keychain" in capsys.readouterr().err


def test_keyvault_is_ignored_off_macos(monkeypatch, linux_files, capsys):
    monkeypatch.setenv("KEYVAULT_URL", KV_URL)
    monkeypatch.delenv("PF_KEY", raising=False)
    assert secret_store.set_secret("PF_KEY", "file-value")
    assert secret_store.get_secret("PF_KEY") == "file-value"
    assert "the Key Vault backend is macOS-only; KEYVAULT_URL ignored" in capsys.readouterr().err


def test_status_reports_sources_and_never_values(monkeypatch, keyvault, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("# no secrets here\n")
    monkeypatch.setattr(secret_store, "SECRETS_ENV", env)
    monkeypatch.setenv("LLM_API_KEY_NAME", "GW_KEY")
    for n in ("GW_KEY", "SOURCE_MAIL_APP_PASSWORD", "SOURCE_MAIL_TOKEN",
              "OPEN_WEBUI_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    keyvault.secrets.update({"gw-key": "s1", "source-mail-app-password": "s2",
                             "source-mail-token": "s3", "open-webui-api-key": "s4"})
    assert secret_store._cli(["status"]) == 0
    out = capsys.readouterr().out
    assert "GW_KEY: keyvault" in out and "OPEN_WEBUI_API_KEY: keyvault" in out
    assert not any(v in out for v in ("s1", "s2", "s3", "s4"))


def test_status_fails_when_only_dotenv_holds_it_while_the_vault_is_on(
        monkeypatch, keyvault, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("SOURCE_MAIL_TOKEN=leftover-plaintext\n")
    monkeypatch.setattr(secret_store, "SECRETS_ENV", env)
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 1
    out = capsys.readouterr().out
    assert ("SOURCE_MAIL_TOKEN: MISSING from Key Vault (a .env copy exists "
            "but is not used while the vault is on)") in out
    assert "leftover-plaintext" not in out


def test_status_names_an_ignored_dotenv_copy(monkeypatch, keyvault, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("SOURCE_MAIL_TOKEN=leftover-plaintext\n")
    monkeypatch.setattr(secret_store, "SECRETS_ENV", env)
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.secrets["source-mail-token"] = "vault-value"
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 0
    out = capsys.readouterr().out
    assert ("SOURCE_MAIL_TOKEN: keyvault  (a .env copy exists and is "
            "ignored; remove it)") in out
    assert "leftover-plaintext" not in out and "vault-value" not in out


def test_status_reports_an_unreadable_keystore(monkeypatch, fake_security,
                                               tmp_path, capsys):
    """Review R6: status must say what the jobs will actually get. A read
    that fails is not a source, and a .env copy is not used instead."""
    env = tmp_path / ".env"
    env.write_text("SOURCE_MAIL_TOKEN=leftover\n")
    monkeypatch.setattr(secret_store, "SECRETS_ENV", env)
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    real = fake_security.run

    def _locked(args, **kw):
        if list(args)[:2] == ["/usr/bin/security", "find-generic-password"]:
            return subprocess.CompletedProcess(args, 36, stdout="", stderr="")
        return real(args, **kw)

    monkeypatch.setattr(secret_store.subprocess, "run", _locked)
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 1
    out = capsys.readouterr().out
    assert ("SOURCE_MAIL_TOKEN: UNAVAILABLE (the keystore could not be read; "
            "jobs get nothing until it can)") in out
    assert "leftover" not in out


def test_status_missing_is_nonzero(monkeypatch, keyvault, tmp_path, capsys):
    monkeypatch.setattr(secret_store, "SECRETS_ENV", tmp_path / "absent.env")
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 1
    assert "SOURCE_MAIL_TOKEN: MISSING" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://evil.example/x",
                                 "http://example-kv.vault.azure.net/secrets/a",
                                 "https://example-kv.vault.azure.net:8443/secrets/a",
                                 "file:///etc/passwd"])
def test_keyvault_http_refuses_any_other_host_or_scheme(keyvault, url):
    with pytest.raises(secret_store.KeyVaultError, match="refusing a Key Vault call"):
        secret_store._kv_http(url)
    assert keyvault.connections == []


def test_keyvault_redirect_is_not_followed(monkeypatch, keyvault, capsys):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.vault_error = (302, {})
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert "SOURCE_MAIL_TOKEN: Key Vault HTTP 302" in capsys.readouterr().err
    assert [r.full_url.split("?")[0] for r in keyvault.requests][-1] == \
        KV_URL + "secrets/source-mail-token"
    assert len(keyvault.requests) == 2      # one token, one GET, no follow-up


def test_keyvault_token_post_is_form_encoded(monkeypatch, keyvault):
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.secrets["source-mail-token"] = "v"
    secret_store.get_secret("SOURCE_MAIL_TOKEN")
    (tok,) = keyvault.token_calls()
    assert tok.method == "POST"
    assert tok.get_header("Content-Type") == "application/x-www-form-urlencoded"


# ---------- re-review of 8062ecb (S1, S2, S8, S9) ------------------------------

def _cert_read(monkeypatch, fake_security, outcome):
    """Make the Keychain read of the certificate key fail: 'timeout' or an
    exit code. Every other call goes to the fake store."""
    real = fake_security.run

    def run(args, **kw):
        a = _expand(args, kw)[0]
        if a[1:2] == ["find-generic-password"] and "azure_kv_client_key" in a:
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout"))
            return subprocess.CompletedProcess(args, outcome, stdout="", stderr="")
        return real(args, **kw)

    monkeypatch.setattr(secret_store.subprocess, "run", run)


@pytest.mark.parametrize("outcome", ["timeout", 36, 51])
def test_an_unreadable_certificate_key_is_reported_not_raised(
        monkeypatch, keyvault, fake_security, capsys, outcome):
    """Review S1: READ_FAILED is truthy, so `if not blob` passed it to
    b64decode, and a TypeError escaped get_secret, status and the mail pull."""
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    keyvault.secrets["source-mail-token"] = "vault-value"
    _cert_read(monkeypatch, fake_security, outcome)
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") is None
    assert ("SOURCE_MAIL_TOKEN: certificate key azure_kv_client_key could not "
            "be read from the Keychain") in capsys.readouterr().err
    assert keyvault.requests == []          # never reached the network


def test_status_survives_an_unreadable_certificate_key(
        monkeypatch, keyvault, fake_security, tmp_path, capsys):
    monkeypatch.setattr(secret_store, "SECRETS_ENV", tmp_path / "absent.env")
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    _cert_read(monkeypatch, fake_security, 36)
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 1
    assert "SOURCE_MAIL_TOKEN: MISSING" in capsys.readouterr().out


def test_not_found_hint_never_advises_storing_after_a_failed_read(
        monkeypatch, fake_security):
    """Review S2: after a failed read the key may well be stored, and writing
    to a Keychain that just failed is the one thing never to do."""
    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert secret_store.get_secret("MY_TOKEN") is None           # not held
    assert secret_store.not_found_hint("MY_TOKEN") == (
        "store it with: python3 secret_store.py set MY_TOKEN")
    fake_security.store["my_token"] = "stored"
    real = fake_security.run
    monkeypatch.setattr(secret_store.subprocess, "run", lambda a, **k: (
        subprocess.CompletedProcess(a, 36, stdout="", stderr="")
        if list(a)[:2] == ["/usr/bin/security", "find-generic-password"]
        else real(a, **k)))
    assert secret_store.get_secret("MY_TOKEN") is None
    hint = secret_store.not_found_hint("MY_TOKEN")
    assert hint.startswith("the keystore could not be read, so MY_TOKEN may "
                           "well be stored; do not store it again")
    assert " set " not in hint


def test_an_unreadable_plain_file_does_not_fall_through_to_env(
        monkeypatch, linux_files):
    """Review S8: returning None here instead of READ_FAILED failed no test."""
    path = secret_store._file_path("PF_KEY", dpapi=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setenv("PF_KEY", "PLANTED")
    assert secret_store.get_secret("PF_KEY") is None
    assert secret_store.last_source("PF_KEY") == "unavailable"


def test_beside_an_older_security_common_reads_work_and_writes_refuse(
        monkeypatch, fake_security, capsys):
    """Review S9: a deploy that copied secret_store.py without
    security_common.py used to die at import in every job."""
    monkeypatch.delattr(security_common, "security_i_line")
    fake_security.store["my_token"] = "stored"
    assert secret_store.get_secret("MY_TOKEN", use_env=False) == "stored"
    assert secret_store.set_secret("MY_TOKEN", "new") is False
    assert fake_security.store["my_token"] == "stored"
    assert ("security_common.py is older than secret_store.py (no "
            "security_i_line); deploy both files together") in capsys.readouterr().err


def test_secret_store_imports_beside_a_security_common_without_the_builder(
        tmp_path, allow_subprocess):
    import shutil
    here = Path(secret_store.__file__).parent
    shutil.copy(here / "secret_store.py", tmp_path / "secret_store.py")
    old = (here / "security_common.py").read_text(encoding="utf-8")
    old = old.replace("def security_i_line(", "def _gone_security_i_line(")
    old = old.replace("SECURITY_I_MAX_LINE = 4000", "_GONE = 4000")
    (tmp_path / "security_common.py").write_text(old, encoding="utf-8")
    p = subprocess.run([sys.executable, "-I", "-B", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import secret_store; print(secret_store.SECURITY_I_MAX_LINE)",
                        str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "4000"
