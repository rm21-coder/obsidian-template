"""test_classify_notes.py — invariants of the classification assistant.

No test here makes a model call. The LLM layer is exercised through a fake
client, because what needs guarding is not the model's taste but the
mechanics around it: that frontmatter is edited surgically, that a verdict is
only ever written upward, that the deterministic detectors cannot be skipped,
and that the tracking hash ignores churn Obsidian introduces on its own.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import classify_notes as C


# ---------------------------------------------------------------------------
# Fake model client.
# ---------------------------------------------------------------------------

class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class FakeClient:
    """Returns a scripted verdict and records the prompts it was handed."""

    def __init__(self, tier: str = "confidential", *, rationale: str = "because",
                 confidence: str = "high", raw: str | None = None) -> None:
        self._raw = raw if raw is not None else json.dumps(
            {"tier": tier, "confidence": confidence, "rationale": rationale})
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self._raw)


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep per-call usage accounting out of the real state dir."""
    monkeypatch.setenv("USAGE_LOG_PATH", str(tmp_path / "usage.jsonl"))


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A vault root the module treats as its own, with the edit guard off."""
    root = tmp_path / "Vault"
    (root / "Knowledge").mkdir(parents=True)
    monkeypatch.setattr(C, "VAULT_ROOT", root)
    monkeypatch.setattr(C, "RECENT_EDIT_GUARD_SECONDS", 0)
    return root


def write(vault: Path, rel: str, body: str, **fm: object) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if fm:
        lines = "\n".join(f"{k.replace('__', '_')}: {v}" for k, v in fm.items())
        p.write_text(f"---\n{lines}\n---\n\n{body}\n", encoding="utf-8")
    else:
        p.write_text(body + "\n", encoding="utf-8")
    return p


def fm_of(path: Path) -> dict:
    fm_body, _, _ = C.split_frontmatter(path.read_text(encoding="utf-8"))
    return C.parse_fm(fm_body)


# ---------------------------------------------------------------------------
# Frontmatter splicing — surgical, never a YAML round trip.
# ---------------------------------------------------------------------------

def test_splice_appends_without_touching_existing_keys():
    src = "---\ntitle: Foo\ntags:\n  - a\n  - b\nclassification: internal-use-only\n---\n\nBody.\n"
    out = C.splice_frontmatter(src, {"classification_reviewed": "false"})
    assert out == (
        "---\ntitle: Foo\ntags:\n  - a\n  - b\nclassification: internal-use-only\n"
        "classification_reviewed: false\n---\n\nBody.\n")


def test_splice_replaces_scalar_in_place():
    src = "---\ntitle: Foo\nclassification: public\nother: 1\n---\nBody\n"
    out = C.splice_frontmatter(src, {"classification": "restricted"})
    assert out == "---\ntitle: Foo\nclassification: restricted\nother: 1\n---\nBody\n"


def test_splice_consumes_a_multiline_list_when_replacing_its_key():
    src = "---\ntags:\n  - a\n  - b\ntitle: Foo\n---\nBody\n"
    assert C.splice_frontmatter(src, {"tags": "[]"}) == (
        "---\ntags: []\ntitle: Foo\n---\nBody\n")


def test_splice_creates_frontmatter_when_absent():
    assert C.splice_frontmatter("Just a body.\n", {"classification": "public"}) == (
        "---\nclassification: public\n---\n\nJust a body.\n")


def test_splice_preserves_crlf_without_doubling_the_cr():
    # Building the block with `lf` inline and translating afterwards turned each
    # CRLF into CR+CRLF. Assert the exact bytes, not just "contains".
    src = "---\r\ntitle: Foo\r\n---\r\nBody\r\n"
    assert C.splice_frontmatter(src, {"classification": "public"}) == (
        "---\r\ntitle: Foo\r\nclassification: public\r\n---\r\nBody\r\n")


def test_splice_leaves_a_horizontal_rule_in_the_body_alone():
    src = "---\ntitle: Foo\n---\nintro\n\n---\n\nmore\n"
    assert C.splice_frontmatter(src, {"classification": "public"}) == (
        "---\ntitle: Foo\nclassification: public\n---\nintro\n\n---\n\nmore\n")


# ---------------------------------------------------------------------------
# Rationale quoting — must survive Obsidian stripping the quotes.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "auto-applied by detector [ssn]: US Social Security number",
    'He said "no" - pipe | and #4',
    "- leading dash: {braces} [brackets]",
    "Contains PHI: patient name, DOB",
])
def test_rationale_parses_identically_quoted_and_unquoted(raw: str):
    quoted = C.yaml_quote(raw)
    assert quoted.startswith('"') and quoted.endswith('"')
    as_quoted = yaml.safe_load(f"r: {quoted}\n")["r"]
    as_stripped = yaml.safe_load(f"r: {quoted[1:-1]}\n")["r"]
    assert as_quoted == as_stripped
    assert isinstance(as_quoted, str) and as_quoted


# ---------------------------------------------------------------------------
# Tracking hash — body only.
# ---------------------------------------------------------------------------

def test_content_hash_ignores_frontmatter_churn_but_not_body_edits():
    quoted = '---\ntitle: X\nr: "a b"\n---\n\nBody text.\n'
    stripped = '---\ntitle: X\nr: a b\n---\n\nBody text.\n'
    reordered = '---\nr: a b\ntitle: X\nupdated: 2026-01-01\n---\n\nBody text.\n'
    edited = '---\ntitle: X\n---\n\nBody text CHANGED.\n'
    assert C.content_hash(quoted) == C.content_hash(stripped)
    assert C.content_hash(quoted) == C.content_hash(reordered)
    assert C.content_hash(quoted) != C.content_hash(edited)


# ---------------------------------------------------------------------------
# Tier reading and baselines.
# ---------------------------------------------------------------------------

def test_unrecognised_tier_reads_as_unset_so_it_is_re_evaluated():
    # The RAG allowlist fails closed on an unknown value, so a typo'd note is
    # already out of the index; it must not be treated as holding a tier.
    assert C.current_tier({"classification": "Uinternal-use-only"}) is None
    assert C.current_tier({"classification": '"Confidential" '}) == "confidential"
    assert C.current_tier({}) is None


def test_nothing_baselines_to_public_any_more():
    """The public tier was retired 2026-08-22. No folder floors at it, and an
    external source URL no longer demotes to it — the vault holds no
    redistribution rights to a clipped article, paywalled or not."""
    assert "public" not in C.FOLDER_BASELINE.values()
    assert C.baseline_tier("Clippings") == C.DEFAULT_TIER
    assert C.baseline_tier("Knowledge") == C.DEFAULT_TIER
    assert "public" in C.TIER_RANK, "still a valid value for a deliberate marking"


# ---------------------------------------------------------------------------
# L0 detectors — instances only, and never skippable.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ("SSN is 123-45-6789 on file.", ["ssn"]),
    ("Patient MRN: 4419920 admitted", ["mrn"]),
    ("AKIAIOSFODNN7EXAMPLE", ["aws-key"]),
    ("leaked sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaa here", ["anthropic-key"]),
    # Topics, not instances.
    ("We must protect PHI and comply with HIPAA and MRN policy.", []),
    ("The MRN field is indexed by the EHR.", []),
    ("call 410-555-1234", []),
    # A policy note quoting a placeholder must not classify itself.
    ("```\nsk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaa\n```", []),
])
def test_detectors_fire_on_instances_only(body: str, expected: list[str]):
    assert [r[0] for r in C.run_detectors(body)] == expected


def test_detectors_still_run_on_a_note_already_marked_reviewed(vault: Path):
    """Regression: the skips once ran BEFORE the detectors, so a settled note
    that later gained a credential was never scanned again — fail open."""
    p = write(vault, "Knowledge/Settled.md",
              "Then someone pasted AKIAIOSFODNN7EXAMPLE",
              classification="internal-use-only", classification_reviewed="true")
    rec = C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    assert rec is not None and rec["action"] == "auto-applied"
    assert fm_of(p)["classification"] == "restricted"


def test_detectors_still_run_on_a_note_awaiting_review(vault: Path):
    p = write(vault, "Knowledge/Queued.md", "Then: SSN 123-45-6789",
              classification="internal-use-only",
              classification_suggested="confidential",
              classification_reviewed="false")
    rec = C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    assert rec is not None and rec["action"] == "auto-applied"
    got = fm_of(p)
    assert got["classification"] == "restricted"
    assert got["classification_prior"] == "internal-use-only"


def test_detector_auto_application_preserves_the_updated_timestamp(vault: Path):
    p = write(vault, "Knowledge/Stamped.md", "SSN 123-45-6789",
              classification="internal-use-only", updated="2026-05-26T16:06")
    C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    text = p.read_text(encoding="utf-8")
    assert "updated: 2026-05-26T16:06" in text
    assert "classification: restricted" in text


# ---------------------------------------------------------------------------
# L1 — proposes, never sets; elevates, never demotes.
# ---------------------------------------------------------------------------

def test_model_verdict_is_proposed_not_applied(vault: Path):
    p = write(vault, "Knowledge/Note.md", "personnel matter",
              classification="internal-use-only")
    client = FakeClient("confidential", rationale="named individual severance")
    rec = C.process_file(p, client, dry_run=False, detectors_only=False, force=False)
    got = fm_of(p)
    assert rec["action"] == "suggested"
    assert got["classification"] == "internal-use-only"   # untouched
    assert got["classification_suggested"] == "confidential"
    assert got["classification_reviewed"] is False
    assert "severance" in got["classification_rationale"]


def test_a_verdict_at_or_below_the_current_tier_is_discarded(vault: Path):
    for verdict in ("public", "internal-use-only"):
        p = write(vault, f"Knowledge/Same-{verdict}.md", "ordinary",
                  classification="confidential")
        before = p.read_text(encoding="utf-8")
        rec = C.process_file(p, FakeClient(verdict), dry_run=False,
                             detectors_only=False, force=False)
        assert rec is None, f"{verdict} should not be written over confidential"
        assert p.read_text(encoding="utf-8") == before


def test_an_unset_note_cannot_be_talked_down_to_public(vault: Path):
    """`public` on an unset Knowledge note is a demotion from the tier it would
    have been created with, so it must not reach classification_suggested."""
    p = write(vault, "Knowledge/Bare.md", "ordinary working note")
    rec = C.process_file(p, FakeClient("public"), dry_run=False,
                         detectors_only=False, force=False)
    got = fm_of(p)
    assert got["classification"] == C.DEFAULT_TIER
    assert "classification_suggested" not in got
    assert rec["action"] == "backfilled"


def test_a_queued_note_is_not_re_adjudicated(vault: Path):
    p = write(vault, "Knowledge/Pending.md", "ordinary",
              classification="internal-use-only",
              classification_suggested="confidential",
              classification_reviewed="false")
    client = FakeClient("restricted")
    assert C.process_file(p, client, dry_run=False, detectors_only=False,
                          force=False) is None
    assert client.calls == [], "no model call should be made for a queued note"


def test_a_reviewed_note_is_left_alone(vault: Path):
    p = write(vault, "Knowledge/Done.md", "ordinary",
              classification="internal-use-only", classification_reviewed="true")
    client = FakeClient("confidential")
    assert C.process_file(p, client, dry_run=False, detectors_only=False,
                          force=False) is None
    assert client.calls == []


def test_force_reopens_a_reviewed_note(vault: Path):
    p = write(vault, "Knowledge/Done.md", "ordinary",
              classification="internal-use-only", classification_reviewed="true")
    client = FakeClient("confidential")
    rec = C.process_file(p, client, dry_run=True, detectors_only=False, force=True)
    assert rec is not None and client.calls


def test_dry_run_writes_nothing(vault: Path):
    p = write(vault, "Knowledge/Note.md", "personnel matter",
              classification="internal-use-only")
    before = p.read_text(encoding="utf-8")
    rec = C.process_file(p, FakeClient("confidential"), dry_run=True,
                         detectors_only=False, force=False)
    assert rec["action"] == "suggested"
    assert p.read_text(encoding="utf-8") == before


def test_an_unparseable_verdict_is_reported_not_guessed(vault: Path):
    p = write(vault, "Knowledge/Note.md", "text", classification="internal-use-only")
    rec = C.process_file(p, FakeClient(raw="I'm not going to answer that."),
                         dry_run=False, detectors_only=False, force=False)
    assert rec["action"] == "error"
    assert "classification_suggested" not in fm_of(p)


def test_an_unknown_tier_name_is_rejected(vault: Path):
    p = write(vault, "Knowledge/Note.md", "text", classification="internal-use-only")
    rec = C.process_file(p, FakeClient(raw='{"tier": "top-secret"}'),
                         dry_run=False, detectors_only=False, force=False)
    assert rec["action"] == "error"


# ---------------------------------------------------------------------------
# Scope.
# ---------------------------------------------------------------------------

def test_generated_log_subtrees_are_not_queued_for_human_review(vault: Path):
    write(vault, "Meetings/2026-01-01 0900.md", "real meeting", classification="x")
    write(vault, "Meetings/_Runs/2026-01-01.md", "pipeline log")
    collected = {p.relative_to(vault).as_posix() for p in C.collect_files(None)}
    assert "Meetings/2026-01-01 0900.md" in collected
    assert "Meetings/_Runs/2026-01-01.md" not in collected


def test_scaffolding_and_archive_folders_are_skipped(vault: Path):
    for rel in ("Templates/Note Template.md", "Z_archive/old.md",
                "Z_attachments/x.md", "Knowledge/real.md"):
        write(vault, rel, "body")
    collected = {p.relative_to(vault).parts[0] for p in C.collect_files(None)}
    assert collected == {"Knowledge"}


# ---------------------------------------------------------------------------
# Reconcile — the other half of accepting a proposal.
# ---------------------------------------------------------------------------

def test_splice_can_drop_keys(vault: Path):
    src = ("---\ntitle: X\nclassification_suggested: confidential\n"
           "classification_rationale: \"why\"\nkeep: yes\n---\nBody\n")
    out = C.splice_frontmatter(
        src, {"classification_reviewed": "true"},
        drop_keys=("classification_suggested", "classification_rationale"))
    assert out == ("---\ntitle: X\nkeep: yes\n"
                   "classification_reviewed: true\n---\nBody\n")


@pytest.mark.parametrize("current,suggested,retired", [
    ("confidential", "confidential", True),    # accepted exactly
    ("restricted", "confidential", True),      # accepted and then some
    ("internal-use-only", "confidential", False),  # still an open decision
    ("public", "restricted", False),
])
def test_reconcile_retires_only_honoured_proposals(vault: Path, current: str,
                                                   suggested: str, retired: bool):
    p = write(vault, "Knowledge/N.md", "body",
              classification=current,
              classification_suggested=suggested,
              classification_rationale='"why"',
              classification_reviewed="false",
              updated="2026-05-01T09:00")
    accepted, still_open = C.reconcile([p], dry_run=False)
    got = fm_of(p)
    assert (accepted, still_open) == ((1, 0) if retired else (0, 1))
    if retired:
        assert "classification_suggested" not in got
        assert "classification_rationale" not in got
        assert got["classification_reviewed"] is True
    else:
        # An undecided note must come out byte-for-byte unchanged: this pass
        # closing a decision the reviewer has not made would be the worst
        # possible failure here.
        assert got["classification_suggested"] == suggested
        assert got["classification_reviewed"] is False
    assert "updated: 2026-05-01T09:00" in p.read_text(encoding="utf-8")


def test_reconcile_dry_run_writes_nothing(vault: Path):
    p = write(vault, "Knowledge/N.md", "body", classification="confidential",
              classification_suggested="confidential",
              classification_rationale='"why"', classification_reviewed="false")
    before = p.read_text(encoding="utf-8")
    accepted, _ = C.reconcile([p], dry_run=True)
    assert accepted == 1
    assert p.read_text(encoding="utf-8") == before


def test_reconcile_ignores_notes_with_no_proposal(vault: Path):
    p = write(vault, "Knowledge/N.md", "body", classification="public")
    before = p.read_text(encoding="utf-8")
    assert C.reconcile([p], dry_run=False) == (0, 0)
    assert p.read_text(encoding="utf-8") == before


def test_reconcile_ignores_an_unparseable_suggested_value(vault: Path):
    p = write(vault, "Knowledge/N.md", "body", classification="confidential",
              classification_suggested="top-secret",
              classification_reviewed="false")
    assert C.reconcile([p], dry_run=False) == (0, 0)
    assert fm_of(p)["classification_suggested"] == "top-secret"


# ---------------------------------------------------------------------------
# Bulk accept / reject.
# ---------------------------------------------------------------------------

def _queued(vault: Path, name: str, current: str, suggested: str,
            reviewed: str = "false", folder: str = "Knowledge") -> Path:
    return write(vault, f"{folder}/{name}.md", "body",
                 classification=current,
                 classification_suggested=suggested,
                 classification_rationale='"reason"',
                 classification_reviewed=reviewed,
                 updated="2026-05-01T09:00")


def test_accept_applies_the_tier_and_retires_the_proposal(vault: Path):
    p = _queued(vault, "N", "internal-use-only", "confidential")
    ruled, out_of_scope = C.rule_on([p], "accept", None, dry_run=False)
    got = fm_of(p)
    assert (ruled, out_of_scope) == (1, 0)
    assert got["classification"] == "confidential"
    assert got["classification_reviewed"] is True
    assert "classification_suggested" not in got
    assert "classification_rationale" not in got
    assert "updated: 2026-05-01T09:00" in p.read_text(encoding="utf-8")


def test_reject_keeps_the_proposal_as_the_record(vault: Path):
    """The declined proposal is the more useful half of a rejection, and the
    review base's "Ruled on" view reads it — so reject must not drop it."""
    p = _queued(vault, "N", "internal-use-only", "confidential")
    ruled, _ = C.rule_on([p], "reject", None, dry_run=False)
    got = fm_of(p)
    assert ruled == 1
    assert got["classification"] == "internal-use-only"      # tier untouched
    assert got["classification_suggested"] == "confidential"  # record kept
    assert got["classification_rationale"] == "reason"
    assert got["classification_reviewed"] is True


