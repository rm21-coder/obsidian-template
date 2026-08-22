"""
test_llm_endpoint.py — endpoint resolution for Claude calls.

What matters here is that an install profile can move every Claude-calling
script onto an institutional gateway by setting two environment values, and
that an install which sets nothing keeps talking to api.anthropic.com with
ANTHROPIC_API_KEY exactly as before. Both directions are asserted, because a
silent fall-back to stock auth is the failure mode with a bill attached.

The anthropic SDK is faked (a module injected into sys.modules) so these run
without it installed, and secret_store.get_secret is monkeypatched so nothing
touches the real Keychain. DNS is stubbed too, because client() preflights a
configured gateway's hostname -- see gateway_resolves.
"""
from __future__ import annotations

import socket
import sys
import types

import pytest

import llm_endpoint
import secret_store


# The proxy vars are cleaned along with the LLM_* ones because a developer
# shell that exports HTTPS_PROXY would silently disable the gateway preflight
# and make the negative tests below pass for the wrong reason.
ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY_NAME", "ANTHROPIC_API_KEY",
            "LLM_SKIP_PREFLIGHT", "HTTPS_PROXY", "https_proxy", "ALL_PROXY",
            "all_proxy")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No inherited LLM_* or key values leaking in from the developer's shell."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Capture the kwargs llm_endpoint.client() hands the SDK."""
    calls: list[dict] = []

    class Anthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.kwargs = kwargs

    module = types.ModuleType("anthropic")
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


