"""
test_source_mail_pull.py — the mail-drop transport.

An inbox is world-writable: anyone who learns the address can post to it, and
whatever survives verification is written to the filesystem and later read by
an LLM (tag_clippings). So the tests that matter here are the ones that pin
*rejection*. A bug that accepts too much is silent -- the drop looks ordinary
once it is on disk.

No network: IMAP is never contacted. Message objects are constructed in
memory, which is enough to cover parsing, authentication and routing.
"""
from __future__ import annotations

import datetime as dt
import email
import os
from email.message import EmailMessage
from pathlib import Path

import pytest

import source_mail_pull as smp

KEY = "test-key-not-the-real-one"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)\
        .isoformat().replace("+00:00", "Z")


def _signed(msg_type: str = "podcast", payload: str = "https://example.com/a.mp3",
            ts: str | None = None, key: str = KEY) -> dict[str, str]:
    ts = ts or _now()
    return {
        "v": "1",
        "type": msg_type,
        "ts": ts,
        "payload": payload,
        "hmac": smp.compute_hmac(key, msg_type, ts, payload),
    }


# ---------------------------------------------------------------------------
# HMAC
# ---------------------------------------------------------------------------

class TestHmac:

    def test_round_trip_verifies(self) -> None:
        assert smp.verify(_signed(), key=KEY) == (True, "")

    def test_wrong_key_is_rejected(self) -> None:
        ok, reason = smp.verify(_signed(key="a-different-key"), key=KEY)
        assert not ok and reason == "hmac mismatch"

    def test_payload_tampering_is_rejected(self) -> None:
        fields = _signed(payload="https://example.com/good.mp3")
        fields["payload"] = "https://evil.test/bad.mp3"
        assert smp.verify(fields, key=KEY)[0] is False

    def test_type_cannot_be_retargeted(self) -> None:
        """The type is inside the MAC precisely so a captured podcast drop
        cannot be replayed at the voice pipeline."""
        fields = _signed(msg_type="podcast")
        fields["type"] = "voice"
        assert smp.verify(fields, key=KEY)[0] is False

    def test_timestamp_tampering_is_rejected(self) -> None:
        # Must differ from the signed value; regenerating "now" can land in
        # the same second, leaving the MAC legitimately valid.
        fields = _signed()
        moved = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
        fields["ts"] = moved.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        assert smp.verify(fields, key=KEY)[0] is False

    def test_canonical_string_is_unambiguous(self) -> None:
        """Field boundaries must not be forgeable by moving the separator --
        two different splits must not produce the same signed string."""
        a = smp.canonical_string("podcast", "T", "x|y")
        b = smp.canonical_string("podcast", "T|x", "y")
        assert a != b


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------

class TestVerifyRejects:

    def test_missing_hmac(self) -> None:
        fields = _signed()
        del fields["hmac"]
        assert smp.verify(fields, key=KEY) == (False, "no hmac")

    def test_unknown_type(self) -> None:
        fields = _signed()
        fields["type"] = "exfiltrate"
        assert "unknown type" in smp.verify(fields, key=KEY)[1]

    def test_unsupported_version(self) -> None:
        fields = _signed()
        fields["v"] = "99"
        assert "unsupported protocol version" in smp.verify(fields, key=KEY)[1]

    def test_empty_payload(self) -> None:
        fields = _signed(payload="")
        assert smp.verify(fields, key=KEY)[1] in ("empty payload", "hmac mismatch")

    def test_stale_message_is_rejected(self) -> None:
        """Bounds replay of a captured message."""
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=smp.REPLAY_WINDOW_DAYS + 1))
        ts = old.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        assert "stale" in smp.verify(_signed(ts=ts), key=KEY)[1]

    def test_future_message_is_rejected(self) -> None:
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        ts = future.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        assert "future" in smp.verify(_signed(ts=ts), key=KEY)[1]

    def test_unparseable_timestamp(self) -> None:
        assert "unparseable" in smp.verify(_signed(ts="not-a-date"), key=KEY)[1]

    def test_oversized_payload(self) -> None:
        fields = _signed(payload="x" * (smp.MAX_PAYLOAD_CHARS + 1))
        assert "exceeds" in smp.verify(fields, key=KEY)[1]


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------