def test_tier_filter_leaves_other_tiers_untouched(vault: Path):
    conf = _queued(vault, "C", "internal-use-only", "confidential")
    rest = _queued(vault, "R", "internal-use-only", "restricted")
    before = rest.read_text(encoding="utf-8")
    ruled, out_of_scope = C.rule_on([conf, rest], "accept", "confidential",
                                    dry_run=False)
    assert (ruled, out_of_scope) == (1, 1)
    assert fm_of(conf)["classification"] == "confidential"
    assert rest.read_text(encoding="utf-8") == before


def test_an_already_settled_note_is_never_re_ruled(vault: Path):
    p = _queued(vault, "N", "internal-use-only", "confidential", reviewed="true")
    before = p.read_text(encoding="utf-8")
    assert C.rule_on([p], "accept", None, dry_run=False) == (0, 0)
    assert p.read_text(encoding="utf-8") == before


def test_bulk_dry_run_writes_nothing(vault: Path):
    p = _queued(vault, "N", "internal-use-only", "confidential")
    before = p.read_text(encoding="utf-8")
    ruled, _ = C.rule_on([p], "accept", None, dry_run=True)
    assert ruled == 1
    assert p.read_text(encoding="utf-8") == before


def test_notes_with_no_proposal_are_ignored(vault: Path):
    p = write(vault, "Knowledge/Plain.md", "body", classification="public")
    before = p.read_text(encoding="utf-8")
    assert C.rule_on([p], "accept", None, dry_run=False) == (0, 0)
    assert p.read_text(encoding="utf-8") == before


