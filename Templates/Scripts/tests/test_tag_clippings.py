"""Frontmatter parsing in the two taggers must survive hostile frontmatter.

A converted document keeps its own leading --- block as frontmatter
(markitdown_cleanup), so the YAML the taggers parse can be outsider text.
About a thousand nested brackets exhaust PyYAML's recursion and raise
RecursionError, which is not a YAMLError. In tag_clippings.py that escaped
collect_all_tags(), which has no handler, so on a vault without a taxonomy
file every scheduled run died at startup.

The anthropic SDK is faked in sys.modules (as test_voice_cleanup does) and
HOME points at a tmp dir so load_dotenv at import reads nothing real.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

DEEP = "---\ntitle: x\nevil: " + "[" * 5000 + "]" * 5000 + "\n---\nbody text\n"
ORDINARY = "---\ntitle: Ordinary\ntags:\n  - Strategy\n  - AI\n---\nbody text\n"


def _import(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module(name)
    monkeypatch.delitem(sys.modules, name, raising=False)
    return mod


@pytest.fixture
def tagger(monkeypatch, tmp_path):
    return _import("tag_clippings", monkeypatch, tmp_path)


@pytest.fixture
def rag_tagger(monkeypatch, tmp_path):
    return _import("tag_clippings_rag", monkeypatch, tmp_path)


def test_deeply_nested_frontmatter_is_treated_as_none(tagger):
    fm, body = tagger.parse_frontmatter(DEEP)
    assert fm is None
    assert body == DEEP


def test_collect_all_tags_survives_a_deeply_nested_note(tagger, tmp_path):
    """The fallback path (no Tag Taxonomy.md) had no handler: one bad note
    stopped the tagger on every run."""
    vault = tmp_path / "vault"
    (vault / "Clippings").mkdir(parents=True)
    (vault / "Clippings" / "converted.md").write_text(DEEP, encoding="utf-8")
    (vault / "Clippings" / "fine.md").write_text(ORDINARY, encoding="utf-8")

    assert tagger.collect_all_tags(vault) == ["AI", "Strategy"]


def test_oversized_frontmatter_is_not_parsed(tagger):
    big = "---\ntitle: x\npad: " + "a" * (tagger.MAX_FRONTMATTER_CHARS + 1) + "\n---\nbody\n"
    assert tagger.parse_frontmatter(big) == (None, big)


def test_ordinary_frontmatter_still_parses(tagger):
    fm, body = tagger.parse_frontmatter(ORDINARY)
    assert fm == {"title": "Ordinary", "tags": ["Strategy", "AI"]}
    assert body == "body text\n"


def test_rag_tagger_deeply_nested_frontmatter_is_treated_as_none(rag_tagger):
    assert rag_tagger.parse_frontmatter(DEEP) == (None, DEEP)


def test_rag_tagger_non_mapping_frontmatter_is_treated_as_none(rag_tagger):
    text = "---\n- just\n- a list\n---\nbody\n"
    assert rag_tagger.parse_frontmatter(text) == (None, text)


def test_rag_tagger_ordinary_frontmatter_still_parses(rag_tagger):
    fm, body = rag_tagger.parse_frontmatter(ORDINARY)
    assert fm["tags"] == ["Strategy", "AI"]
    assert body == "body text\n"