class TestParseBody:

    def test_parses_the_documented_format(self) -> None:
        fields = smp.parse_body(
            "v: 1\ntype: voice\nts: 2026-01-01T00:00:00Z\n"
            "payload: https://example.com\nhmac: abc")
        assert fields["type"] == "voice"
        assert fields["payload"] == "https://example.com"

    def test_first_occurrence_wins(self) -> None:
        """An appended duplicate must not override a signed value. Taking the
        last occurrence would let anyone extend a captured message with their
        own payload line while the original MAC still covered the first."""
        fields = smp.parse_body(
            "type: podcast\npayload: https://good.example\n"
            "payload: https://evil.test\n")
        assert fields["payload"] == "https://good.example"

    def test_ignores_unknown_keys_and_comments(self) -> None:
        fields = smp.parse_body("# a comment\nx-injected: whatever\ntype: voice\n")
        assert fields == {"type": "voice"}

    def test_tolerates_signature_block_noise(self) -> None:
        """Mail clients append signatures and quoted text; those must not
        break parsing of the fields above them."""
        fields = smp.parse_body(
            "type: voice\npayload: https://example.com\n\n--\nSent from my iPhone\n")
        assert fields["payload"] == "https://example.com"


# ---------------------------------------------------------------------------
# Sender allowlist
# ---------------------------------------------------------------------------

class TestSenderAllowed:

    def _msg(self, from_header: str) -> email.message.Message:
        m = EmailMessage()
        m["From"] = from_header
        m.set_content("x")
        return m

    def test_allows_a_listed_address(self) -> None:
        ok, who = smp.sender_allowed(self._msg("Rich <me@example.com>"),
                                     ["me@example.com"])
        assert ok and who == "me@example.com"

    def test_rejects_an_unlisted_address(self) -> None:
        ok, _ = smp.sender_allowed(self._msg("evil@attacker.test"),
                                   ["me@example.com"])
        assert ok is False

    def test_display_name_spoof_does_not_help(self) -> None:
        """A display name that looks like an allowed address must not pass --
        only the parsed addr-spec counts."""
        ok, _ = smp.sender_allowed(
            self._msg('"me@example.com" <evil@attacker.test>'),
            ["me@example.com"])
        assert ok is False

    def test_empty_allowlist_rejects_everything(self) -> None:
        """Fail closed: a misconfigured allowlist must not mean 'allow all'."""
        ok, _ = smp.sender_allowed(self._msg("me@example.com"), [])
        assert ok is False


# ---------------------------------------------------------------------------
# MIME handling
# ---------------------------------------------------------------------------

class TestPlainTextBody:

    def test_reads_plain_text(self) -> None:
        m = EmailMessage()
        m.set_content("type: voice\n")
        assert "type: voice" in smp.plain_text_body(m)

    def test_ignores_html_alternative(self) -> None:
        """HTML is ignored rather than parsed -- recovering a payload from
        hostile markup is a needless attack surface."""
        m = EmailMessage()
        m.set_content("type: voice\npayload: https://good.example\n")
        m.add_alternative("<p>payload: https://evil.test</p>", subtype="html")
        body = smp.plain_text_body(m)
        assert "good.example" in body
        assert "evil.test" not in body

    def test_ignores_attached_text_files(self) -> None:
        m = EmailMessage()
        m.set_content("type: voice\n")
        m.add_attachment(b"payload: https://evil.test\n", maintype="text",
                         subtype="plain", filename="drop.txt")
        assert "evil.test" not in smp.plain_text_body(m)


