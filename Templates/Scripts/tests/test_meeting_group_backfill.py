"""
test_meeting_group_backfill.py — the reason an overlap proposal gives.

meeting_group_backfill.py borrows GroupsIndex.match so its proposals can never
drift from the pipeline. The matcher counts overlap in people, but the
backfill used to explain its proposals in addresses: a member on file at two
addresses counted twice, so a two-person meeting was reported as "3/3
attendees overlap" against a decision made on 2 of 2 people.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import classification_tier
import meeting_group_backfill as bf
import meeting_prepopulate as mp


def _person(people: Path, stem: str, work: str = "", personal: str = "") -> None:
    (people / f"{stem}.md").write_text(
        "---\n"
        f"Email-Personal: {personal}\n"
        f"Email-Work: {work}\n"
        "---\n", encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    people, groups, meetings = (tmp_path / d for d in ("People", "Groups", "Meetings"))
    for d in (people, groups, meetings):
        d.mkdir()
    monkeypatch.setattr(mp, "PEOPLE_DIR", people)
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", people / "_Unresolved")
    monkeypatch.setattr(mp, "GROUPS_DIR", groups)

    _person(people, "Delta, Di", work="di@corp.example", personal="di@home.example")
    _person(people, "Echo, Ed", work="ed@corp.example")
    (groups / "Pair.md").write_text(
        "---\ntitle: Pair\n---\n\n[[Delta, Di]]\n[[Echo, Ed]]\n", encoding="utf-8")
    (meetings / "2026-10-01 Untitled.md").write_text(
        "---\ncategories:\n  - \"[[Meetings]]\"\ntype: Group\n"
        "people:\n  - \"[[Delta, Di]]\"\n  - \"[[Echo, Ed]]\"\n---\n\nbody\n",
        encoding="utf-8")
    return tmp_path


def test_overlap_reason_counts_people_not_addresses(vault: Path) -> None:
    pi = mp.PeopleIndex()
    pi.load()
    gi = mp.GroupsIndex(pi)
    gi.load()
    rev = bf.stem_emails(mp, pi)
    note = bf.Note(vault / "Meetings" / "2026-10-01 Untitled.md", vault)

    props, unmatched = bf.build_proposals([note], gi, mp, pi, rev)

    assert not unmatched
    assert [(p.stem, p.confidence) for p in props] == [("Pair", "overlap")]
    assert props[0].why == "2/2 attendees overlap"


# ---- the digest (F-20261020 5268559, 5278121) -------------------------------

class _NoGroups:
    def match(self, subject, emails):
        return None


def _adhoc(tmp_path: Path, stem: str, subject: str, tier="confidential"):
    from types import SimpleNamespace
    head = f"classification: {tier}\n" if tier else ""
    return SimpleNamespace(type="Ad-hoc", title=subject, title_plain=subject,
                           people=["[[A]]", "[[B]]"], path=tmp_path / f"{stem}.md",
                           text=f"---\n{head}type: Ad-hoc\n---\n")


def _digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subjects: list,
            tiers=None) -> str:
    monkeypatch.setattr(bf, "_MP", mp)
    tiers = tiers or ["confidential"] * len(subjects)
    notes = [_adhoc(tmp_path, f"2026-10-0{i + 1} m", s, tiers[i])
             for i, s in enumerate(subjects)]
    return bf.write_digest(notes, _NoGroups(), apply_=False)


def test_digest_subject_cannot_load_an_image_or_carry_markup(tmp_path, monkeypatch):
    evil = "Sync ![x](https://attacker.example/p.png?d=1) <img src=x> [o](obsidian://open) #task `x` **b**"
    text = _digest(tmp_path, monkeypatch, [evil])
    line = next(l for l in text.splitlines() if l.startswith("- **"))
    assert "![" not in line and "<img" not in line and "obsidian://" not in line
    assert "\\#task" in line and "\\`x\\`" in line and "\\*\\*b\\*\\*" in line
    assert line.startswith("- **Sync \\!\u200b\\[x\\]\\(https://attacker.example/p.png?d\\=1\\) \\<\u200bimg")


def test_recurring_subject_variants_are_not_in_a_code_span(tmp_path, monkeypatch):
    subs = ["Weekly sync secret", "Weekly sync secret", "Sync weekly `]] [[Secret"]
    text = _digest(tmp_path, monkeypatch, subs)
    variants = next(l for l in text.splitlines() if l.startswith("Subject variants seen:"))
    assert "`" not in variants.replace("\\`", "")
    assert "\\]\\] \\[\\[Secret" in variants


def test_digest_is_classified_as_its_meeting_sources(tmp_path, monkeypatch):
    text = _digest(tmp_path, monkeypatch, ["Weekly sync", "Other"])
    assert "\nclassification: confidential\n" in text
    assert "internal-use-only" not in text


# ---- v2.6 F21: the digest is at least the tier of every meeting it lists ----

@pytest.mark.parametrize("tiers,expected", [
    (["confidential", "confidential"], "confidential"),
    (["internal-use-only", "public"], "confidential"),       # never below the floor
    (["confidential", "restricted"], "restricted"),
    (["confidential", None], "restricted"),                 # untiered fails closed
    (["confidential", "secret"], "restricted"),             # unreadable fails closed
])
def test_digest_tier_is_the_highest_source_tier(tmp_path, monkeypatch, tiers, expected):
    text = _digest(tmp_path, monkeypatch, ["Weekly sync", "Board prep"], tiers)
    assert classification_tier.effective(text) == (expected, [])


def test_a_restricted_meeting_not_listed_does_not_raise_the_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "_MP", mp)
    listed = _adhoc(tmp_path, "2026-10-01 m", "Weekly sync")
    solo = _adhoc(tmp_path, "2026-10-02 m", "PHI review", "restricted")
    solo.people = ["[[A]]"]              # a solo block is never listed
    text = bf.write_digest([listed, solo], _NoGroups(), apply_=False)
    assert "PHI review" not in text
    assert classification_tier.effective(text) == ("confidential", [])
