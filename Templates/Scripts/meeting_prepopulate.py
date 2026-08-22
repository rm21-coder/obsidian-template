#!/usr/bin/env python3
"""meeting_prepopulate.py — Generate Obsidian meeting placeholders + People
stubs from a producer agent's weekly schedule handoff JSON (e.g. a
Microsoft 365 / Power Automate flow — see docs/Meeting-Pre-Population.md).

Docs: docs/Meeting-Pre-Population.md — feature overview, configuration,
      and the JSON handoff contract a producer must implement.

Triggered by launchd `WatchPaths` on the local handoff-drop folder (populated
by handoff_blob_pull.py — see docs/HANDOFF-ARCHITECTURE.md and
docs/Azure-Blob-Handoff-Relay.md — or any other drop-folder relay), plus a
Sunday 21:00 belt-and-suspenders timer.

Manual usage:
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/meeting_prepopulate.py                   # process all .ready
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/meeting_prepopulate.py --dry-run
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/meeting_prepopulate.py --handoff <path>
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/meeting_prepopulate.py --no-move --verbose
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

# Cross-platform: force UTF-8 on stdout/stderr so non-ASCII meeting subjects or
# attendee names can't crash logging on Windows' legacy cp1252 console. No-op
# on macOS/Linux.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# Pluggable handoff ingestion (contract vs transport). Ensure this script's own
# directory is importable so the sibling module resolves under any working dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_lock  # noqa: E402  (needs the path insert above)
import handoff_source as hs  # noqa: E402

# ============================================================
# Paths and constants
# ============================================================

def _path_env(env_key: str, default: Path) -> Path:
    """Allow overrides via env vars for testability (e.g., sandbox runs)."""
    v = os.environ.get(env_key)
    return Path(v) if v else default


HOME = Path.home()
VAULT = _path_env('MEETING_PREPOP_VAULT', HOME / 'Obsidian')
PEOPLE_DIR = _path_env('MEETING_PREPOP_PEOPLE_DIR', VAULT / 'People')
# Sub-folder for low-confidence stubs (e.g., external attendees whose only
# identifier was an email or an unsplit display name). Quarantined here so
# they don't pollute the main People/ list but stay wikilink-resolvable.
PEOPLE_UNRESOLVED_DIR = PEOPLE_DIR / '_Unresolved'
MEETINGS_DIR = _path_env('MEETING_PREPOP_MEETINGS_DIR', VAULT / 'Meetings')
MEETINGS_SERIES_DIR = MEETINGS_DIR / 'Series'

SCRIPTS_DIR = _path_env(
    'MEETING_PREPOP_SCRIPTS_DIR',
    HOME / 'Obsidian' / 'Templates' / 'Scripts')

# Local handoff-drop folder. Populated by handoff_blob_pull.py (the Azure
# Blob Tier B relay) or any other drop-folder relay (rsync, SFTP, manual
# import) - see docs/HANDOFF-ARCHITECTURE.md and
# docs/Azure-Blob-Handoff-Relay.md. Override with MEETING_PREPOP_HANDOFF_DIR.
# ~/MeetingIngest rather than a folder under the vault: the drop folder sits OUTSIDE the vault on purpose: raw handoff JSON is
# ingest staging, not content, and a folder under Templates/Scripts/ would be
# carried to every device by Obsidian Sync and walked by every vault scan.
HANDOFF_DIR = _path_env('MEETING_PREPOP_HANDOFF_DIR',
                        Path.home() / 'MeetingIngest')
PROCESSED_SUBDIR = '_processed'

LOCK_DIR = SCRIPTS_DIR / '.locks'
LOG_DIR = _path_env('MEETING_PREPOP_LOG_DIR', SCRIPTS_DIR / 'logs')
LOG_FILE = LOG_DIR / 'meeting_prepopulate.log'

DEFAULT_TZ = 'America/New_York'

# Supported schema versions
SUPPORTED_SCHEMA_VERSIONS = {1}

# Fence markers (this pipeline's; meeting_prep.py owns its own)
FENCE_AGENDA_START = '<!-- meeting-prepopulate:agenda:start -->'
FENCE_AGENDA_END = '<!-- meeting-prepopulate:agenda:end -->'
FENCE_PREP_START = '<!-- meeting-prepopulate:prep:start -->'
FENCE_PREP_END = '<!-- meeting-prepopulate:prep:end -->'
FENCE_INVITE_START = '<!-- meeting-prepopulate:invite:start -->'
FENCE_INVITE_END = '<!-- meeting-prepopulate:invite:end -->'

# Classification thresholds (Pre-Pop Spec §5.2)
GROUP_MIN = 2
GROUP_MAX = 9
BROADCAST_MIN = 10

# Personal email domains (for stub Email-Personal vs Email-Work routing)
PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com',
    'icloud.com', 'me.com', 'aol.com', 'protonmail.com',
    'msn.com', 'comcast.net', 'verizon.net', 'live.com',
    'mac.com', 'sbcglobal.net', 'cox.net', 'att.net',
}

# Group/office mailbox heuristics (Handoff Contract §1a; defense-in-depth)
GROUP_MAILBOX_DOMAIN_RE = re.compile(r'@exchange\.[a-z0-9-]+\.[a-z]+$', re.I)
GROUP_MAILBOX_DISPLAY_PREFIXES = ('grp-', 'dg-', 'dl-', 'org-')
GROUP_MAILBOX_DIRECTORY_TYPES = {
    'DistributionList', 'MailUniversalDistributionGroup',
    'MailUniversalSecurityGroup', 'RoomMailbox', 'EquipmentMailbox',
}

# Video credential patterns (Pre-Pop Spec §15)
VIDEO_PATTERNS = [
    ('zoom', re.compile(r'https?://[a-z0-9.-]*zoom\.us/\S+', re.I)),
    ('zoom', re.compile(r'Meeting ID:\s*[\d\s]+', re.I)),
    ('zoom', re.compile(r'Passcode:\s*\S+', re.I)),
    ('teams', re.compile(
        r'https?://teams\.microsoft\.com/l/meetup-join/\S+', re.I)),
    ('teams', re.compile(r'Conference ID:\s*[\d\s]+', re.I)),
    ('webex', re.compile(r'https?://[a-z0-9.-]*\.webex\.com/\S+', re.I)),
    ('meet', re.compile(r'https?://meet\.google\.com/\S+', re.I)),
]

# Email regex (RFC 5322 simplified)
EMAIL_RE = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')

# Cleanliness regexes for name normalization (Pre-Pop Spec §8.2)
PAREN_STRIP_RE = re.compile(r'\s*\([^)]*\)\s*')
EMBEDDED_EMAIL_STRIP_RE = re.compile(r'\s*<[^>]*@[^>]*>\s*')
CREDENTIAL_SUFFIX_RE = re.compile(
    r',\s*(MD|PhD|Ph\.D\.|MBA|RN|MPH|DO|DDS|JD|EdD|MS|MA)'
    r'(\s+(MD|PhD|Ph\.D\.|MBA|RN|MPH|DO|DDS|JD|EdD|MS|MA))*\s*$',
    re.I,
)

# Filename time prefix (also enforced by meeting_prep.py)
FILENAME_TIME_FMT = '%Y-%m-%d %H%M'


# ============================================================
# Logging
# ============================================================

log = logging.getLogger('meeting_prepopulate')


def setup_logging(verbose: bool, dry_run: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(
        '%(asctime)s %(levelname)s [%(funcName)s] %(message)s')
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log.info('---- run start (dry_run=%s, verbose=%s) ----',
             dry_run, verbose)


# ============================================================
# Concurrency: single-instance lock
# ============================================================

def acquire_lock() -> 'object':
    """Single-instance guard; logs and exits 0 if another run holds it.

    Lock lives under SCRIPTS_DIR, which honours MEETING_PREPOP_SCRIPTS_DIR, so
    a relocated install keeps its own lock instead of sharing the default.
    Caller holds the returned handle for the lifetime of the run -- the flock
    is tied to the open file description, so dropping it releases silently.
    """
    return script_lock.acquire_or_exit(
        'meeting_prepopulate', warn=log.warning, dir=LOCK_DIR)


# ============================================================
# Filtering and classification (Pre-Pop Spec §5)
# ============================================================

def parse_iso(s: str) -> dt.datetime:
    # Python <3.11 fromisoformat rejects 'Z' suffix; accept it explicitly.
    if isinstance(s, str) and s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return dt.datetime.fromisoformat(s)


def is_group_mailbox(email: str, display: str | None,
                     directory_type: str | None = None) -> bool:
    """Defense-in-depth: catch any office/group mailbox that slipped past
    the producer's filter (Handoff Contract §1a, Pre-Pop Spec §5.5)."""
    if email and GROUP_MAILBOX_DOMAIN_RE.search(email):
        return True
    if directory_type in GROUP_MAILBOX_DIRECTORY_TYPES:
        return True
    if display:
        d = display.lower()
        if any(d.startswith(p) for p in GROUP_MAILBOX_DISPLAY_PREFIXES):
            return True
        if d.startswith('office of '):
            return True
    return False