@pytest.fixture(autouse=True)
def gateway_resolves(monkeypatch):
    """Every hostname resolves, to one fixed address.

    conftest's block_external_dns fails any test that reaches the real
    resolver, and client() now preflights the gateway hostname -- so without
    this every gateway test in this file would fail on a DNS assertion instead
    of testing endpoint resolution. Tests that want the unresolvable case
    patch getaddrinfo again; the later monkeypatch wins.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ])


@pytest.fixture
def gateway_unresolvable(monkeypatch):
    """DNS says no -- the off-VPN case, where the host does not exist."""
    def boom(host, port, *a, **kw):
        raise socket.gaierror(socket.EAI_NONAME,
                              "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def vault_secrets(monkeypatch):
    """A dict-backed get_secret, so no test reads the real keystore."""
    store: dict[str, str] = {}
    monkeypatch.setattr(secret_store, "get_secret",
                        lambda name, **kw: store.get(name))
    return store


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------

def test_default_is_stock_anthropic():
    assert llm_endpoint.base_url() is None
    assert llm_endpoint.key_name() == "ANTHROPIC_API_KEY"
    assert llm_endpoint.describe() == "ANTHROPIC_API_KEY via api.anthropic.com"


def test_gateway_env_redirects_endpoint_and_key_name(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")
    assert llm_endpoint.base_url() == "https://api.ai.example.edu"
    assert llm_endpoint.key_name() == "EXAMPLE_AI_API_KEY"
    assert llm_endpoint.describe() == (
        "EXAMPLE_AI_API_KEY via https://api.ai.example.edu")


def test_trailing_slash_is_stripped(monkeypatch):
    # The SDK joins paths onto base_url; a trailing slash yields //v1/messages,
    # which some gateways 404 on.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu/")
    assert llm_endpoint.base_url() == "https://api.ai.example.edu"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_mean_unset(monkeypatch, blank):
    # An install profile that ships the key commented out to a blank string
    # must not produce base_url="" — that would break every call.
    monkeypatch.setenv("LLM_BASE_URL", blank)
    monkeypatch.setenv("LLM_API_KEY_NAME", blank)
    assert llm_endpoint.base_url() is None
    assert llm_endpoint.key_name() == "ANTHROPIC_API_KEY"


def test_describe_never_leaks_the_key(monkeypatch, vault_secrets):
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")
    vault_secrets["EXAMPLE_AI_API_KEY"] = "sk-super-secret"
    assert "sk-super-secret" not in llm_endpoint.describe()


# ---------------------------------------------------------------------------
# Client construction.
# ---------------------------------------------------------------------------

def test_client_routes_through_the_gateway(monkeypatch, fake_anthropic,
                                          vault_secrets):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")
    vault_secrets["EXAMPLE_AI_API_KEY"] = "sk-gateway"

    llm_endpoint.client()

    assert fake_anthropic == [{"api_key": "sk-gateway",
                              "base_url": "https://api.ai.example.edu"}]


def test_client_without_gateway_passes_no_base_url(fake_anthropic,
                                                   vault_secrets):
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client()

    assert fake_anthropic == [{"api_key": "sk-stock"}]


def test_explicit_base_url_kwarg_wins(monkeypatch, fake_anthropic,
                                      vault_secrets):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client(base_url="https://override.example.edu")

    assert fake_anthropic[0]["base_url"] == "https://override.example.edu"


def test_extra_kwargs_reach_the_sdk(fake_anthropic, vault_secrets):
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client(max_retries=5)

    assert fake_anthropic[0]["max_retries"] == 5


def test_missing_credential_names_the_key_and_the_fix(monkeypatch,
                                                      fake_anthropic,
                                                      vault_secrets):
    # The gateway key is a different credential from a personal Anthropic key,
    # so the error has to say WHICH name resolved nowhere and how to store it —
    # "no API key found" sends people to the wrong console.
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")

    with pytest.raises(llm_endpoint.MissingCredential) as excinfo:
        llm_endpoint.client()

    message = str(excinfo.value)
    assert excinfo.value.name == "EXAMPLE_AI_API_KEY"
    assert "EXAMPLE_AI_API_KEY not found" in message
    assert "secret_store.py set EXAMPLE_AI_API_KEY" in message
    assert not fake_anthropic, "no client should be built without a credential"


# ---------------------------------------------------------------------------
# Gateway preflight.
# ---------------------------------------------------------------------------

def test_unresolvable_gateway_names_the_host_and_the_vpn(
        monkeypatch, fake_anthropic, vault_secrets, gateway_unresolvable):
    # What this replaces is APIConnectionError("Connection error.") -- no host,
    # no cause, and it reads as a bad credential. The message IS the feature,
    # so assert its parts rather than just the exception type.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")
    vault_secrets["EXAMPLE_AI_API_KEY"] = "sk-gateway"

    with pytest.raises(llm_endpoint.GatewayUnreachable) as excinfo:
        llm_endpoint.client()

    message = str(excinfo.value)
    assert excinfo.value.host == "api.ai.example.edu"
    assert "api.ai.example.edu did not resolve" in message
    assert "VPN" in message
    assert "LLM_SKIP_PREFLIGHT=1" in message
    assert not fake_anthropic, "no client should be built for a dead endpoint"


def test_gateway_unreachable_is_catchable_as_endpoint_error(
        monkeypatch, fake_anthropic, vault_secrets, gateway_unresolvable):
    # Every call site catches EndpointError, so both failure modes have to be
    # reachable through that one clause. If this breaks, four scripts start
    # printing tracebacks instead of a message.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    with pytest.raises(llm_endpoint.EndpointError):
        llm_endpoint.client()

    assert issubclass(llm_endpoint.MissingCredential,
                      llm_endpoint.EndpointError)
    assert issubclass(llm_endpoint.GatewayUnreachable,
                      llm_endpoint.EndpointError)


def test_stock_anthropic_is_not_preflighted(fake_anthropic, vault_secrets,
                                            gateway_unresolvable):
    # With no gateway configured, a resolution failure means the machine has no
    # internet -- not that someone is off the VPN. Answering it with the VPN
    # remediation would send them the wrong way, so the check must not run.
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client()

    assert fake_anthropic == [{"api_key": "sk-stock"}]


def test_preflight_checks_the_base_url_actually_used(monkeypatch,
                                                    fake_anthropic,
                                                    vault_secrets):
    # An explicit base_url kwarg wins over the environment, so it is the host
    # that must be resolved. Checking the environment's host would clear a call
    # that is about to be made somewhere else entirely.
    seen: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **kw: seen.append(host) or [])
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.edu")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client(base_url="https://override.example.edu")

    assert seen == ["override.example.edu"]


def test_skip_preflight_env_bypasses_the_check(monkeypatch, fake_anthropic,
                                              vault_secrets,
                                              gateway_unresolvable):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_SKIP_PREFLIGHT", "1")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client()

    assert fake_anthropic[0]["base_url"] == "https://api.ai.example.edu"


@pytest.mark.parametrize("blank", ["", "0", "   "])
def test_skip_preflight_unset_or_zero_still_checks(monkeypatch, fake_anthropic,
                                                  vault_secrets,
                                                  gateway_unresolvable, blank):
    # An installer that writes LLM_SKIP_PREFLIGHT=0 must not disable the check.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_SKIP_PREFLIGHT", blank)
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    with pytest.raises(llm_endpoint.GatewayUnreachable):
        llm_endpoint.client()


@pytest.mark.parametrize("var", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY",
                                 "all_proxy"])
def test_proxied_egress_bypasses_the_check(monkeypatch, fake_anthropic,
                                           vault_secrets,
                                           gateway_unresolvable, var):
    # With a proxy in front, the PROXY resolves the gateway name and we never
    # do. Local NXDOMAIN predicts nothing, and refusing on it would break an
    # install that works.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv(var, "http://proxy.example.edu:3128")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client()

    assert fake_anthropic[0]["base_url"] == "https://api.ai.example.edu"


def test_preflight_precedes_the_keystore(monkeypatch, fake_anthropic,
                                         gateway_unresolvable):
    # get_secret() can block on a keystore consent dialog; the resolve cannot.
    # When both would fail the cheap failure is the one that should surface,
    # and the keystore must not be read at all.
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_API_KEY_NAME", "EXAMPLE_AI_API_KEY")

    def refuse(name, **kw):
        raise AssertionError("the keystore was read before the preflight ran")

    monkeypatch.setattr(secret_store, "get_secret", refuse)

    with pytest.raises(llm_endpoint.GatewayUnreachable):
        llm_endpoint.client()

    assert not fake_anthropic


def test_base_url_without_a_parseable_host_is_left_to_the_sdk(
        monkeypatch, fake_anthropic, vault_secrets, gateway_unresolvable):
    # A scheme-less base_url yields no hostname. Refusing here would invent a
    # failure the SDK might not have had, so the call goes through.
    monkeypatch.setenv("LLM_BASE_URL", "api.ai.example.edu")
    vault_secrets["ANTHROPIC_API_KEY"] = "sk-stock"

    llm_endpoint.client()

    assert fake_anthropic[0]["base_url"] == "api.ai.example.edu"