def test_an_unrecognised_proposed_tier_is_not_applied(vault: Path):
    p = _queued(vault, "N", "internal-use-only", "top-secret")
    assert C.rule_on([p], "accept", None, dry_run=False) == (0, 0)
    assert fm_of(p)["classification"] == "internal-use-only"


# ---------------------------------------------------------------------------
# Folder baselines — sensitive by class rather than by content.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder,expected", [
    ("Meetings", "confidential"),
    ("People", "confidential"),
    ("Clippings", "internal-use-only"),
    ("Knowledge", "internal-use-only"),
    ("Groups", "internal-use-only"),
    ("Creations", "internal-use-only"),
])
def test_folder_baseline_floors(folder: str, expected: str):
    assert C.baseline_tier(folder) == expected


def test_an_elevated_folder_changes_what_counts_as_an_elevation(vault: Path):
    """A Meetings note the model calls confidential is now AT its baseline, so
    there is nothing to propose — the whole point of moving the floor."""
    p = write(vault, "Meetings/M.md", "personnel discussion")
    rec = C.process_file(p, FakeClient("confidential"), dry_run=False,
                         detectors_only=False, force=False)
    got = fm_of(p)
    assert got["classification"] == "confidential"     # baseline written
    assert "classification_suggested" not in got       # nothing to review
    assert rec["action"] == "backfilled"


def test_an_elevated_folder_still_surfaces_a_genuine_elevation(vault: Path):
    p = write(vault, "Meetings/M.md", "patient detail",
              classification="confidential")
    rec = C.process_file(p, FakeClient("restricted"), dry_run=False,
                         detectors_only=False, force=False)
    assert rec["action"] == "suggested"
    assert fm_of(p)["classification_suggested"] == "restricted"