def should_skip_meeting(m: dict, now: dt.datetime,
                        tz: ZoneInfo, treat_as_utc: bool) -> str | None:
    """Apply §5.1 skip rules. Return reason or None."""
    if m.get('my_response_status') == 'declined':
        return 'declined'
    if m.get('is_cancelled'):
        return 'cancelled'
    if m.get('is_private_appointment'):
        return 'private'
    hint = (m.get('producer_classification_hint') or {}).get('class')
    if hint == 'solo':
        return 'solo-block'
    if hint == 'personal_block':
        return 'personal-block-others-pto'
    try:
        start = normalize_event_time(
            m['start'], bool(m.get('is_all_day')), tz, treat_as_utc)
    except (KeyError, ValueError, TypeError):
        return 'unparseable-start'
    if start < now:
        return 'in-the-past'
    return None


def classify(m: dict, user_email: str, groups_idx: 'GroupsIndex',
             contact_by_email: dict, admin_emails: set[str]
             ) -> tuple[str, str | None, int]:
    """Return (meeting_type, group_link_or_none, n_others).

    Classification (Pre-Pop Spec §5.2 revised 2026-05-15 to match canonical
    meeting-note convention):
      - n_others == 1 → 'Individual'
      - n_others >= 2 AND Groups/X.md matched by subject or attendee set → 'Group'
      - n_others >= 2 AND no Group match → 'Ad-hoc'

    Admin-assistant emails (config-driven) are treated as effectively-
    optional and excluded from n_others.
    """
    others_emails: set[str] = set()
    for a in m.get('attendees') or []:
        if a.get('is_resource'):
            continue
        if a.get('response_status') == 'declined':
            continue
        if a.get('is_optional'):
            continue
        if is_group_mailbox(a.get('email') or '', a.get('display_name')):
            continue
        e = (a.get('email') or '').lower()
        if not e or e == user_email:
            continue
        if e in admin_emails:
            continue  # admin-assistant demotion (Pre-Pop Spec §5.5)
        others_emails.add(e)
    n_others = len(others_emails)

    if n_others <= 1:
        return 'Individual', None, n_others

    subject = m.get('subject') or ''
    group_stem = groups_idx.match(subject, others_emails)
    if group_stem:
        return 'Group', group_stem, n_others

    return 'Ad-hoc', None, n_others


# ============================================================
# Name normalization (Pre-Pop Spec §8.2, §8.3)
# ============================================================

def _strip_clean(s: str) -> str:
    s = EMBEDDED_EMAIL_STRIP_RE.sub(' ', s)
    s = PAREN_STRIP_RE.sub(' ', s)
    s = CREDENTIAL_SUFFIX_RE.sub('', s)
    s = s.replace("'", '').replace('"', '')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_credential_token(s: str) -> bool:
    return bool(re.fullmatch(
        r'(MD|PhD|Ph\.D\.|MBA|RN|MPH|DO|DDS|JD|EdD|MS|MA)', s, re.I))


def _is_email_like(s: str) -> bool:
    return '@' in s and EMAIL_RE.fullmatch(s) is not None


def _parse_email_local_as_name(email: str) -> tuple[str, str] | None:
    """jane.doe@example.edu -> ('Jane', 'Doe'). Returns None if local-part
    doesn't look like first.last."""
    local = email.split('@', 1)[0]
    if '.' not in local:
        return None
    parts = local.split('.')
    if len(parts) < 2:
        return None
    if not all(p.isalpha() and len(p) >= 2 for p in parts):
        return None
    first = parts[0].capitalize()
    last = parts[-1].capitalize()
    return first, last


def canonical_name(given: str | None, surname: str | None,
                   display: str | None, email: str
                   ) -> tuple[str, str]:
    """Apply the Pre-Pop §8.2 + §8.3 waterfall. Return (canonical, source)
    where canonical is 'Last, First' or similar suitable as a People filename
    stem, and source is one of: gn+sn-clean, display-comma, display-split,
    display-single, email-local-parsed, email-local-fallback."""

    # §8.3 cleanliness checks on producer-supplied gn/sn
    g_clean = _strip_clean(given) if given else ''
    s_clean = _strip_clean(surname) if surname else ''

    # Reject if gn or sn contains corruption
    if any(ch in (g_clean or '') for ch in '<@') or _is_credential_token(g_clean):
        g_clean = ''
    if any(ch in (s_clean or '') for ch in '<@') or _is_credential_token(s_clean):
        s_clean = ''

    if g_clean and s_clean:
        return f'{s_clean}, {g_clean}', 'gn+sn-clean'

    # Display-name parsing
    if display:
        d = _strip_clean(display)
        if d:
            if ',' in d:
                left, _, right = d.partition(',')
                left = left.strip()
                right = right.strip()
                if left and right:
                    return f'{left}, {right}', 'display-comma'
                return d, 'display-comma-malformed'
            if _is_email_like(d):
                # Email-format display - fall through to email-local parse
                pass
            else:
                parts = d.rsplit(' ', 1)
                if len(parts) == 2:
                    return f'{parts[1]}, {parts[0]}', 'display-split'
                return d, 'display-single'

    # Email-local parsing (§8.2 step 3)
    parsed = _parse_email_local_as_name(email)
    if parsed:
        first, last = parsed
        return f'{last}, {first}', 'email-local-parsed'

    # Final fallback (§8.2 step 4)
    return email.split('@', 1)[0], 'email-local-fallback'


