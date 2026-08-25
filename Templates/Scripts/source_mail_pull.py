#!/usr/bin/env python3
"""
source_mail_pull.py — mail-drop transport for source media.

Pulls authenticated drops out of a dedicated IMAP mailbox and writes them into
the local drop folders the existing watchers already read, so voice notes and
podcast links can reach the vault without iCloud Drive.

Why not iCloud Drive: docs/HANDOFF-ARCHITECTURE.md already rules on this for
meeting handoff -- a cloud-drive sync client is fragile as a transport (sync
races, conflict copies, online-only placeholders that don't hydrate in time),
which is why Tier B exists. The same reasoning applies here, plus iCloud Drive
forces Apple iCloud for Windows onto any non-Mac endpoint. This module is the
same idea as handoff_blob_pull.py with a mailbox as the relay.

    transport (this file)  ->  ~/SourceMedia/<Type>/  ->  existing watcher

The drop-folder contract is preserved exactly, so voice_cleanup.py and
podcast_watch.py keep their behaviour; they
changed only to resolve the folder through source_media.drop_dir, which is
also what this module writes through, so a writer and its reader cannot
disagree about the location.

Security model
--------------
An inbox is *world-writable*: anyone who learns the address can post to it, and
whatever lands here is written to the filesystem and then read by an LLM
(tag_clippings). So nothing is trusted until it authenticates, and the
defences are layered so no single failure is fatal:

 1. Sender allowlist. Weak alone -- From is trivially spoofed -- but it costs
    nothing and stops the noise floor.
 2. HMAC-SHA256 over a canonical string, keyed with SOURCE_MAIL_TOKEN, checked
    with hmac.compare_digest. This is the real control. The token is NEVER
    transmitted: sending it would leave a long-lived secret sitting in Gmail's
    storage, whereas an HMAC proves knowledge of it without disclosing it.
 3. The routing `type` is taken from *inside* the HMAC'd payload, never from
    the Subject line, so an attacker cannot retarget an authentic payload at a
    different pipeline.
 4. A timestamp inside the HMAC'd string bounds replay, and a seen-cache of
    accepted digests (.seen_drops.json under the drop root) closes the window
    entirely: a captured drop cannot be replayed even inside the 7 days.
 5. Filenames are generated here and never taken from the message, which makes
    path traversal structurally impossible rather than filtered.
 6. text/plain preferred, with a size cap. text/html is read only as an
    explicit opt-in fallback (SOURCE_MAIL_ALLOW_HTML_FALLBACK=1) for producers
    that can't guarantee a plain-text part -- some mail clients' silent/
    programmatic send paths compose HTML-only regardless of account settings.
    Off by default: parsing markup from an unauthenticated inbox is attack
    surface a producer that can send plain text doesn't need. When enabled,
    tags are stripped with Python's stdlib html.parser (no external entity/DTD
    resolution -- not an XML parser, so no XXE-shaped bug is possible here),
    and <script>/<style> *contents* are discarded, not just the tags -- a mail
    client never renders that text, so a naive tag-stripper letting it through
    would recover content the sender never intended to be part of the message.

Body format (text/plain), as produced by --emit:

    v: 1
    type: podcast
    ts: 2026-08-09T15:04:05Z
    payload: https://example.com/episode.mp3
    hmac: <hex>

HMAC covers a length-prefixed canonical string; see canonical_string().

Usage:
    source_mail_pull.py --once                     # scheduled drain
    source_mail_pull.py --once --dry-run           # parse and verify only
    source_mail_pull.py --emit podcast "<url>"     # print a valid body

Config, read from the environment / ~/dev/secrets/.env:
    SOURCE_MAIL_USER               the dedicated mailbox address
    SOURCE_MAIL_APP_PASSWORD       app password (or OAuth token, see README)
    SOURCE_MAIL_TOKEN              HMAC key; never sent, only proven
    SOURCE_MAIL_ALLOWED_SENDERS    comma-separated From allowlist
    SOURCE_MAIL_IMAP_HOST          default imap.gmail.com
    SOURCE_MAIL_ALLOW_HTML_FALLBACK  1/true/yes to accept text/html when no
                                     text/plain part exists (default: off).
                                     See security model item 6 above.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import email
import email.utils
import hashlib
import hmac
import imaplib
import json
import logging
import os
import re
import sys
import time
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv

import script_lock
import security_common

SCRIPTS_DIR = Path(__file__).resolve().parent
LOCK_NAME = "source_mail_pull"

load_dotenv(Path.home() / "dev" / "secrets" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("source_mail_pull")

PROTOCOL_VERSION = "1"

# Routing table: authenticated `type` -> drop subfolder. Imported rather than
# redeclared so this module and the watchers cannot drift apart.
from source_media import DIR_NAMES as TYPE_DIRS  # noqa: E402

MAX_BODY_BYTES = 64 * 1024          # a drop is a URL or a dictation snippet
MAX_PAYLOAD_CHARS = 8 * 1024
MAX_MESSAGES_PER_RUN = 50
REPLAY_WINDOW_DAYS = 7

IMAP_HOST = os.environ.get("SOURCE_MAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = 993

# Opt-in, off by default -- see the security model note (item 6) at the top
# of this file before setting it. Read once at import time like the other
# config in this module.
ALLOW_HTML_FALLBACK = os.environ.get(
    "SOURCE_MAIL_ALLOW_HTML_FALLBACK", "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _secret(name: str) -> str:
    """Read a secret from the environment / ~/dev/secrets/.env.

    Deliberately not pretending to consult the platform keystore. Keychain and
    DPAPI would be the stronger store, but security_common only exposes
    get_or_create_hmac_key -- a *generated* trust anchor, not a vault for
    externally-issued secrets like an app password. secret_store provides
    exactly that (env/.env first, then Keychain on macOS / DPAPI on Windows),
    so route through it; the ImportError fallback keeps a partial deploy of
    just this file working the way it always did.
    """
    try:
        from secret_store import get_secret
        return (get_secret(name) or "").strip()
    except ImportError:
        return os.environ.get(name, "").strip()


def default_source_media_root() -> Path:
    """Local drop root. Purely local -- no cloud client involved."""
    import source_media
    return source_media.drop_root()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def acquire_lock() -> object | None:
    """Single-instance lock. See script_lock.py -- notably the "a+" open,
    which is what keeps an overlapping scheduled tick reporting "already
    running" rather than crashing on Windows."""
    return script_lock.acquire(LOCK_NAME, warn=log.warning)


