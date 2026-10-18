"""add_people_fields.py: existing People notes get the template's newer email
fields, empty and in template order, and nothing else in the note changes."""
from __future__ import annotations

from pathlib import Path

import add_people_fields as apf

OLD = ("---\ncategories:\n  - \"[[Categories/People]]\"\nTitle: Engineer\n"
       "Email-Personal:\nEmail-Work: bob@example.edu\nMobile Phone: 555-0100\n"
       "tags: []\n---\n# Bob\n\nBody with Email-Work: in prose.\n")


def test_both_fields_are_added_after_email_work_in_template_order() -> None:
    new, added = apf.add_fields(OLD)
    assert added == ["Email-Work-2", "Email-Priority"]
    assert "Email-Work: bob@example.edu\nEmail-Work-2:\nEmail-Priority:\nMobile Phone: 555-0100\n" in new
    assert new.split("\n---\n", 1)[1] == OLD.split("\n---\n", 1)[1], "body untouched"
    assert new.replace("Email-Work-2:\nEmail-Priority:\n", "") == OLD


def test_an_existing_second_address_is_kept_and_priority_follows_it() -> None:
    text = OLD.replace("Email-Work: bob@example.edu\n",
                       "Email-Work: bob@example.edu\nEmail-Work-2: b2@example.edu\n")
    new, added = apf.add_fields(text)
    assert added == ["Email-Priority"]
    assert "Email-Work-2: b2@example.edu\nEmail-Priority:\nMobile Phone" in new


def test_idempotent() -> None:
    once, _ = apf.add_fields(OLD)
    twice, added = apf.add_fields(once)
    assert added == [] and twice == once


def test_crlf_notes_stay_crlf() -> None:
    new, added = apf.add_fields(OLD.replace("\n", "\r\n"))
    assert added and "\n" not in new.replace("\r\n", "")
    assert "Email-Work-2:\r\nEmail-Priority:\r\n" in new


def test_a_list_value_stays_with_its_key() -> None:
    text = OLD.replace("Email-Work: bob@example.edu\n", "Email-Work:\n  - bob@example.edu\n")
    new, _ = apf.add_fields(text)
    assert "Email-Work:\n  - bob@example.edu\nEmail-Work-2:\n" in new


def test_notes_without_the_anchor_are_skipped() -> None:
    assert apf.add_fields("# no frontmatter\n") == (None, "no frontmatter")
    assert apf.add_fields("---\nTitle: x\n---\n") == (None, "no Email-Work line")
    assert apf.add_fields("---\nEmail-Work: x\n") == (None, "unterminated frontmatter")


def test_dry_run_writes_nothing_and_write_is_refused_while_obsidian_runs(
        tmp_path: Path, monkeypatch, capsys) -> None:
    people = tmp_path / "People"
    people.mkdir()
    note = people / "Bob.md"
    note.write_text(OLD, encoding="utf-8")
    (people / "Index.md").write_text("---\nTitle: x\n---\n", encoding="utf-8")
    assert apf.main(["--vault", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "would update 1 note(s)" in out and "skipped People/Index.md: no Email-Work line" in out
    assert note.read_text(encoding="utf-8") == OLD

    monkeypatch.setattr(apf, "obsidian_running", lambda: True)
    assert apf.main(["--vault", str(tmp_path), "--write"]) == 1
    assert "Obsidian is running" in capsys.readouterr().out
    assert note.read_text(encoding="utf-8") == OLD

    monkeypatch.setattr(apf, "obsidian_running", lambda: False)
    assert apf.main(["--vault", str(tmp_path), "--write"]) == 0
    assert "Email-Priority:" in note.read_text(encoding="utf-8")
    assert not list(people.glob("*.tmp"))


def test_every_people_email_field_has_a_shipped_property_type() -> None:
    """An empty, untyped property gets a guessed type: Email-Priority came up
    as a number, and setting it to "high" asked to convert the type
    (2026-10-18). The template's email fields are typed text up front."""
    import json
    repo = Path(__file__).resolve().parents[3]
    types = json.loads((repo / ".obsidian" / "types.json").read_text(encoding="utf-8"))["types"]
    tpl = (repo / "Templates" / "People Template.md").read_text(encoding="utf-8")
    fields = [l.split(":")[0] for l in tpl.split("---")[1].splitlines() if l.startswith("Email")]
    assert set(fields) >= {"Email-Work-2", "Email-Priority"}
    for f in fields:
        assert types.get(f) == "text", f