def normalize_name_for_match(name: str) -> str:
    """Lowercase, strip middle initials, collapse whitespace, strip
    credential suffixes. Used for the §8.1 step 2 secondary match."""
    n = CREDENTIAL_SUFFIX_RE.sub('', name)
    n = re.sub(r'\b[A-Z]\.\s*', '', n)  # middle initials
    n = re.sub(r'\s+', ' ', n).lower().strip()
    return n


# ============================================================
# Video credential stripping (Pre-Pop Spec §15)
# ============================================================

def strip_video_credentials(text: str | None
                            ) -> tuple[str, str | None, int]:
    """Strip Zoom/Teams/Webex/Meet URLs, IDs, passcodes from `text`.
    Return (cleaned_text, provider_detected, redactions). The provider is
    derived from URL patterns seen *before* stripping, so it can be written
    to frontmatter even when the URL itself is removed."""
    if not text:
        return text or '', None, 0
    provider: str | None = None
    redactions = 0
    cleaned = text
    for prov, pat in VIDEO_PATTERNS:
        matches = pat.findall(cleaned)
        if matches:
            redactions += len(matches)
            if provider is None:
                provider = prov
            cleaned = pat.sub('[video credentials redacted]', cleaned)
    # Collapse multiple consecutive redaction markers
    cleaned = re.sub(
        r'(\[video credentials redacted\]\s*){2,}',
        '[video credentials redacted] ', cleaned)
    return cleaned.strip(), provider, redactions


def strip_location_credentials(location_display: str | None,
                               is_teams: bool, provider_hint: str | None
                               ) -> tuple[str, str | None]:
    """For location.display, replace stripped credentials with a generic
    `[Provider meeting]` marker so the meeting-type info is preserved."""
    if not location_display:
        return '', provider_hint
    provider = provider_hint
    cleaned = location_display
    for prov, pat in VIDEO_PATTERNS:
        if pat.search(cleaned):
            if provider is None:
                provider = prov
            cleaned = pat.sub('', cleaned)
    if is_teams and provider is None:
        provider = 'teams'
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if provider and not cleaned:
        cleaned = f'[{provider.capitalize()} meeting]'
    elif provider and cleaned:
        cleaned = f'{cleaned} ({provider.capitalize()})'
    return cleaned, provider


# ============================================================
# People index and dedup (Pre-Pop Spec §8.1)
# ============================================================

class PeopleIndex:
    """Holds email -> filename and normalized-name -> filename maps,
    plus alias and preferred_name lookups, built from People/*.md."""

    def __init__(self) -> None:
        self.email_to_stem: dict[str, str] = {}
        self.name_to_stem: dict[str, str] = {}
        self.stem_to_emptyfields: dict[str, set[str]] = {}
        self._loaded = False

    def load(self) -> None:
        if not PEOPLE_DIR.is_dir():
            log.warning('People dir does not exist: %s', PEOPLE_DIR)
            return
        # Scan both People/ and the low-confidence quarantine subfolder so
        # subsequent runs dedup against previously-quarantined stubs.
        candidate_paths = list(PEOPLE_DIR.glob('*.md'))
        if PEOPLE_UNRESOLVED_DIR.is_dir():
            candidate_paths.extend(PEOPLE_UNRESOLVED_DIR.glob('*.md'))
        for p in candidate_paths:
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            stem = p.stem
            fm_lines = self._extract_fm(text)
            if fm_lines is None:
                continue
            fm = '\n'.join(fm_lines)

            empties: set[str] = set()
            for key in ('Email-Work', 'Email-Personal'):
                m = re.search(
                    rf'(?m)^{re.escape(key)}\s*:\s*(.*)$', fm)
                if not m:
                    continue
                val = m.group(1).strip()
                if val:
                    self.email_to_stem[val.lower()] = stem
                else:
                    empties.add(key)
            if empties:
                self.stem_to_emptyfields[stem] = empties

            # Name indices: filename + aliases + preferred_name
            self.name_to_stem[normalize_name_for_match(stem)] = stem
            for alias in self._parse_aliases(fm):
                self.name_to_stem[normalize_name_for_match(alias)] = stem
            pref = self._parse_preferred_name(fm)
            if pref:
                self.name_to_stem[normalize_name_for_match(pref)] = stem

        log.info('PeopleIndex: %d files, %d email keys, %d name keys, '
                 '%d files with empty email fields',
                 len(candidate_paths),
                 len(self.email_to_stem),
                 len(self.name_to_stem),
                 len(self.stem_to_emptyfields))
        self._loaded = True

    @staticmethod
    def _extract_fm(text: str) -> list[str] | None:
        if not text.startswith('---'):
            return None
        lines = text.splitlines()
        for i in range(1, len(lines)):
            if lines[i].rstrip() == '---':
                return lines[1:i]
        return None

    @staticmethod
    def _parse_aliases(fm: str) -> list[str]:
        out: list[str] = []
        m = re.search(r'(?ms)^aliases\s*:\s*\n((?:^- .*\n?)+)', fm)
        if m:
            for line in m.group(1).splitlines():
                ln = line.strip()
                if ln.startswith('- '):
                    v = ln[2:].strip().strip("'").strip('"')
                    if v:
                        out.append(v)
            return out
        m = re.search(r'(?m)^aliases\s*:\s*(.+)$', fm)
        if m:
            raw = m.group(1).strip()
            if raw.startswith('[') and raw.endswith(']'):
                return [v.strip().strip("'").strip('"')
                        for v in raw[1:-1].split(',') if v.strip()]
            return [raw.strip().strip("'").strip('"')]
        return []

    @staticmethod
    def _parse_preferred_name(fm: str) -> str | None:
        m = re.search(r'(?m)^preferred_name\s*:\s*(.+)$', fm)
        if not m:
            return None
        v = m.group(1).strip().strip("'").strip('"')
        return v or None

    def lookup_by_email(self, email: str) -> str | None:
        return self.email_to_stem.get(email.lower()) if email else None

    def lookup_by_name(self, name: str) -> str | None:
        return self.name_to_stem.get(normalize_name_for_match(name))


