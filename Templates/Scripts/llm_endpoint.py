#!/usr/bin/env python3
"""llm_endpoint.py — one place that decides WHERE Claude calls go and WHICH
stored secret opens the door.

The default is stock Anthropic with ANTHROPIC_API_KEY, so an install that
sets nothing behaves exactly as it always has. Two entries in
~/dev/secrets/.env move every Claude-calling script in the vault onto an
institutional gateway — a university AI gateway, a LiteLLM or Bedrock proxy,
an enterprise egress — with no edit at any call site:

    LLM_BASE_URL=https://api.ai.example.edu
    LLM_API_KEY_NAME=EXAMPLE_AI_API_KEY

Why the config names a key rather than carrying one: the gateway credential
is a different secret from a personal Anthropic key, with a different owner
and lifecycle. Naming it here lets secret_store hold the value in the
Keychain like every other secret, while .env keeps only non-secret config.

Requirement on the gateway: it must speak the Anthropic Messages API
(`/v1/messages`, `x-api-key` auth), because that is all the SDK's base_url
override changes. An OpenAI-shaped proxy needs a translating layer in front
of it — this module is not that layer.

client() resolves the gateway hostname before it builds a client and raises
GatewayUnreachable when DNS says no. That check earns its keep because the
SDK's version of the same failure is APIConnectionError, whose entire message
is the string "Connection error." — no host, no cause — which every caller
prints verbatim. It reads as a bad key or a broken gateway, and the actual
cause is almost always that an internal endpoint is unreachable off the VPN.
Set LLM_SKIP_PREFLIGHT=1 to stand the check down; proxied egress does that
automatically (see _skip_preflight).

Prompt caching is deliberately NOT decided here. Gateways differ on whether
they pass `cache_control` through, and each caller already owns that switch
(the tagger's TAGGER_PROMPT_CACHE=0, for instance), which keeps the failure
local to the one call that would break.

Resolution happens on call, never at import, so a caller's own load_dotenv()
has already run by the time we read the environment.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# secret_store lives beside this file; mirror its own sys.path handling so an
# `import llm_endpoint` works from a vault install, a repo checkout, or a test.
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_KEY_NAME = "ANTHROPIC_API_KEY"
STOCK_HOST = "api.anthropic.com"

# Egress-proxy variables. When one is set, the proxy resolves the gateway
# hostname and we never do, so a local resolution failure predicts nothing
# about the call and must not block it.
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


class EndpointError(RuntimeError):
    """Base for "this endpoint cannot be used": no credential, or no route to
    it. Callers catch this rather than the subclasses, so adding a failure mode
    here does not turn into an uncaught traceback at five call sites — which is
    exactly what happened when GatewayUnreachable was introduced.
    """


class MissingCredential(EndpointError):
    """The configured key name resolved nowhere — not the environment, not
    .env, not the platform keystore. Carries a ready-to-print remediation so
    every call site reports the SAME fix for the SAME problem."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"{name} not found. Set it in your environment or "
            f"~/dev/secrets/.env, or store it with:\n"
            f"  python3 secret_store.py set {name}"
        )


class GatewayUnreachable(EndpointError):
    """The configured gateway hostname does not resolve.

    Carries a ready-to-print remediation for the same reason MissingCredential
    does: the SDK reports this as APIConnectionError("Connection error."), a
    bare string with no host in it, and callers print it verbatim. Naming the
    host and leading with the VPN turns a five-minute misdiagnosis into a
    one-line fix.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(
            f"gateway host {host} did not resolve. An institutional gateway "
            f"is usually reachable only from the campus network — connect to "
            f"the VPN and retry.\n"
            f"  check with: host {host}\n"
            f"  to skip this check (proxied egress, split DNS): "
            f"LLM_SKIP_PREFLIGHT=1"
        )


def base_url() -> str | None:
    """The gateway to route through, or None for stock Anthropic."""
    return (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/") or None


def key_name() -> str:
    """Which secret name holds the credential for that endpoint."""
    return (os.environ.get("LLM_API_KEY_NAME") or "").strip() or DEFAULT_KEY_NAME


def describe() -> str:
    """One line for a startup banner or a log — which key, which endpoint.
    Never includes the key's value."""
    return f"{key_name()} via {base_url() or STOCK_HOST}"


def _skip_preflight() -> bool:
    """True when local DNS does not predict whether the call can be made."""
    if (os.environ.get("LLM_SKIP_PREFLIGHT") or "").strip() not in ("", "0"):
        return True
    return any((os.environ.get(var) or "").strip() for var in PROXY_VARS)


def check_reachable(url: str | None) -> None:
    """Raise GatewayUnreachable if `url`'s host does not resolve.

    Only a gateway is preflighted. A resolution failure against
    api.anthropic.com means the machine has no working internet, and answering
    that with "connect to the VPN" sends someone the wrong way — the SDK's own
    error is no worse there, so stock installs are left alone and pay nothing
    for this.

    A url we cannot parse a host out of is also left alone. Rejecting it here
    would turn this from a diagnostic into a new way for the module to refuse
    a call that might have worked.
    """
    if not url or _skip_preflight():
        return
    host = urlparse(url).hostname
    if not host:
        return
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GatewayUnreachable(host) from exc


def client(**kwargs):
    """An anthropic.Anthropic pointed at the configured endpoint.

    Raises MissingCredential if the configured key name resolves nowhere, or
    GatewayUnreachable if a configured gateway's hostname does not resolve;
    callers decide whether that is a hard exit or a skipped run. Extra kwargs
    pass through to the SDK (timeout, max_retries), and an explicit base_url
    kwarg still wins over the environment.

    anthropic is imported here rather than at module scope so importing this
    module for base_url()/describe() alone — a test, an installer probe —
    doesn't require the SDK to be present.
    """
    import anthropic  # noqa: PLC0415 — lazy on purpose, see docstring

    from secret_store import get_secret

    url = base_url()
    if url:
        kwargs.setdefault("base_url", url)

    # Preflight the endpoint the SDK will actually use -- read after the
    # setdefault above, so an explicit base_url kwarg is what gets checked --
    # and do it BEFORE get_secret(). The resolve is milliseconds and cannot
    # prompt; get_secret() reaches a platform keystore that can block on a
    # consent dialog. When both would fail, failing on the cheap one first is
    # strictly better, so this does not belong below the credential lookup.
    check_reachable(kwargs.get("base_url"))

    name = key_name()
    key = get_secret(name)
    if not key:
        raise MissingCredential(name)
    return anthropic.Anthropic(api_key=key, **kwargs)


if __name__ == "__main__":
    # `python3 llm_endpoint.py` — report the resolved endpoint without making
    # a call. Used by 90-verify and worth having when an adopter asks "am I
    # actually going through the gateway?"
    from dotenv import load_dotenv

    load_dotenv(Path.home() / "dev" / "secrets" / ".env")
    print(describe())

    # STDOUT stays exactly one line: 90-verify parses the first word of this
    # output as the key name. The reachability probe therefore reports on
    # STDERR, which that script discards, so an operator running this by hand
    # gets the diagnosis and the installer's parsing is untouched. Exit stays
    # 0 for the same reason -- this reports, it does not gate.
    gateway = base_url()
    if gateway:
        try:
            check_reachable(gateway)
        except GatewayUnreachable as exc:
            print(f"UNREACHABLE: {exc}", file=sys.stderr)
        else:
            print(f"reachable: {urlparse(gateway).hostname} resolves",
                  file=sys.stderr)
    sys.exit(0)
