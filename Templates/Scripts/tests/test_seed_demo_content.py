"""
test_seed_demo_content.py — tests for the demo-content seeder.

The seeder writes a synthetic dataset into a vault and, more importantly, can
delete it again. Deletion is the part worth testing: `--remove` walks real
content folders and unlinks files, so the guard that keeps it away from a
user's actual notes is a correctness property, not a nicety. If that guard
ever regresses, someone loses work.

The other properties covered here are the ones the rest of the workflow
depends on: everything written must be `classification: public` (or the repo's
classification audit hard-fails at install time), today's meetings must exist
(or the Morning Dashboard reads empty on the day of a demo), and the dataset
must cover every content folder (the whole point is that nothing is shipped as
committed files anymore).

Every test runs against a tmp vault via the OBSIDIAN_VAULT override, so the
real vault is never touched.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

import pytest

CONTENT_DIRS = ("Actions", "Categories", "Clippings", "Creations", "Daily",
                "Groups", "Knowledge", "Meetings", "People", "Topics")


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Import seed_demo_content bound to a throwaway vault.

    The module resolves VAULT at import time, so the env var has to be set
    before the import and the module has to be reloaded per test to pick up
    a fresh tmp_path.
    """
    vault = tmp_path / "Obsidian"
    for folder in CONTENT_DIRS:
        (vault / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBSIDIAN_VAULT", str(vault))
    sys.modules.pop("seed_demo_content", None)
    mod = importlib.import_module("seed_demo_content")
    importlib.reload(mod)
    yield mod, vault
    sys.modules.pop("seed_demo_content", None)


ANCHOR = dt.date(2026, 8, 14)   # a Friday


# ---------------------------------------------------------------------------
# The safety property: removal only ever touches marked demo files.
# ---------------------------------------------------------------------------

def test_remove_leaves_unmarked_notes_alone(seeded):
    """A real note sitting in Meetings/ must survive --remove.

    This is the whole reason removal keys off a frontmatter marker instead of
    a path glob. Someone will eventually run --remove in a vault that has real
    notes in it.
    """
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)

    real = vault / "Meetings" / "2026-08-14 1600.md"
    real.write_text(
        "---\ncategories:\n  - \"[[Meetings]]\"\n"
        "classification: internal-use-only\n---\n\nMy actual notes.\n",
        encoding="utf-8")
    other = vault / "People" / "Real, Person.md"
    other.write_text("---\nclassification: internal-use-only\n---\n\nreal\n",
                     encoding="utf-8")

    mod.remove(dry_run=False)

    assert real.exists(), "removal deleted an unmarked meeting note"
    assert other.exists(), "removal deleted an unmarked person note"
    assert real.read_text(encoding="utf-8").endswith("My actual notes.\n")


def test_remove_ignores_marked_files_outside_content_folders(seeded):
    """The folder allowlist is the second guard. A marked file in Templates/
    is not demo content and must not be swept up."""
    mod, vault = seeded
    stray = vault / "Templates" / "Scripts" / "note.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("---\ndemo_seed: generated\n---\n\nnot content\n",
                     encoding="utf-8")

    mod.seed(ANCHOR, dry_run=False)
    mod.remove(dry_run=False)

    assert stray.exists()


def test_remove_also_clears_legacy_static_marker(seeded):
    """An earlier version shipped part of the dataset as committed files
    marked `demo_seed: static`. Upgrading and running --remove should still
    give a clean sweep rather than stranding those files."""
    mod, vault = seeded
    legacy = vault / "People" / "Widget, Wanda.md"
    legacy.write_text(
        "---\nclassification: public\ndemo_seed: static\n---\n\nlegacy\n",
        encoding="utf-8")

    mod.remove(dry_run=False)

    assert not legacy.exists()


