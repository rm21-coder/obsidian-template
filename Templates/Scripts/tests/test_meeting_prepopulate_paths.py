"""
test_meeting_prepopulate_paths.py -- an attendee name must not choose a path.

People stubs are named after an attendee's display name or email local part,
and both are text any outsider can author: anyone can send a meeting invite.
Before 2026-09-24 that text flowed unmodified into `target_dir / f"{stem}.md"`,
so a display name such as "../../Elsewhere/evil" wrote a stub outside People/.
Found by Microsoft M-DASH (five CWE-22 findings on canonical_name,
resolve_or_create_person and process_handoff).

These tests drive resolve_or_create_person for real against a throwaway vault
and assert on where files land, not on the helper in isolation -- the property
that matters is "nothing is written outside People/", and a unit test of the
sanitizer alone would not notice a future code path that bypasses it.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import meeting_prepopulate as mp

NOW = "2026-09-24T08:00:00-04:00"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    people = tmp_path / "vault" / "People"
    unresolved = people / "_Unresolved"
    people.mkdir(parents=True)
    monkeypatch.setattr(mp, "PEOPLE_DIR", people)
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", unresolved)
    return tmp_path


def _resolve(display: str | None, email: str = "") -> tuple:
    idx = mp.PeopleIndex()
    counters: Counter = Counter()
    stem, status = mp.resolve_or_create_person(
        {"display_name": display, "email": email}, {}, idx, NOW,
        dry_run=False, counters=counters)
    return stem, status, counters


def _outside_people(vault: Path) -> list[Path]:
    people = (vault / "vault" / "People").resolve()
    return [p for p in vault.rglob("*.md")
            if not p.resolve().is_relative_to(people)]


HOSTILE_NAMES = [
    "../../Elsewhere/evil",
    "..\\..\\Elsewhere\\evil",
    "/tmp/absolute-evil",
    "C:\\Windows\\evil",
    "..",
    "Smith, John/../../evil",
    "evil\x00name",
]


class TestAttendeeNamesCannotChooseAPath:

    @pytest.mark.parametrize("display", HOSTILE_NAMES)
    def test_hostile_display_name_stays_inside_people(
            self, vault: Path, display: str) -> None:
        stem, _status, _c = _resolve(display)
        assert _outside_people(vault) == [], (
            f"display name {display!r} wrote a stub outside People/")
        assert stem is not None
        assert "/" not in stem and "\\" not in stem

    @pytest.mark.parametrize("local", ["../../evil", "..\\evil", "a/b"])
    def test_hostile_email_local_part_stays_inside_people(
            self, vault: Path, local: str) -> None:
        _resolve(None, email=f"{local}@example.test")
        assert _outside_people(vault) == []

    def test_ordinary_names_are_unchanged(self, vault: Path) -> None:
        """The fix must not rename real people. Dots inside a name survive."""
        stem, _s, _c = _resolve("Jane A. Doe", email="jane@example.test")
        assert stem is not None and "Doe" in stem and "Jane" in stem
        assert _outside_people(vault) == []


class TestSafeStem:

    @pytest.mark.parametrize("raw,expected", [
        ("Doe, Jane", "Doe, Jane"),
        ("Jane A. Doe", "Jane A. Doe"),
        ("../../x", "x"),
        ("..", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ])
    def test_reduces_to_one_component(self, raw, expected) -> None:
        assert mp._safe_stem(raw) == expected