class TestHtmlFallback:
    """SOURCE_MAIL_ALLOW_HTML_FALLBACK: opt-in, off by default. Some mail
    clients' silent/programmatic send paths compose HTML-only regardless of
    account settings -- this is the escape hatch for that, gated behind an
    explicit setting so the default posture for anyone else who clones this
    repo stays "ignore HTML outright"."""

    def test_html_only_still_rejected_when_fallback_off(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(smp, "ALLOW_HTML_FALLBACK", False)
        m = EmailMessage()
        m.set_content("<p>type: voice</p>", subtype="html")
        assert smp.plain_text_body(m) == ""

    def test_html_only_recovered_when_fallback_on(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(smp, "ALLOW_HTML_FALLBACK", True)
        m = EmailMessage()
        m.set_content("<html><body>type: voice<br>payload: "
                      "https://good.example</body></html>", subtype="html")
        body = smp.plain_text_body(m)
        assert "type: voice" in body
        assert "good.example" in body

    def test_plain_text_still_preferred_when_both_present(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """multipart/alternative with both parts: text/plain wins regardless
        of the fallback setting -- the fallback must never even be consulted
        when a text/plain part exists."""
        monkeypatch.setattr(smp, "ALLOW_HTML_FALLBACK", True)
        m = EmailMessage()
        m.set_content("payload: https://good.example\n")
        m.add_alternative("<p>payload: https://evil.test</p>", subtype="html")
        body = smp.plain_text_body(m)
        assert "good.example" in body
        assert "evil.test" not in body

    def test_script_and_style_contents_are_not_recovered(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mail client never renders <script>/<style> contents, so a naive
        tag-stripper letting that text through would recover content that
        never appeared in the message as displayed."""
        monkeypatch.setattr(smp, "ALLOW_HTML_FALLBACK", True)
        m = EmailMessage()
        m.set_content(
            "<html><head><style>should-not-appear-style</style></head>"
            "<body><script>should_not_appear_script();</script>"
            "type: voice<br>payload: https://good.example</body></html>",
            subtype="html")
        body = smp.plain_text_body(m)
        assert "good.example" in body
        assert "should-not-appear" not in body
        assert "should_not_appear" not in body

    def test_end_to_end_verify_accepts_html_fallback_drop(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Confirms the fallback interacts correctly with the full pipeline,
        not just plain_text_body in isolation -- HMAC verification still
        gates everything regardless of which MIME part carried the text."""
        monkeypatch.setattr(smp, "ALLOW_HTML_FALLBACK", True)
        fields = _signed(msg_type="voice", payload="hello from html")
        body_text = (f"v: {fields['v']}\ntype: {fields['type']}\n"
                     f"ts: {fields['ts']}\npayload: {fields['payload']}\n"
                     f"hmac: {fields['hmac']}\n")
        m = EmailMessage()
        m.set_content(
            "<html><body>" + body_text.replace("\n", "<br>") + "</body></html>",
            subtype="html")
        parsed = smp.parse_body(smp.plain_text_body(m))
        ok, reason = smp.verify(parsed, key=KEY)
        assert ok, reason


# ---------------------------------------------------------------------------
# Filesystem safety
# ---------------------------------------------------------------------------

class TestDropFilename:

    @pytest.mark.parametrize("payload", [
        "../../.obsidian/plugins/evil/main.js",
        "..\\..\\..\\windows\\system32\\evil.ps1",
        "/etc/passwd",
        "a" * 500,
    ])
    def test_payload_never_reaches_the_filename(self, payload: str) -> None:
        """Names are generated, not derived from message content, so traversal
        is impossible by construction rather than by filtering."""
        name = smp.drop_filename("podcast", "2026-01-01T00:00:00Z", payload)
        assert "/" not in name and "\\" not in name
        assert ".." not in name
        assert name.endswith(".txt")

    def test_timestamp_is_slugified(self) -> None:
        name = smp.drop_filename("voice", "../../evil", "x")
        assert "/" not in name and ".." not in name

    def test_distinct_payloads_get_distinct_names(self) -> None:
        ts = "2026-01-01T00:00:00Z"
        a = smp.drop_filename("voice", ts, "https://a.example")
        b = smp.drop_filename("voice", ts, "https://b.example")
        assert a != b


class TestWriteDrop:

    def test_writes_into_the_typed_subfolder(self, tmp_path: Path) -> None:
        dest = smp.write_drop(tmp_path, "podcast", "2026-01-01T00:00:00Z",
                              "https://example.com/a.mp3", dry_run=False)
        assert dest.parent.name == "PodcastInput"
        assert dest.read_text(encoding="utf-8").strip() == "https://example.com/a.mp3"

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        dest = smp.write_drop(tmp_path, "voice", "2026-01-01T00:00:00Z",
                              "https://example.com", dry_run=True)
        assert not dest.exists()

    def test_every_type_maps_to_a_watched_folder(self) -> None:
        """Each routing target must be a folder some watcher actually reads;
        a typo here would silently drop payloads into a folder nobody drains."""
        assert set(smp.TYPE_DIRS.values()) == {"VoiceInput", "PodcastInput"}


# ---------------------------------------------------------------------------
# Replay seen-cache
# ---------------------------------------------------------------------------
# The ts check bounds replay to a window; the seen-cache closes it. The
# attack it pins: an attacker (or a mail loop) re-delivers a byte-identical,
# correctly signed drop while its timestamp is still fresh.

class _FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for process_mailbox: serves the same
    raw messages as UNSEEN on every connect, like a re-delivered mail."""

    def __init__(self, raw_messages: list[bytes]):
        self._raw = raw_messages
        self.stored: list[tuple[bytes, str]] = []
        self.timeout = None

    def __call__(self, host, port, timeout=None):
        # `timeout` is required, not merely tolerated: a double that silently
        # accepted **kwargs would keep passing if the production call ever
        # lost the argument again, and losing it is what cost fourteen hours
        # of runs on 2026-09-15. Asserted rather than defaulted.
        assert timeout, "IMAP4_SSL must be constructed with a socket timeout"
        self.timeout = timeout
        return self

    def login(self, user, password): return "OK", []
    def select(self, box): return "OK", []

    def search(self, charset, criterion):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self._raw)))
        return "OK", [ids]

    def fetch(self, num, spec):
        idx = int(num) - 1
        if spec == "(RFC822.SIZE)":
            # Report the real length, as a server would, so the size gate is
            # exercised against the same bytes the RFC822 fetch would return.
            self.size_probes = getattr(self, "size_probes", 0) + 1
            return "OK", [b"%d (RFC822.SIZE %d)" % (idx + 1, len(self._raw[idx]))]
        self.full_fetches = getattr(self, "full_fetches", 0) + 1
        return "OK", [(b"header", self._raw[idx])]

    def store(self, num, op, flags):
        self.stored.append((num, flags))
        return "OK", []

    def close(self): pass
    def logout(self): pass


def _raw_drop(fields: dict[str, str], sender: str = "phone@example.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = "voice drop"
    msg.set_content("\n".join(f"{k}: {v}" for k, v in fields.items()))
    return msg.as_bytes()


class TestHangGuards:
    """The 2026-09-15 stall: one stuck socket suppressed fourteen hours of runs.

    A run reached "No new messages", returned into its own finally, and blocked
    in conn.close()/conn.logout() on a connection the server had silently
    stopped answering. The teardown was already wrapped in
    `except Exception: pass`, which cannot help -- a hang is not an exception.
    Because launchd will not start a second instance of a StartInterval job
    while the first is alive, the 5-minute cadence stopped dead and the process
    sat in that read for 14h36m.

    Two guards, tested separately because they cover different things: the
    socket timeout fixes the cause that was observed, and the run deadline
    bounds every cause that was not.
    """

    def test_the_connection_is_opened_with_a_timeout(self, monkeypatch) -> None:
        """The actual regression. Without `timeout=`, imaplib blocks forever on
        every operation including teardown, so this asserts the argument is
        passed rather than that some timeout exists somewhere."""
        seen = {}

        class _Probe:
            def __init__(self, host, port, timeout=None):
                seen["host"], seen["port"], seen["timeout"] = host, port, timeout
                raise RuntimeError("stop here; only the constructor matters")

        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", _Probe)
        with pytest.raises(RuntimeError):
            smp.process_mailbox(user="u", password="p", key="k", allowed=[],
                                root=Path("/tmp"), dry_run=True)
        assert seen["timeout"] == smp.IMAP_TIMEOUT
        assert seen["timeout"] is not None and seen["timeout"] > 0

    def test_the_timeout_is_shorter_than_the_run_deadline(self) -> None:
        """Ordering matters: a socket that times out should surface as an IMAP
        error with a useful message, not get cut off mid-read by the alarm."""
        assert smp.IMAP_TIMEOUT < smp.RUN_DEADLINE_SEC

    def test_the_run_deadline_is_shorter_than_the_schedule(self) -> None:
        """The invariant the outage violated: a run must never outlive its own
        interval, or it owns the slot its successor needed. StartInterval for
        this job is 300s."""
        assert smp.RUN_DEADLINE_SEC < 300

    def test_the_deadline_fires_and_names_the_consequence(self) -> None:
        import time
        assert smp._arm_deadline(1) is True
        try:
            with pytest.raises(smp.RunDeadlineExceeded) as exc:
                time.sleep(5)
        finally:
            smp._disarm_deadline()
        # The message has to say why exiting is the right move, or the next
        # person reads it as the job giving up on its own work.
        assert "suppressed" in str(exc.value)

    def test_disarming_stops_it_firing_later(self) -> None:
        """A disarmed alarm going off mid-teardown would turn a clean run into
        a spurious failure."""
        import time
        smp._arm_deadline(1)
        smp._disarm_deadline()
        time.sleep(1.5)          # would raise if still armed

    def test_a_socket_timeout_is_reported_not_raised(self, monkeypatch,
                                                     tmp_path) -> None:
        """TimeoutError is an OSError, and main() has to catch it: before the
        timeout existed there was nothing to catch, so the handler did not
        cover it and a raise would surface as a traceback instead of a line."""
        def _boom(*_a, **_k):
            raise TimeoutError("timed out")

        monkeypatch.setattr(smp, "process_mailbox", _boom)
        monkeypatch.setattr(smp, "_secret", lambda name: (
            "tok" if name == "SOURCE_MAIL_TOKEN" else
            "a@example.com" if name == "SOURCE_MAIL_ALLOWED_SENDERS" else "x"))

        class _Lock:
            def close(self): pass

        monkeypatch.setattr(smp, "acquire_lock", lambda: _Lock())
        assert smp.main(["--once", "--root", str(tmp_path)]) == 1


class TestReplaySeenCache:

    def _pull(self, fake, root):
        return smp.process_mailbox(
            user="u", password="p", key=KEY,
            allowed=["phone@example.com"], root=root, dry_run=False)

    def test_second_delivery_of_same_drop_is_rejected(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = _FakeIMAP([_raw_drop(_signed())])
        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", fake)
        a1, r1 = self._pull(fake, tmp_path)
        assert (a1, r1) == (1, 0)
        # same message shows up again (fresh ts window, identical bytes)
        a2, r2 = self._pull(fake, tmp_path)
        assert (a2, r2) == (0, 1)
        drops = [p for p in tmp_path.rglob("*") if p.is_file()
                 and p.name != smp.SEEN_CACHE_NAME
                 and not p.name.endswith(".tmp")]
        assert len(drops) == 1, drops

    def test_distinct_drops_both_accepted(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = _FakeIMAP([_raw_drop(_signed(payload="https://example.com/a.mp3")),
                          _raw_drop(_signed(payload="https://example.com/b.mp3"))])
        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", fake)
        a, r = self._pull(fake, tmp_path)
        assert (a, r) == (2, 0)

    def test_dry_run_does_not_record(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = _FakeIMAP([_raw_drop(_signed())])
        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", fake)
        a, r = smp.process_mailbox(
            user="u", password="p", key=KEY,
            allowed=["phone@example.com"], root=tmp_path, dry_run=True)
        assert (a, r) == (1, 0)
        assert not (tmp_path / smp.SEEN_CACHE_NAME).exists()

    def test_cache_prunes_expired_entries(self, tmp_path: Path) -> None:
        import time as _time
        old = _time.time() - (smp.REPLAY_WINDOW_DAYS + 2) * 86400
        smp.save_seen(tmp_path, {"deadbeef": old, "cafef00d": _time.time()})
        assert set(smp.load_seen(tmp_path)) == {"cafef00d"}

    def test_corrupt_cache_fails_open(self, tmp_path: Path) -> None:
        (tmp_path / smp.SEEN_CACHE_NAME).write_text("{not json", encoding="utf-8")
        assert smp.load_seen(tmp_path) == {}

    def test_cache_file_is_owner_only(self, tmp_path: Path) -> None:
        smp.save_seen(tmp_path, {"deadbeef": 1.0})
        # save_seen goes through security_common.restrict_file, which uses
        # icacls on Windows -- inheritance dropped, current user + SYSTEM
        # only. stat() still reports 0o666 there because Windows has no POSIX
        # mode to report, so asserting one tests the OS rather than our code.
        # Verified on Windows 11 ARM64 2026-08-25: the ACL is applied
        # correctly while this assertion fails. Same treatment as
        # test_secret_store.py::test_plainfile_roundtrip_and_mode.
        if os.name == "nt":
            pytest.skip("POSIX file modes are not represented on Windows; "
                        "restrict_file's icacls path is covered separately")
        mode = (tmp_path / smp.SEEN_CACHE_NAME).stat().st_mode & 0o777
        assert mode == 0o600


class TestMessageSizeGate:
    """A message is sized before it is downloaded (M-DASH, CWE-770).

    The raw RFC822 fetch used to happen before the sender allowlist or the
    HMAC were checked, so anyone who learned the intake address could push
    provider-maximum messages through the parser every run. These assert the
    oversized message is never downloaded at all, not merely rejected after.
    """

    def _pull(self, fake, root, monkeypatch):
        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", fake)
        return smp.process_mailbox(
            user="u", password="p", key=KEY,
            allowed=["phone@example.com"], root=root, dry_run=False)

    def test_oversized_message_is_never_downloaded(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        huge = _raw_drop(_signed()) + b"X" * (smp.MAX_RAW_MESSAGE_BYTES + 1)
        fake = _FakeIMAP([huge])
        accepted, rejected = self._pull(fake, tmp_path, monkeypatch)
        assert (accepted, rejected) == (0, 1)
        assert getattr(fake, "full_fetches", 0) == 0, (
            "the oversized message body was downloaded before being rejected")
        assert fake.stored == [(b"1", "\\Seen")], (
            "a rejected oversized drop should be marked Seen so it is not "
            "re-probed every run")

    def test_normal_drop_still_goes_through(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = _FakeIMAP([_raw_drop(_signed())])
        accepted, rejected = self._pull(fake, tmp_path, monkeypatch)
        assert (accepted, rejected) == (1, 0)
        assert fake.size_probes == 1 and fake.full_fetches == 1

    def test_unsizeable_message_fails_closed(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = _FakeIMAP([_raw_drop(_signed())])
        real = fake.fetch

        def no_size(num, spec):
            if spec == "(RFC822.SIZE)":
                return "OK", [b"1 (FLAGS ())"]
            return real(num, spec)

        fake.fetch = no_size
        accepted, rejected = self._pull(fake, tmp_path, monkeypatch)
        assert (accepted, rejected) == (0, 1)
        assert getattr(fake, "full_fetches", 0) == 0

    @pytest.mark.parametrize("data,expected", [
        ([b"7 (RFC822.SIZE 4321)"], 4321),
        ([(b"7 (RFC822.SIZE 99 FLAGS (\\Seen))", b"")], 99),
        ([b"7 (FLAGS ())"], None),
        ([], None),
        (None, None),
    ])
    def test_size_parser(self, data, expected) -> None:
        assert smp._rfc822_size(data) == expected


class TestHostileMessageCannotWedgeIntake:
    """Adversarial review 2026-09-25: parsing ran before the sender check and
    a parse crash was uncaught, so one 2 KB email from anyone crashed every
    run and blocked every signed drop behind it."""

    @pytest.mark.parametrize("raw", [
        b"From: " + b"a:" * 1000 + b"\r\nSubject: x\r\n\r\nbody\r\n",
        b"Content-Type: multipart/mixed; boundary=b0\r\n\r\n" + b"".join(
            b"--b%d\r\nContent-Type: multipart/mixed; boundary=b%d\r\n\r\n" % (i, i + 1)
            for i in range(3000)),
    ], ids=["nested-address-groups", "deep-multipart"])
    def test_unparseable_message_is_rejected_marked_seen_and_intake_continues(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes) -> None:
        fake = _FakeIMAP([raw, _raw_drop(_signed())])
        monkeypatch.setattr(smp.imaplib, "IMAP4_SSL", fake)
        a, r = smp.process_mailbox(
            user="u", password="p", key=KEY,
            allowed=["phone@example.com"], root=tmp_path, dry_run=False)
        assert (a, r) == (1, 1), "the signed drop behind the hostile message must land"
        assert (b"1", "\\Seen") in fake.stored, "hostile message must be marked Seen"