def test_remove_takes_the_whole_dataset(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    assert mod.seeded_files()

    mod.remove(dry_run=False)

    assert mod.seeded_files() == []
    for folder in CONTENT_DIRS:
        leftover = list((vault / folder).rglob("*.md"))
        assert not leftover, f"{folder} still holds {leftover}"


def test_content_folders_survive_removal(seeded):
    """Clearing the demo data must leave the vault structure behind -- a new
    user who removes it still needs the folders."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    mod.remove(dry_run=False)
    for folder in CONTENT_DIRS:
        assert (vault / folder).is_dir(), f"{folder} was removed"


def test_dry_run_removes_nothing(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    before = len(mod.seeded_files())
    assert before > 0

    mod.remove(dry_run=True)

    assert len(mod.seeded_files()) == before


def test_dry_run_seed_writes_nothing(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=True)
    assert mod.seeded_files() == []


# ---------------------------------------------------------------------------
# Coverage: one command produces the whole dataset.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", CONTENT_DIRS)
def test_every_content_folder_is_populated(seeded, folder):
    """Nothing ships as committed files anymore, so a single seed run has to
    fill every folder the dataset covers."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    assert list((vault / folder).rglob("*.md")), f"{folder} is empty"


def test_everything_written_is_classified_public(seeded):
    """The repo's classification audit hard-fails the installer on any
    content file that is not `classification: public`."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)

    bad = []
    for path, _ in mod.seeded_files():
        fm = mod.FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
        assert fm, f"{path} has no frontmatter"
        if "classification: public" not in fm.group(1):
            bad.append(path.name)
    assert not bad, f"not marked public: {bad}"


def test_every_file_carries_the_generated_marker(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    markers = {m for _, m in mod.seeded_files()}
    assert markers == {"generated"}


def test_required_frontmatter_keys_are_present(seeded):
    """vault_lint.py reports notes missing the keys their folder's template
    defines. The demo data should not trip the vault's own lint."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    required = {
        "People": ["categories", "classification", "tags"],
        "Meetings": ["categories", "type", "classification", "tags"],
        "Knowledge": ["tags", "classification"],
        "Creations": ["categories", "classification", "tags"],
        "Groups": ["classification", "tags"],
    }
    missing = []
    for folder, keys in required.items():
        for p in (vault / folder).rglob("*.md"):
            fm = mod.FRONTMATTER_RE.match(p.read_text(encoding="utf-8")).group(1)
            for k in keys:
                if not any(line.startswith(f"{k}:") for line in fm.splitlines()):
                    missing.append(f"{p.relative_to(vault)} missing {k}")
    assert not missing, missing


def test_no_broken_wikilinks(seeded):
    """Every [[link]] in the dataset must resolve to a note the seeder also
    writes -- an unresolved link is the most visible kind of demo-data rot."""
    import re
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)

    targets = set()
    for p, _ in mod.seeded_files():
        targets.add(p.stem)
        targets.add(str(p.relative_to(vault).with_suffix("")))

    # Attachments and Bases ship with the repo, not the seeder.
    known_external = {"placeholder-person.png", "Meetings.base", "People.base",
                      "Clippings.base", "Notes.base"}

    link_re = re.compile(r"\[\[([^\]|#]+?)(?:[#|][^\]]*)?\]\]")
    broken = []
    for p, _ in mod.seeded_files():
        for m in link_re.finditer(p.read_text(encoding="utf-8")):
            tgt = m.group(1).strip()
            if tgt in targets or tgt in known_external:
                continue
            broken.append(f"{p.relative_to(vault)} -> [[{tgt}]]")
    assert not broken, broken


def test_tags_used_come_from_the_taxonomy(seeded):
    """Knowledge/Tag Taxonomy.md is the tagger's hard allowlist. Demo notes
    should not use topical tags that are missing from it, or the very first
    tagger run reports drift against data we shipped."""
    import re
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)

    tax = (vault / "Knowledge" / "Tag Taxonomy.md").read_text(encoding="utf-8")
    canonical = set(re.findall(r"(?m)^-\s+([A-Za-z][A-Za-z0-9/]*)\s*$", tax))
    # Structural, not topical -- tag_clippings.py keeps these in IGNORE_TAGS.
    structural = {"note", "journal", "clippings"}

    offenders = {}
    for p, _ in mod.seeded_files():
        fm = mod.FRONTMATTER_RE.match(p.read_text(encoding="utf-8"))
        if not fm:
            continue
        block = re.search(r"(?m)^tags:\s*$\n((?:\s+-\s+.*\n?)+)", fm.group(1))
        if not block:
            continue
        for line in block.group(1).splitlines():
            t = line.strip().lstrip("-").strip()
            if t and t not in canonical and t not in structural:
                offenders.setdefault(t, str(p.relative_to(vault)))
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Contracts the dashboard depends on.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("anchor", [
    dt.date(2026, 8, 10),   # Monday
    dt.date(2026, 8, 12),   # Wednesday
    dt.date(2026, 8, 13),   # Thursday
    dt.date(2026, 8, 14),   # Friday
    dt.date(2026, 8, 15),   # Saturday
])
def test_today_always_has_three_meetings(seeded, anchor):
    """The Morning Dashboard lists Meetings/<today>*.md. If the anchor day
    produced none, a demo recorded that day shows an empty dashboard --
    including when the anchor lands on a weekend or on a day that already
    carries a recurring instance."""
    mod, vault = seeded
    mod.seed(anchor, dry_run=False)
    today = sorted((vault / "Meetings").glob(f"{anchor.isoformat()}*.md"))
    assert len(today) == 3, [p.name for p in today]


