"""test_disclosure_check.py — invariants of the disclosure export gate.

The gate's whole value is that it refuses. These tests are therefore mostly
negative: they assert the specific reason a file was blocked, not merely that
nothing was copied — a gate that blocks everything for the wrong reason passes
an "assert not exported" test and is useless.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import disclosure_check as D


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "Vault"
    (root / "Knowledge").mkdir(parents=True)
    monkeypatch.setattr(D, "VAULT_ROOT", root)
    return root


def note(vault: Path, name: str, body: str = "body",
         tier: str | None = "public", folder: str = "Knowledge") -> Path:
    p = vault / folder / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntitle: {name}\nclassification: {tier}\n---\n\n" if tier else ""
    p.write_text(fm + body + "\n", encoding="utf-8")
    return p


def reasons_for(results: list[dict], path: Path) -> list[str]:
    return next(r["reasons"] for r in results if r["path"] == path)


# ---------------------------------------------------------------------------
# Tier reading.
# ---------------------------------------------------------------------------

def test_note_tier_reads_normalises_and_rejects(vault: Path):
    assert D.note_tier(note(vault, "a", tier="confidential")) == "confidential"
    assert D.note_tier(note(vault, "b", tier='"Restricted" ')) == "restricted"
    assert D.note_tier(note(vault, "c", tier="Uinternal-use-only")) is None
    assert D.note_tier(note(vault, "d", tier=None)) is None


# ---------------------------------------------------------------------------
# The audience lattice.
# ---------------------------------------------------------------------------

def test_every_audience_maps_to_a_real_tier_and_none_reaches_restricted():
    assert set(D.AUDIENCES) == {"public", "internal", "cleared"}
    for audience, ceiling in D.AUDIENCES.items():
        assert ceiling in D.TIER_RANK
        assert D.TIER_RANK[ceiling] < D.TIER_RANK[D.NEVER_EXPORTABLE], (
            f"audience {audience} would permit {D.NEVER_EXPORTABLE}")


@pytest.mark.parametrize("tier,audience,blocked", [
    ("public", "public", False),
    ("internal-use-only", "public", True),
    ("internal-use-only", "internal", False),
    ("confidential", "internal", True),
    ("confidential", "cleared", False),
    ("restricted", "cleared", True),
])
def test_ceiling_is_enforced_per_audience(vault: Path, tier: str,
                                          audience: str, blocked: bool):
    p = note(vault, "n", tier=tier)
    res = D.evaluate([p], D.AUDIENCES[audience])
    assert res[0]["blocked"] is blocked
    if blocked:
        assert f"`{tier}`" in " ".join(res[0]["reasons"])


# ---------------------------------------------------------------------------
# Transclusion — the reason this is not a grep.
# ---------------------------------------------------------------------------

def test_an_embed_pulls_its_target_into_the_judgement(vault: Path):
    secret = note(vault, "Secret", tier="confidential")
    host = note(vault, "Host", body="overview ![[Secret]]", tier="public")
    res = D.evaluate([host], "public")
    assert res[0]["blocked"]
    assert "embeds `confidential` note" in " ".join(res[0]["reasons"])
    assert secret in res[0]["embedded"]


def test_a_plain_link_carries_no_content_and_does_not_block(vault: Path):
    note(vault, "Secret", tier="confidential")
    host = note(vault, "Host", body="see [[Secret]] for detail", tier="public")
    res = D.evaluate([host], "public")
    assert not res[0]["blocked"]
    assert res[0]["embedded"] == []
    assert "Secret" in res[0]["links"]


def test_embeds_are_followed_transitively(vault: Path):
    deep = note(vault, "Deep", tier="restricted")
    note(vault, "Middle", body="mid ![[Deep]]", tier="public")
    top = note(vault, "Top", body="top ![[Middle]]", tier="public")
    res = D.evaluate([top], "confidential")
    assert res[0]["blocked"]
    assert "embeds `restricted` note" in " ".join(res[0]["reasons"])
    assert deep in res[0]["embedded"]


def test_an_embed_cycle_terminates(vault: Path):
    a = note(vault, "A", body="![[B]]", tier="public")
    note(vault, "B", body="![[A]]", tier="public")
    res = D.evaluate([a], "public")          # must not recurse forever
    assert not res[0]["blocked"]


def test_attachments_and_unresolved_embeds_are_reported_not_silently_passed(vault: Path):
    host = note(vault, "Host", body="![[diagram.png]] and ![[Nope]]", tier="public")
    res = D.evaluate([host], "public")[0]
    assert any("diagram.png" in m and "cannot be classified" in m for m in res["media"])
    assert res["unresolved"] == ["Nope"]


def test_embed_targets_resolve_by_stem_and_by_path(vault: Path):
    target = note(vault, "Deep", tier="confidential", folder="Knowledge/Sub")
    by_stem = note(vault, "H1", body="![[Deep]]", tier="public")
    by_path = note(vault, "H2", body="![[Knowledge/Sub/Deep]]", tier="public")
    for host in (by_stem, by_path):
        res = D.evaluate([host], "public")
        assert target in res[0]["embedded"], host


def test_a_heading_or_alias_suffix_still_resolves(vault: Path):
    target = note(vault, "Secret", tier="confidential")
    host = note(vault, "Host", body="![[Secret#Section|alias]]", tier="public")
    assert target in D.evaluate([host], "public")[0]["embedded"]


# ---------------------------------------------------------------------------
# Fail-closed posture.
# ---------------------------------------------------------------------------

def test_an_unclassified_note_blocks_and_says_so(vault: Path):
    p = note(vault, "Bare", tier=None)
    res = D.evaluate([p], "confidential")
    assert res[0]["blocked"]
    assert "unclassified" in " ".join(res[0]["reasons"])


def test_treat_unclassified_relaxes_only_when_asked(vault: Path):
    p = note(vault, "Bare", tier=None)
    assert D.evaluate([p], "public")[0]["blocked"]
    assert not D.evaluate([p], "public", unclassified_as="public")[0]["blocked"]


def test_an_unclassified_embed_blocks_its_host(vault: Path):
    note(vault, "Bare", tier=None)
    host = note(vault, "Host", body="![[Bare]]", tier="public")
    res = D.evaluate([host], "public")
    assert res[0]["blocked"]
    assert "unclassified note" in " ".join(res[0]["reasons"])


# ---------------------------------------------------------------------------
# Restricted is absolute.
# ---------------------------------------------------------------------------

def test_restricted_is_flagged_for_the_override_refusal(vault: Path):
    direct = note(vault, "R", tier="restricted")
    assert D.evaluate([direct], "confidential")[0]["restricted"]


def test_restricted_reached_only_through_an_embed_still_flags(vault: Path):
    note(vault, "Deep", tier="restricted")
    host = note(vault, "Host", body="![[Deep]]", tier="public")
    assert D.evaluate([host], "confidential")[0]["restricted"]


# ---------------------------------------------------------------------------
# Export + --override: the override must actually export (regression).
# ---------------------------------------------------------------------------
# Pre-fix behavior: main() printed "OVERRIDE IN EFFECT", then the export
# filter still keyed on each result's blocked flag — the override-approved
# notes were silently withheld into WITHHELD.md while the run exited 0.
# Fail-closed, but the tool lied about what it did.

def _run_main(monkeypatch, argv):
    import sys as _sys
    import security_common
    monkeypatch.setattr(_sys, "argv", ["disclosure_check.py"] + argv)
    return D.main()


def test_export_with_override_actually_exports(
        vault: Path, tmp_path: Path, monkeypatch, capsys):
    import security_common
    monkeypatch.setattr(security_common, "state_dir",
                        lambda: tmp_path / "sec-state")
    ok_note = note(vault, "Open", tier="public")
    hot = note(vault, "Hot", tier="confidential")  # exceeds public ceiling
    dest = tmp_path / "out"
    rc = _run_main(monkeypatch, ["export", "--audience", "public",
                                 "--to", str(dest),
                                 "--override", "reviewed by owner 2026-08-22",
                                 str(ok_note), str(hot)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OVERRIDE IN EFFECT" in out
    assert "PROCEEDING UNDER OVERRIDE" in out          # the specific marker
    assert (dest / "Knowledge" / "Hot.md").exists()     # it actually shipped
    assert (dest / "Knowledge" / "Open.md").exists()
    withheld = dest / "WITHHELD.md"
    if withheld.exists():
        assert "Hot.md" not in withheld.read_text()


def test_export_without_override_still_blocks(
        vault: Path, tmp_path: Path, monkeypatch, capsys):
    import security_common
    monkeypatch.setattr(security_common, "state_dir",
                        lambda: tmp_path / "sec-state")
    hot = note(vault, "Hot", tier="confidential")
    dest = tmp_path / "out"
    rc = _run_main(monkeypatch, ["export", "--audience", "public",
                                 "--to", str(dest), str(hot)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out                             # the specific marker
    assert not (dest / "Knowledge" / "Hot.md").exists()


# ---------------------------------------------------------------------------
# The gate fails closed on dependencies it could not evaluate.
#
# Before 2026-09-24 a note past MAX_EMBED_DEPTH, or one that could not be read,
# was skipped silently -- so a restricted note could sit behind either while
# the root reported clear. Microsoft M-DASH, CWE-863 (nine findings).
# ---------------------------------------------------------------------------

def _chain(vault: Path, length: int, tail_tier: str) -> Path:
    """N0 embeds N1 embeds ... N{length}; only the tail carries tail_tier."""
    note(vault, f"N{length}", tier=tail_tier)
    for i in range(length - 1, -1, -1):
        note(vault, f"N{i}", body=f"![[N{i + 1}]]", tier="public")
    return vault / "Knowledge" / "N0.md"


def test_restricted_note_beyond_the_depth_limit_blocks(vault: Path):
    top = _chain(vault, D.MAX_EMBED_DEPTH + 1, "restricted")
    res = D.evaluate([top], "confidential")[0]
    assert res["blocked"], "a restricted note past the depth limit let the root clear"
    assert any("beyond embed depth" in r for r in res["reasons"])


def test_depth_gap_cannot_be_overridden(vault: Path):
    """The gate cannot know what it did not read, so override must not
    release it -- a restricted note may be behind the gap."""
    top = _chain(vault, D.MAX_EMBED_DEPTH + 1, "public")
    res = D.evaluate([top], "confidential")[0]
    assert res["blocked"] and res["restricted"]


def test_chain_within_the_limit_is_judged_normally(vault: Path):
    top = _chain(vault, D.MAX_EMBED_DEPTH, "public")
    res = D.evaluate([top], "confidential")[0]
    assert not res["blocked"], res["reasons"]
    assert res["incomplete"] == []


def test_unreadable_dependency_blocks(vault: Path):
    note(vault, "Bad", tier="public")
    (vault / "Knowledge" / "Bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")
    top = note(vault, "Top", body="![[Bad]]", tier="public")
    res = D.evaluate([top], "public")[0]
    assert res["blocked"] and res["restricted"]
    assert any("unreadable" in r for r in res["reasons"])


def test_media_stays_advisory_but_an_unresolved_embed_blocks(vault: Path):
    """Decided 2026-09-25: fail closed. "Nothing exists behind an unresolved
    embed" held only if resolution matched Obsidian's, and it never fully did.
    An unresolved note embed now BLOCKS -- overridable, since the operator can
    see the target does not exist. Media stays advisory: it carries no tier,
    and blocking it would block every note with a picture."""
    pic = note(vault, "Pic", body="![[diagram.png]]", tier="public")
    assert not D.evaluate([pic], "public")[0]["blocked"]
    host = note(vault, "Host", body="![[diagram.png]] and ![[Nope]]", tier="public")
    res = D.evaluate([host], "public")[0]
    assert res["blocked"] and not res["restricted"], res
    assert any("`Nope` does not resolve" in r for r in res["reasons"]), res["reasons"]


@pytest.mark.parametrize("form", [
    "Secret Note.md",            # explicit extension -- Obsidian renders it
    "Knowledge/Secret Note.md",  # vault path with extension
    "SECRET NOTE.MD",            # case of both stem and extension
    "Secret Note",
    "knowledge/secret note",
    "Secret Note#Heading",
    "Secret Note|alias",
])
def test_every_embed_form_obsidian_renders_is_resolved(vault: Path, form: str):
    """An unresolved embed is advisory, so resolution must never be stricter
    than Obsidian's. `![[Secret Note.md]]` used to come back unresolved, and a
    restricted note embedded that way exported as clear."""
    note(vault, "Secret Note", body="TOP SECRET BODY", tier="restricted")
    top = note(vault, "Top", body=f"![[{form}]]", tier="public")
    res = D.evaluate([top], "public")[0]
    assert res["blocked"] and res["restricted"], f"![[{form}]] let a restricted note clear"
    assert res["unresolved"] == [], res["unresolved"]
    assert any("Secret Note" in r and "restricted" in r for r in res["reasons"]), res["reasons"]


def test_unicode_normalization_does_not_hide_an_embed(vault: Path):
    """Same name, different Unicode normalization: NFD on disk (as some macOS
    tools write it), NFC in the typed link."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", "Café Plan")
    note(vault, nfd, body="TOP SECRET BODY", tier="restricted")
    top = note(vault, "Top", body="![[" + unicodedata.normalize("NFC", "Café Plan") + "]]",
               tier="public")
    res = D.evaluate([top], "public")[0]
    assert res["blocked"] and res["restricted"], res
    assert res["unresolved"] == []



