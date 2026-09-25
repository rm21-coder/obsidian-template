"""test_url_redaction.py -- URLs reach logs without their secrets.

A URL can carry credentials in its userinfo and bearer-like material in its
query (SAS tokens, signed CDN links). Microsoft M-DASH 2026-09-23 (CWE-532)
flagged the log sites; the adversarial review of that fix (2026-09-25) found
it incomplete: a bad port crashed the redactor, malformed forms left userinfo
in the path, and one log line appended the exception text, which carries the
full URL. Both redactors are tested -- podcast_watch keeps a stdlib twin.
"""
from __future__ import annotations

import pytest
import requests

import podcast_watch
import url_safety

REDACTORS = [url_safety.redact_url, podcast_watch._redact_url]
SECRETS = ("pass", "SASTOKEN", "sig=")


@pytest.mark.parametrize("redact", REDACTORS, ids=["url_safety", "podcast_watch"])
@pytest.mark.parametrize("url", [
    "https://user:pass@host.example/p?sig=SASTOKEN",
    "https://user@host.example/p",
    "https://user:pass@host.example@evil.example/p",
    "https://USER:PASS@HOST.EXAMPLE/p".lower(),
    "http://user:pass@[::1]:8080/p?sig=SASTOKEN",
    "http:user:pass@host.example/p",          # userinfo parsed into the path
    "http:///user:pass@host.example/p",
    "https://h.example/p?sig=SASTOKEN#frag",
])
def test_no_secret_survives(redact, url: str) -> None:
    out = redact(url)
    for s in SECRETS:
        assert s not in out, f"{s!r} survived: {out}"
    assert "user" not in out.split("//", 1)[-1].split("/", 1)[0]


@pytest.mark.parametrize("redact", REDACTORS, ids=["url_safety", "podcast_watch"])
def test_bad_port_does_not_crash_the_redactor(redact) -> None:
    """.port raises ValueError; it sat outside the try, so a hostile redirect
    Location crashed the refusal log instead of being refused."""
    assert redact("https://user:pass@localhost:abc/p") == "<unparseable url>"


def test_safe_fetch_refuses_a_bad_port_instead_of_raising() -> None:
    assert url_safety.safe_fetch("https://user:pass@localhost:abc/p") is None


def test_transport_error_log_omits_the_exception_text(monkeypatch) -> None:
    """requests puts the whole URL, query and all, in its exception message."""
    def boom(url, **kw):
        raise requests.ConnectionError(
            "HTTPConnectionPool(host='h', port=1): Max retries exceeded "
            "with url: /p?sig=SASTOKEN123")

    monkeypatch.setattr(url_safety, "is_safe_url", lambda u: (True, ""))
    monkeypatch.setattr(url_safety.requests, "get", boom)
    logged: list[str] = []
    url_safety.safe_fetch("https://example.com/p?sig=SASTOKEN123", log=logged.append)
    joined = "\n".join(logged)
    assert "transport error" in joined, logged
    assert "SASTOKEN" not in joined, joined