# ============================================================
# Groups index and fuzzy subject matching (Pre-Pop Spec §5)
# ============================================================

GROUPS_DIR = _path_env('MEETING_PREPOP_GROUPS_DIR', VAULT / 'Groups')

# Common prefixes to strip from meeting subjects (groups don't have these,
# but stripping is benign there). Suffix-stripping is deliberately NOT
# done — collapsing "Microsoft Biweekly" → "microsoft" produces false
# positives against any subject containing "Microsoft".
_SUBJECT_PREFIX_RE = re.compile(
    r'^\s*(fyi|save the date|reminder|invitation|invite|'
    r'action required|hold)\s*[:\-]\s*',
    re.I)


def normalize_for_group_match(s: str) -> str:
    s = s.lower()
    # Strip prefixes repeatedly (catch nested "FYI: Reminder: foo")
    while True:
        new = _SUBJECT_PREFIX_RE.sub('', s)
        if new == s:
            break
        s = new
    s = s.replace('&', ' and ')
    s = s.replace('-', '')  # bi-weekly → biweekly
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


class GroupsIndex:
    """Index of Groups/*.md files: normalized title → stem, plus member-email
    sets for each group (built by resolving body wikilinks against the
    PeopleIndex). Used for fuzzy subject match and attendee-overlap fallback
    when classifying meetings."""

    def __init__(self, people_idx: 'PeopleIndex') -> None:
        self._people = people_idx
        self.stems: list[str] = []
        self.norm_to_stem: dict[str, str] = {}
        self.stem_to_members: dict[str, set[str]] = {}  # emails

    def load(self) -> None:
        if not GROUPS_DIR.is_dir():
            log.warning('Groups dir does not exist: %s', GROUPS_DIR)
            return
        wikilink_re = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]')
        for p in GROUPS_DIR.glob('*.md'):
            stem = p.stem
            self.stems.append(stem)
            self.norm_to_stem[normalize_for_group_match(stem)] = stem
            members: set[str] = set()
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            # Strip frontmatter for member-link parsing
            if text.startswith('---'):
                end = text.find('\n---', 4)
                if end >= 0:
                    body = text[end + 4:]
                else:
                    body = text
            else:
                body = text
            for link in wikilink_re.findall(body):
                link = link.strip()
                # Skip ![[image.png]] (these don't appear as link.group(1) here
                # because findall captures the text inside [[...]]; however
                # the same regex catches them, so filter by file-extension).
                if link.lower().endswith(('.png', '.jpg', '.jpeg', '.svg',
                                          '.webp', '.gif')):
                    continue
                if link.startswith('Meetings.base') or link == 'Meetings':
                    continue
                # Resolve link → email via PeopleIndex
                stem_match = self._people.lookup_by_name(link)
                if stem_match:
                    # find any email associated with that stem
                    for email, s in self._people.email_to_stem.items():
                        if s == stem_match:
                            members.add(email)
            self.stem_to_members[stem] = members
        log.info('GroupsIndex: %d groups, %d with at least one resolved member',
                 len(self.stems),
                 sum(1 for v in self.stem_to_members.values() if v))

    def match(self, subject: str, attendee_emails: set[str]) -> str | None:
        """Match a meeting to a Groups/ entry. Three-tier:

        1. Exact normalized-title match.
        2. All title-tokens ⊆ subject-tokens (most specific match wins —
           prefer larger token sets to avoid 'Microsoft' matching 'Microsoft
           Biweekly' when the subject is 'Microsoft CAB').
        3. Attendee-set Jaccard/coverage fallback (Jaccard ≥ 0.5 OR group-
           coverage ≥ 0.7, with at least 2 emails overlapping).
        """
        norm_subj = normalize_for_group_match(subject)
        subject_tokens = set(norm_subj.split())

        # 1. Exact normalized match
        if norm_subj in self.norm_to_stem:
            return self.norm_to_stem[norm_subj]

        # 2. All title-tokens contained in subject-tokens
        candidates: list[tuple[int, str]] = []
        for norm_title, stem in self.norm_to_stem.items():
            title_tokens = set(norm_title.split())
            if not title_tokens:
                continue
            # Avoid false positives on single short tokens like "it" or "ai"
            if len(title_tokens) == 1:
                only = next(iter(title_tokens))
                if len(only) < 4:
                    continue
            if title_tokens.issubset(subject_tokens):
                candidates.append((len(title_tokens), stem))
        if candidates:
            candidates.sort(reverse=True)  # prefer largest (most specific)
            return candidates[0][1]

        # 3. Attendee-set overlap fallback
        best_stem: str | None = None
        best_score = 0.0
        for stem, members in self.stem_to_members.items():
            if not members:
                continue
            overlap = len(attendee_emails & members)
            if overlap < 2:
                continue
            union = len(attendee_emails | members)
            jaccard = overlap / union if union else 0.0
            coverage = overlap / len(members)
            score = max(jaccard, coverage * 0.8)
            if (jaccard >= 0.5 or coverage >= 0.7) and score > best_score:
                best_score = score
                best_stem = stem
        return best_stem


# ============================================================
# People stub creation / organic backfill (Pre-Pop Spec §8)
# ============================================================

def organic_email_backfill(stem: str, email: str, dry_run: bool,
                           is_personal: bool) -> bool:
    """If the existing People file `stem.md` has empty Email-Work / Email-
    Personal that corresponds to this email's category, write it. Returns
    True if a change was made."""
    target_field = 'Email-Personal' if is_personal else 'Email-Work'
    path = PEOPLE_DIR / f'{stem}.md'
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return False
    fm_match = re.search(
        rf'(?m)^{re.escape(target_field)}\s*:\s*(.*)$', text)
    if not fm_match:
        return False
    current = fm_match.group(1).strip()
    if current:
        return False  # already populated, never overwrite
    new_line = f'{target_field}: {email}'
    new_text = (text[:fm_match.start()] + new_line +
                text[fm_match.end():])
    log.info('  organic-backfill %s=%s on %s.md',
             target_field, email, stem)
    if not dry_run:
        path.write_text(new_text, encoding='utf-8')
    return True