def test_today_covers_all_three_meeting_types(seeded):
    """Group, Individual, and Ad-hoc each render a different dashboard label,
    so today should exercise all three."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    types = set()
    for p in (vault / "Meetings").glob(f"{ANCHOR.isoformat()}*.md"):
        fm = mod.FRONTMATTER_RE.match(p.read_text(encoding="utf-8")).group(1)
        for line in fm.splitlines():
            if line.startswith("type:"):
                types.add(line.split(":", 1)[1].strip())
    assert types == {"Group", "Individual", "Ad-hoc"}


def test_something_is_created_today_for_the_new_today_section(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    today = ANCHOR.isoformat()
    fresh = [p for p, _ in mod.seeded_files()
             if p.parent.name in {"Clippings", "Creations"}
             and f"created: {today}" in p.read_text(encoding="utf-8")]
    assert fresh, "nothing dated today in Clippings/ or Creations/"


def test_seed_is_idempotent(seeded):
    """Re-seeding the same anchor must not accumulate files or change content --
    the maintainer re-runs this before every recording."""
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)
    first = {p: p.read_text(encoding="utf-8") for p, _ in mod.seeded_files()}

    mod.seed(ANCHOR, dry_run=False)
    second = {p: p.read_text(encoding="utf-8") for p, _ in mod.seeded_files()}

    assert first == second


def test_recurring_instances_link_to_an_existing_series_root(seeded):
    mod, vault = seeded
    mod.seed(ANCHOR, dry_run=False)

    linked = 0
    for p in (vault / "Meetings").glob("*.md"):
        fm = mod.FRONTMATTER_RE.match(p.read_text(encoding="utf-8")).group(1)
        for line in fm.splitlines():
            if line.startswith("series_link:"):
                target = vault / (line.split(":", 1)[1].strip() + ".md")
                assert target.exists(), f"{p.name} -> missing {target}"
                linked += 1
    assert linked > 0, "no recurring instances were generated"


# ---------------------------------------------------------------------------
# Task aging.
# ---------------------------------------------------------------------------

BODY = (
    "## Action Items\n\n"
    "- [ ] #task First item 🔺\n"
    "- [ ] #task Second item\n"
    "- [ ] #task Third item\n"
)


def test_recent_meeting_tasks_stay_open(seeded):
    mod, _ = seeded
    out = mod.age_tasks(BODY, ANCHOR - dt.timedelta(days=1), ANCHOR)
    assert out == BODY


def test_mid_age_meeting_keeps_one_task_open(seeded):
    mod, _ = seeded
    out = mod.age_tasks(BODY, ANCHOR - dt.timedelta(days=7), ANCHOR)
    assert out.count("- [ ] #task") == 1
    assert out.count("- [x] #task") == 2
    assert "Third item" in out.split("- [ ]")[1]


def test_old_meeting_closes_every_task(seeded):
    mod, _ = seeded
    out = mod.age_tasks(BODY, ANCHOR - dt.timedelta(days=30), ANCHOR)
    assert out.count("- [ ] #task") == 0
    assert out.count("- [x] #task") == 3
    assert "✅ " in out


def test_completed_tasks_drop_the_priority_marker(seeded):
    """A finished task carrying 🔺 renders as a high-priority done item in the
    Tasks plugin, which is noise in the completed list."""
    mod, _ = seeded
    out = mod.age_tasks(BODY, ANCHOR - dt.timedelta(days=30), ANCHOR)
    assert "🔺" not in out


def test_done_date_never_exceeds_the_anchor(seeded):
    """Done dates are stamped meeting-date + 3 days; a meeting three days ago
    must not be marked completed in the future."""
    mod, _ = seeded
    out = mod.age_tasks(BODY, ANCHOR - dt.timedelta(days=3), ANCHOR)
    for line in out.splitlines():
        if "✅" in line:
            stamped = dt.date.fromisoformat(line.split("✅")[1].strip())
            assert stamped <= ANCHOR


def test_age_tasks_leaves_bodies_without_tasks_untouched(seeded):
    mod, _ = seeded
    body = "## Notes\n\nNo action items here.\n"
    assert mod.age_tasks(body, ANCHOR - dt.timedelta(days=30), ANCHOR) == body


def test_age_tasks_ignores_untagged_checkboxes(seeded):
    """This vault's Tasks plugin uses `#task` as a global filter, so a plain
    checkbox is not a task and must not be completed."""
    mod, _ = seeded
    body = "- [ ] grocery list item\n- [ ] #task real work\n"
    out = mod.age_tasks(body, ANCHOR - dt.timedelta(days=30), ANCHOR)
    assert "- [ ] grocery list item" in out
    assert "- [x] #task real work" in out
