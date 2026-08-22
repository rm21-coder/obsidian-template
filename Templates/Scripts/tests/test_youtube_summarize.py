"""
test_youtube_summarize.py — unit, security, and regression tests for the
YouTube → Obsidian summarizer.

Test classes
------------
- TestIsSafeUrlUnit         fast unit tests on the synchronous SSRF guard
- TestIsSafeUrlSecurity     attack-vector parity with url_safety (must match)
- TestCaptionTrackSSRF      verify is_safe_url runs before urlopen on every
                            caption track URL surfaced by yt-dlp
- TestParseCaptionBody      json3 / vtt / srt / ttml parsing
- TestSuggestedTags         parse_suggested_tags + strip_suggested_tags_section
- TestHelpers               yaml_escape, safe_filename, format_duration,
                            first_paragraph, collapse_whitespace
- TestTextBlockExtraction   response parsing filters blocks by type, so a
                            leading ThinkingBlock cannot raise
- TestStatic                source invariants: no credential in a URL query,
                            one user turn (not a system prompt), no
                            temperature, SSRF guard at the caption fetch,
                            and the call is metered

This module was added in v1.6 as part of closing the test-harness gap the
CISO reviewer identified in v1.5, when the script carried its own HTTP call
to a third-party summarization API. Summarization now goes through
llm_endpoint.py and the SDK, so the retry-loop and endpoint-auth tests that
guarded that code are gone with it; the caption-track SSRF perimeter is
unchanged and remains the security core of this file.

Mocking strategy
----------------
- urllib.request.urlopen is monkeypatched on the youtube_summarize
  module for every test that exercises the caption-fetch path.
- The summarization call is never made: TestTextBlockExtraction feeds
  fake response objects to the parsing helper directly, and the rest of
  the call shape is asserted against source.
- Subprocess is gated by conftest's block_unmocked_subprocess fixture.

- DNS is blocked outright by conftest's block_external_dns (autouse), so a
  test that needs is_safe_url to accept a hostname takes the `public_dns`
  fixture, which resolves everything to one fixed public address. Seven tests
  here needed it. Two of them failed offline; the other five were worse --
  the parity cases PASSED offline because both predicates failed to resolve
  and therefore "agreed" on False, asserting parity while testing nothing
  about acceptance.
"""
from __future__ import annotations

import json
import socket
import sys
import textwrap
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Module under test. Imported via the conftest sys.path injection.
import youtube_summarize as ys


# ---------------------------------------------------------------------------
# Helpers — fake urlopen Response objects.
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal context-manager response that returns a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    """Build an HTTPError of the requested status."""
    fp = BytesIO(body)
    err = urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/...",
        code=code, msg=f"status {code}", hdrs=None, fp=fp,
    )
    return err


# ---------------------------------------------------------------------------
# is_safe_url — fast unit tests on literal-string and IP-literal paths.
#
# These mirror the parametrize list used in test_url_safety.py.
# Parity between the two scripts is enforced as a separate test below.
# ---------------------------------------------------------------------------

