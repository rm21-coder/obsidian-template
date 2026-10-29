"""secret_store: resolution order, keystore backends, CLI.

The autouse block_unmocked_subprocess fixture guarantees none of these
tests can touch a real Keychain — every subprocess.run is monkeypatched.
"""
from __future__ import annotations

import io
import os
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


def test_keyvault_env_still_wins_without_a_network_call(monkeypatch, keyvault):
    monkeypatch.setenv("SOURCE_MAIL_TOKEN", "env-value")
    assert secret_store.get_secret("SOURCE_MAIL_TOKEN") == "env-value"
    assert keyvault.requests == []


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


def test_status_fails_when_dotenv_shadows_the_vault(monkeypatch, keyvault, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("SOURCE_MAIL_TOKEN=leftover-plaintext\n")
    monkeypatch.setattr(secret_store, "SECRETS_ENV", env)
    monkeypatch.delenv("SOURCE_MAIL_TOKEN", raising=False)
    assert secret_store._cli(["status", "SOURCE_MAIL_TOKEN"]) == 1
    out = capsys.readouterr().out
    assert "SOURCE_MAIL_TOKEN: .env  <-- overrides Key Vault; remove it" in out
    assert "leftover-plaintext" not in out


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
