#!/usr/bin/env python3
"""Backfill Email-Work / Email-Personal frontmatter into People notes.

Scans ~/Obsidian/People/*.md. For files where both Email-Work and Email-Personal
are empty/missing in frontmatter, looks in the body for plausible email
addresses and writes the best one to frontmatter.

Dry-run by default — print what would change. Pass --apply to actually write.

Conservative defaults:
- Only writes if the email's local-part shares a substring with the file's
  canonical name (last name or first name). This avoids capturing an email
  that happens to be quoted in someone else's Bio (e.g., "...wrote to
  joe@example.com about..." in Alice's note).
- Personal-domain emails go to Email-Personal; everything else to Email-Work.
- If a file has multiple emails, prefers work-domain over personal.
- Files with no email in body are reported and left untouched.

Usage:
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/backfill_people_email.py            # dry-run
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/backfill_people_email.py --apply    # write
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/backfill_people_email.py --verbose
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from collections import Counter

PEOPLE_DIR = Path.home() / 'Obsidian' / 'People'

PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com',
    'icloud.com', 'me.com', 'aol.com', 'protonmail.com',
    'msn.com', 'comcast.net', 'verizon.net', 'live.com',
    'mac.com', 'sbcglobal.net', 'cox.net', 'att.net',
}

EMAIL_RE = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')
LINE_KEY_RE = re.compile(r'^(Email-Work|Email-Personal)\s*:\s*(.*)$')


def split_frontmatter(text: str) -> tuple[list[str], list[str]]:
    """Return (frontmatter_lines, body_lines) WITHOUT the '---' fences in
    frontmatter_lines. Returns ([], lines) if no frontmatter detected."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != '---':
        return [], lines
    for i in range(1, len(lines)):
        if lines[i].rstrip() == '---':
            return lines[1:i], lines[i + 1:]
    return [], lines


def parse_fm_emails(fm_lines: list[str]) -> tuple[str | None, str | None,
                                                   int | None, int | None]:
    """Return (work_value, personal_value, work_line_idx, personal_line_idx).
    None values mean the field is absent. Empty strings mean the field exists
    but is unpopulated."""
    work_val: str | None = None
    pers_val: str | None = None
    work_idx: int | None = None
    pers_idx: int | None = None
    for i, line in enumerate(fm_lines):
        m = LINE_KEY_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == 'Email-Work':
            work_val = val
            work_idx = i
        elif key == 'Email-Personal':
            pers_val = val
            pers_idx = i
    return work_val, pers_val, work_idx, pers_idx


def canonical_name_parts(filename_stem: str) -> tuple[str, str]:
    """For 'Lastname, Firstname M.' return ('lastname', 'firstname')."""
    if ',' in filename_stem:
        last, first = filename_stem.split(',', 1)
        first = first.strip().split(' ')[0]
        return last.strip().lower(), first.strip().lower()
    return filename_stem.lower(), ''


def email_matches_name(email: str, last: str, first: str) -> bool:
    local = email.split('@', 1)[0].lower()
    if not last:
        return True  # can't check, accept
    if last in local:
        return True
    if first and first in local:
        return True
    # initials, e.g., 'tdeweese' matches 'deweese' with 't'
    if last and len(last) >= 4 and (first[:1] + last) == local:
        return True
    return False


def classify(email: str) -> str:
    domain = email.split('@', 1)[1].lower() if '@' in email else ''
    return 'personal' if domain in PERSONAL_DOMAINS else 'work'


def pick_best(emails: list[str], last: str, first: str
              ) -> tuple[str | None, str]:
    """Return (chosen_email, reason). Reason ∈ {'work-match', 'personal-match',
    'name-mismatch', 'none-found'}."""
    if not emails:
        return None, 'none-found'
    name_matches = [e for e in emails if email_matches_name(e, last, first)]
    candidates = name_matches if name_matches else emails
    work = [e for e in candidates if classify(e) == 'work']
    personal = [e for e in candidates if classify(e) == 'personal']
    if work:
        return work[0], 'work-match' if name_matches else 'work-no-namematch'
    if personal:
        return (personal[0],
                'personal-match' if name_matches else 'personal-no-namematch')
    return None, 'none-found'


