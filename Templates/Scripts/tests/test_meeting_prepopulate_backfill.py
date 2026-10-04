"""
test_meeting_prepopulate_backfill.py -- People email fields: read one line,
and backfill only addresses that plausibly belong to the person.

Until 2026-10 the Email-Work / Email-Personal / preferred_name readers used
`\\s*:\\s*(.*)$`. `\\s` matches a newline, so an empty `Email-Work:` captured
the next line ("Mobile Phone: ...") as its value: the People index filled
with junk email keys, and organic backfill saw every empty field as already
populated, so it never wrote.

Fixing that turns backfill back on, and backfill is reached by a NAME match
on an invite attendee -- a display name the invite's sender chose. Without a
second rule, "Pat Quinn <pat@attacker.example>" would plant the attacker's
address on the real Pat Quinn's note, and every later invite from it would
email-match there. So backfill now writes only addresses in one of the user's
org domains or a domain already on the note.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

import pytest

import meeting_prepopulate as mp

NOW = "2026-10-05T08:00:00-04:00"
STUB = """---
categories:
  - "[[Categories/People]]"
Title:
Organization:
Email-Personal: {personal}
Email-Work:
Mobile Phone: 555-0100
preferred_name:
aliases:
classification: confidential
---

Body text.
"""


@pytest.fixture
def people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "People"
    d.mkdir()
    monkeypatch.setattr(mp, "PEOPLE_DIR", d)
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", d / "_Unresolved")
    monkeypatch.setattr(mp, "CONFIG_FILE", tmp_path / "cfg" / "meeting_prepopulate.json")
    return d


def _note(people: Path, personal: str = "") -> Path:
    p = people / "Quinn, Pat.md"
    p.write_text(STUB.format(personal=personal), encoding="utf-8")
    return p


def _resolve(email: str, org_domains: frozenset = frozenset()):
    idx = mp.PeopleIndex()
    idx.load()
    idx.org_domains = org_domains
    counters: Counter = Counter()
    stem, status = mp.resolve_or_create_person(
        {"display_name": "Pat Quinn", "email": email}, {}, idx, NOW,
        dry_run=False, counters=counters)
    return stem, status, counters


def test_empty_field_does_not_capture_the_next_line(people):
    _note(people)
    idx = mp.PeopleIndex()
    idx.load()

    assert idx.email_to_stem == {}
    assert idx.stem_to_emptyfields == {"Quinn, Pat": {"Email-Work", "Email-Personal"}}
    assert mp.normalize_name_for_match("aliases:") not in idx.name_to_stem


def test_populated_field_is_still_indexed(people):
    _note(people, personal="pat@home.example")
    idx = mp.PeopleIndex()
    idx.load()

    assert idx.lookup_by_email("PAT@home.example") == "Quinn, Pat"


def test_backfill_writes_an_org_domain_address(people):
    note = _note(people)

    stem, status, counters = _resolve("pat@example.edu",
                                      frozenset({"example.edu"}))

    assert (stem, status) == ("Quinn, Pat", "name-match")
    assert counters["organic-email-backfill"] == 1
    text = note.read_text(encoding="utf-8")
    assert "\nEmail-Work: pat@example.edu\nMobile Phone: 555-0100\n" in text


def test_backfill_refuses_a_foreign_domain(people, caplog):
    note = _note(people)
    before = note.read_text(encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="meeting_prepopulate"):
        stem, status, counters = _resolve("pat@attacker.example",
                                          frozenset({"example.edu"}))

    assert (stem, status) == ("Quinn, Pat", "name-match")
    assert counters["organic-email-backfill"] == 0
    assert note.read_text(encoding="utf-8") == before
    assert ("organic-backfill SKIPPED Email-Work on Quinn, Pat.md: domain "
            "attacker.example is free mail, or neither an org domain nor "
            "already on the note"
            ) in caplog.text


def test_backfill_accepts_a_domain_already_on_the_note(people):
    note = _note(people, personal="pat@vendor.example")

    _stem, _status, counters = _resolve("pat.quinn@vendor.example")

    assert counters["organic-email-backfill"] == 1
    assert "\nEmail-Work: pat.quinn@vendor.example\n" in note.read_text(encoding="utf-8")


def test_backfill_never_edits_the_body(people):
    note = people / "Quinn, Pat.md"
    note.write_text("---\nTitle: x\n---\n\nEmail-Work:\n", encoding="utf-8")

    _stem, _status, counters = _resolve("pat@example.edu",
                                        frozenset({"example.edu"}))

    assert counters["organic-email-backfill"] == 0
    assert note.read_text(encoding="utf-8") == "---\nTitle: x\n---\n\nEmail-Work:\n"


def test_org_domains_come_from_local_config_and_the_user(people):
    cfg = mp.CONFIG_FILE.parent
    cfg.mkdir()
    mp.CONFIG_FILE.write_text(json.dumps({"tenant_domains": "Corp.Example.edu, "}),
                              encoding="utf-8")
    (cfg / "meeting_pull.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"tenant_domains": ["med.example.edu"]}).encode("utf-8"))

    assert mp.load_org_domains("Ada@Example.edu") == frozenset(
        {"corp.example.edu", "med.example.edu", "example.edu"})


def test_org_domains_without_config_is_just_the_user_domain(people):
    assert mp.load_org_domains("ada@example.edu") == frozenset({"example.edu"})


def test_process_handoff_hands_the_org_domains_to_the_index():
    src = Path(mp.__file__).read_text(encoding="utf-8")
    assert "people_idx.org_domains = load_org_domains(user_email)" in src


def test_an_empty_type_does_not_read_the_next_line(tmp_path: Path) -> None:
    """Same newline-crossing pattern as the email fields: an emptied `type:`
    picked up the next frontmatter line as the meeting type."""
    note = tmp_path / "m.md"
    note.write_text("---\ntype:\ngroup: x\n---\nbody\n", encoding="utf-8")
    assert mp.read_note_type(note)[0] is None
    note.write_text("---\ntype: Group\n---\n", encoding="utf-8")
    assert mp.read_note_type(note)[0] == "Group"

# --- Shared free-mail domains identify no one -------------------------------
# A domain shared by millions of strangers is not evidence that an address
# belongs to this person, nor is it the user's organisation. Adversarial
# review of the first version of this guard found all three cases below.

def test_free_mail_domain_on_the_note_does_not_vouch(people, caplog):
    note = people / "Quinn, Pat.md"
    note.write_text(STUB.format(personal="").replace(
        "Email-Work:\n", "Email-Work: pat@gmail.com\n"), encoding="utf-8")
    before = note.read_text(encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="meeting_prepopulate"):
        _stem, _status, counters = _resolve("evil@gmail.com")

    assert counters["organic-email-backfill"] == 0
    assert note.read_text(encoding="utf-8") == before
    assert ("organic-backfill SKIPPED Email-Personal on Quinn, Pat.md: domain "
            "gmail.com is free mail, or neither an org domain nor "
            "already on the note"
            ) in caplog.text


def test_proton_me_is_personal_not_a_work_address(people):
    note = _note(people, personal="pat@proton.me")
    before = note.read_text(encoding="utf-8")

    _stem, _status, counters = _resolve("evil@proton.me")

    assert counters["organic-email-backfill"] == 0
    assert note.read_text(encoding="utf-8") == before
    assert "proton.me" in mp.PERSONAL_DOMAINS


def test_users_own_free_mail_domain_is_not_an_org_domain(people):
    cfg = mp.CONFIG_FILE.parent
    cfg.mkdir()
    mp.CONFIG_FILE.write_text(json.dumps(
        {"tenant_domains": ["outlook.com", "corp.example.edu"]}), encoding="utf-8")

    assert mp.load_org_domains("rich@gmail.com") == frozenset({"corp.example.edu"})


def test_free_mail_domain_passed_as_org_domain_is_still_refused(people, caplog):
    note = _note(people)
    before = note.read_text(encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="meeting_prepopulate"):
        changed = mp.organic_email_backfill(
            "Quinn, Pat", "evil@gmail.com", False, True,
            frozenset({"gmail.com"}))

    assert changed is False
    assert note.read_text(encoding="utf-8") == before
    assert "organic-backfill SKIPPED Email-Personal on Quinn, Pat.md" in caplog.text