def render_people_stub(canonical: str, contact: dict, attendee_display: str,
                       source_subsource: str, now_iso: str) -> str:
    """Render the People stub markdown per Pre-Pop Spec §8.4."""
    email = (contact.get('email') or '').strip()
    is_personal = email.split('@', 1)[1].lower() in PERSONAL_DOMAINS \
        if '@' in email else False
    work_email = '' if is_personal else email
    pers_email = email if is_personal else ''

    title = contact.get('title') or ''
    org = contact.get('company') or ''
    phone = contact.get('phone') or ''

    aliases_set: list[str] = []
    if attendee_display:
        aliases_set.append(attendee_display)
    disp = contact.get('display_name')
    if disp and disp not in aliases_set:
        aliases_set.append(disp)
    aliases_set = [a for a in aliases_set if a and a != canonical]

    aliases_yaml = '\n'.join(f'  - "{a}"' for a in aliases_set)

    fm = f'''---
categories:
  - "[[Categories/People]]"
Title: {_yaml_scalar(title)}
Organization: {_yaml_scalar(org)}
Email-Personal: {pers_email}
Email-Work: {work_email}
Mobile Phone: {phone}
preferred_name:
aliases:
{aliases_yaml if aliases_set else ''}
classification: confidential
tags: []
created: {now_iso}
updated: {now_iso}
status: stub
source: meeting-prepopulate
source_subsource: {source_subsource}
---


## Photo



## Notes



## Bio

> Stub created automatically by meeting-prepopulate ({source_subsource}). Bio pending — next People-bio pass will enrich.

## Meetings

![[Meetings.base#Person]]

## Referenced In

![[Notes.base#Person]]
'''
    return fm


def _yaml_scalar(s: str) -> str:
    if not s:
        return ''
    if any(ch in s for ch in ':#&*!|>%@`{}[]'):
        return f'"{s}"'
    return s


def resolve_or_create_person(attendee: dict, contact_by_email: dict,
                             people_idx: PeopleIndex, now_iso: str,
                             dry_run: bool, counters: Counter,
                             ) -> tuple[str, str]:
    """Resolve an attendee to a People-file stem, creating a stub if needed.
    Returns (stem, status) where status ∈ {'email-match', 'name-match',
    'created', 'created-low-confidence'}."""
    email = (attendee.get('email') or '').lower()
    display = attendee.get('display_name')
    contact = contact_by_email.get(email, {}) if email else {}

    # Build canonical first (we may need it for name match)
    canonical, name_source = canonical_name(
        contact.get('given_name'),
        contact.get('surname'),
        display or contact.get('display_name'),
        email or '')
    # Trailing periods from middle initials (e.g., 'Last, First A.') would
    # otherwise produce filenames like 'Last, First A..md'. No existing
    # People file in the vault uses that pattern; strip the trailing dot.
    canonical = canonical.rstrip('.').rstrip()

    # Low-confidence stubs go to a quarantine subfolder so they don't
    # pollute the main People/ list. Wikilinks still resolve by filename.
    is_low_confidence = name_source in (
        'display-single', 'email-local-fallback', 'display-comma-malformed')
    target_dir = PEOPLE_UNRESOLVED_DIR if is_low_confidence else PEOPLE_DIR

    # Step 1: email-keyed match (§8.1 step 1)
    if email:
        stem = people_idx.lookup_by_email(email)
        if stem:
            counters['dedup-email-match'] += 1
            return stem, 'email-match'

    # Step 2: name-normalized match (§8.1 step 2)
    stem = people_idx.lookup_by_name(canonical)
    if stem:
        counters['dedup-name-match'] += 1
        # Organic email backfill on the matched file
        if email:
            is_personal = email.split('@', 1)[1] in PERSONAL_DOMAINS
            if organic_email_backfill(stem, email, dry_run, is_personal):
                counters['organic-email-backfill'] += 1
        return stem, 'name-match'

    # Step 3: create new stub (§8.1 step 3)
    stem = canonical
    # Handle filename collision (different person with same canonical name).
    # Check both the target dir AND the alternate dir, since the same person
    # could plausibly have a high- and low-confidence stub from different
    # data sources across runs.
    def _stub_exists(candidate: str) -> bool:
        return ((PEOPLE_DIR / f'{candidate}.md').exists() or
                (PEOPLE_UNRESOLVED_DIR / f'{candidate}.md').exists())
    suffix = 2
    while _stub_exists(stem):
        stem = f'{canonical} {suffix}'
        suffix += 1
    path = target_dir / f'{stem}.md'

    content = render_people_stub(
        canonical, contact, display or '', name_source, now_iso)
    rel = 'People/_Unresolved' if is_low_confidence else 'People'
    log.info('  CREATE-STUB %s/%s.md  (source=%s)', rel, stem, name_source)
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    # Always update in-memory index so subsequent attendees in the same
    # run don't re-create the same stub (matters in dry-run especially).
    if email:
        people_idx.email_to_stem[email] = stem
    people_idx.name_to_stem[normalize_name_for_match(canonical)] = stem

    if is_low_confidence:
        counters['stub-created-low-confidence'] += 1
        return stem, 'created-low-confidence'
    counters['stub-created'] += 1
    return stem, 'created'


# ============================================================
# Meeting file rendering (Pre-Pop Spec §6, §7)
# ============================================================

def slugify_subject(subject: str) -> str:
    s = subject.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:60] or 'meeting'


def meeting_filename(start: dt.datetime, meetings_dir: Path) -> Path:
    base = start.strftime(FILENAME_TIME_FMT)
    path = meetings_dir / f'{base}.md'
    suffix = 2
    while path.exists():
        path = meetings_dir / f'{base}-{suffix}.md'
        suffix += 1
    return path


STATE_DIR = SCRIPTS_DIR / '.state'
STATE_FILE = STATE_DIR / 'meeting_prepopulate_seen.json'

CONFIG_DIR = SCRIPTS_DIR / '.config'
CONFIG_FILE = CONFIG_DIR / 'meeting_prepopulate.json'


def load_config() -> dict:
    """Read the full config dict, or {} if missing/unreadable."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        log.warning('config file unreadable, ignoring: %s (%s)',
                    CONFIG_FILE, e)
        return {}


def load_admin_emails() -> set[str]:
    """Load admin-assistant emails to demote to effectively-optional. These
    are people (admins, EAs) the user does not want appearing as required
    attendees on their meetings — they're scheduling on the user's behalf
    rather than being meeting participants. Lowercased on load.

    Config shape: {"admin_emails": ["assistant@example.com", ...]}
    """
    return {e.lower() for e in (load_config().get('admin_emails') or [])}


def load_treat_start_as_utc() -> bool:
    """Workaround for a known producer bug (handoff contract pre-v0.5):
    the producer sends the UTC instant value in the `start` / `end` fields
    but stamps them with the user's local tz offset (e.g., `-04:00`)
    instead of `+00:00`. Result: every non-all-day meeting is 4 hours late.

    When this flag is true, the consumer reinterprets the numerical time
    as UTC and converts to the user's tz. Default false to stay backward-
    compatible once the producer is fixed.
    """
    return bool(load_config().get('treat_start_as_utc', False))


def normalize_event_time(iso_str: str, is_all_day: bool,
                         tz: ZoneInfo, treat_as_utc: bool) -> dt.datetime:
    """Parse an ISO datetime string from the handoff and return a tz-aware
    datetime in the user's local tz. Applies the UTC-relabel workaround
    when treat_as_utc is True AND the event is not all-day."""
    parsed = parse_iso(iso_str)
    if treat_as_utc and not is_all_day:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def strip_subject_prefixes(s: str) -> str:
    """Strip common Outlook prefixes ('FYI:', 'Save the Date:', etc.) from
    a meeting subject. Used for the Ad-hoc title field. Case-insensitive
    match, preserves original casing of the remaining text."""
    if not s:
        return s
    prev = None
    while prev != s:
        prev = s
        s = _SUBJECT_PREFIX_RE.sub('', s).lstrip()
    return s.strip()


def load_seen_state() -> dict[str, dict]:
    """Load the source_uid → {filename, generated_at} state. Empty on first
    run or if the file is missing/corrupt."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        log.warning('state file unreadable; treating as empty: %s',
                    STATE_FILE)
        return {}