class TestIsSafeUrlUnit:

    @pytest.mark.parametrize("url, reason_substring", [
        # Disallowed schemes
        ("file:///etc/passwd", "scheme"),
        ("gopher://example.com", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("ftp://example.com", "scheme"),
        # Loopback hostnames
        ("http://localhost/", "loopback"),
        ("http://ip6-localhost/", "loopback"),
        ("http://broadcasthost/", "loopback"),
        # Local TLDs
        ("http://thing.local/", "local TLD"),
        ("http://thing.internal/", "local TLD"),
        ("http://thing.lan/", "local TLD"),
        ("http://thing.corp/", "local TLD"),
        ("http://thing.intranet/", "local TLD"),
        ("http://thing.home/", "local TLD"),
        ("http://thing.localdomain/", "local TLD"),
        # IPv4 literals — loopback / RFC1918 / link-local / etc.
        ("http://127.0.0.1/", "IP literal"),
        ("http://10.0.0.1/", "IP literal"),
        ("http://192.168.1.1/", "IP literal"),
        ("http://172.16.0.1/", "IP literal"),
        ("http://169.254.169.254/", "IP literal"),  # cloud metadata
        ("http://224.0.0.1/", "IP literal"),         # multicast
        # IPv6 literals
        ("http://[::1]/", "IP literal"),
        ("http://[fe80::1]/", "IP literal"),
        # Malformed
        ("http:///path", "hostname"),
    ])
    def test_rejects(self, url: str, reason_substring: str) -> None:
        ok, reason = ys.is_safe_url(url)
        assert not ok, f"expected {url!r} to be rejected, got ok=True"
        assert reason_substring.lower() in reason.lower(), (
            f"reason {reason!r} did not contain {reason_substring!r}")

    def test_accepts_public_ip_literal(self) -> None:
        ok, _ = ys.is_safe_url("https://93.184.216.34/")
        assert ok

    def test_accepts_public_hostname(self, public_dns: str) -> None:
        # Hostname path: is_safe_url resolves the name (v1.7, to defeat DNS
        # rebinding) and accepts it when every answer is public. The comment
        # here used to say resolution did NOT happen, which stopped being true
        # in v1.7 -- and this test then silently depended on real DNS, so it
        # failed offline and on sandboxed CI runners.
        ok, _ = ys.is_safe_url("https://www.googleapis.com/v1beta/...")
        assert ok

    def test_accepts_googlevideo_caption_host(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Real-world host shape: YouTube caption tracks come from
        # r*---sn-*.googlevideo.com. The exact subdomain here is a
        # template that doesn't resolve in DNS; mock getaddrinfo to a
        # public IP so the v1.7 DNS-resolution guard sees a clean public
        # answer. (In production these hostnames resolve to real CDN IPs.)
        import socket
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p: [(socket.AF_INET,
                                           socket.SOCK_STREAM, 0, "",
                                           ("142.250.80.110", 0))])
        ok, reason = ys.is_safe_url(
            "https://r5---sn-abc.googlevideo.com/...")
        assert ok, reason

    def test_returns_tuple(self, public_dns: str) -> None:
        result = ys.is_safe_url("https://example.com/")
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


# ---------------------------------------------------------------------------
# is_safe_url — security parity with url_safety.is_safe_url.
#
# The two scripts duplicate the SSRF guard intentionally (each pipeline
# owns its perimeter). Parity is enforced here so a drift between the
# two predicates would be caught in CI.
# ---------------------------------------------------------------------------

class TestIsSafeUrlSecurity:

    @pytest.mark.parametrize("url", [
        "https://www.nytimes.com/article",
        "http://example.com/path",
        "https://93.184.216.34/",
        "https://r5---sn-abc.googlevideo.com/track",
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
    ])
    def test_url_safety_and_youtube_agree_accept(self, url: str,
                                                 public_dns: str) -> None:
        """Both predicates must accept the same legitimate URLs.

        public_dns matters more here than it looks. Without it these cases
        depended on live DNS, and offline they still PASSED -- both predicates
        failed to resolve, both returned False, and "they agree" held on the
        wrong answer. A vacuous pass is worse than a failure: it asserted
        parity while testing nothing about acceptance.
        """
        import url_safety
        ys_ok, _ = ys.is_safe_url(url)
        ca_ok, _ = url_safety.is_safe_url(url)
        assert ys_ok == ca_ok, (
            f"SSRF guard parity broken for {url!r}: "
            f"youtube_summarize={ys_ok}, url_safety={ca_ok}")

    @pytest.mark.parametrize("url", [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://thing.local/",
        "http://thing.internal/",
        "file:///etc/passwd",
        "gopher://example.com",
    ])
    def test_url_safety_and_youtube_agree_reject(self, url: str) -> None:
        """Both predicates must reject the same attack vectors."""
        import url_safety
        ys_ok, _ = ys.is_safe_url(url)
        ca_ok, _ = url_safety.is_safe_url(url)
        assert ys_ok == ca_ok, (
            f"SSRF guard parity broken for {url!r}: "
            f"youtube_summarize={ys_ok}, url_safety={ca_ok}")
        assert not ys_ok, f"expected {url!r} to be rejected"

    def test_disallowed_tlds_match_url_safety(self) -> None:
        """The DISALLOWED_TLDS tuple must be identical across modules."""
        import url_safety
        assert set(ys.DISALLOWED_TLDS) == set(url_safety.DISALLOWED_TLDS), (
            "DISALLOWED_TLDS drifted between youtube_summarize and url_safety")

    def test_loopback_names_match_url_safety(self) -> None:
        """The LOOPBACK_NAMES tuple must be identical across modules."""
        import url_safety
        assert set(ys.LOOPBACK_NAMES) == set(url_safety.LOOPBACK_NAMES), (
            "LOOPBACK_NAMES drifted between youtube_summarize and url_safety")


# ---------------------------------------------------------------------------
# extract_transcript — verify SSRF guard runs before urlopen on caption URLs.
# ---------------------------------------------------------------------------

class TestCaptionTrackSSRF:

    def _info_with_caption_urls(self, urls: list[str]) -> dict:
        """Build a yt-dlp info dict containing the given caption-track URLs
        in the 'subtitles' / 'en' / 'json3' position."""
        tracks = [{"ext": "json3", "url": u} for u in urls]
        return {"subtitles": {"en": tracks}, "automatic_captions": {}}

    def test_unsafe_caption_url_is_skipped(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If yt-dlp returns a caption URL pointing at loopback, the script
        must skip it without calling urlopen. This is the load-bearing
        v1.6 SSRF guard."""
        called_urls: list[str] = []

        def fake_urlopen(req, timeout=30):
            called_urls.append(req.full_url if hasattr(req, "full_url")
                               else str(req))
            return _FakeResponse(b'{"events":[]}')

        monkeypatch.setattr(ys.urllib.request, "urlopen", fake_urlopen)

        info = self._info_with_caption_urls([
            "http://127.0.0.1:11434/api/x",       # Ollama-like loopback
            "http://169.254.169.254/metadata",    # cloud-metadata link-local
        ])
        result = ys.extract_transcript(info)
        assert result == "", (
            "expected empty transcript because all caption URLs were unsafe")
        assert called_urls == [], (
            f"urlopen should not have been called for unsafe URLs, "
            f"but was called for: {called_urls}")

    def test_safe_caption_url_is_fetched(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normal googlevideo.com URL must pass the SSRF guard and be
        fetched. Confirms the guard isn't over-blocking."""
        captured_urls: list[str] = []

        # v1.7: is_safe_url now resolves hostnames via socket.getaddrinfo.
        # The r5---sn-abc template doesn't actually resolve in DNS, so we
        # mock it to a public IP for the duration of the test. (Real
        # googlevideo.com hostnames resolve to real CDN IPs in production.)
        import socket
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p: [(socket.AF_INET,
                                           socket.SOCK_STREAM, 0, "",
                                           ("142.250.80.110", 0))])

        def fake_urlopen(req, timeout=30):
            captured_urls.append(req.full_url)
            body = json.dumps({
                "events": [{"segs": [{"utf8": "hello world"}]}]
            }).encode("utf-8")
            return _FakeResponse(body)

        monkeypatch.setattr(ys.urllib.request, "urlopen", fake_urlopen)

        info = self._info_with_caption_urls([
            "https://r5---sn-abc.googlevideo.com/api/timedtext?x=1",
        ])
        result = ys.extract_transcript(info)
        assert "hello world" in result
        assert len(captured_urls) == 1
        assert "googlevideo.com" in captured_urls[0]

    def test_mixed_unsafe_then_safe(
            self, monkeypatch: pytest.MonkeyPatch,
            public_dns: str) -> None:
        """When the first track is unsafe and the second is safe, the
        script must skip the unsafe one and fetch the safe one.

        public_dns resolves the safe host without a real lookup. It cannot
        weaken the assertion: the unsafe track is an IP literal (192.168.1.5),
        and is_safe_url rejects literals in its own branch before any
        resolution happens.
        """
        attempted: list[str] = []

        def fake_urlopen(req, timeout=30):
            attempted.append(req.full_url)
            body = json.dumps({
                "events": [{"segs": [{"utf8": "fallback content"}]}]
            }).encode("utf-8")
            return _FakeResponse(body)

        monkeypatch.setattr(ys.urllib.request, "urlopen", fake_urlopen)

        info = self._info_with_caption_urls([
            "http://192.168.1.5/poisoned",         # unsafe — must skip
            "https://www.googlevideo.com/safe",    # safe — must fetch
        ])
        result = ys.extract_transcript(info)
        assert "fallback content" in result
        assert len(attempted) == 1, (
            f"urlopen called {len(attempted)} times, expected exactly 1")
        assert "192.168" not in attempted[0]


# ---------------------------------------------------------------------------
# Caption parsing.
# ---------------------------------------------------------------------------

class TestParseCaptionBody:

    def test_json3_well_formed(self) -> None:
        body = json.dumps({
            "events": [
                {"segs": [{"utf8": "hello "}, {"utf8": "world"}]},
                {"segs": [{"utf8": "\n"}, {"utf8": "second line"}]},
            ]
        })
        result = ys.parse_caption_body(body, "json3")
        assert "hello world" in result
        assert "second line" in result
        # Newline-only segments are dropped per the source filter.
        assert "\n " not in result

    def test_json3_malformed_returns_empty(self) -> None:
        result = ys.parse_caption_body("not json", "json3")
        assert result == ""

    def test_vtt_strips_timestamps_and_cues(self) -> None:
        body = textwrap.dedent("""\
            WEBVTT

            1
            00:00:00.000 --> 00:00:02.000
            Hello world.

            2
            00:00:02.000 --> 00:00:04.000
            Second line of dialog.
            """)
        result = ys.parse_caption_body(body, "vtt")
        assert "Hello world" in result
        assert "Second line" in result
        assert "00:00" not in result
        assert "WEBVTT" not in result
        # Cue numbers (lone digits on their own line) must be stripped.
        assert " 1 " not in f" {result} "
        assert " 2 " not in f" {result} "

    def test_vtt_strips_inline_tags(self) -> None:
        body = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<c.speaker>hi</c>\n"
        result = ys.parse_caption_body(body, "vtt")
        assert "hi" in result
        assert "<c" not in result
        assert "</c>" not in result

    def test_unknown_ext_falls_through_to_vtt_parser(self) -> None:
        # The function uses the vtt-style path for any non-json3 input,
        # so srt / ttml input should still produce text.
        body = "1\n00:00:00,000 --> 00:00:01,000\nSubtitle text\n"
        result = ys.parse_caption_body(body, "srt")
        assert "Subtitle text" in result


# ---------------------------------------------------------------------------
# parse_suggested_tags / strip_suggested_tags_section.
# ---------------------------------------------------------------------------

class TestSuggestedTags:

    def test_parse_basic(self) -> None:
        md = textwrap.dedent("""\
            # Summary

            Some content.

            ## Suggested tags
            ai, ml, alignment, safety
            """)
        tags = ys.parse_suggested_tags(md)
        assert tags == ["ai", "ml", "alignment", "safety"]

    def test_parse_lowercases_and_strips(self) -> None:
        md = "## Suggested tags\n  AI ,  ML  , Safety-Research\n"
        tags = ys.parse_suggested_tags(md)
        assert tags == ["ai", "ml", "safety-research"]

    def test_parse_drops_quotes_and_hashes(self) -> None:
        md = '## Suggested tags\n"ai", #ml, \'safety\'\n'
        tags = ys.parse_suggested_tags(md)
        assert tags == ["ai", "ml", "safety"]

    def test_parse_replaces_spaces_with_dashes(self) -> None:
        md = "## Suggested tags\nartificial intelligence, machine learning\n"
        tags = ys.parse_suggested_tags(md)
        assert tags == ["artificial-intelligence", "machine-learning"]

    def test_parse_returns_empty_when_section_absent(self) -> None:
        md = "# Summary\n\nNo tags section.\n"
        assert ys.parse_suggested_tags(md) == []

    def test_strip_removes_section(self) -> None:
        md = textwrap.dedent("""\
            # Summary

            Body content.

            ## Suggested tags
            a, b, c
            """)
        stripped = ys.strip_suggested_tags_section(md)
        assert "Suggested tags" not in stripped
        assert "a, b, c" not in stripped
        assert "Body content" in stripped

    def test_strip_noop_when_section_absent(self) -> None:
        md = "# Summary\n\nBody.\n"
        # strip_suggested_tags_section appends a trailing newline.
        assert ys.strip_suggested_tags_section(md).rstrip() == md.rstrip()


# ---------------------------------------------------------------------------
# Frontmatter / filename / format helpers.
# ---------------------------------------------------------------------------

class TestHelpers:

    @pytest.mark.parametrize("inp, expected_contains", [
        ("Plain text", "Plain text"),
        ("with: colon", '"'),       # quoted because of ':'
        ("with # hash", '"'),
        ("- starts with dash", '"'),
        ("[bracket start", '"'),
        ("trailing 'quote'", '"'),
    ])
    def test_yaml_escape(self, inp: str, expected_contains: str) -> None:
        result = ys.yaml_escape(inp)
        assert expected_contains in result

    def test_yaml_escape_empty(self) -> None:
        assert ys.yaml_escape("") == '""'

    def test_yaml_escape_none(self) -> None:
        assert ys.yaml_escape(None) == '""'

    @pytest.mark.parametrize("title, expected", [
        ("Hello World", "Hello World"),
        ("a/b\\c", "abc"),               # slashes stripped
        ("a:b*c?d", "abcd"),             # forbidden filename chars
        ("  spaces  collapsed  ", "spaces collapsed"),
        ("", "Untitled"),
        ("!" * 200, "!" * 120),           # length cap at 120
    ])
    def test_safe_filename(self, title: str, expected: str) -> None:
        assert ys.safe_filename(title) == expected

    @pytest.mark.parametrize("seconds, expected", [
        (0, "0:00"),
        (1, "0:01"),
        (59, "0:59"),
        (60, "1:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
    ])
    def test_format_duration(self, seconds: int, expected: str) -> None:
        assert ys.format_duration(seconds) == expected

    def test_collapse_whitespace(self) -> None:
        assert ys.collapse_whitespace("  a   b\tc\n d  ") == "a b c d"

    def test_first_paragraph_skips_headings(self) -> None:
        md = "# Heading\n\nFirst real paragraph.\n\nSecond.\n"
        assert ys.first_paragraph(md) == "First real paragraph."

    def test_first_paragraph_truncates_at_max(self) -> None:
        body = "Word " * 100
        result = ys.first_paragraph(body, max_len=50)
        assert len(result) <= 51   # trailing ellipsis
        assert result.endswith("…")


# ---------------------------------------------------------------------------
# The summarization call: invariants that cost a production failure to learn.
# ---------------------------------------------------------------------------

class _FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _FakeMessage:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)
        self.usage = {"input_tokens": 1, "output_tokens": 1}


class TestTextBlockExtraction:
    """Response parsing must filter by block type.

    A model with extended thinking returns a ThinkingBlock first, and a
    ThinkingBlock has no `.text`. Indexing content[0] therefore raises
    AttributeError — but only against models that think, so a smoke test on a
    model that doesn't will pass while production breaks.
    """

    def test_skips_a_leading_thinking_block(self) -> None:
        msg = _FakeMessage(_FakeBlock("thinking"), _FakeBlock("text", "summary"))
        assert ys._text_blocks(msg) == "summary"

    def test_joins_multiple_text_blocks(self) -> None:
        msg = _FakeMessage(_FakeBlock("text", "one"), _FakeBlock("text", "two"))
        assert ys._text_blocks(msg) == "one\ntwo"

    def test_thinking_only_response_yields_empty(self) -> None:
        # summarize() turns this into a loud RuntimeError rather than writing
        # a note whose body is the empty string.
        assert ys._text_blocks(_FakeMessage(_FakeBlock("thinking"))) == ""

    def test_a_thinking_block_without_text_does_not_raise(self) -> None:
        class Bare:
            type = "thinking"  # no .text at all
        msg = _FakeMessage()
        msg.content = [Bare(), _FakeBlock("text", "ok")]
        assert ys._text_blocks(msg) == "ok"


class TestStatic:

    def test_no_credential_in_a_url_query(self, scripts_dir: Path) -> None:
        """No credential may ride in a URL query string, where it lands in
        logs, tracebacks, and process listings. Kept from the Gemini era, when
        the fix was header auth: the endpoint changed, the invariant didn't."""
        src = (scripts_dir / "youtube_summarize.py").read_text()
        assert "?key=" not in src, "credential in a URL query string"

    def test_summarizer_sends_one_user_turn(self, scripts_dir: Path) -> None:
        """The instructions stay in the user turn, NOT a system prompt.

        Moving them to `system=` is the obvious refactor and it measurably
        regressed output: on a blind three-arm comparison over the same
        transcript, the system-prompt arm was the only one that broke the
        format spec, opening with a title heading the prompt forbids. This
        pins the finding so the cleanup doesn't get made twice.
        """
        src = (scripts_dir / "youtube_summarize.py").read_text()
        start = src.find("def summarize(")
        assert start != -1, "summarize() not found"
        body = src[start:src.find("\ndef ", start + 1)]
        assert "system=" not in body, (
            "summarize() passes a system prompt; the blind A/B preferred the "
            "single-user-turn form. See the docstring before changing this.")

    def test_summarizer_sends_no_temperature(self, scripts_dir: Path) -> None:
        """Current Claude models reject `temperature` outright. Through a
        LiteLLM-style gateway it surfaces as a 400 reading "`temperature` is
        deprecated for this model", which looks like a gateway
        misconfiguration and sends you debugging the wrong layer."""
        src = (scripts_dir / "youtube_summarize.py").read_text()
        start = src.find("def summarize(")
        body = src[start:src.find("\ndef ", start + 1)]
        assert "temperature=" not in body, (
            "summarize() passes temperature, which current models reject")

    def test_summarizer_is_metered(self, scripts_dir: Path) -> None:
        """Every model call in the vault reports to usage_log, or the
        dashboard's cost view silently understates spend."""
        src = (scripts_dir / "youtube_summarize.py").read_text()
        assert "usage_log.record" in src, "the summarizer is not metered"

    def test_ssrf_guard_called_in_extract_transcript(
            self, scripts_dir: Path) -> None:
        """extract_transcript must call is_safe_url before urlopen on each
        caption track. Static check: both names must appear in close
        proximity in the function body."""
        src = (scripts_dir / "youtube_summarize.py").read_text()
        # Find the extract_transcript function body
        marker = "def extract_transcript("
        idx = src.find(marker)
        assert idx != -1, "extract_transcript function not found"
        # Look at the function body — bounded by the next top-level def.
        body_end = src.find("\ndef ", idx + len(marker))
        body = src[idx:body_end if body_end != -1 else len(src)]
        assert "is_safe_url" in body, (
            "is_safe_url not called in extract_transcript — SSRF guard "
            "is missing from the caption-track fetch path.")
        # Confirm urlopen is also there (otherwise the test is hollow).
        assert "urlopen" in body, (
            "urlopen not found in extract_transcript — test premise broken.")

    def test_default_output_inside_vault(self) -> None:
        """DEFAULT_OUT must point inside the Obsidian vault. Catching this
        in static ensures a future edit doesn't redirect summaries to
        an unaudited location."""
        assert "Obsidian" in str(ys.DEFAULT_OUT), (
            f"DEFAULT_OUT {ys.DEFAULT_OUT} does not appear to be in the vault")
        assert "YouTube" in str(ys.DEFAULT_OUT)
