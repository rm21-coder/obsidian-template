#!/usr/bin/env python3
"""meeting_group_backfill.py — repair group membership on existing meeting
notes, and report the calendar subjects that no Groups/ entry claims.

Two jobs, both driven by the *same* matcher the pre-population pipeline uses
(meeting_prepopulate.GroupsIndex) so this tool can never drift from it:

  1. BACKFILL. Meeting notes are attached to a group by the `group:` property
     -- that is what `Templates/Bases/Meetings.base` filters on for its Group
     view (`list(group).contains(this)`). Notes generated before a Groups/
     entry existed (or before it was given a matching alias) are stranded as
     `type: Ad-hoc`, and notes hand-edited to `type: Group` with the group
     link in `title:` are invisible to that view. This rewrites both into the
     canonical shape.

  2. DIGEST. Any Ad-hoc note whose title no group claims is either a genuine
     one-off or a group you have not declared yet. Recurring ones are the
     latter. `--digest` writes them to a note, ranked by recurrence.

Confidence tiers, because a wrong group assignment is worse than none:

  - title       -- the note's own title matched a group name or alias.
                   Applied by default.
  - overlap     -- the note has no usable title and was resolved only by
                   attendee-set overlap. Reported but NOT applied unless
                   --apply-overlap is passed.

Usage:
    /usr/bin/python3 meeting_group_backfill.py                  # dry run
    /usr/bin/python3 meeting_group_backfill.py --apply
    /usr/bin/python3 meeting_group_backfill.py --apply --apply-overlap
    /usr/bin/python3 meeting_group_backfill.py --digest
    /usr/bin/python3 meeting_group_backfill.py --keep-title --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
VAULT = SCRIPTS_DIR.parent.parent
MEETINGS_DIR = VAULT / 'Meetings'
BACKUP_ROOT = SCRIPTS_DIR / '.backups'
DIGEST_NOTE = VAULT / 'Z_dashboards' / 'Unmatched Meeting Subjects.md'

# A subject seen at least this many times with no group is very likely a
# standing meeting that simply has no Groups/ entry yet.
RECURRENCE_THRESHOLD = 3


# ============================================================
# Borrow the pipeline's matcher rather than reimplementing it
# ============================================================

def load_pipeline():
    path = SCRIPTS_DIR / 'meeting_prepopulate.py'
    spec = importlib.util.spec_from_file_location('meeting_prepopulate', path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# Frontmatter read/write (surgical -- no YAML round-trip)
# ============================================================

WIKILINK_ONLY_RE = re.compile(r'^\[\[([^\]|]+)(?:\|[^\]]+)?\]\]$')


def split_note(text: str) -> tuple[str, str] | None:
    """Return (frontmatter_text, rest_including_closing_fence) or None."""
    if not text.startswith('---'):
        return None
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip('\n').rstrip() == '---':
            return ''.join(lines[1:i]), ''.join(lines[i:])
    return None


def scalar(fm: str, key: str) -> str | None:
    m = re.search(r'(?m)^%s[ \t]*:[ \t]*(.*)$' % re.escape(key), fm)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def has_list_value(fm: str, key: str) -> bool:
    """True when `key:` is present AND followed by at least one list item.

    `group:` with nothing under it is what the pipeline writes for a note it
    could not attach, and is exactly the case this tool exists to fix -- so
    an empty key must not count as present.
    """
    m = re.search(r'(?ms)^%s[ \t]*:[ \t]*(.*?)$\n?((?:^[ \t]*-[ \t].*\n?)*)'
                  % re.escape(key), fm)
    if not m:
        return False
    inline = m.group(1).strip()
    if inline and inline not in ('[]', '~', 'null'):
        return True
    return bool(m.group(2).strip())


def list_items(fm: str, key: str) -> list[str]:
    m = re.search(r'(?ms)^%s[ \t]*:[ \t]*$\n((?:^[ \t]*-[ \t].*\n?)*)'
                  % re.escape(key), fm)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        ln = line.strip()
        if ln.startswith('-'):
            v = ln[1:].strip().strip('"').strip("'")
            if v:
                out.append(v)
    return out


def detect_indent(fm: str) -> str:
    """Match the file's existing list style. Two YAML dialects are present in
    this vault: Obsidian's Properties editor writes '  - "[[x]]"', an earlier
    python round-trip wrote '- ''[[x]]'''. Rewriting one into the other would
    make every touched note show a spurious diff in Obsidian Sync."""
    m = re.search(r'(?m)^(\s*)-[ \t]', fm)
    return m.group(1) if m else '  '


def detect_quote(fm: str) -> str:
    m = re.search(r'(?m)^\s*-[ \t]*(["\'])\[\[', fm)
    return m.group(1) if m else '"'


def set_type_group(fm: str) -> str:
    if re.search(r'(?m)^type[ \t]*:', fm):
        return re.sub(r'(?m)^type[ \t]*:.*$', 'type: Group', fm, count=1)
    return 'type: Group\n' + fm


def insert_group(fm: str, stem: str) -> str:
    indent, q = detect_indent(fm), detect_quote(fm)
    block = f'group:\n{indent}- {q}[[{stem}]]{q}\n'
    # Canonical key order puts `group:` immediately before `people:`.
    if re.search(r'(?m)^people[ \t]*:', fm):
        return re.sub(r'(?m)^(people[ \t]*:)', block + r'\1', fm, count=1)
    if re.search(r'(?m)^type[ \t]*:', fm):
        return re.sub(r'(?m)^(type[ \t]*:.*\n)', r'\1' + block, fm, count=1)
    return fm + block


def drop_title(fm: str) -> str:
    return re.sub(r'(?m)^title[ \t]*:.*\n', '', fm, count=1)


def replace_empty_group(fm: str, stem: str) -> str:
    """Fill in a `group:` key that exists but carries no items."""
    indent, q = detect_indent(fm), detect_quote(fm)
    return re.sub(r'(?m)^group[ \t]*:[ \t]*$',
                  f'group:\n{indent}- {q}[[{stem}]]{q}', fm, count=1)


# ============================================================
# Scanning
# ============================================================

class Note:
    __slots__ = ('path', 'rel', 'text', 'fm', 'body', 'type', 'title',
                 'people', 'has_group')

    def __init__(self, path: Path, vault: Path):
        self.path = path
        self.rel = str(path.relative_to(vault))
        self.text = path.read_text(encoding='utf-8', errors='replace')
        parts = split_note(self.text)
        self.fm, self.body = parts if parts else ('', self.text)
        self.type = scalar(self.fm, 'type')
        self.title = scalar(self.fm, 'title') or ''
        self.people = list_items(self.fm, 'people')
        self.has_group = has_list_value(self.fm, 'group')

    @property
    def title_plain(self) -> str:
        """Title with a wrapping wikilink stripped -- a hand-edit that put the
        group link in `title:` still tells us which group was meant."""
        m = WIKILINK_ONLY_RE.match(self.title.strip())
        return (m.group(1) if m else self.title).strip()


def is_meeting_note(n: Note) -> bool:
    if not n.fm:
        return False
    if 'Template' in n.path.stem:
        return False
    if not re.search(r'\[\[Meetings\]\]', n.fm):
        return False
    return n.type in ('Group', 'Individual', 'Ad-hoc')


def collect_notes(vault: Path, include_history: bool) -> list[Note]:
    paths = sorted(MEETINGS_DIR.glob('*.md'))
    if include_history and (MEETINGS_DIR / 'History').is_dir():
        paths += sorted((MEETINGS_DIR / 'History').glob('*.md'))
    out = []
    for p in paths:
        try:
            n = Note(p, vault)
        except OSError:
            continue
        if is_meeting_note(n):
            out.append(n)
    return out


def stem_emails(mp, people_idx) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = defaultdict(set)
    for email, stem in people_idx.email_to_stem.items():
        rev[stem].add(email)
    return rev


def note_emails(n: Note, mp, people_idx, rev) -> set[str]:
    out: set[str] = set()
    for raw in n.people:
        m = WIKILINK_ONLY_RE.match(raw.strip())
        name = m.group(1) if m else raw.strip()
        stem = people_idx.lookup_by_name(name)
        if stem:
            out |= rev.get(stem, set())
    return out


# ============================================================
# Proposal
# ============================================================

class Proposal:
    def __init__(self, note: Note, stem: str, confidence: str, why: str):
        self.note, self.stem = note, stem
        self.confidence, self.why = confidence, why

    def render(self, keep_title: bool) -> str:
        fm = self.note.fm
        fm = set_type_group(fm)
        if re.search(r'(?m)^group[ \t]*:[ \t]*$', fm):
            fm = replace_empty_group(fm, self.stem)
        else:
            fm = insert_group(fm, self.stem)
        # A Group note's identity is its `group:` link; a leftover `title:`
        # holding the same name (or a wikilink to it) is duplicate state that
        # drifts. A title that says something *else* is kept -- it is the
        # calendar subject and the only record of what the invite was called.
        if not keep_title and self.note.title:
            tp = self.note.title_plain.lower()
            if tp == self.stem.lower() or WIKILINK_ONLY_RE.match(
                    self.note.title.strip()):
                fm = drop_title(fm)
        # split_note() hands back the frontmatter *without* the opening fence
        # or its newline, so both have to be put back. Getting this wrong
        # yields '---categories:' on line 1, which Obsidian reads as a note
        # with no frontmatter at all -- see validate().
        return '---\n' + fm + self.note.body


def validate(new_text: str, note: Note, stem: str) -> str | None:
    """Return an error string if `new_text` is not a safe replacement.

    Nothing is written unless this passes. The first version of this tool
    shipped without it, emitted '---categories:' as line 1 on twelve notes,
    and Obsidian's linter then stacked a second frontmatter block on top of
    the wreckage. Cheap invariants, checked every time.
    """
    if not new_text.startswith('---\n'):
        return 'does not open with a frontmatter fence'
    parts = split_note(new_text)
    if parts is None:
        return 'frontmatter block does not close'
    fm, body = parts
    if scalar(fm, 'type') != 'Group':
        return 'type is not Group'
    if not has_list_value(fm, 'group'):
        return 'group: is missing or empty'
    if f'[[{stem}]]' not in fm:
        return f'group: does not contain [[{stem}]]'
    if body != note.body:
        return 'note body changed'
    if list_items(fm, 'people') != note.people:
        return 'people: changed'
    for key in ('categories', 'classification', 'created', 'tags'):
        if (key in note.fm) != (key in fm):
            return f'{key} appeared or vanished'
    if new_text.count('\n---\n') != note.text.count('\n---\n'):
        return 'frontmatter fence count changed'
    return None


def build_proposals(notes, gi, mp, people_idx, rev):
    props, unmatched = [], []
    for n in notes:
        if n.has_group:
            continue
        needs = (n.type == 'Group') or (n.type == 'Ad-hoc' and n.title)
        if not needs:
            continue
        stem = None
        conf = why = ''
        if n.title_plain:
            stem = gi.match(n.title_plain, set())
            if stem:
                conf, why = 'title', f'title {n.title_plain!r}'
        if not stem:
            emails = note_emails(n, mp, people_idx, rev)
            if len(emails) >= 2:
                stem = gi.match('', emails)
                if stem:
                    ov = len(emails & gi.stem_to_members.get(stem, set()))
                    conf = 'overlap'
                    why = f'{ov}/{len(emails)} attendees overlap'
        if stem:
            props.append(Proposal(n, stem, conf, why))
        else:
            unmatched.append(n)
    return props, unmatched


# ============================================================
# Digest
# ============================================================

def write_digest(notes, gi, apply_: bool) -> str:
    subjects: dict[str, list[Note]] = defaultdict(list)
    for n in notes:
        if n.type != 'Ad-hoc' or not n.title:
            continue
        if gi.match(n.title_plain, set()):
            continue
        if len(n.people) < 2:
            continue  # a solo block is not a group waiting to be declared
        subjects[n.title_plain.strip()].append(n)

    # Near-duplicate subjects ("AI bootcamp for leadership" vs "... planning
    # meeting") are the same standing meeting typed differently week to week.
    # Bucket on the pipeline's own normalization so they count together.
    buckets: dict[str, list[tuple[str, Note]]] = defaultdict(list)
    for subj, ns in subjects.items():
        key = ' '.join(sorted(
            mp_norm(subj).split()))  # order-insensitive token bag
        for n in ns:
            buckets[key].append((subj, n))

    ranked = sorted(buckets.values(), key=lambda v: -len(v))
    now = dt.datetime.now().strftime('%Y-%m-%dT%H:%M')
    # Deliberately NO `categories: [[Meetings]]` and no `type:` -- those two
    # keys are what Meetings.base filters on, and a report *about* meetings
    # must not enrol itself in the meeting list it is reporting on.
    out = [
        '---',
        'title: Unmatched Meeting Subjects',
        'tags: []',
        'classification: internal-use-only',
        f'created: {now}',
        f'updated: {now}',
        '---',
        '',
        'Ad-hoc meeting notes with 2+ attendees that no `Groups/` entry '
        'claims, ranked by how often the subject recurs. Generated by '
        '`Templates/Scripts/meeting_group_backfill.py --digest`.',
        '',
        f'A subject appearing **{RECURRENCE_THRESHOLD}+ times** is almost '
        'certainly a standing meeting with no group note yet. Fix it by '
        'creating `Groups/<name>.md`, or by adding the subject to an '
        'existing group\'s `aliases:` — then re-run the backfill.',
        '',
    ]
    likely = [v for v in ranked if len(v) >= RECURRENCE_THRESHOLD]
    rest = [v for v in ranked if len(v) < RECURRENCE_THRESHOLD]

    if likely:
        out += ['## Likely undeclared groups', '']
        for v in likely:
            variants = sorted({s for s, _ in v})
            out.append(f'### {variants[0]} ({len(v)}×)')
            if len(variants) > 1:
                out.append('Subject variants seen: '
                           + ', '.join(f'`{s}`' for s in variants))
            for _, n in sorted(v, key=lambda x: x[1].path.stem):
                out.append(f'- [[{n.path.stem}]] — {len(n.people)} attendees')
            out.append('')

    out += ['## Seen once or twice', '']
    for v in rest:
        variants = sorted({s for s, _ in v})
        links = ', '.join(f'[[{n.path.stem}]]'
                          for _, n in sorted(v, key=lambda x: x[1].path.stem))
        out.append(f'- **{variants[0]}** — {links}')
    out.append('')

    text = '\n'.join(out)
    if apply_:
        DIGEST_NOTE.parent.mkdir(parents=True, exist_ok=True)
        DIGEST_NOTE.write_text(text, encoding='utf-8')
    return text


_MP = None


def mp_norm(s: str) -> str:
    return _MP.normalize_for_group_match(s)


# ============================================================
# Main
# ============================================================

def main(argv=None) -> int:
    global _MP
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='write changes (default is a dry run)')
    ap.add_argument('--apply-overlap', action='store_true',
                    help='also write the lower-confidence attendee-overlap '
                         'matches (implies nothing without --apply)')
    ap.add_argument('--keep-title', action='store_true',
                    help='leave `title:` in place on converted notes')
    ap.add_argument('--no-history', action='store_true',
                    help='skip Meetings/History/')
    ap.add_argument('--digest', action='store_true',
                    help='write the unmatched-subject digest note')
    args = ap.parse_args(argv)

    _MP = mp = load_pipeline()
    people_idx = mp.PeopleIndex()
    people_idx.load()
    gi = mp.GroupsIndex(people_idx)
    gi.load()
    rev = stem_emails(mp, people_idx)

    notes = collect_notes(VAULT, not args.no_history)
    print(f'Scanned {len(notes)} meeting notes '
          f'({len(gi.stems)} groups, '
          f'{sum(len(v) for v in gi.stem_to_aliases.values())} aliases)\n')

    if args.digest:
        text = write_digest(notes, gi, args.apply)
        n_lines = text.count('\n')
        if args.apply:
            print(f'Wrote digest -> {DIGEST_NOTE.relative_to(VAULT)} '
                  f'({n_lines} lines)')
        else:
            print(text)
        return 0

    props, unmatched = build_proposals(notes, gi, mp, people_idx, rev)
    by_conf = Counter(p.confidence for p in props)

    backup_dir = BACKUP_ROOT / dt.datetime.now().strftime(
        'meeting-groups-%Y%m%dT%H%M%S')
    written = skipped = refused = 0

    for conf in ('title', 'overlap'):
        group = [p for p in props if p.confidence == conf]
        if not group:
            continue
        will_write = args.apply and (conf == 'title' or args.apply_overlap)
        head = f'{conf.upper()}-CONFIDENCE ({len(group)})'
        head += '  [will write]' if will_write else '  [report only]'
        print(head)
        print('-' * len(head))
        for p in sorted(group, key=lambda x: x.note.rel):
            print(f'  {p.note.rel:<40} {p.note.type:<10} -> [[{p.stem}]]'
                  f'   ({p.why})')
            if not will_write:
                skipped += 1
                continue
            new = p.render(args.keep_title)
            if new == p.note.text:
                continue
            err = validate(new, p.note, p.stem)
            if err:
                print(f'      !! REFUSED: {err}')
                refused += 1
                continue
            # Mirror the vault-relative path into the backup dir: Meetings/
            # and Meetings/History/ can hold the same basename for the same
            # slot on the same day, and a flat copy would lose one of them.
            dest = backup_dir / p.note.rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p.note.path, dest)
            p.note.path.write_text(new, encoding='utf-8')
            written += 1
        print()

    if unmatched:
        print(f'UNRESOLVED ({len(unmatched)})')
        print('-' * 20)
        for n in sorted(unmatched, key=lambda x: x.rel):
            print(f'  {n.rel:<40} {n.type:<10} '
                  f'{n.title_plain or "(no title)"}')
        print()

    print(f'Summary: {by_conf.get("title", 0)} by title, '
          f'{by_conf.get("overlap", 0)} by attendee overlap, '
          f'{len(unmatched)} unresolved')
    if args.apply:
        print(f'Wrote {written} note(s); {skipped} reported but not '
              f'written; {refused} refused by validation.')
        if written:
            print(f'Backups: {backup_dir}')
    else:
        print('Dry run — nothing written. Re-run with --apply.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