def save_seen_state(state: dict[str, dict], dry_run: bool) -> None:
    if dry_run:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True),
                          encoding='utf-8')


def build_people_wikilinks(attendees: list[dict],
                           contact_by_email: dict,
                           people_idx: PeopleIndex,
                           now_iso: str, dry_run: bool,
                           counters: Counter,
                           user_email: str,
                           admin_emails: set[str],
                           ) -> list[str]:
    """Return list of required-attendee wikilinks for the meeting's
    `people:` frontmatter list. Optional attendees and configured admin-
    assistant emails are excluded entirely (Pre-Pop Spec §5.5)."""
    required: list[str] = []
    seen: set[str] = set()
    for a in attendees:
        if a.get('is_resource'):
            continue
        if a.get('response_status') == 'declined':
            continue
        if a.get('is_optional'):
            continue
        email = (a.get('email') or '').lower()
        if not email or email == user_email:
            continue
        if email in admin_emails:
            counters['attendee-admin-demoted'] += 1
            continue
        if is_group_mailbox(email, a.get('display_name')):
            continue

        stem, _status = resolve_or_create_person(
            a, contact_by_email, people_idx, now_iso, dry_run, counters)
        link = f'[[{stem}]]'
        if link not in seen:
            required.append(link)
            seen.add(link)
    return required


def render_meeting_file(m: dict, mtype: str, group_stem: str | None,
                        required_people: list[str], now_iso: str) -> str:
    """Compose the meeting Markdown file in the canonical minimal form
    (Pre-Pop Spec §7 revised 2026-05-15). Matches the shape of the three
    canonical examples in ~/Obsidian/Meetings/History/:

      - Individual: 2026-05-13 1500.md
      - Group:      2026-05-16 0900.md
      - Ad-hoc:     2026-05-08 0930.md

    Only the canonical frontmatter is written. Body is empty — user fills
    as they take notes. Tags are empty initially; the tagger pipeline
    populates them from body content later.
    """
    fm_lines = [
        '---',
        'categories:',
        '  - "[[Meetings]]"',
        f'type: {mtype}',
    ]
    if mtype == 'Ad-hoc':
        # Ad-hoc carries a descriptive title; strip Outlook prefixes
        # ("FYI:", "Save the Date:", etc.) to keep the title clean.
        subject = strip_subject_prefixes((m.get('subject') or '').strip())
        if subject:
            fm_lines.append(f'title: {_yaml_scalar(subject)}')
    if mtype == 'Group' and group_stem:
        fm_lines.append('group:')
        fm_lines.append(f'  - "[[{group_stem}]]"')
    fm_lines.append('people:')
    for p in required_people:
        fm_lines.append(f'  - "{p}"')
    fm_lines.append('tags: []')
    fm_lines.append('classification: confidential')
    fm_lines.append(f'created: {now_iso[:16]}')
    fm_lines.append(f'updated: {now_iso[:16]}')
    fm_lines.append('---')
    fm_lines.append('')
    return '\n'.join(fm_lines)


# ============================================================
# Series root (Pre-Pop Spec §10)
# ============================================================

def series_link_for(m: dict) -> str | None:
    if not m.get('is_recurring_instance'):
        return None
    slug = slugify_subject(m.get('subject') or 'series')
    return f'Meetings/Series/{slug}'


def ensure_series_root(m: dict, slug_path: str, required_people: list[str],
                       now_iso: str, dry_run: bool) -> None:
    """Lazy-create the series root at `Meetings/Series/<slug>.md`."""
    path = MEETINGS_DIR / f'{slug_path.replace("Meetings/", "")}.md'
    if path.exists():
        return
    subject = m.get('subject') or '(untitled series)'
    series_uid = m.get('series_uid') or ''
    recur_human = m.get('recurrence_human') or ''
    rrule = m.get('rrule_raw') or ''
    people_yaml = '\n'.join(f"  - '{p}'" for p in required_people)
    content = f'''---
categories:
  - '[[Meetings]]'
type: meeting-series
subject: {_yaml_scalar(subject)}
series_uid: {_yaml_scalar(series_uid)}
recurrence_human: {_yaml_scalar(recur_human)}
rrule_raw: {_yaml_scalar(rrule)}
people:
{people_yaml}
created_by: meeting-prepopulate
generated: {now_iso}
---

# {subject}

Recurrence: **{recur_human}**

## Standing Agenda

## Ongoing Threads

## Per-instance Notes

(Each instance under Meetings/YYYY-MM-DD HHMM.md links back here via `series_link:` in frontmatter.)
'''
    log.info('  CREATE-SERIES-ROOT %s', path.name)
    if not dry_run:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(content, encoding='utf-8')


# ============================================================
# Run summary
# ============================================================



