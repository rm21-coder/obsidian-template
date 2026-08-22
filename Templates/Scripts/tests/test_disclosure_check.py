"""test_disclosure_check.py — invariants of the disclosure export gate.

The gate's whole value is that it refuses. These tests are therefore mostly
negative: they assert the specific reason a file was blocked, not merely that
nothing was copied — a gate that blocks everything for the wrong reason passes
an "assert not exported" test and is useless.
"""
from __future__ import annotations

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
    res = D.evaluate([host], "public")
    joined = " ".join(res[0]["unresolved"])
    assert "diagram.png" in joined and "cannot be classified" in joined
    assert "Nope" in joined and "unresolved" in joined


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
