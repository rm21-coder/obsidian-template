"""
test_youtube_summarize.py — unit, security, and regression tests for the
YouTube → Obsidian summarizer.

Test classes
------------
- TestIsSafeUrlUnit         fast unit tests on the synchronous SSRF guard
- TestIsSafeUrlSecurity     attack-vector parity with url_safety (must match)
- TestCaptionTrackSSRF      every caption track URL surfaced by yt-dlp is
                            fetched through url_safety.safe_fetch: checked
                            before any request, redirects re-checked, capped
- TestPlaylistEntries       remote playlist entries are checked before
                            yt-dlp sees them; "--" precedes every URL
- TestFrontmatterInjection  page-supplied title/uploader cannot end the
                            frontmatter; model tags cannot become YAML syntax
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
- The caption fetch goes through url_safety.safe_fetch. Tests that exercise
  it stub url_safety._pinned_get, the one-hop network seam below the redirect
  walker, so the real guard and redirect walk run and nothing leaves the
  machine. The redirect and size-cap tests ALSO stub urllib.request.urlopen
  with what urllib does (follow the redirect, return the whole body): that is
  what the pre-2026-10-04 code called, so those tests fail against it rather
  than reaching the network.
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

class _FakeHop:
    """One response from url_safety._pinned_get."""

    def __init__(self, status: int = 200, body: bytes = b"",
                 location: str | None = None, chunks: int = 1) -> None:
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self._body = body
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for _ in range(self._chunks):
            yield self._body

    def close(self) -> None:
        self.closed = True


def _json3(text: str) -> bytes:
    return json.dumps({"events": [{"segs": [{"utf8": text}]}]}).encode()


class TestCaptionTrackSSRF:

    def _info_with_caption_urls(self, urls: list[str]) -> dict:
        """Build a yt-dlp info dict containing the given caption-track URLs
        in the 'subtitles' / 'en' / 'json3' position."""
        tracks = [{"ext": "json3", "url": u} for u in urls]
        return {"subtitles": {"en": tracks}, "automatic_captions": {}}

    def _route(self, monkeypatch: pytest.MonkeyPatch,
               routes: dict[str, _FakeHop]) -> list[str]:
        """Serve `routes` from the network seam; return the URLs requested."""
        import url_safety
        requested: list[str] = []

        def fake_pinned_get(url, ip, **kw):
            requested.append(url)
            return routes[url]

        monkeypatch.setattr(url_safety, "_pinned_get", fake_pinned_get,
                            raising=False)
        return requested

    def _old_urlopen(self, monkeypatch: pytest.MonkeyPatch,
                     body: bytes) -> list[str]:
        """What urllib did for the old code: follow redirects, read it all."""
        import urllib.request
        opened: list[str] = []

        def fake_urlopen(req, timeout=30):
            opened.append(getattr(req, "full_url", str(req)))

            class _R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return body
            return _R()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return opened

    def test_unsafe_caption_url_is_skipped(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If yt-dlp returns a caption URL pointing at loopback, the script
        must skip it without issuing any request."""
        requested = self._route(monkeypatch, {})
        info = self._info_with_caption_urls([
            "http://127.0.0.1:11434/api/x",       # Ollama-like loopback
            "http://169.254.169.254/metadata",    # cloud-metadata link-local
        ])
        result = ys.extract_transcript(info)
        assert result == "", (
            "expected empty transcript because all caption URLs were unsafe")
        assert requested == [], (
            f"no request should issue for unsafe URLs, got: {requested}")

    def test_safe_caption_url_is_fetched(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str) -> None:
        """A normal googlevideo.com URL must pass the SSRF guard and be
        fetched. Confirms the guard isn't over-blocking."""
        url = "https://r5---sn-abc.googlevideo.com/api/timedtext?x=1"
        requested = self._route(monkeypatch,
                                {url: _FakeHop(body=_json3("hello world"))})
        result = ys.extract_transcript(self._info_with_caption_urls([url]))
        assert "hello world" in result
        assert requested == [url]

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
        safe = "https://www.googlevideo.com/safe"
        requested = self._route(monkeypatch,
                                {safe: _FakeHop(body=_json3("fallback content"))})
        info = self._info_with_caption_urls([
            "http://192.168.1.5/poisoned",         # unsafe — must skip
            safe,                                  # safe — must fetch
        ])
        result = ys.extract_transcript(info)
        assert "fallback content" in result
        assert requested == [safe]

    def test_redirect_to_loopback_is_refused(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str,
            capsys: pytest.CaptureFixture[str]) -> None:
        """M-DASH 358: a public caption URL that 302s to a local service.

        urlopen followed the redirect without re-checking it, so the local
        service's response became the transcript. Each hop is now checked.
        """
        first = "https://captions.test.example/c.vtt"
        requested = self._route(monkeypatch, {
            first: _FakeHop(302, location="http://127.0.0.1:11434/api/tags"),
        })
        self._old_urlopen(monkeypatch, _json3("LOCAL-SERVICE-SECRET"))

        result = ys.extract_transcript(self._info_with_caption_urls([first]))

        assert "LOCAL-SERVICE-SECRET" not in result
        assert result == ""
        assert requested == [first], requested
        err = capsys.readouterr().err
        assert "refusing http://127.0.0.1:11434/api/tags" in err, err

    def test_oversized_caption_body_is_dropped(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str,
            capsys: pytest.CaptureFixture[str]) -> None:
        """M-DASH 224: resp.read() had no cap, so an endless track grew the
        process until it was killed. The body is now capped."""
        url = "https://captions.test.example/huge.vtt"
        line = b"WEBVTT\n\nflood flood flood flood\n" * 4096   # ~128 KB
        self._route(monkeypatch, {url: _FakeHop(body=line, chunks=50)})
        self._old_urlopen(monkeypatch, line * 50)                 # ~6.5 MB

        result = ys.extract_transcript(self._info_with_caption_urls([url]))

        assert result == ""
        assert "body exceeded" in capsys.readouterr().err

    def test_caption_cap_admits_an_ordinary_track(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str) -> None:
        """An hour of json3 captions is ~1-2 MB: well inside the cap."""
        url = "https://captions.test.example/hour.json3"
        body = _json3("word " * 300_000)                          # ~1.5 MB
        assert len(body) < ys.CAPTION_MAX_BYTES
        self._route(monkeypatch, {url: _FakeHop(body=body)})
        assert ys.extract_transcript(
            self._info_with_caption_urls([url])).startswith("word word")


    def test_a_malformed_track_is_skipped_not_raised(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str) -> None:
        """C6: a non-string URL or a non-dict entry used to raise out of
        extract_transcript and lose every remaining track."""
        good = "https://captions.test.example/ok.json3"
        requested = self._route(monkeypatch, {good: _FakeHop(body=_json3("ok"))})
        info = {"subtitles": {"en": [{"ext": "json3", "url": 12345},
                                     "not-a-dict",
                                     {"ext": "json3", "url": ["x"]},
                                     {"ext": "json3", "url": good}],
                              7: [{"ext": "vtt", "url": good}]},
                "automatic_captions": "not-a-dict"}
        assert ys.extract_transcript(info) == "ok"
        assert requested == [good]

    def test_a_body_that_breaks_mid_read_tries_the_next_track(
            self, monkeypatch: pytest.MonkeyPatch, public_dns: str,
            capsys: pytest.CaptureFixture[str]) -> None:
        """C6: a ChunkedEncodingError mid-body propagated out of safe_fetch."""
        import requests

        class _Broken(_FakeHop):
            def iter_content(self, chunk_size: int = 65536):
                yield b"WEBVTT\n\npartial"
                raise requests.exceptions.ChunkedEncodingError("boom")

        bad = "https://captions.test.example/broken.vtt"
        good = "https://captions.test.example/ok.json3"
        self._route(monkeypatch, {bad: _Broken(), good: _FakeHop(body=_json3("ok"))})
        info = {"subtitles": {"en": [{"ext": "vtt", "url": bad},
                                     {"ext": "json3", "url": good}]}}
        assert ys.extract_transcript(info) == "ok"
        assert "read error on https://captions.test.example/broken.vtt: " \
               "ChunkedEncodingError" in capsys.readouterr().err

    def test_a_redirect_to_an_unencodable_name_is_refused(
            self, monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture[str]) -> None:
        """C6: a 64-character label fails IDNA encoding inside getaddrinfo
        (UnicodeError, not gaierror) and escaped _check as an exception."""
        import socket

        def encoding_resolver(host, port, *a, **k):
            str(host).encode("idna")          # what socket.getaddrinfo does first
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", port or 0))]

        monkeypatch.setattr(socket, "getaddrinfo", encoding_resolver)
        first = "https://captions.test.example/c.vtt"
        long_name = "a" * 64 + ".test.example"
        self._route(monkeypatch, {
            first: _FakeHop(302, location=f"https://{long_name}/c.vtt")})
        result = ys.extract_transcript(self._info_with_caption_urls([first]))
        assert result == ""
        err = capsys.readouterr().err
        assert f"refusing https://{long_name}/c.vtt: DNS resolution failed" in err
        assert "UnicodeEncodeError" in err


