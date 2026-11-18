"""
test_add_classification.py -- the backfill never lowers or hides a tier (v2.6 F22).

The backfill found frontmatter with its own regex, which needed "---" at
offset 0. A note starting with a byte-order mark looked untiered, and
--apply prepended a folder-default block above the real `restricted` one --
the block every reader then saw.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import add_classification as ac
import classification_tier as ct


def _run(tmp_path: Path, rel: str, text: str, apply: bool = True):
    note = tmp_path / "vault" / rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(text.encode("utf-8"))
    status, _, value = ac.process_file(note, tmp_path / "vault", tmp_path / "bak", apply)
    return status, value, note.read_bytes().decode("utf-8")


@pytest.mark.parametrize("text", [
    "﻿---\nclassification: restricted\n---\nPHI\n",              # the finding
    "﻿﻿---\nclassification: restricted\n---\nPHI\n",        # Obsidian strips two
    "---\nclassification: restricted   # PHI\n---\nPHI\n",
    "---\nclassification: secret\n---\nx\n",                          # unreadable value
    "---\nClassification: public\n---\nx\n",                          # near-miss key
    "---\nclassification: restricted\n--- end\nx\n",                  # "---"-prefixed close
])
def test_a_note_that_declares_a_tier_is_left_byte_identical(tmp_path, text):
    status, _, after = _run(tmp_path, "Notes/n.md", text)
    assert status in ("skip-has-class", "skip-unreadable")
    assert after == text


def test_a_bom_note_without_a_tier_gets_it_inside_its_own_block(tmp_path):
    status, value, after = _run(tmp_path, "Meetings/m.md", "﻿---\ntitle: x\n---\nbody\n")
    assert (status, value) == ("modified", "confidential")
    assert after == "﻿---\ntitle: x\nclassification: confidential\n---\nbody\n"
    assert ct.effective(after) == ("confidential", [])


def test_the_close_is_found_as_obsidian_finds_it(tmp_path):
    # The old regex needed a bare "---" close and would have run on to the
    # decoy below, inserting the tier into what Obsidian shows as body text.
    text = "---\ntitle: x\n--- end\nclassification: public\n---\nbody\n"
    status, _, after = _run(tmp_path, "Notes/n.md", text)
    assert status == "modified"
    assert after.startswith("---\ntitle: x\nclassification: internal-use-only\n--- end\n")
    assert ct.effective(after) == ("internal-use-only", [])


def test_no_frontmatter_gets_a_minimal_block(tmp_path):
    status, _, after = _run(tmp_path, "Notes/n.md", "just text\n")
    assert status == "modified"
    assert after == "---\nclassification: internal-use-only\n---\n\njust text\n"


def test_dry_run_writes_nothing(tmp_path):
    text = "﻿---\ntitle: x\n---\nbody\n"
    status, _, after = _run(tmp_path, "Notes/n.md", text, apply=False)
    assert status == "modified" and after == text


def test_an_unreadable_block_is_never_written_to(tmp_path):
    # An indented first line makes the block unreadable to the strict reader
    # (YAML reads the indented block as the mapping); the backfill leaves it.
    text = "---\n  indented: x\n---\nbody\n"
    status, _, after = _run(tmp_path, "Notes/n.md", text)
    assert status in ("skip-has-class", "skip-unreadable")
    assert after == text


def test_the_post_write_check_refuses_and_writes_nothing(tmp_path, monkeypatch):
    # declared() already flags every form the reader cannot read, so this is
    # the backstop: if a rewrite ever fails tier_not_lowered, nothing lands.
    monkeypatch.setattr(ac.classification_tier, "tier_not_lowered",
                        lambda before, after, expected=None: False)
    text = "---\ntitle: x\n---\nbody\n"
    status, value, after = _run(tmp_path, "Notes/n.md", text)
    assert (status, value) == ("refused", "internal-use-only")
    assert after == text
    assert not (tmp_path / "bak").exists()
    with pytest.raises(ac.Refused, match="does not read as `public`"):
        ac.insert_classification(text, "public")


@pytest.mark.parametrize("text", [
    "﻿---\ntitle: x\n---\nbody\n", "---\ntitle: x\n---\n", "x\n", "",
    "---\ntitle: x\n----\nbody\n",
])
def test_every_modified_note_reads_as_the_value_set(tmp_path, text):
    status, value, after = _run(tmp_path, "People/p.md", text)
    assert status == "modified"
    assert ct.effective(after) == (value, [])


# --- review RC5: a declaration no gate can evaluate is reported, not hidden ---

@pytest.mark.parametrize("text", [
    "---\nclassification: secret\n---\nx\n",
    "---\nclassification: restricted\nclassification: bogus\n---\nx\n",
])
def test_an_unreadable_declaration_is_reported_as_unreadable(tmp_path, text):
    status, _, after = _run(tmp_path, "Notes/n.md", text)
    assert status == "skip-unreadable"
    assert after == text


def test_unreadable_is_listed_by_path_in_the_summary(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "odd.md").write_text("---\nclassification: secret\n---\nx\n")
    monkeypatch.setattr(ac, "BACKUP_ROOT", tmp_path / "bak")
    monkeypatch.setattr("sys.argv", ["add_classification.py", str(vault)])
    ac.main()
    out = capsys.readouterr().out
    rel = str(Path("Notes") / "odd.md")
    assert "UNREADABLE TIER (left alone; every gate refuses it, fix by hand): " + rel in out


# --- review RC4: the note keeps its own line endings on disk ---

@pytest.mark.parametrize("nl", ["\r\n", "\r", "\n"])
def test_a_modified_note_differs_only_by_the_inserted_line(tmp_path, nl):
    text = nl.join(["---", "title: x", "---", "body", "more", ""])
    status, value, after = _run(tmp_path, "Notes/n.md", text)
    assert status == "modified"
    assert after == nl.join(["---", "title: x", "classification: " + value, "---",
                             "body", "more", ""])


# --- closing review RC14: a prepended block uses the note's line ending ---

@pytest.mark.parametrize("text,nl", [("body\r\nmore\r\n", "\r\n"), ("body\rmore\r", "\r"),
                                     ("\r\nbody\r\n", "\r\n"), ("body\n", "\n"), ("body", "\n")])
def test_a_prepended_block_keeps_the_notes_line_ending(tmp_path, text, nl):
    status, value, after = _run(tmp_path, "Notes/n.md", text)
    assert status == "modified"
    head = nl.join(["---", "classification: " + value, "---", ""])
    sep = "" if text.startswith(("\n", "\r")) else nl
    assert after == head + sep + text


# --- closing review RC13: a note over the read cap is reported by path ---

def test_an_oversize_note_is_reported_by_path(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    big = vault / "Notes" / "big.md"
    big.write_text("x" * 64)
    monkeypatch.setattr(ct, "MAX_NOTE_BYTES", 32)
    monkeypatch.setattr(ac, "BACKUP_ROOT", tmp_path / "bak")
    monkeypatch.setattr("sys.argv", ["add_classification.py", str(vault), "--apply"])
    ac.main()
    out = capsys.readouterr().out
    assert "TOO LARGE (over the 32-byte read cap; left alone, tier it by hand): " \
        + str(Path("Notes") / "big.md") in out
    assert big.read_text() == "x" * 64
