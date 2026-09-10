"""meeting_prepopulate.parse_fm_aliases — the frontmatter alias parser.

Ported from a standalone runner that lived beside the scripts and was never
collected by this suite, so its cases only ran when someone remembered to
invoke them by hand. The regression they guard is worth having in CI.

The bug: PeopleIndex carried a stricter private copy of this parser that
matched only UNINDENTED block lists. Obsidian's Properties editor indents, so
every alias written through the UI was either dropped or indexed under a
corrupt "- name" key — silently, and only for aliases the user added the
normal way rather than by hand-editing YAML.

Two shapes carry most of the risk and are called out separately below: an
empty `aliases:` must not swallow the following key's line, and a blank list
item must not discard its siblings.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "mp_aliases", SCRIPTS / "meeting_prepopulate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mp = _load()


@pytest.mark.parametrize("frontmatter,expected", [
    # Indented block list — what the Properties editor writes, and the shape
    # the stricter private copy of this parser used to drop entirely.
    ("aliases:\n  - Bob Smith\n", ["Bob Smith"]),
    ("aliases:\n    - Bob Smith\n    - Bobby\n", ["Bob Smith", "Bobby"]),
    ("aliases:\n\t- Tabbed\n", ["Tabbed"]),
    # Unindented block list — hand-edited YAML.
    ("aliases:\n- Bob Smith\n", ["Bob Smith"]),
    # Quoted scalars inside a block list.
    ('aliases:\n  - "Bob Smith"\n', ["Bob Smith"]),
    ("aliases:\n  - 'Bob Smith'\n", ["Bob Smith"]),
    # Inline flow list and bare scalar.
    ("aliases: [A, B]\n", ["A", "B"]),
    ('aliases: ["A", "B"]\n', ["A", "B"]),
    ("aliases: Solo\n", ["Solo"]),
    ("aliases: []\n", []),
    # Block must stop at the next key, and need not be the first key.
    ("aliases:\n  - Bob Smith\ntags: []\n", ["Bob Smith"]),
    ("other: 1\naliases:\n  - Bob\nz: 2\n", ["Bob"]),
    # Absent key.
    ("preferred_name: Bob\n", []),
])
def test_alias_shapes_parse(frontmatter: str, expected: list[str]) -> None:
    assert mp.parse_fm_aliases(frontmatter) == expected


@pytest.mark.parametrize("frontmatter", [
    "aliases:\n",
    "aliases:\nclassification: confidential\n",
    "aliases:\ntags: []\n",
    "aliases:   \nclassification: x\n",
])
def test_an_empty_aliases_key_does_not_swallow_the_next_line(
        frontmatter: str) -> None:
    """An empty `aliases:` followed by another key must yield nothing.

    Returning the next key's text as an alias would index a person under
    "classification: confidential" — a garbage People key that then attracts
    wikilinks from the meeting pipeline.
    """
    assert mp.parse_fm_aliases(frontmatter) == []


@pytest.mark.parametrize("frontmatter,expected", [
    ("aliases:\n  -\n  - Bobby\n", ["Bobby"]),
    ("aliases:\n  - Bobby\n  -\n", ["Bobby"]),
])
def test_a_blank_item_does_not_discard_its_siblings(
        frontmatter: str, expected: list[str]) -> None:
    """A stray `-` is easy to leave behind while editing. Dropping the whole
    list because of it would silently unlink every alias on that person."""
    assert mp.parse_fm_aliases(frontmatter) == expected