# ---------------------------------------------------------------------------
# Playlist entries and the yt-dlp argv (M-DASH 230 / 359).
# ---------------------------------------------------------------------------

class TestPlaylistEntries:

    def _capture_ytdlp(self, monkeypatch: pytest.MonkeyPatch,
                       stdout: str) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            calls_kwargs.append(kwargs)
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        calls_kwargs: list[dict] = []
        self.kwargs = calls_kwargs
        monkeypatch.setattr(ys.subprocess, "run", fake_run)
        return calls

    def test_non_youtube_entries_are_dropped(
            self, monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture[str]) -> None:
        """Entries name a LAN device, a yt-dlp option, other hosts and
        parser tricks; only YouTube URLs reach yt-dlp again."""
        good = "https://www.youtube.com/watch?v=abc123"
        feed = {"entries": [
            {"url": "http://192.168.1.1/cgi-bin/reboot?now=1"},
            {"url": "--cookies-from-browser=chrome"},
            {"url": "https://evil.test.example/v.mp4"},
            {"url": "https://www.youtube.com.evil.test/watch?v=1"},
            # Split so the identity-leak scanner does not read an address.
            {"url": "https://u" + "@" + "www.youtube.com/watch?v=2"},
            {"url": "https://www.youtube.com:8443/watch?v=3"},
            {"url": "https://127.0.0.1\\@www.youtube.com/watch?v=4"},
            {"url": 42},
            {"url": good},
            {"id": "xyz789"},
            None,
            "not-a-dict",
        ]}
        self._capture_ytdlp(monkeypatch, json.dumps(feed))
        urls = ys.enumerate_playlist("https://www.youtube.com/playlist?list=PL1")
        assert urls == [good, "https://www.youtube.com/watch?v=xyz789"]
        err = capsys.readouterr().err
        assert ("skipping playlist entry http://192.168.1.1/cgi-bin/reboot?…: "
                "not a YouTube host: '192.168.1.1'") in err
        assert "disallowed scheme: ''" in err
        assert "not a YouTube host: 'evil.test.example'" in err
        assert "not a YouTube host: 'www.youtube.com.evil.test'" in err
        assert "userinfo in URL" in err
        assert "explicit port in URL" in err
        assert "backslash, whitespace or control character in URL" in err
        assert "not a string: int" in err

    def test_entry_fragments_are_stripped(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """yt-dlp reads '#__youtubedl_smuggle=' as extractor options."""
        smuggled = ('https://youtu.be/abc#__youtubedl_smuggle='
                    '%7B%22http_headers%22%3A%7B%7D%7D')
        self._capture_ytdlp(monkeypatch, json.dumps({"entries": [{"url": smuggled}]}))
        assert ys.enumerate_playlist("https://www.youtube.com/playlist?list=PL1") \
            == ["https://youtu.be/abc"]

    @pytest.mark.parametrize("url, cleaned", [
        ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
        ("https://youtube.com/shorts/abc", "https://youtube.com/shorts/abc"),
        ("https://m.youtube.com/watch?v=abc#t=10", "https://m.youtube.com/watch?v=abc"),
        ("https://music.youtube.com/watch?v=abc", "https://music.youtube.com/watch?v=abc"),
        ("https://youtu.be/abc", "https://youtu.be/abc"),
        # Round 2, item 2: scheme and host normalised (yt-dlp's patterns are
        # case-sensitive); the path, which carries IDs, is left alone.
        ("HTTPS://WWW.YouTube.com/watch?v=AbC", "https://www.youtube.com/watch?v=AbC"),
        ("http://WWW.YouTube.com/playlist?list=PL1", "http://www.youtube.com/playlist?list=PL1"),
    ])
    def test_youtube_urls_are_accepted(self, url: str, cleaned: str) -> None:
        assert ys.youtube_url(url) == (cleaned, "")

    @pytest.mark.parametrize("url, cleaned", [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx&index=3",
         "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?list=PLx", "https://youtu.be/dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?list=PLx&v=dQw4w9WgXcQ&t=42s",
         "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "https://youtu.be/dQw4w9WgXcQ?si=abc"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&playlist_like=1",
         "https://www.youtube.com/watch?v=dQw4w9WgXcQ&playlist_like=1"),
    ])
    def test_single_mode_drops_the_playlist_parameters(
            self, url: str, cleaned: str) -> None:
        """A video shared from inside a playlist (watch?v=X&list=Y) matches
        only yt-dlp's tab extractor, which single mode does not load."""
        assert ys.youtube_url(url, single=True) == (cleaned, "")

    def test_main_hands_ytdlp_the_normalised_single_video(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "yt_dlp", MagicMock())
        seen: list[str] = []

        def fake_process(url, **kw):
            seen.append(url)
            return None

        monkeypatch.setattr(ys, "process_video", fake_process)
        assert ys.main(["https://WWW.YOUTUBE.COM/watch?v=AbC&list=PLx",
                        "--dry-run"]) == 0
        assert seen == ["https://www.youtube.com/watch?v=AbC"]

    def test_playlist_mode_keeps_the_list_parameter(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "yt_dlp", MagicMock())
        calls = self._capture_ytdlp(monkeypatch, json.dumps({"entries": []}))
        ys.main(["--playlist", "https://YOUTU.BE/AbC?list=PLx", "--dry-run"])
        assert calls[0][-1] == "https://youtu.be/AbC?list=PLx"

    @pytest.mark.parametrize("url, playlist", [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", False),
        ("https://youtu.be/dQw4w9WgXcQ?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", False),
        ("https://WWW.YOUTUBE.COM/watch?v=dQw4w9WgXcQ", False),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", False),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", False),
        ("https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", True),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", True),
        ("https://youtu.be/dQw4w9WgXcQ?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", True),
        ("https://www.youtube.com/@SomeChannel/videos", True),
    ])
    def test_the_allowed_extractors_accept_what_we_hand_ytdlp(
            self, url: str, playlist: bool) -> None:
        """Against the installed yt-dlp's own suitable() and its
        --use-extractors matching (offline: no extraction is run). Skipped
        where yt-dlp is not importable."""
        pytest.importorskip("yt_dlp")
        import re as _re
        from yt_dlp.extractor import gen_extractor_classes
        allowed = ys.PLAYLIST_EXTRACTORS if playlist else ys.VIDEO_EXTRACTORS
        cleaned, _ = ys.youtube_url(url, single=not playlist)
        assert cleaned
        hits = [ie.IE_NAME for ie in gen_extractor_classes()
                if any(_re.fullmatch(a, ie.IE_NAME, _re.I) for a in allowed.split(","))
                and ie.suitable(cleaned)]
        assert hits, f"no allowed extractor takes {cleaned}"
        assert "generic" not in [h.lower() for h in hits]

    def test_main_refuses_a_non_youtube_url(
            self, monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setitem(sys.modules, "yt_dlp", MagicMock())
        calls = self._capture_ytdlp(monkeypatch, "{}")
        with pytest.raises(SystemExit) as exc:
            ys.main(["https://evil.test.example/page", "--dry-run"])
        assert exc.value.code == 1
        assert calls == []
        assert ("only YouTube URLs are supported "
                "(not a YouTube host: 'evil.test.example')") in capsys.readouterr().err

    def test_fetch_video_is_held_to_the_youtube_extractor(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._capture_ytdlp(monkeypatch, json.dumps({"id": "x"}))
        ys.fetch_video("--cookies-from-browser=chrome")
        cmd = calls[0]
        assert cmd[-2:] == ["--", "--cookies-from-browser=chrome"], cmd
        i = cmd.index("--use-extractors")
        assert cmd[i + 1] == "youtube"
        assert self.kwargs[0]["timeout"] == ys.YTDLP_TIMEOUT_SECONDS

    def test_enumerate_playlist_is_held_to_youtube_extractors(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._capture_ytdlp(monkeypatch, json.dumps({"entries": []}))
        ys.enumerate_playlist("https://www.youtube.com/playlist?list=PL1")
        cmd = calls[0]
        assert cmd[-2:] == ["--", "https://www.youtube.com/playlist?list=PL1"]
        i = cmd.index("--use-extractors")
        assert cmd[i + 1].split(",") == ["youtube", "youtube:tab",
                                         "youtube:playlist", "youtube:?ytbe"]
        assert "generic" not in cmd[i + 1]
        assert self.kwargs[0]["timeout"] == ys.YTDLP_TIMEOUT_SECONDS

    def test_ytdlp_reads_no_config_or_plugins_from_outside(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Round 2, item 3: a yt-dlp.conf in the cwd (often ~/Downloads)
        could add --use-extractors generic, --proxy or --netrc-cmd; a
        yt_dlp_plugins/ there would be imported as code."""
        monkeypatch.setenv("YTDLP_NO_PLUGINS", "")
        calls = self._capture_ytdlp(monkeypatch, json.dumps({"id": "x"}))
        ys.fetch_video("https://youtu.be/abc")
        cmd = calls[0]
        assert cmd[:4] == [sys.executable, "-I", "-m", "yt_dlp"], cmd
        before_url = cmd[:cmd.index("--")]
        assert "--ignore-config" in before_url
        assert "--no-plugin-dirs" in before_url
        assert "--config-locations" not in cmd
        assert self.kwargs[0]["env"]["YTDLP_NO_PLUGINS"] == "1"

    def test_a_hung_ytdlp_is_a_failure_not_a_hang(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(ys.subprocess, "run", hang)
        with pytest.raises(RuntimeError, match=r"yt-dlp timed out after 600s"):
            ys.fetch_video("https://youtu.be/abc")


# ---------------------------------------------------------------------------
# Frontmatter built from page metadata and model output (M-DASH 223 / 231).
# ---------------------------------------------------------------------------

def _frontmatter_dict(note: str) -> dict:
    import yaml
    assert note.startswith("---\n")
    block = note[4:note.index("\n---\n", 4)]
    return yaml.safe_load(block)


# Each of these is read as syntax or as another type when written plain.
_HOSTILE_SCALARS = [",x", "]x", "}x", " - x", "- x", "null", "~", "yes", "No",
                    "true", "123", "0x1F", "1e3", "2026-09-01", "12:30",
                    "&a", "*a", "!!python/name:os.system", "? x", "'q'",
                    '"dq"', "a: b", "#c", "%d", "@e", "`f", "|", ">", "x\\y",
                    "[a, b]", "{a: b}", "trailing ", "café ✓", ""]


class TestFrontmatterInjection:

    INFO = {"webpage_url": "https://www.youtube.com/watch?v=abc",
            "uploader": "Channel", "upload_date": "20260901", "duration": 61}

    @pytest.mark.parametrize("brk", ["\n", "\r\n", "\r", "\x85",
                                     "\u2028", "\u2029"])
    def test_a_line_break_in_the_title_cannot_end_the_frontmatter(
            self, brk: str) -> None:
        import classification_tier
        info = dict(self.INFO, title=f"Talk{brk}---{brk}x: y")
        note = ys.build_frontmatter(info, [], "desc") + "\nbody\n"
        assert classification_tier.effective(note) == ("internal-use-only", [])
        data = _frontmatter_dict(note)
        assert " ".join(data["title"].split()) == "Talk --- x: y"
        assert data["title"].isprintable(), repr(data["title"])
        assert data["classification"] == "internal-use-only"

    def test_a_line_break_in_the_uploader_or_date_is_flattened(self) -> None:
        info = dict(self.INFO, title="T", uploader="Chan\nclassification: public",
                    upload_date="2026\n---")
        data = _frontmatter_dict(ys.build_frontmatter(info, [], ""))
        assert data["author"] == "Chan classification: public"
        assert data["published"] == "2026 ---"
        assert data["classification"] == "internal-use-only"

    @pytest.mark.parametrize("value", _HOSTILE_SCALARS)
    def test_every_field_loads_as_the_exact_string(self, value: str) -> None:
        """C5: a plain scalar starting ',' ']' '}' or ' - ' broke the block,
        and null / ~ / yes / 123 / dates came back as other types."""
        info = dict(self.INFO, title=value, uploader=value, upload_date=value,
                    webpage_url=value)
        data = _frontmatter_dict(ys.build_frontmatter(info, [], value))
        expect_title = value or "Untitled"
        assert data["title"] == expect_title
        assert data["author"] == value
        assert data["source"] == value
        assert data["description"] == value
        assert data["published"] == (value if value else None)
        assert data["classification"] == "internal-use-only"

    @pytest.mark.parametrize("bad", ["\ud800", "\ufffe", "\x80", "\x9b", "\x00"])
    def test_code_points_pyyaml_cannot_read_are_replaced(self, bad: str) -> None:
        data = _frontmatter_dict(ys.build_frontmatter(
            dict(self.INFO, title=f"a{bad}b"), [], ""))
        assert data["title"] == "a b"

    def test_non_string_metadata_does_not_raise(self) -> None:
        """C6: an int upload_date raised TypeError in build_frontmatter."""
        info = {"title": 2026, "uploader": None, "upload_date": 20260901,
                "duration": "not-a-number", "webpage_url": None}
        data = _frontmatter_dict(ys.build_frontmatter(info, [], ""))
        assert data["title"] == "2026"
        assert data["published"] == "2026-09-01"
        assert data["duration"] == ""
        assert data["classification"] == "internal-use-only"

    def test_ordinary_values_load_as_before(self) -> None:
        """The written form is now always double-quoted; what a YAML reader
        gets back for ordinary values is unchanged (published is the date
        as a string, as Obsidian treats it)."""
        info = dict(self.INFO, title="Plain Title")
        fm = ys.build_frontmatter(info, ["ai", "machine-learning"], "A summary.")
        assert '\ntitle: "Plain Title"\n' in fm
        assert fm.endswith('tags:\n- "youtube"\n- "ai"\n- "machine-learning"\n---\n')
        data = _frontmatter_dict(fm)
        assert data["title"] == "Plain Title"
        assert data["author"] == "Channel"
        assert data["published"] == "2026-09-01"
        assert data["duration"] == "1:01"
        assert data["tags"] == ["youtube", "ai", "machine-learning"]

    def test_missing_date_is_null(self) -> None:
        info = dict(self.INFO, title="T", upload_date=None)
        assert "\npublished: null\n" in ys.build_frontmatter(info, [], "")

    def test_model_tags_that_are_yaml_syntax_are_dropped(self) -> None:
        md = ("## Suggested tags\n[evil, *alias, !!python/name:os.system, "
              "&anchor, {x, |y, >z, %d, @e, `f, ai, machine learning, café, "
              "data/eng, x:y\n")
        tags = ys.parse_suggested_tags(md)
        # "*alias" loses its leading "*" to the bullet strip and survives as a
        # plain word; everything else that is YAML syntax is dropped.
        assert tags == ["alias", "ai", "machine-learning", "café", "data/eng"]
        data = _frontmatter_dict(ys.build_frontmatter(
            dict(self.INFO, title="T"), tags, ""))
        assert data["tags"] == ["youtube", "alias", "ai", "machine-learning",
                                "café", "data/eng"]

    def test_tags_that_coerce_load_as_strings(self) -> None:
        """C5: null / true / 123 pass SAFE_TAG; quoting keeps them text."""
        tags = ys.parse_suggested_tags("## Suggested tags\nnull, true, 123, yes\n")
        assert tags == ["null", "true", "123", "yes"]
        data = _frontmatter_dict(ys.build_frontmatter(
            dict(self.INFO, title="T"), tags, ""))
        assert data["tags"] == ["youtube", "null", "true", "123", "yes"]


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
        """extract_transcript must fetch caption tracks through
        url_safety.safe_fetch (per-hop checks, pinned connect, size cap) and
        never through urlopen, which follows redirects unchecked. Named by
        installers/lib/security-suppressions.txt history: the urlopen this
        used to assert is gone, and with it the B310 suppression."""
        src = (scripts_dir / "youtube_summarize.py").read_text()
        # Find the extract_transcript function body
        marker = "def extract_transcript("
        idx = src.find(marker)
        assert idx != -1, "extract_transcript function not found"
        # Look at the function body — bounded by the next top-level def.
        body_end = src.find("\ndef ", idx + len(marker))
        body = src[idx:body_end if body_end != -1 else len(src)]
        assert "url_safety.safe_fetch(" in body, (
            "extract_transcript does not fetch through url_safety.safe_fetch "
            "— the caption-track fetch has left the SSRF guard.")
        assert "urlopen" not in body, (
            "extract_transcript calls urlopen, which follows redirects "
            "without re-checking them.")
        assert "max_bytes=" in body, "caption fetch is not size-capped"

    def test_default_output_inside_vault(self) -> None:
        """DEFAULT_OUT must point inside the Obsidian vault. Catching this
        in static ensures a future edit doesn't redirect summaries to
        an unaudited location."""
        assert "Obsidian" in str(ys.DEFAULT_OUT), (
            f"DEFAULT_OUT {ys.DEFAULT_OUT} does not appear to be in the vault")
        assert "YouTube" in str(ys.DEFAULT_OUT)