def insert_into_frontmatter(text: str, work_idx: int | None,
                            pers_idx: int | None,
                            field: str, value: str) -> str:
    """Surgically write field=value into frontmatter, preserving everything
    else verbatim."""
    fm_lines, body_lines = split_frontmatter(text)

    if field == 'Email-Work':
        target_idx = work_idx
    else:
        target_idx = pers_idx

    new_line = f'{field}: {value}'

    if target_idx is not None:
        # Replace the existing empty line
        fm_lines[target_idx] = new_line
    else:
        # Insert. Place after the OTHER email field if it exists,
        # otherwise after 'Mobile Phone:' if it exists, else at end.
        other_idx = pers_idx if field == 'Email-Work' else work_idx
        if other_idx is not None:
            fm_lines.insert(other_idx + 1, new_line)
        else:
            # find Mobile Phone
            for i, ln in enumerate(fm_lines):
                if ln.startswith('Mobile Phone'):
                    fm_lines.insert(i, new_line)
                    break
            else:
                fm_lines.append(new_line)

    # Reassemble: original text might end with or without trailing newline.
    # Detect by checking original suffix.
    out_lines = ['---'] + fm_lines + ['---'] + body_lines
    out = '\n'.join(out_lines)
    if text.endswith('\n') and not out.endswith('\n'):
        out += '\n'
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='Actually write changes (default: dry-run).')
    ap.add_argument('--verbose', action='store_true',
                    help='Print per-file outcomes for all files, not just '
                         'changes.')
    ap.add_argument('--people-dir', default=str(PEOPLE_DIR),
                    help='Override the People folder location.')
    args = ap.parse_args()

    people_dir = Path(args.people_dir)
    if not people_dir.is_dir():
        print(f'ERROR: not a directory: {people_dir}', file=sys.stderr)
        return 2

    counts: Counter[str] = Counter()
    changes: list[tuple[Path, str, str]] = []
    no_emails: list[Path] = []
    flagged_mismatch: list[tuple[Path, list[str]]] = []
    malformed: list[Path] = []

    files = sorted(people_dir.glob('*.md'))
    for p in files:
        try:
            text = p.read_text(encoding='utf-8')
        except OSError as e:
            print(f'SKIP (read error): {p.name} — {e}', file=sys.stderr)
            counts['read-error'] += 1
            continue

        fm_lines, body_lines = split_frontmatter(text)
        if not fm_lines:
            malformed.append(p)
            counts['malformed-frontmatter'] += 1
            continue

        work_val, pers_val, work_idx, pers_idx = parse_fm_emails(fm_lines)

        if (work_val and work_val.strip()) or (pers_val and pers_val.strip()):
            counts['already-populated'] += 1
            if args.verbose:
                print(f'  SKIP (already has email): {p.name}')
            continue

        # Scan body for emails
        body_text = '\n'.join(body_lines)
        # Exclude email-like patterns inside Source-tracking HTML comments
        # only weakly — keep them but lower priority. For now include all.
        found = EMAIL_RE.findall(body_text)
        # Dedupe preserving order
        seen: set[str] = set()
        uniq_emails: list[str] = []
        for e in found:
            el = e.lower()
            if el not in seen:
                seen.add(el)
                uniq_emails.append(e)

        last, first = canonical_name_parts(p.stem)
        chosen, reason = pick_best(uniq_emails, last, first)

        if chosen is None:
            no_emails.append(p)
            counts['no-email-found'] += 1
            if args.verbose:
                print(f'  NO EMAIL: {p.name}')
            continue

        if reason.endswith('-no-namematch'):
            flagged_mismatch.append((p, uniq_emails))
            counts['flagged-no-namematch'] += 1
            print(f'  FLAG (no name match, manual review): {p.name} — '
                  f'found {uniq_emails}')
            continue

        field = ('Email-Personal' if classify(chosen) == 'personal'
                 else 'Email-Work')
        changes.append((p, field, chosen))
        counts[f'would-backfill-{field}'] += 1
        action = 'BACKFILL' if args.apply else 'WOULD-BACKFILL'
        print(f'  {action} {field}={chosen}: {p.name}')

        if args.apply:
            new_text = insert_into_frontmatter(text, work_idx, pers_idx,
                                               field, chosen)
            p.write_text(new_text, encoding='utf-8')

    print()
    print('=' * 60)
    print(f'SUMMARY ({"APPLIED" if args.apply else "DRY-RUN"})')
    print('=' * 60)
    print(f'Total People files scanned:           {len(files)}')
    for k in ['already-populated', 'no-email-found',
              'flagged-no-namematch',
              'would-backfill-Email-Work',
              'would-backfill-Email-Personal',
              'malformed-frontmatter', 'read-error']:
        if counts[k]:
            print(f'  {k:36s}  {counts[k]}')
    print()

    if malformed:
        print('Files with malformed frontmatter (untouched):')
        for p in malformed[:20]:
            print(f'  {p.name}')
        if len(malformed) > 20:
            print(f'  ... and {len(malformed) - 20} more')
        print()

    if not args.apply and (changes or flagged_mismatch):
        print('Re-run with --apply to write changes.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
