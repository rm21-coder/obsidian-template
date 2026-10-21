"""test_note_read_cap.py — the shared 1 MiB per-note read cap.

classification_tier.read_note() is the one reader for note bodies in the
classifier, the RAG sync, both taggers, strip-ads and the morning dashboard
(the shared mitigation for 5278128 / 5278610). The classifier and RAG sync
cases live with their own tests; this file covers the rest, each of which
may skip an oversize note but must not process it.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

import classification_tier as T


def _oversize(path: Path, head: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + "x" * T.MAX_NOTE_BYTES, encoding="utf-8")
    return path


def test_the_cap_is_one_mebibyte():
    assert T.MAX_NOTE_BYTES == 1024 * 1024


def test_a_note_at_the_cap_is_read(tmp_path: Path):
    p = tmp_path / "n.md"
    p.write_bytes(b"x" * T.MAX_NOTE_BYTES)
    assert len(T.read_note(p)) == T.MAX_NOTE_BYTES


def test_strip_ads_reports_an_oversize_clipping_and_leaves_it(tmp_path: Path, capsys):
    import strip_ads
    p = _oversize(tmp_path / "Clip.md", "<iframe title=\"Advertisement\"></iframe>\n")
    before = p.read_bytes()
    assert strip_ads.process_file(p, dry_run=False, quiet=True, verbose=False) is False
    assert "note read cap" in capsys.readouterr().err
    assert p.read_bytes() == before


def test_dashboard_skips_tasks_in_an_oversize_note(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    import morning_dashboard as md
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "ok.md").write_text("- [ ] #task small one\n", encoding="utf-8")
    _oversize(vault / "Notes" / "huge.md", "- [ ] #task in the big one\n")
    monkeypatch.setattr(md, "VAULT", vault)
    assert [t for _, t in md.collect_todos()] == ["small one"]


def test_dashboard_frontmatter_of_an_oversize_note_is_empty(tmp_path: Path):
    import morning_dashboard as md
    p = _oversize(tmp_path / "m.md", "---\ntitle: big\n---\n")
    assert md._frontmatter(p) == ""


def _import_tagger(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    fake = types.ModuleType("anthropic")
    fake.Anthropic = type("Anthropic", (), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module(name)
    monkeypatch.delitem(sys.modules, name, raising=False)
    return mod


def test_tagger_skips_an_oversize_clipping(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path, capsys):
    tagger = _import_tagger("tag_clippings", monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / "Clippings").mkdir(parents=True)
    (vault / "Clippings" / "p.md").write_text(
        "---\ntitle: P\nclassification: internal-use-only\n---\n" + "x " * 60,
        encoding="utf-8")
    _oversize(vault / "Clippings" / "huge.md", "---\ntitle: H\n---\n")
    import llm_endpoint
    monkeypatch.setattr(llm_endpoint, "client", lambda **kw: object())
    sent = []
    monkeypatch.setattr(tagger, "process_file",
                        lambda fp, *a, **k: sent.append(fp.name) or False)
    monkeypatch.setattr(tagger, "RECENT_EDIT_GUARD_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", ["tag_clippings.py", "--vault", str(vault)])
    tagger.main()
    assert sent == ["p.md"]
    assert "Skipping (over the note read cap): huge.md" in capsys.readouterr().out


def test_tagger_taxonomy_fallback_skips_an_oversize_note(monkeypatch, tmp_path):
    tagger = _import_tagger("tag_clippings", monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    # Notes/, not Clippings/: the fallback no longer reads Clippings at all
    # (finding 5268568).
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "fine.md").write_text(
        "---\ntags:\n  - AI\n---\nbody\n", encoding="utf-8")
    _oversize(vault / "Notes" / "huge.md", "---\ntags:\n  - Big\n---\n")
    assert tagger.collect_all_tags(vault) == ["AI"]


def test_rag_tagger_skips_an_oversize_note(monkeypatch, tmp_path, capsys):
    rag_tagger = _import_tagger("tag_clippings_rag", monkeypatch, tmp_path)
    p = _oversize(tmp_path / "huge.md", "---\ntitle: H\n---\n")
    assert rag_tagger.process(p, None, [], None, False, False, 3) is None
    assert "skipped (over the note read cap): huge.md" in capsys.readouterr().out
