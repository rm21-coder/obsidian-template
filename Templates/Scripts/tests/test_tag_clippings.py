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
    # The module annotates with anthropic.Anthropic at def time; Python 3.14
    # defers annotations (PEP 649) but 3.10-3.13 evaluate them on import, so
    # an empty fake module was an AttributeError there (found 2026-10-12 when
    # the suite first ran in a 3.13 test venv).
    fake = types.ModuleType("anthropic")
    fake.Anthropic = type("Anthropic", (), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)
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
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "converted.md").write_text(DEEP, encoding="utf-8")
    (vault / "Notes" / "fine.md").write_text(ORDINARY, encoding="utf-8")

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


# ---------------------------------------------------------------------------
# Alias expansion ("billion laughs"). PyYAML loads aliases as shared
# references, so the load is instant and the 64 KB cap never trips; it is
# str(tag) in the tagger (and yaml.dump on rewrite) that expands each level
# ~10x. A few hundred bytes became hours of CPU or an OOM.
# ---------------------------------------------------------------------------

def laughs(levels: int) -> str:
    lines = ['l0: &l0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for i in range(1, levels):
        refs = ",".join([f"*l{i - 1}"] * 10)
        lines.append(f"l{i}: &l{i} [{refs}]")
    lines.append(f"tags: [*l{levels - 1}]")
    return "---\ntitle: x\n" + "\n".join(lines) + "\n---\nbody text\n"


BILLION = laughs(9)     # ~350 bytes of YAML, 10^9 "lol"s once walked


def test_billion_laughs_frontmatter_is_refused(tagger, capsys):
    assert len(BILLION) < 1024
    fm, _ = tagger.parse_frontmatter(BILLION)
    # Never let pytest repr a parsed result: that walk IS the expansion.
    refused = fm is None
    assert refused, "billion-laughs frontmatter was parsed"
    assert "frontmatter not parsed (YAML anchor in frontmatter)" in capsys.readouterr().err


def test_an_anchor_alone_is_refused(tagger, capsys):
    text = "---\ntitle: &t x\n---\nbody\n"
    assert tagger.parse_frontmatter(text) == (None, text)
    assert "YAML anchor in frontmatter" in capsys.readouterr().err


def test_collect_all_tags_does_not_expand_aliases(tagger, tmp_path):
    """Kept to 4 levels so the pre-fix code fails fast instead of hanging.
    Mappings rather than lists, so the expanded str() does not start with
    "[[" and slip past the wikilink filter: the pre-fix code then shows the
    expansion as a 10^4-"lol" tag instead of discarding it after paying."""
    lines = ['l0: &l0 {a: lol, b: lol, c: lol, d: lol, e: lol, f: lol, g: lol, h: lol, i: lol, j: lol}']
    for i in range(1, 4):
        refs = ", ".join(f"{k}: *l{i - 1}" for k in "abcdefghij")
        lines.append(f"l{i}: &l{i} {{{refs}}}")
    doc = "---\ntitle: x\n" + "\n".join(lines) + "\ntags: [*l3]\n---\nbody\n"
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "laughs.md").write_text(doc, encoding="utf-8")
    (vault / "Notes" / "fine.md").write_text(ORDINARY, encoding="utf-8")

    assert tagger.collect_all_tags(vault) == ["AI", "Strategy"]


def test_rag_tagger_billion_laughs_frontmatter_is_refused(rag_tagger, capsys):
    fm, _ = rag_tagger.parse_frontmatter(BILLION)
    # Never let pytest repr a parsed result: that walk IS the expansion.
    refused = fm is None
    assert refused, "billion-laughs frontmatter was parsed"
    assert "frontmatter not parsed (YAML anchor in frontmatter)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A run where every call fails exits non-zero (Windows, 2026-10-13)
# ---------------------------------------------------------------------------

def _run_main(tagger, monkeypatch, tmp_path, outcomes):
    vault = tmp_path / "vault"
    (vault / "Clippings").mkdir(parents=True)
    for i, _ in enumerate(outcomes):
        (vault / "Clippings" / f"n{i}.md").write_text(f"# note {i}\nbody\n", encoding="utf-8")
    import llm_endpoint
    monkeypatch.setattr(llm_endpoint, "client", lambda **kw: object())
    results = iter(outcomes)

    def fake_process(*a, **k):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(tagger, "process_file", fake_process)
    monkeypatch.setattr(tagger, "RECENT_EDIT_GUARD_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", ["tag_clippings.py", "--vault", str(vault)])
    return tagger.main


def test_every_note_failing_exits_1(tagger, monkeypatch, tmp_path, capsys):
    """Every model call got a 401 and the task still exited 0, so the
    scheduler and the dashboard showed a pass while nothing was tagged."""
    main = _run_main(tagger, monkeypatch, tmp_path,
                     [RuntimeError("401 LiteLLM Virtual Key expected")] * 3)
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1
    assert "Every note this run tried (3) failed" in capsys.readouterr().out