# ---------------------------------------------------------------------------
# Payload authentication
# ---------------------------------------------------------------------------

def canonical_string(msg_type: str, ts: str, payload: str) -> str:
    """The exact bytes the HMAC covers.

    `type` is inside the MAC on purpose: if only the payload were signed, an
    attacker who captured an authentic podcast drop could resend it as `voice`
    and steer it at a different pipeline.

    Fields are length-prefixed rather than merely delimited. Plain delimiters
    are ambiguous when a field can contain the delimiter -- ("T", "x|y") and
    ("T|x", "y") both flatten to the same string, so one MAC would authenticate
    two different field sets. Length prefixes make the split deterministic and
    the encoding injective.
    """
    parts = (msg_type, ts, payload)
    return f"v{PROTOCOL_VERSION}|" + "|".join(f"{len(p)}:{p}" for p in parts)


def compute_hmac(key: str, msg_type: str, ts: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"),
                    canonical_string(msg_type, ts, payload).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def parse_body(text: str) -> dict[str, str]:
    """Parse the simple `key: value` drop format. Unknown keys are ignored."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("v", "type", "ts", "payload", "hmac") and key not in fields:
            fields[key] = value.strip()
    return fields


def _ts_within_window(ts: str) -> tuple[bool, str]:
    try:
        parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False, f"unparseable ts {ts!r}"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    age = (now - parsed).total_seconds()
    if age > REPLAY_WINDOW_DAYS * 86400:
        return False, f"stale ts {ts} ({age / 86400:.1f} days old)"
    if age < -300:  # small allowance for clock skew
        return False, f"ts {ts} is in the future"
    return True, ""


def verify(fields: dict[str, str], *, key: str) -> tuple[bool, str]:
    """Authenticate a parsed drop. Returns (ok, reason)."""
    if fields.get("v") != PROTOCOL_VERSION:
        return False, f"unsupported protocol version {fields.get('v')!r}"
    msg_type = fields.get("type", "")
    if msg_type not in TYPE_DIRS:
        return False, f"unknown type {msg_type!r}"
    ts = fields.get("ts", "")
    ok, reason = _ts_within_window(ts)
    if not ok:
        return False, reason
    payload = fields.get("payload", "")
    if not payload:
        return False, "empty payload"
    if len(payload) > MAX_PAYLOAD_CHARS:
        return False, f"payload exceeds {MAX_PAYLOAD_CHARS} chars"
    supplied = fields.get("hmac", "")
    if not supplied:
        return False, "no hmac"
    expected = compute_hmac(key, msg_type, ts, payload)
    # Constant-time: a byte-wise early return would leak the correct prefix.
    if not hmac.compare_digest(expected, supplied.strip().lower()):
        return False, "hmac mismatch"
    return True, ""


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def sender_allowed(msg: Message, allowed: list[str]) -> tuple[bool, str]:
    raw = msg.get("From", "")
    _, addr = email.utils.parseaddr(raw)
    addr = (addr or "").lower().strip()
    if not allowed:
        return False, "no allowlist configured"
    if addr not in allowed:
        return False, f"sender {addr or raw!r} not in allowlist"
    return True, addr


class _HTMLTextExtractor(HTMLParser):
    """Visible-text extractor for the opt-in HTML fallback below.

    Discards tags and, critically, discards the *contents* of <script> and
    <style> -- not just the tags themselves. A mail client never renders that
    text, so a naive "strip tags, keep everything between them" approach
    would let a sender smuggle content into the recovered payload that never
    appeared in the message as displayed. convert_charrefs=True (the
    HTMLParser default) handles entity decoding, so no separate unescape step
    is needed. This is the stdlib parser -- no external entity/DTD resolution
    happens here at all, since it isn't an XML parser to begin with.
    """

    _BLOCK_TAGS = frozenset({"br", "p", "div", "li", "tr"})
    _SKIPPED_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html_body: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html_body)
    parser.close()
    return parser.text()


def _collect_parts(msg: Message, content_type: str) -> str:
    """Concatenate parts of the given MIME content type, size-capped."""
    chunks: list[str] = []
    total = 0
    for part in msg.walk():
        if part.get_content_type() != content_type:
            continue
        if part.get_filename():          # an attachment, not the body
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        total += len(payload)
        if total > MAX_BODY_BYTES:
            break
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks.append(payload.decode(charset, errors="replace"))
        except LookupError:
            chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def plain_text_body(msg: Message) -> str:
    """Concatenate text/plain parts. text/plain is always preferred; text/html
    is read only when SOURCE_MAIL_ALLOW_HTML_FALLBACK is set and no
    text/plain part exists at all -- see the security model note (item 6) at
    the top of this file. Off by default: parsing markup from an
    unauthenticated inbox is attack surface a producer that can send plain
    text doesn't need.
    """
    text = _collect_parts(msg, "text/plain")
    if text or not ALLOW_HTML_FALLBACK:
        return text
    html_text = _collect_parts(msg, "text/html")
    if not html_text:
        return ""
    log.info("no text/plain part found; falling back to text/html "
             "(SOURCE_MAIL_ALLOW_HTML_FALLBACK=1)")
    return _html_to_text(html_text)


# Deliberately excludes '.' — the only dot in a generated name is the one this
# module appends for the extension. Allowing dots through would let a run of
# them survive into a path component, which is the shape traversal takes.
_SAFE_SLUG = re.compile(r"[^A-Za-z0-9-]+")


def drop_filename(msg_type: str, ts: str, payload: str) -> str:
    """Build the on-disk name ourselves.

    Nothing from the message contributes an untrusted path component -- not the
    subject, not a supplied filename. Path traversal is therefore impossible by
    construction rather than by filtering, which is the difference between a
    guarantee and a guess.
    """
    stamp = _SAFE_SLUG.sub("-", ts)[:32] or "unknown"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{msg_type}-{stamp}-{digest}.txt"


def write_drop(root: Path | None, msg_type: str, ts: str, payload: str,
               *, dry_run: bool) -> Path:
    """Write a verified drop.

    With no explicit root, the destination is resolved by source_media.drop_dir
    — the same function the watcher uses. Deriving it from a root instead would
    let the two disagree whenever a legacy folder is still in play, and the
    failure is silent: drops land somewhere nothing drains.
    """
    if root is None:
        import source_media
        target_dir = source_media.drop_dir(msg_type)
    else:
        target_dir = root / TYPE_DIRS[msg_type]
    name = drop_filename(msg_type, ts, payload)
    dest = (target_dir / name).resolve()
    # Belt and braces: even though the name is generated, assert containment.
    if not str(dest).startswith(str(target_dir.resolve())):
        raise RuntimeError(f"refusing to write outside {target_dir}: {dest}")
    if dry_run:
        return dest
    target_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(payload + "\n", encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Replay seen-cache
# ---------------------------------------------------------------------------
# The ts check bounds replay to REPLAY_WINDOW_DAYS; this closes it outright.
# Keyed on the drop's HMAC digest (unique per (type, ts, payload) under the
# shared token), it rejects a re-send of an already-accepted drop even while
# the timestamp is still fresh. Entries age out one day after the ts check
# would reject them anyway, so the file stays tiny. Writes happen under the
# run's single-instance lock, so plain read/modify/write is race-free.

SEEN_CACHE_NAME = ".seen_drops.json"


def _seen_cache_path(root: Path) -> Path:
    return root / SEEN_CACHE_NAME


def load_seen(root: Path) -> dict[str, float]:
    """digest -> first-seen unix time. Any read problem means an empty cache:
    fail open here is correct — the ts window still bounds replay, and a
    corrupt cache must not wedge the transport."""
    try:
        data = json.loads(_seen_cache_path(root).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def save_seen(root: Path, seen: dict[str, float]) -> None:
    cutoff = time.time() - (REPLAY_WINDOW_DAYS + 1) * 86400
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    path = _seen_cache_path(root)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pruned, indent=0, sort_keys=True) + "\n",
                       encoding="utf-8")
        # restrict_file, not a bare chmod: on Windows os.chmod only toggles
        # the read-only attribute, so a 0o600 call there leaves the replay
        # seen-cache carrying whatever ACL it inherited. restrict_file drops
        # inheritance via icacls and grants only the current user + SYSTEM.
        security_common.restrict_file(tmp)
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not persist seen-cache %s: %s", path, exc)


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def process_mailbox(*, user: str, password: str, key: str,
                    allowed: list[str], root: Path, dry_run: bool,
                    host: str = IMAP_HOST) -> tuple[int, int]:
    """Drain unseen messages. Returns (accepted, rejected)."""
    accepted = rejected = 0
    seen = load_seen(root)
    seen_dirty = False
    conn = imaplib.IMAP4_SSL(host, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            log.error("IMAP search failed: %s", status)
            return 0, 0
        ids = (data[0] or b"").split()
        if not ids:
            log.info("No new messages.")
            return 0, 0
        if len(ids) > MAX_MESSAGES_PER_RUN:
            log.info("%d unseen; taking %d this run", len(ids),
                     MAX_MESSAGES_PER_RUN)
            ids = ids[:MAX_MESSAGES_PER_RUN]

        for num in ids:
            status, raw = conn.fetch(num, "(RFC822)")
            if status != "OK" or not raw or not raw[0]:
                log.error("fetch failed for message %s", num)
                continue
            msg = email.message_from_bytes(raw[0][1])
            subject = (msg.get("Subject") or "")[:80]

            ok, who = sender_allowed(msg, allowed)
            if not ok:
                log.warning("REJECT [%s]: %s", subject, who)
                rejected += 1
                if not dry_run:
                    conn.store(num, "+FLAGS", "\\Seen")
                continue

            fields = parse_body(plain_text_body(msg))
            ok, reason = verify(fields, key=key)
            if not ok:
                # Deliberately terse: do not echo payload content from an
                # unauthenticated message into the log.
                log.warning("REJECT [%s] from %s: %s", subject, who, reason)
                rejected += 1
                if not dry_run:
                    conn.store(num, "+FLAGS", "\\Seen")
                continue

            digest = fields["hmac"].strip().lower()
            if digest in seen:
                log.warning("REJECT [%s] from %s: replay of an "
                            "already-accepted drop", subject, who)
                rejected += 1
                if not dry_run:
                    conn.store(num, "+FLAGS", "\\Seen")
                continue

            dest = write_drop(root, fields["type"], fields["ts"],
                              fields["payload"], dry_run=dry_run)
            log.info("ACCEPT [%s] from %s -> %s%s", subject, who, dest,
                     " (dry-run)" if dry_run else "")
            accepted += 1
            if not dry_run:
                seen[digest] = time.time()
                seen_dirty = True
                conn.store(num, "+FLAGS", "\\Seen")
        return accepted, rejected
    finally:
        if seen_dirty:
            save_seen(root, seen)
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull authenticated source-media drops from a mailbox.")
    p.add_argument("--once", action="store_true",
                   help="drain the mailbox and exit (for scheduled runs)")
    p.add_argument("--dry-run", action="store_true",
                   help="verify and report without writing or marking read")
    p.add_argument("--root", default=None,
                   help=f"drop root (default: {default_source_media_root()})")
    p.add_argument("--emit", nargs=2, metavar=("TYPE", "PAYLOAD"),
                   help="print a valid signed body for TYPE and exit")
    args = p.parse_args(argv)

    key = _secret("SOURCE_MAIL_TOKEN")
    if not key:
        log.error("SOURCE_MAIL_TOKEN is not set (checked keystore and .env)")
        return 1

    if args.emit:
        msg_type, payload = args.emit
        if msg_type not in TYPE_DIRS:
            log.error("type must be one of: %s", ", ".join(sorted(TYPE_DIRS)))
            return 1
        ts = _dt.datetime.now(_dt.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        print(f"v: {PROTOCOL_VERSION}")
        print(f"type: {msg_type}")
        print(f"ts: {ts}")
        print(f"payload: {payload}")
        print(f"hmac: {compute_hmac(key, msg_type, ts, payload)}")
        return 0

    user = _secret("SOURCE_MAIL_USER")
    password = _secret("SOURCE_MAIL_APP_PASSWORD")
    allowed = [a.strip().lower()
               for a in _secret("SOURCE_MAIL_ALLOWED_SENDERS").split(",")
               if a.strip()]
    missing = [n for n, v in (("SOURCE_MAIL_USER", user),
                              ("SOURCE_MAIL_APP_PASSWORD", password),
                              ("SOURCE_MAIL_ALLOWED_SENDERS", allowed)) if not v]
    if missing:
        log.error("missing config: %s", ", ".join(missing))
        return 1

    # None means "resolve per drop kind", which keeps the writer and the
    # watchers on the same folder even when a legacy location is still in use.
    root = Path(args.root).expanduser() if args.root else None

    lock = acquire_lock()
    if lock is None:
        log.info("Another source_mail_pull run is in progress; exiting.")
        return 0
    try:
        accepted, rejected = process_mailbox(
            user=user, password=password, key=key, allowed=allowed,
            root=root, dry_run=args.dry_run)
        log.info("Done — %d accepted, %d rejected.", accepted, rejected)
        return 0
    except imaplib.IMAP4.error as e:
        log.error("IMAP error: %s", e)
        return 1
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