# ---------------------------------------------------------------------------
# Adversarial review of the M-DASH fixes, 2026-09-25. Each case below is a
# restricted note that the previous gate reported CLEAR (or let --override
# release). They are the reason the gate now fails closed.

def _restricted(vault: Path, name: str = "Secret", folder: str = "Knowledge") -> Path:
    return note(vault, name, body="TOP SECRET BODY", tier="restricted", folder=folder)


def _assert_restricted_blocks(res: dict) -> None:
    assert res["blocked"], f"cleared: {res}"
    assert res["restricted"], f"overridable: {res['reasons']}"


@pytest.mark.parametrize("form", [
    "![](Secret.md)", "![x](Secret%20Note.md)", "![](<Secret Note.md>)",
    "![](Knowledge/Secret%20Note.md 'title')",
])
def test_markdown_style_embeds_are_judged(vault: Path, form: str):
    _restricted(vault, "Secret"); _restricted(vault, "Secret Note")
    host = note(vault, "Host", body=form, tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_remote_markdown_images_are_not_vault_content(vault: Path):
    host = note(vault, "Host", body="![logo](https://example.com/x.md)", tier="public")
    assert not D.evaluate([host], "public")[0]["blocked"]


@pytest.mark.parametrize("form", ["![[./Data]]", "![[../Proj/Data]]", "![[Proj/Data]]"])
def test_relative_and_partial_paths_reach_every_candidate(vault: Path, form: str):
    note(vault, "Data", tier="public", folder="A")
    note(vault, "Data", body="TOP SECRET", tier="restricted", folder="Z/Proj")
    host = note(vault, "Host", body=form, tier="public", folder="Z/Other")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_duplicate_stem_judges_every_candidate(vault: Path):
    note(vault, "Data", body="TOP SECRET", tier="restricted", folder="A")
    note(vault, "Data", tier="public", folder="Z")
    host = note(vault, "Host", body="![[Data]]", tier="public", folder="A")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_escaped_pipe_in_a_table_is_an_alias_separator(vault: Path):
    _restricted(vault)
    host = note(vault, "Host", body="| a | ![[Secret\\|alias]] |", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_symlinked_folder_is_indexed(vault: Path, tmp_path: Path):
    ext = tmp_path / "ext"; ext.mkdir()
    (ext / "Secret.md").write_text("---\nclassification: restricted\n---\nX\n")
    (vault / "Linked").symlink_to(ext)
    host = note(vault, "Host", body="![[Secret]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_canvas_file_and_text_nodes_are_judged(vault: Path):
    _restricted(vault)
    note(vault, "Other", body="x", tier="restricted")
    (vault / "Knowledge" / "Board.canvas").write_text(json.dumps({"nodes": [
        {"id": "1", "type": "file", "file": "Knowledge/Secret.md"},
        {"id": "2", "type": "text", "text": "see ![[Other]]"}]}))
    host = note(vault, "Host", body="![[Board.canvas]]", tier="public")
    res = D.evaluate([host], "public")[0]
    _assert_restricted_blocks(res)
    names = {p.name for p in res["embedded"]}
    assert {"Secret.md", "Other.md"} <= names, names


def test_unparseable_canvas_cannot_be_overridden(vault: Path):
    (vault / "Knowledge" / "Board.canvas").write_text("{not json")
    host = note(vault, "Host", body="![[Board.canvas]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_excalidraw_drawing_links_are_embeds(vault: Path):
    _restricted(vault)
    d = vault / "Knowledge" / "Drawing.excalidraw.md"
    d.write_text("---\nexcalidraw-plugin: parsed\nclassification: public\n---\n"
                 "## Embedded files\nabc123: [[Secret]]\n")
    host = note(vault, "Host", body="![[Drawing.excalidraw]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


@pytest.mark.parametrize("body", [
    "```dataview\nTABLE file.name FROM #secret\n```",
    "```dataviewjs\ndv.list()\n```", "```tasks\nnot done\n```",
    "```base\nfilters: x\n```", "Total: `= this.file.name`", "`$= dv.current()`",
])
def test_query_blocks_block_with_override_allowed(vault: Path, body: str):
    """Dynamic content: the operator can look at what it renders, the gate
    cannot. Blocks; overridable with a logged reason."""
    host = note(vault, "Host", body=body, tier="public")
    res = D.evaluate([host], "public")[0]
    assert res["blocked"] and not res["restricted"], res
    assert any(r.startswith("dynamic content:") for r in res["reasons"]), res["reasons"]


def test_base_file_blocks_with_override_allowed(vault: Path):
    (vault / "Knowledge" / "All.base").write_text("filters: x\n")
    host = note(vault, "Host", body="![[All.base]]", tier="public")
    res = D.evaluate([host], "public")[0]
    assert res["blocked"] and not res["restricted"], res
    assert any("All.base" in r for r in res["reasons"]), res["reasons"]


def test_dynamic_content_does_not_make_a_restricted_embed_overridable(vault: Path):
    _restricted(vault)
    host = note(vault, "Host", body="![[Secret]]\n```dataview\nLIST\n```", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


@pytest.mark.parametrize("line", [
    "classification: restricted   # PHI",
    "classification: 'restricted' # PHI",
    "classification: public\nclassification: restricted",
    "classification: restricted\nclassification: public",
    "Classification: restricted",
])
def test_restricted_tier_survives_comments_duplicates_and_case(vault: Path, line: str):
    p = vault / "Knowledge" / "R.md"
    p.write_text(f"---\n{line}\n---\nbody\n")
    assert D.note_tier(p) == "restricted"
    _assert_restricted_blocks(D.evaluate([p], "confidential")[0])


def test_bom_prefixed_restricted_note_is_restricted(vault: Path):
    p = vault / "Knowledge" / "R.md"
    p.write_text("\ufeff---\nclassification: restricted\n---\nbody\n", encoding="utf-8")
    _assert_restricted_blocks(D.evaluate([p], "confidential")[0])


def test_unrecognised_declared_tier_cannot_be_overridden_or_defaulted(vault: Path):
    """--treat-unclassified is for notes with NO label; a label the gate cannot
    read is a claim it cannot check."""
    p = note(vault, "Typo", tier="restircted")
    res = D.evaluate([p], "confidential", unclassified_as="public")[0]
    _assert_restricted_blocks(res)


def test_override_cannot_export_restricted_with_a_comment(vault: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    """The reviewer's exact CLI reproduction: this exported with rc=0."""
    p = vault / "Knowledge" / "R.md"
    p.write_text("---\nclassification: restricted   # PHI\n---\nbody\n")
    out = tmp_path / "out"
    monkeypatch.setattr(D, "audit", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["dc", "export", str(p), "--to", str(out),
                                      "--audience", "cleared", "--override", "fine",
                                      "--vault", str(vault)])
    assert D.main() == 1
    assert not (out / "Knowledge" / "R.md").exists()


def test_note_outside_the_vault_is_refused_before_anything_happens(
        vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    outside = tmp_path / "Outside.md"
    outside.write_text("---\nclassification: public\n---\nx\n")
    monkeypatch.setattr(sys, "argv", ["dc", "check", str(outside), "--audience", "public",
                                      "--vault", str(vault)])
    with pytest.raises(SystemExit) as e:
        D.main()
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# Adversarial review ROUND 2, 2026-09-25: each was a restricted note that the
# first fail-closed version still cleared, or let --override release.

@pytest.mark.parametrize("frontmatter", [
    '{"classification": "restricted", "x": {\nclassification: public\n}}',   # decoy
    '{"classification": "restricted"}',                                    # flow mapping
    '"classification": restricted',                                        # quoted key
    '"classific\\u0061tion": restricted',                                  # escaped key
    'classification: public\n  restricted',                                # continuation
    'classification: public\nclassification: !!str restricted',            # known + unknown
    'meta:\n  classification: public',                                     # nested
])
def test_frontmatter_the_reader_cannot_read_blocks_without_override(
        vault: Path, frontmatter: str):
    p = vault / "Knowledge" / "R.md"
    p.write_text(f"---\n{frontmatter}\n---\nPHI\n", encoding="utf-8")
    _assert_restricted_blocks(D.evaluate([p], "public", unclassified_as="public")[0])


@pytest.mark.parametrize("body", [
    "![](Secret.md (title))", "![multi\nline alt](Secret.md)", "![[Secret|a]b]]",
    "![](Secret\\(1\\).md)".replace("\\(1\\)", "(1)").replace("Secret", "Secret"),
    '<span class="internal-embed" src="Secret"></span>',
])
def test_round2_embed_forms_are_judged(vault: Path, body: str):
    _restricted(vault, "Secret"); _restricted(vault, "Secret(1)")
    host = note(vault, "Host", body=body, tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


@pytest.mark.parametrize("body", ["![](Sec\\_ret.md)", "![](S&#101;cret.md)"])
def test_commonmark_escapes_and_entities_are_decoded(vault: Path, body: str):
    _restricted(vault, "Secret"); _restricted(vault, "Sec_ret")
    host = note(vault, "Host", body=body, tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_media_named_target_that_is_a_note_is_judged(vault: Path):
    _restricted(vault, "Diagram.png")               # the file Diagram.png.md
    host = note(vault, "Host", body="![[Diagram.png]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


@pytest.mark.parametrize("body", [
    '> [!note]\n> ```query\n> "MRN"\n> ```',
    '- ```dataview\n  TABLE x FROM "Knowledge"\n  ```',
    '1. ```tasks\n   path includes Knowledge\n   ```',
])
def test_query_fence_inside_callout_or_list_is_seen(vault: Path, body: str):
    host = note(vault, "Host", body=body, tier="public")
    res = D.evaluate([host], "public")[0]
    assert res["blocked"] and any(r.startswith("dynamic content:") for r in res["reasons"])


@pytest.mark.parametrize("body", [
    '```dataviewjs\ndv.paragraph(await dv.io.load("Knowledge/Secret.md"))\n```',
    '```query\nfile:Secret\n```',
    '```dataview\nLIST FROM "Knowledge"\n```',
])
def test_query_naming_a_restricted_note_or_its_folder_cannot_be_overridden(
        vault: Path, body: str):
    _restricted(vault)
    host = note(vault, "Host", body=body, tier="public", folder="Other")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_base_filtering_a_restricted_folder_cannot_be_overridden(vault: Path):
    _restricted(vault)
    (vault / "Other").mkdir(exist_ok=True)
    (vault / "Other" / "All.base").write_text('filters:\n  and:\n    - file.inFolder("Knowledge")\n')
    host = note(vault, "Host", body="![[All.base]]", tier="public", folder="Other")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_excalidraw_key_deep_in_frontmatter_is_detected(vault: Path):
    _restricted(vault)
    d = vault / "Knowledge" / "Drawing.md"
    d.write_text("---\ntags:\n" + "".join(f"  - t{i}\n" for i in range(400))
                 + "excalidraw-plugin: parsed\nclassification: public\n---\n"
                 "## Embedded Files\nabc: [[Secret]]\n")
    host = note(vault, "Host", body="![[Drawing]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])


def test_symlink_alias_of_a_folder_is_indexed_under_both_paths(vault: Path):
    real = vault / "Zreal"; real.mkdir()
    (real / "Secret.md").write_text("---\nclassification: restricted\n---\nX\n")
    (vault / "Aalias").symlink_to(real)
    host = note(vault, "Host", body="![[Aalias/Secret]]", tier="public")
    _assert_restricted_blocks(D.evaluate([host], "public")[0])