def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block_including_fences, body). ('', text) if none."""
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            fm_end = text.find('\n', end + 1)
            if fm_end == -1:
                fm_end = len(text) - 1
            return text[:fm_end + 1], text[fm_end + 1:]
    return '', text


def _fm_set_or_add(fm: str, key: str, value: str) -> str:
    """Set `key: value` inside a frontmatter block (fences included), adding it
    just before the closing '---' if the key is absent."""
    pat = re.compile(rf'(?m)^{re.escape(key)}\s*:.*$')
    line = f'{key}: {value}'
    if pat.search(fm):
        return pat.sub(line, fm, count=1)
    lines = fm.rstrip('\n').split('\n')
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == '---':
            lines.insert(i, line)
            break
    return '\n'.join(lines) + '\n'


# Fences for the reschedule banner injected into an existing meeting note.
# (Restored after the run-summary removal in 85740af swept these out while
# the reschedule machinery below still uses them — a NameError on the first
# rescheduled meeting. Pinned by test_meeting_prepopulate.py.)
RESCHEDULE_FENCE_START = '<!-- prepop:reschedule:start -->'
RESCHEDULE_FENCE_END = '<!-- prepop:reschedule:end -->'


def _reschedule_banner(old_slot: str, new_slot: str, now_iso: str) -> str:
    return (f'{RESCHEDULE_FENCE_START}\n'
            f'> [!warning] Rescheduled\n'
            f'> Moved from `{old_slot}` to `{new_slot}` '
            f'(updated {now_iso[:10]}).\n'
            f'{RESCHEDULE_FENCE_END}\n')


def _inject_banner(body: str, banner: str) -> str:
    """Replace an existing reschedule fence in body, else prepend a fresh one."""
    if RESCHEDULE_FENCE_START in body and RESCHEDULE_FENCE_END in body:
        pat = re.compile(
            re.escape(RESCHEDULE_FENCE_START) + r'.*?' +
            re.escape(RESCHEDULE_FENCE_END) + r'\n?', re.DOTALL)
        return pat.sub(banner, body, count=1)
    stripped = body.lstrip('\n')
    lead = body[:len(body) - len(stripped)]
    return f'{lead}{banner}\n{stripped}' if stripped else f'{lead}{banner}'


def _redirect_stub(new_slot: str, now_iso: str) -> str:
    return (
        '---\n'
        'categories:\n'
        '  - "[[Meetings]]"\n'
        'type: redirect\n'
        f'moved_to: "[[{new_slot}]]"\n'
        'classification: confidential\n'
        f'created: {now_iso[:16]}\n'
        f'updated: {now_iso[:16]}\n'
        '---\n'
        '\n'
        '> [!info] Rescheduled\n'
        f'> This meeting moved to [[{new_slot}]].\n'
    )


def apply_reschedule(prev: dict, start: dt.datetime, now_iso: str,
                     dry_run: bool, changes: list[str]) -> Path | None:
    """Move an owned meeting note to its new time slot, preserving the body and
    leaving a redirect stub at the old name. Returns the new path, or None if
    the old note is missing (caller should recreate it fresh)."""
    old_fname = prev.get('filename') or ''
    old_slot = prev.get('slot') or (old_fname[:-3] if old_fname else '')
    old_path = MEETINGS_DIR / old_fname if old_fname else None
    if not old_path or not old_path.exists():
        return None
    try:
        text = old_path.read_text(encoding='utf-8')
    except OSError:
        return None

    new_path = meeting_filename(start, MEETINGS_DIR)
    new_slot = new_path.name[:-3]

    fm, body = _split_frontmatter(text)
    if fm:
        fm = _fm_set_or_add(fm, 'updated', now_iso[:16])
        fm = _fm_set_or_add(fm, 'rescheduled_from', old_slot)
    body = _inject_banner(body, _reschedule_banner(old_slot, new_slot, now_iso))
    moved = fm + body

    log.info('  RESCHEDULED %s -> %s', old_slot, new_slot)
    changes.append(f'RESCHEDULED `{old_slot}` -> `{new_slot}`')
    if not dry_run:
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        new_path.write_text(moved, encoding='utf-8')
        old_path.write_text(_redirect_stub(new_slot, now_iso),
                            encoding='utf-8')
    return new_path


def delete_cancelled(prev: dict, reason: str, dry_run: bool,
                     changes: list[str]) -> None:
    """Delete an owned meeting note that has been cancelled/declined, plus any
    redirect stub left over from a prior reschedule."""
    fname = prev.get('filename') or ''
    if fname:
        p = MEETINGS_DIR / fname
        log.info('  CANCELLED (%s) -> DELETE %s', reason, fname)
        changes.append(f'CANCELLED ({reason}) -> deleted `{fname}`')
        if not dry_run and p.exists():
            p.unlink()
    orig = prev.get('rescheduled_from')
    if orig:
        stub = MEETINGS_DIR / f'{orig}.md'
        if stub.exists():
            try:
                if 'type: redirect' in stub.read_text(encoding='utf-8'):
                    log.info('  CANCELLED -> remove redirect stub %s.md', orig)
                    if not dry_run:
                        stub.unlink()
            except OSError:
                pass


def _is_cancelled(m: dict) -> tuple[bool, str]:
    """Return (cancelled, reason). A meeting counts as cancelled for an
    already-created note if the calendar source marks it cancelled or the user has declined it."""
    if m.get('is_cancelled'):
        return True, 'cancelled'
    if m.get('my_response_status') == 'declined':
        return True, 'declined'
    return False, ''


# ============================================================
# Main
# ============================================================

def process_handoff(record: 'hs.HandoffRecord', source: 'hs.HandoffSource',
                    dry_run: bool, no_move: bool) -> int:
    # Integrity (SHA-256), producer signature, and schema were already verified
    # by source.load(); we operate on the validated payload here. The source
    # also owns acknowledgement (drop: move-to-processed; MCP: server ack).
    payload = record.payload
    handoff_name = record.id

    tz_name = payload.get('user', {}).get('timezone', DEFAULT_TZ)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    now = dt.datetime.now(tz=tz)
    now_iso = now.isoformat(timespec='seconds')

    user_email = (payload.get('user', {}).get('email') or '').lower()
    contact_by_email = {(c.get('email') or '').lower(): c
                        for c in payload.get('contacts', [])
                        if c.get('email')}

    people_idx = PeopleIndex()
    people_idx.load()
    groups_idx = GroupsIndex(people_idx)
    groups_idx.load()
    admin_emails = load_admin_emails()
    if admin_emails:
        log.info('Admin-demote list: %s', sorted(admin_emails))
    treat_as_utc = load_treat_start_as_utc()
    if treat_as_utc:
        log.info('treat_start_as_utc=True — reinterpreting non-all-day '
                 'start/end times as UTC then converting to user tz '
                 '(producer-bug workaround, pre-Handoff-Contract-v0.5)')

    seen_state = load_seen_state()
    counters: Counter = Counter()
    errors: list[str] = []
    changes: list[str] = []

    for m in payload.get('meetings', []):
        uid = m.get('uid') or ''
        prev = seen_state.get(uid) if uid else None
        cancelled, cancel_reason = _is_cancelled(m)

        try:
            # --- Tombstone: a note we previously deleted on cancellation ---
            if prev and prev.get('status') == 'cancelled':
                if cancelled:
                    continue                 # still cancelled, nothing to do
                prev = None                  # un-cancelled -> recreate fresh

            # --- Owned, live note: handle cancellation / reschedule ---
            if prev:
                if cancelled:
                    delete_cancelled(prev, cancel_reason, dry_run, changes)
                    prev['status'] = 'cancelled'
                    prev['updated'] = now_iso
                    counters[f'cancelled-{cancel_reason}-deleted'] += 1
                    continue

                try:
                    start = normalize_event_time(
                        m['start'], bool(m.get('is_all_day')),
                        tz, treat_as_utc)
                except (KeyError, ValueError, TypeError):
                    counters['re-run-same-uid-skipped'] += 1
                    continue

                new_slot = start.strftime(FILENAME_TIME_FMT)
                old_slot = prev.get('slot')
                if not old_slot:
                    # Pre-existing state entry from before slot tracking: record
                    # the slot now (no move) so future runs can detect changes.
                    prev['slot'] = new_slot
                    prev['start'] = start.isoformat(timespec='seconds')
                    prev.setdefault('status', 'active')
                    counters['state-backfilled'] += 1
                    continue
                if new_slot == old_slot:
                    counters['re-run-same-uid-skipped'] += 1
                    continue

                # Time changed -> move the note (preserve body, leave a stub).
                new_path = apply_reschedule(
                    prev, start, now_iso, dry_run, changes)
                if new_path is None:
                    prev = None              # old note gone -> recreate fresh
                else:
                    prev['filename'] = new_path.name
                    prev['slot'] = new_slot
                    prev['start'] = start.isoformat(timespec='seconds')
                    prev['updated'] = now_iso
                    counters['rescheduled-moved'] += 1
                    continue

            # --- New meeting (or recreate after un-cancel / vanished note) ---
            skip = should_skip_meeting(m, now, tz, treat_as_utc)
            if skip:
                counters[f'skipped-{skip}'] += 1
                log.info('SKIP (%s): uid=%s', skip, uid[:24])
                continue

            start = normalize_event_time(
                m['start'], bool(m.get('is_all_day')), tz, treat_as_utc)

            mtype, group_stem, n_others = classify(
                m, user_email, groups_idx, contact_by_email, admin_emails)
            counters[f'class-{mtype}'] += 1
            if mtype == 'Group' and group_stem:
                counters['group-matched'] += 1

            # Resolve attendees -> stubs / wikilinks (required only;
            # optional and admin-demoted attendees excluded)
            required = build_people_wikilinks(
                m.get('attendees') or [], contact_by_email, people_idx,
                now_iso, dry_run, counters, user_email, admin_emails)

            # Meeting file path
            path = meeting_filename(start, MEETINGS_DIR)
            content = render_meeting_file(
                m, mtype, group_stem, required, now_iso)

            log.info('  WRITE-MEETING %s  (type=%s%s, n_others=%d)',
                     path.name, mtype,
                     f', group=[[{group_stem}]]' if group_stem else '',
                     n_others)
            if not dry_run:
                MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
            counters['meeting-file-written'] += 1

            if uid:
                seen_state[uid] = {
                    'filename': path.name,
                    'slot': start.strftime(FILENAME_TIME_FMT),
                    'start': start.isoformat(timespec='seconds'),
                    'generated_at': now_iso,
                    'type': mtype,
                    'group': group_stem,
                    'status': 'active',
                }
        except Exception as e:  # noqa: BLE001
            msg = f'Error processing meeting uid={(m.get("uid") or "")[:24]}: {e}'
            log.exception(msg)
            errors.append(msg)
            counters['meeting-error'] += 1

    save_seen_state(seen_state, dry_run)
    # Producer notes used to reach the operator only through a run-summary
    # note written into the vault. That note duplicated the counts, reschedules
    # and errors already logged here, was never linked or read, and accumulated
    # without bound — so it was removed. These notes were the one thing it
    # carried that the log did not, so they are logged instead.
    for _n in (payload.get('notes') or []):
        log.info('Producer note [%s]: %s',
                 _n.get('level', 'info').upper(), _n.get('text', ''))
    if not no_move and not errors:
        source.ack(record.handle, dry_run)

    log.info('Handoff complete: %s', handoff_name)
    log.info('Counts: %s', dict(counters))
    return 0 if not errors else 2


# ============================================================
# Handoff source selection (pluggable transport)
# ============================================================

def select_handoff_source() -> 'hs.HandoffSource':
    """Pick the transport from MEETING_PREPOP_SOURCE (default 'drop'):
      drop : generic signed-drop folder (relay / SFTP / manual import) - this
             is what handoff_blob_pull.py's Azure Blob Tier B relay feeds
      mcp  : tenant MCP server (stub until the endpoint is wired)
    Signing: set HANDOFF_HMAC_KEY[_FILE] to verify producer authenticity;
    HANDOFF_REQUIRE_SIGNATURE=1 makes a missing/invalid signature fatal."""
    mode = os.environ.get('MEETING_PREPOP_SOURCE', 'drop').strip().lower()
    hmac_key = hs.load_hmac_key()
    require_sig = os.environ.get(
        'HANDOFF_REQUIRE_SIGNATURE', '').strip().lower() in ('1', 'true', 'yes')

    if mode in ('', 'drop', 'folder'):
        # Auto-create, matching handoff_blob_pull.py's own LOCAL_DIR.mkdir():
        # an empty folder discovers as "no handoffs" (exit 0), same as before
        # any producer has ever run, rather than DropFolderSource.discover()
        # raising because the folder doesn't exist yet.
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        return hs.DropFolderSource(HANDOFF_DIR, hmac_key=hmac_key,
                                   require_signature=require_sig)
    if mode == 'mcp':
        endpoint = os.environ.get('HANDOFF_MCP_ENDPOINT', '')
        token = os.environ.get('HANDOFF_MCP_TOKEN', '')
        return hs.MCPSource(endpoint, token, hmac_key=hmac_key,
                            require_signature=True)
    raise hs.HandoffError(f'unknown MEETING_PREPOP_SOURCE={mode!r}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help="Don't write any files; report what would happen.")
    ap.add_argument('--handoff', type=Path, default=None,
                    help='Process a specific .ready sentinel (otherwise '
                         'scan the handoff-drop folder).')
    ap.add_argument('--no-move', action='store_true',
                    help="Don't move processed handoffs to _processed/.")
    ap.add_argument('--verbose', action='store_true',
                    help='DEBUG-level logging.')
    args = ap.parse_args()

    setup_logging(args.verbose, args.dry_run)

    _lock = acquire_lock()  # noqa: F841  hold for run lifetime

    try:
        source = select_handoff_source()
    except hs.HandoffError as e:
        log.error('%s', e)
        return 1
    log.info('Handoff source: %s', source.describe())

    if args.handoff:
        handles = [args.handoff]
    else:
        try:
            handles = source.discover()
        except hs.HandoffError as e:
            log.error('Handoff discovery failed: %s', e)
            return 1

    if not handles:
        log.info('No handoffs to process.')
        return 0

    rc = 0
    for h in handles:
        try:
            record = source.load(h)
        except hs.HandoffError as e:
            log.error('Skipping handoff: %s', e)
            rc = max(rc, 1)
            continue
        ec = process_handoff(record, source, args.dry_run, args.no_move)
        rc = max(rc, ec)

    log.info('---- run end (rc=%d) ----', rc)
    return rc


if __name__ == '__main__':
    sys.exit(main())