def test_one_bad_note_among_good_ones_still_exits_0(tagger, monkeypatch, tmp_path):
    main = _run_main(tagger, monkeypatch, tmp_path, [True, RuntimeError("bad note"), False])
    assert main() is None


# ---------------------------------------------------------------------------
# Restricted notes stay off the model endpoint unless a gateway is allowed
# (2026-10-17)
# ---------------------------------------------------------------------------

RESTRICTED = "---\ntitle: R\nclassification: restricted\n---\n" + "x " * 60 + "\n"
UNREADABLE = "---\ntitle: U\nclassification: secret-ish\n---\n" + "x " * 60 + "\n"
PLAIN = "---\ntitle: P\nclassification: internal-use-only\n---\n" + "x " * 60 + "\n"


def _tag_run(tagger, monkeypatch, tmp_path, notes):
    vault = tmp_path / "vault"
    (vault / "Clippings").mkdir(parents=True)
    for name, text in notes.items():
        (vault / "Clippings" / name).write_text(text, encoding="utf-8")
    import llm_endpoint
    monkeypatch.setattr(llm_endpoint, "client", lambda **kw: object())
    sent = []
    monkeypatch.setattr(tagger, "process_file", lambda fp, *a, **k: sent.append(fp.name) or False)
    monkeypatch.setattr(tagger, "RECENT_EDIT_GUARD_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", ["tag_clippings.py", "--vault", str(vault)])
    tagger.main()
    return sorted(sent)


NOTES = {"r.md": RESTRICTED, "u.md": UNREADABLE, "p.md": PLAIN}


def test_restricted_and_unreadable_notes_are_withheld_by_default(tagger, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LLM_ALLOW_RESTRICTED", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert _tag_run(tagger, monkeypatch, tmp_path, NOTES) == ["p.md"]
    assert "Withheld (restricted): 2" in capsys.readouterr().out


def test_the_setting_alone_does_not_send_them_to_stock_anthropic(tagger, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_ALLOW_RESTRICTED", "1")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert _tag_run(tagger, monkeypatch, tmp_path, NOTES) == ["p.md"]
    assert "no gateway (LLM_BASE_URL) is configured" in capsys.readouterr().err


def test_an_allowed_gateway_receives_them(tagger, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ALLOW_RESTRICTED", "1")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.ai.example.edu")
    monkeypatch.setenv("LLM_SKIP_PREFLIGHT", "1")
    assert _tag_run(tagger, monkeypatch, tmp_path, NOTES) == ["p.md", "r.md", "u.md"]


def test_rag_tagger_withholds_a_restricted_note(rag_tagger, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LLM_ALLOW_RESTRICTED", raising=False)
    p = tmp_path / "r.md"
    p.write_text(RESTRICTED, encoding="utf-8")
    assert rag_tagger.process(p, None, [], None, False, False, 3) is None
    assert "withheld (restricted)" in capsys.readouterr().out


# 5277567: clean_body ran its link patterns over the whole note before
# cutting to MAX_CONTENT_CHARS, and `[^\]]*` / `[^)]*` rescanned from every
# "[" to the end of an unclosed run.
@pytest.mark.parametrize("body", ["[" * 500_000, "[](" * 200_000,
                                  "![" * 250_000])
def test_rag_clean_body_is_bounded(rag_tagger, body):
    import time
    t = time.perf_counter()
    out = rag_tagger.clean_body(body)
    assert time.perf_counter() - t < 1.0
    assert len(out) <= rag_tagger.MAX_CONTENT_CHARS


def test_rag_clean_body_still_strips_images_links_and_urls(rag_tagger):
    body = ("See ![chart](https://img.example/c.png) and [the plan](https://x.example/p) "
            "or https://y.example/q for more.")
    assert rag_tagger.clean_body(body).split() == ["See", "and", "or", "for", "more."]


def test_fallback_taxonomy_takes_only_canonical_tags_and_skips_clippings(tagger, tmp_path):
    """Finding 5268568 (Mythos review 2026-10-20): the vault-scan fallback is
    the allowlist on a fresh install; it took any string from any folder."""
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Clippings" / "Sub").mkdir(parents=True)
    (vault / "Notes" / "mine.md").write_text(
        "---\ntags:\n  - Strategy\n  - \"<img src=x>\"\n  - \"two words\"\n"
        "  - 9Lives\n  - AI/LLM\n---\nbody\n", encoding="utf-8")
    (vault / "Clippings" / "page.md").write_text(
        "---\ntags:\n  - PlantedByAuthor\n---\nbody\n", encoding="utf-8")
    (vault / "Clippings" / "Sub" / "deep.md").write_text(
        "---\ntags:\n  - AlsoPlanted\n---\nbody\n", encoding="utf-8")
    assert tagger.collect_all_tags(vault) == ["AI/LLM", "Strategy"]
