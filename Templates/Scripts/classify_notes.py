#!/usr/bin/env python3
"""
classify_notes.py — DLP-style data-classification assistant for the vault.

Every note carries a `classification` property (see Knowledge/Data
Classification.md for the four-tier scheme). Templates set a
default of `internal-use-only` at creation; in practice almost nothing ever
gets elevated by hand, so the property is present but carries no signal.

This script closes that gap WITHOUT taking the human out of the loop, which
the classification policy requires. Three layers:

  L0  Deterministic detectors (DETECTORS below). High-precision patterns for
      material that is regulated on sight — SSNs, private keys, API tokens,
      MRN-with-digits. These AUTO-APPLY `classification: restricted`. No model
      is consulted; a regex is more trustworthy than an LLM for "is this a
      private key". Fail closed.

  L1  LLM adjudication. Everything else goes to a Haiku-class model with the
      tier rubric and, critically, the topic-vs-instance distinction: this
      vault discusses HIPAA, breaches and board materials constantly while
      rarely containing PHI, incident detail or an actual board pre-read.
      Keyword matching alone is ~80% false positive here.

  L2  Human gate. The model NEVER writes `classification`. It writes
      `classification_suggested` / `classification_rationale` /
      `classification_reviewed: false`, which surface as a review queue in
      Topics/Classification. Accepting is a one-line edit; ignoring leaves
      the note where it was.

Two deliberate asymmetries:

  * Only elevation is ever proposed. A suggestion at or below the note's
    current tier is discarded. An automated DEMOTION path is a data-exfil
    primitive and is not built.
  * L0 writes `classification` directly, L1 never does. The difference is
    whether a human could disagree with the finding.

Frontmatter is edited by text splice, not by YAML round-trip: reserialising
2,500 notes' frontmatter to add one key would rewrite quoting and date forms
across the whole vault and bury the real change in diff noise.

Usage:
    python3 classify_notes.py --dry-run              # preview (start here)
    python3 classify_notes.py --dry-run --limit 40   # sample the first 40
    python3 classify_notes.py                        # apply
    python3 classify_notes.py --folder Meetings      # one folder (repeatable)
    python3 classify_notes.py --file "path/to/note.md"
    python3 classify_notes.py --force                # ignore tracking + reviewed flags
    python3 classify_notes.py --detectors-only       # L0 only, no API calls, no key needed

Setup:
    Credentials and endpoint come from llm_endpoint.py — ANTHROPIC_API_KEY by
    default, or set LLM_BASE_URL / LLM_API_KEY_NAME in ~/dev/secrets/.env to
    route through an institutional gateway. Store the key itself with
    `python3 secret_store.py set <NAME>`.
"""
from __future__ import annotations

import argparse
import hashlib
import threading
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv(Path.home() / "dev" / "secrets" / ".env")

# ─── Configuration ───────────────────────────────────────────────────────────

VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()

# Skipped entirely. Z_archive is retired content, Excalidraw/Z_attachments are
# not prose, Templates would poison the queue with placeholder text.
SKIP_TOP = {"Templates", ".obsidian", ".trash", "Z_attachments", "Z_archive",
            "Excalidraw", "Z_dashboards"}

# Subtrees of machine-generated operational output that happen to live under a
# content folder. Same list vault_lint excludes from its schema check — they are
# logs, not notes, and queueing them for a human classification review is noise.
#
# Nothing writes Meetings/_Runs any more (the run-summary note was removed once
# it turned out to duplicate the rotated log and to be read by nobody). The
# entry stays for installs that still carry the folder from before that change:
# dropping it would make their leftovers surface here as unclassified notes.
SKIP_SUBTREES = {"Meetings/_Runs"}

TRACKING_FILE = VAULT_ROOT / ".classification_tracking.json"
REPORT_FILE = VAULT_ROOT / "Templates" / "Scripts" / "last-classification-review.md"

# Tier lattice. Index IS the severity — compared numerically throughout.
TIERS = ["public", "internal-use-only", "confidential", "restricted"]
TIER_RANK = {t: i for i, t in enumerate(TIERS)}
DEFAULT_TIER = "internal-use-only"

# Adjudication against a fixed four-tier rubric is a Haiku-class task — the
# rubric is the guardrail, so the cheaper model loses little. Override with
# CLASSIFIER_MODEL; an institutional gateway usually wants its own alias
# (`claude-haiku-4.5`, dot notation, no date suffix) rather than the stock id.
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL",
                                  "claude-haiku-4-5-20251001")

# The system block (rubric) is identical for every note in a run, so it carries
# a cache_control breakpoint. Set CLASSIFIER_PROMPT_CACHE=0 where the endpoint
# does not pass cache_control through — some gateways accept the field and
# silently ignore it, which bills every call at full price either way.
PROMPT_CACHE = os.environ.get("CLASSIFIER_PROMPT_CACHE", "1") != "0"

MAX_CONTENT_CHARS = int(os.environ.get("CLASSIFIER_MAX_CHARS", "6000"))

# One line describing whose vault this is and what field they work in. It is the
# single highest-leverage input to accuracy, because the topic-versus-instance
# rule below depends on knowing which sensitive-sounding subjects are simply
# this person's day job. Set CLASSIFIER_ORG_CONTEXT in ~/dev/secrets/.env — it
# is deployment config, so this file stays identical to its repo copy.
ORG_CONTEXT = os.environ.get(
    "CLASSIFIER_ORG_CONTEXT",
    "a professional knowledge worker who keeps detailed work notes").strip()

# Skip notes touched within this window — the note is probably open in the
# editor and a rewrite would drop unsaved keystrokes. Same guard the tagger uses.
RECENT_EDIT_GUARD_SECONDS = int(
    os.environ.get("CLASSIFY_RECENT_EDIT_GUARD_SECONDS", "120"))

# ─── L0: deterministic detectors ─────────────────────────────────────────────
#
# Every pattern here must match an INSTANCE, never a topic. "HIPAA" is a topic;
# a nine-digit SSN is an instance. Anything that could plausibly fire on a note
# *about* a subject belongs in the LLM rubric instead, not in this table.
# Each entry: (rule id, tier, compiled pattern, human-readable finding).

DETECTORS: list[tuple[str, str, re.Pattern, str]] = [
    ("ssn", "restricted",
     re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
     "US Social Security number"),
    ("private-key", "restricted",
     re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
     "embedded private key"),
    ("anthropic-key", "restricted",
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
     "Anthropic API key"),
    ("generic-secret", "restricted",
     re.compile(r"(?i)\b(?:api[_\- ]?key|secret[_\- ]?key|access[_\- ]?token|"
                r"client[_\- ]?secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{24,}"),
     "credential assigned in text"),
    ("bearer-token", "restricted",
     re.compile(r"\bBearer\s+[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{8,}"),
     "bearer token"),
    ("aws-key", "restricted",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "AWS access key id"),
    ("mrn", "restricted",
     re.compile(r"(?i)\b(?:MRN|medical record (?:number|no\.?))\b\s*[:#]?\s*\d{5,}"),
     "medical record number with value"),
]

# Fenced code blocks are stripped before L0 runs: the vault documents these
# very patterns (Data Classification.md, the security runbooks) in examples,
# and a policy note quoting a placeholder key must not classify itself
# restricted. Real leaked credentials in a code fence are the accepted cost —
# they are also the case the LLM layer is asked to look for.
_CODE_FENCE = re.compile(r"^```.*?^```", re.M | re.S)
_INLINE_CODE = re.compile(r"`[^`\n]+`")

# ─── Prompt ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a data-classification reviewer for the personal knowledge vault of \
{ORG_CONTEXT}. You assign one of four tiers to a note, following the vault's own \
classification policy.

TIERS, least to most sensitive:

public — Reserved for material deliberately marked as safe to release. Do NOT assign this \
tier to a note merely because its source was published elsewhere: a clipped article, a video \
summary or a recipe is internal-use-only here, because the vault holds no redistribution \
rights to it and the choice of what was clipped is itself informative. Assign public only \
when the note IS the owner's own material intended for release.

internal-use-only — THE DEFAULT. Ordinary organization-internal working material. Most meeting \
notes, most authored notes, contact records, project status, technical design, budget \
discussion at a normal level of detail, vendor relationships in the ordinary course. \
Disclosure outside the organization would be inappropriate but carries no regulatory or material \
consequence.

confidential — Sensitive business information needing protection beyond default. Board or \
executive-committee materials before release, named-individual personnel matters (compensation, \
discipline, performance, hiring/firing decisions about a specific person), live M&A or \
vendor negotiation positions and pricing not yet agreed, undisclosed security incident \
detail (real findings about real systems), pre-decisional strategy, sensitive donor or \
trustee context, legal advice or privileged communication. Disclosure could cause \
material harm.

restricted — Legally or contractually protected. Actual PHI or PII about an identifiable \
person (HIPAA/FERPA/GLBA), material covered by a specific NDA or BAA, credentials and \
secrets. Disclosure may trigger regulatory notification.

THE CENTRAL RULE — TOPIC IS NOT INSTANCE:

A vault like this one discusses its owner's professional domain constantly — regulation, \
security, governance, personnel policy, vendor contracts — as SUBJECT MATTER. That is \
ordinary professional content and it is internal-use-only.

A note is elevated only when it CONTAINS the sensitive thing, not when it TALKS ABOUT the \
category. Apply these tests:

  - A note explaining what PHI is, or a policy on handling PHI, is internal-use-only. \
A note containing a patient's name, condition or record is restricted.
  - A note about security architecture, threat trends, or a vendor's product is \
internal-use-only. A note with specific undisclosed findings, live vulnerabilities in \
named internal systems, or incident-response detail is confidential.
  - A note describing an org chart, a role, or a job description is internal-use-only. \
A note discussing a named person's salary, performance problem, or pending termination \
is confidential.
  - A note about the budget process or published figures is internal-use-only. A note \
with an unagreed negotiating position or a not-to-exceed number is confidential.
  - A published bio, resume, or article by or about the author is public.

WHEN IN DOUBT, RETURN internal-use-only. A false elevation costs the reviewer's attention \
and trains them to ignore the queue; that is the failure mode to avoid. Elevate only when \
you can name the specific sensitive content in the note.

Respond with ONLY a JSON object, no prose:
{{"tier": "<one of the four>", "confidence": "high"|"medium"|"low", \
"rationale": "<one sentence, max 25 words, naming the specific content that drove the \
tier; for internal-use-only say why briefly>"}}"""


# ─── Frontmatter handling (read via YAML, write via splice) ──────────────────

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Return (frontmatter_body, note_body, end_offset). fm is None if absent."""
    m = _FM_RE.match(text)
    if not m:
        return None, text, 0
    return m.group(1), text[m.end():], m.end()


def parse_fm(fm_body: str | None) -> dict:
    if not fm_body:
        return {}
    try:
        data = yaml.safe_load(fm_body)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def current_tier(fm: dict) -> str | None:
    """The note's tier today, or None if unset/unrecognised.

    An unrecognised value returns None rather than a tier: rag-sync fails
    closed on unknown values, so a typo'd note is already out of the index and
    should be re-evaluated from scratch, not treated as if it held its tier.
    """
    raw = fm.get("classification")
    if not isinstance(raw, str):
        return None
    val = raw.strip().strip('"').strip("'").lower()
    return val if val in TIER_RANK else None


# Folders whose contents are a higher tier by their nature rather than by their
# content, so an unlabeled note there is assumed sensitive rather than ordinary.
# Meetings and People carry this because the *class* of note is sensitive: a
# meeting of this vault's owner routinely turns to personnel and undisclosed
# matters, and a person record accumulates family and contact detail. Setting
# the floor here is what stops the classifier proposing the same elevation for a
# third of the vault, one note at a time.
#
# Folders absent from this map use DEFAULT_TIER. Nothing floors at `public` any
# more — that tier was retired 2026-08-22 and is now reserved for a deliberate
# manual marking.
FOLDER_BASELINE = {
    "Meetings": "confidential",
    "People": "confidential",
}


def baseline_tier(folder: str) -> str:
    """The tier an unclassified note is assumed to hold before any judgement.

    Notes are never left unset: rag-sync fails closed on an absent or
    unrecognised value, so a note with no tier is silently missing from the
    index.

    Provenance no longer enters into this. Until 2026-08-22 an external
    `source:` URL demoted a note to `public`, on the reasoning that externally
    published content is public wherever it landed. That was retired: the vault
    holds no redistribution rights to a third-party article whether or not it
    sat behind a paywall, and the *selection* of what was clipped is itself
    signal about its owner. Nothing defaults to `public` any more — see
    Knowledge/Data Classification.md, "Retiring the public tier".
    """
    return FOLDER_BASELINE.get(folder, DEFAULT_TIER)


def raw_fm_value(fm_body: str | None, key: str) -> str | None:
    """The literal text after `key:` in the frontmatter, unparsed.

    Deliberately not the YAML-parsed value: `updated: 2026-08-17T12:30` parses
    to a datetime whose repr is not the string the vault stores, and writing
    the repr back would itself be an edit.
    """
    if not fm_body:
        return None
    m = re.search(rf"(?m)^{re.escape(key)}\s*:[ \t]*(.*)$", fm_body)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


def yaml_quote(value: str) -> str:
    """A rationale rendered so it is valid YAML quoted OR unquoted.

    The quotes alone are not enough: this vault's Obsidian plugins reserialise
    frontmatter after an external write and drop "unnecessary" quoting. An
    unquoted `rationale: found [ssn]: a number` would then parse as a flow
    sequence or fail outright, corrupting the note's frontmatter. So the text
    is first stripped of every character that means something to a YAML plain
    scalar, and THEN quoted.
    """
    flat = " ".join(str(value).split())
    flat = flat.replace(":", " -").replace("[", "(").replace("]", ")")
    flat = flat.replace("{", "(").replace("}", ")").replace("#", "no.")
    flat = flat.replace('"', "'").replace("\\", "/")
    flat = flat.lstrip("-?,&*!|>%@`' ").strip()
    flat = " ".join(flat.split())
    return '"' + flat + '"'


def splice_frontmatter(text: str, updates: dict[str, str],
                       drop_keys: tuple[str, ...] = ()) -> str:
    """Insert/replace top-level keys in the frontmatter without reserialising it.

    `updates` values are pre-rendered YAML scalars. Keys already present at
    column 0 are replaced in place (preserving their position); new keys are
    appended before the closing fence. A file with no frontmatter gets a
    minimal block. Keys in `drop_keys` are removed if present.
    """
    m = _FM_RE.match(text)
    lf = "\r\n" if text[:2048].find("\r\n") != -1 else "\n"

    if not m:
        block = "---\n" + "".join(
            f"{k}: {v}\n" for k, v in updates.items()) + "---\n"
        sep = "" if text.startswith("\n") or not text else "\n"
        return (block + sep).replace("\n", lf) + text

    body_lines = m.group(1).split("\n")
    remaining = dict(updates)
    out: list[str] = []
    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        key_m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:", line)
        key = key_m.group(1) if key_m else None
        if key and (key in remaining or key in drop_keys):
            # Consume this key's whole entry, including any indented block
            # scalar or list items that belong to it.
            i += 1
            while i < len(body_lines) and re.match(r"^(\s+\S|\s*-\s)", body_lines[i]):
                i += 1
            if key in remaining:
                out.append(f"{key}: {remaining.pop(key)}")
            continue
        out.append(line)
        i += 1

    for k, v in remaining.items():
        out.append(f"{k}: {v}")

    new_body = "\n".join(out).rstrip("\n")
    rest = text[m.end():]
    # Assemble with LF only, then translate once — building with `lf` inline
    # and translating afterwards would turn each CRLF into CR + CRLF.
    return f"---\n{new_body}\n---\n".replace("\n", lf) + rest


# ─── Tracking ────────────────────────────────────────────────────────────────

def content_hash(text: str) -> str:
    """Hash of the note BODY only — frontmatter is deliberately excluded.

    Obsidian reserialises frontmatter whenever it indexes a note an external
    process touched: it rewrites list style, rewraps long values, and strips
    "unnecessary" quotes from ours. Hashing the whole file made that
    reserialisation look like a content change, so every queued note came back
    for re-adjudication on the next run — we rewrite the quotes, Obsidian
    strips them again, forever. The verdict depends on the body alone, so the
    body alone decides whether the verdict is stale.
    """
    _, body, _ = split_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def load_tracking() -> dict:
    if TRACKING_FILE.exists():
        try:
            return json.loads(TRACKING_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_tracking(tracking: dict) -> None:
    try:
        TRACKING_FILE.write_text(json.dumps(tracking, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"  Warning: could not write tracking file: {exc}")


# ─── Detection + adjudication ────────────────────────────────────────────────

def strip_code(body: str) -> str:
    return _INLINE_CODE.sub(" ", _CODE_FENCE.sub(" ", body))


def run_detectors(body: str) -> list[tuple[str, str, str]]:
    """Return [(rule_id, tier, finding)] for every L0 pattern that fires."""
    scanned = strip_code(body)
    return [(rid, tier, finding)
            for rid, tier, pattern, finding in DETECTORS
            if pattern.search(scanned)]


def adjudicate(client, title: str, folder: str, body: str) -> dict | None:
    """Ask the model for a tier. Returns None if the response was unusable."""
    excerpt = body[:MAX_CONTENT_CHARS]
    if len(body) > MAX_CONTENT_CHARS:
        excerpt += "\n[... truncated ...]"

    prompt = (f"Folder: {folder}\nTitle: {title}\n\n"
              f"NOTE BODY:\n{excerpt}")

    system_block = ([{"type": "text", "text": SYSTEM_PROMPT,
                      "cache_control": {"type": "ephemeral"}}]
                    if PROMPT_CACHE else SYSTEM_PROMPT)

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        system=system_block,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        import usage_log
        usage_log.record("classify_notes", CLASSIFIER_MODEL, response.usage)
    except Exception:
        pass

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        obj = re.search(r"\{.*\}", text, re.DOTALL)
        if obj:
            try:
                data = json.loads(obj.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        print(f"     Warning: unparseable response: {text[:160]}")
        return None
    tier = str(data.get("tier", "")).strip().lower()
    if tier not in TIER_RANK:
        print(f"     Warning: unknown tier {tier!r}")
        return None
    return {
        "tier": tier,
        "confidence": str(data.get("confidence", "")).strip().lower() or "unknown",
        "rationale": str(data.get("rationale", "")).strip(),
    }


# ─── Per-file processing ─────────────────────────────────────────────────────

def preserve_updated(updates: dict[str, str], fm_body: str | None) -> dict[str, str]:
    """Carry the note's existing `updated:` value through our own write.

    Annotating a note with a classification verdict is not a content edit by
    the author, and must not present as one: the vault's update-time-on-edit
    plugin covers Meetings/Knowledge/People, and a bulk pass that bumped every
    note to today would erase the recency signal the dashboard and the
    stale-sensitive-content query both read.
    """
    original = raw_fm_value(fm_body, "updated")
    if original:
        updates["updated"] = original
    return updates


def process_file(filepath: Path, client, dry_run: bool,
                 detectors_only: bool, force: bool) -> dict | None:
    """Evaluate one note. Returns an action record, or None if nothing to do."""
    with filepath.open("r", encoding="utf-8", newline="") as fh:
        original = fh.read()
    file_newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n").replace("\r", "\n")

    fm_body, body, _ = split_frontmatter(text)
    fm = parse_fm(fm_body)
    rel = filepath.relative_to(VAULT_ROOT)
    folder = rel.parts[0] if len(rel.parts) > 1 else "(root)"
    title = str(fm.get("title") or filepath.stem)

    cur = current_tier(fm)
    # An unset or typo'd note is judged against the tier it *would* have been
    # given at creation, not against -1. Without this floor the model could
    # "elevate" such a note to public, which is a demotion in disguise.
    baseline = baseline_tier(folder)
    cur_rank = TIER_RANK[cur] if cur else TIER_RANK[baseline]

    # A note the reviewer has already ruled on is settled. Only --force reopens
    # it; otherwise every run would re-nag on the same rejected suggestion.
    # L0 runs before any skip. The detectors cost nothing, admit no judgement,
    # and cover material that is regulated on sight — so a note that gains a
    # credential after being reviewed or queued must still be caught. Skipping
    # them to save a decision that was already made would be fail-open.
    hits = run_detectors(body)
    if hits:
        tier = max((h[1] for h in hits), key=lambda t: TIER_RANK[t])
        if TIER_RANK[tier] > cur_rank:
            findings = "; ".join(sorted({h[2] for h in hits}))
            rules = ",".join(sorted({h[0] for h in hits}))
            updates = {
                "classification": tier,
                "classification_rationale": yaml_quote(
                    f"auto-applied by detector [{rules}]: {findings}"),
                "classification_reviewed": "false",
            }
            updates["classification_prior"] = cur or f"(unset:{baseline})"
            preserve_updated(updates, fm_body)
            if not dry_run:
                new_text = splice_frontmatter(text, updates)
                filepath.write_text(new_text, encoding="utf-8", newline=file_newline)
            return {"action": "auto-applied", "rel": str(rel), "title": title,
                    "from": cur or "(unset)", "to": tier, "layer": "detector",
                    "confidence": "certain", "rationale": findings}
        return None

    if detectors_only:
        return None

    # Past this point every path costs a model call, so the skips apply here.
    if fm.get("classification_reviewed") is True and not force:
        return None

    # Already queued and not yet ruled on: the note is waiting on a person, not
    # on a better verdict. Re-adjudicating would spend tokens to overwrite the
    # rationale with a differently-worded one — and because the model is not
    # perfectly stable across runs, it could silently change what is being
    # proposed while it sits in the queue.
    if fm.get("classification_suggested") and not force:
        return None

    verdict = adjudicate(client, title, folder, body)
    if verdict is None:
        return {"action": "error", "rel": str(rel), "title": title,
                "from": cur or "(unset)", "to": "", "layer": "llm",
                "confidence": "", "rationale": "unparseable model response"}

    tier = verdict["tier"]

    elevated = TIER_RANK[tier] > cur_rank

    # Elevation only. A verdict at or below the note's tier is dropped rather
    # than written: the model has no demotion path, by design.
    if cur is not None and not elevated:
        return None

    updates: dict[str, str] = {}
    if cur is None:
        # Close the hole first — an unset note is invisible to rag-sync's
        # allowlist regardless of what the model decided.
        updates["classification"] = baseline
    if elevated:
        updates["classification_suggested"] = tier
        updates["classification_rationale"] = yaml_quote(verdict["rationale"])
        updates["classification_reviewed"] = "false"

    if not updates:
        return None
    preserve_updated(updates, fm_body)
    if not dry_run:
        filepath.write_text(splice_frontmatter(text, updates),
                            encoding="utf-8", newline=file_newline)

    return {"action": "suggested" if elevated else "backfilled",
            "rel": str(rel), "title": title,
            "from": cur or f"(unset -> {baseline})",
            "to": tier if elevated else baseline, "layer": "llm",
            "confidence": verdict["confidence"], "rationale": verdict["rationale"]}


# ─── Report ──────────────────────────────────────────────────────────────────

def reconcile(files: list[Path], dry_run: bool) -> tuple[int, int]:
    """Retire suggestion keys on notes whose tier now satisfies the proposal.

    Accepting a proposal means editing `classification` — which the Bases
    review table does in place, and which deliberately does not touch the
    three companion keys. Left alone they are inert but they keep the note in
    the queue view forever. This pass is the other half of accepting: where
    the current tier already meets or exceeds what was suggested, the
    proposal has been honoured, so the suggestion keys come off and the note
    is marked ruled-on.

    A tier BELOW the suggestion is left completely alone. That is an open
    decision, not an accepted one, and this pass must never silently close it.
    """
    accepted = skipped = 0
    for filepath in files:
        try:
            with filepath.open("r", encoding="utf-8", newline="") as fh:
                original = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        file_newline = "\r\n" if "\r\n" in original else "\n"
        text = original.replace("\r\n", "\n").replace("\r", "\n")
        fm_body, _, _ = split_frontmatter(text)
        fm = parse_fm(fm_body)

        suggested = fm.get("classification_suggested")
        if not isinstance(suggested, str):
            continue
        suggested = suggested.strip().strip('"').strip("'").lower()
        cur = current_tier(fm)
        if suggested not in TIER_RANK or cur is None:
            continue

        if TIER_RANK[cur] < TIER_RANK[suggested]:
            skipped += 1
            continue

        updates = {"classification_reviewed": "true"}
        preserve_updated(updates, fm_body)
        new_text = splice_frontmatter(
            text, updates,
            drop_keys=("classification_suggested", "classification_rationale"))
        if not dry_run:
            filepath.write_text(new_text, encoding="utf-8", newline=file_newline)
        accepted += 1
        print(f"  accepted {filepath.relative_to(VAULT_ROOT)} "
              f"({suggested} -> now {cur})")
    return accepted, skipped


def rebaseline(files: list[Path], dry_run: bool) -> tuple[int, int, int]:
    """Raise notes to their folder's FOLDER_BASELINE. Returns (raised, already, skipped).

    This is the retroactive half of moving a folder's floor. Changing the
    templates fixes new notes; the ones created under the old default are still
    sitting below the policy their folder now states, which matters because the
    export gate reads the property rather than the folder.

    It only ever RAISES. A note already at or above its floor is left alone, so
    the single restricted note in a confidential folder keeps its tier and the
    pass is idempotent — running it twice is a no-op rather than a slow slide
    toward the floor.

    It deliberately does not touch `classification_reviewed` or any pending
    proposal. This is a policy re-baseline, not a review decision, and
    conflating the two would silently mark notes as human-reviewed that no
    human looked at. Where a raise happens to satisfy a pending proposal,
    --reconcile is what retires it.
    """
    raised = already = skipped = 0
    for filepath in files:
        rel = filepath.relative_to(VAULT_ROOT)
        folder = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        floor = FOLDER_BASELINE.get(folder, DEFAULT_TIER)
        try:
            with filepath.open("r", encoding="utf-8", newline="") as fh:
                original = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        file_newline = "\r\n" if "\r\n" in original else "\n"
        text = original.replace("\r\n", "\n").replace("\r", "\n")
        fm_body, _, _ = split_frontmatter(text)
        fm = parse_fm(fm_body)
        cur = current_tier(fm)

        if cur is not None and TIER_RANK[cur] >= TIER_RANK[floor]:
            already += 1
            continue

        updates = {"classification": floor}
        preserve_updated(updates, fm_body)
        if not dry_run:
            filepath.write_text(splice_frontmatter(text, updates),
                                encoding="utf-8", newline=file_newline)
        raised += 1
        print(f"  {cur or '(unset)'} -> {floor}: {rel}")
    return raised, already, skipped


def rule_on(files: list[Path], action: str, tier_filter: str | None,
            dry_run: bool, set_tier: str | None = None) -> tuple[int, int]:
    """Accept or reject queued proposals in bulk. Returns (ruled, skipped).

    `accept` sets `classification` to the proposed tier and retires the
    suggestion keys — the same end state as editing the tier by hand and then
    running --reconcile, in one step.

    `set` records a tier of the reviewer's own choosing — the common case where
    they agree the note should move but disagree with where the model wanted to
    put it. Unlike the model, a person may move a note in either direction, so
    this is the one path that can lower a tier; it says so on the line it
    prints, because a reduction is the direction worth reading twice.

    `reject` marks the note ruled-on and deliberately KEEPS
    `classification_suggested` and `classification_rationale`. What was
    proposed and declined is the more useful record of the two, and it is what
    the review base's "Ruled on" view reads.

    Neither action will touch a note that has already been ruled on. Re-opening
    a settled decision is what --force is for.
    """
    ruled = skipped = 0
    for filepath in files:
        try:
            with filepath.open("r", encoding="utf-8", newline="") as fh:
                original = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        file_newline = "\r\n" if "\r\n" in original else "\n"
        text = original.replace("\r\n", "\n").replace("\r", "\n")
        fm_body, _, _ = split_frontmatter(text)
        fm = parse_fm(fm_body)

        suggested = fm.get("classification_suggested")
        if not isinstance(suggested, str):
            continue
        suggested = suggested.strip().strip('"').strip("'").lower()
        if suggested not in TIER_RANK:
            continue
        if fm.get("classification_reviewed") is True:
            continue
        if tier_filter and suggested != tier_filter:
            skipped += 1
            continue

        rel = filepath.relative_to(VAULT_ROOT)
        if action == "accept":
            updates = {"classification": suggested,
                       "classification_reviewed": "true"}
            drop = ("classification_suggested", "classification_rationale")
            verb = f"accepted -> {suggested}"
        elif action == "set":
            cur = current_tier(fm)
            updates = {"classification": set_tier,
                       "classification_reviewed": "true"}
            drop = ("classification_suggested", "classification_rationale")
            direction = ""
            if cur and TIER_RANK[set_tier] < TIER_RANK[cur]:
                direction = "  [LOWERED]"
            verb = (f"set -> {set_tier} (was {cur or 'unset'}, "
                    f"model proposed {suggested}){direction}")
        else:
            updates = {"classification_reviewed": "true"}
            drop = ()
            verb = f"rejected (stays {current_tier(fm) or 'unset'})"

        preserve_updated(updates, fm_body)
        new_text = splice_frontmatter(text, updates, drop_keys=drop)
        if not dry_run:
            filepath.write_text(new_text, encoding="utf-8", newline=file_newline)
        ruled += 1
        print(f"  {verb}: {rel}")
    return ruled, skipped


def write_report(records: list[dict], scanned: int, dry_run: bool) -> None:
    # The report lists, in one place, every note the run judged sensitive and
    # why — a concentrated index of the vault's most sensitive material. It
    # inherits the highest tier it names; classifying it internal-use-only
    # would leak past exactly the gates this script exists to feed.
    report_tier = DEFAULT_TIER
    for r in records:
        for tier in (r.get("to"), r.get("from")):
            if tier in TIER_RANK and TIER_RANK[tier] > TIER_RANK[report_tier]:
                report_tier = tier

    lines = [
        "---",
        "title: Last Classification Review",
        f"classification: {report_tier}",
        "tags:",
        "  - classification",
        "---",
        "",
        f"# Last classification review — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Mode: {'DRY RUN' if dry_run else 'LIVE'} · notes evaluated: {scanned}",
        "",
        f"> Classified `{report_tier}` — it inherits the highest tier it lists.",
        "",
    ]
    for action, heading in (
            ("auto-applied", "Auto-applied by detector (L0, already written)"),
            ("suggested", "Suggested for review (L1, `classification` untouched)"),
            ("backfilled", "Backfilled missing default"),
            ("error", "Errors")):
        rows = [r for r in records if r["action"] == action]
        if not rows:
            continue
        lines += [f"## {heading} — {len(rows)}", ""]
        lines += ["| Note | From | To | Confidence | Rationale |",
                  "| --- | --- | --- | --- | --- |"]
        for r in rows:
            note = r["rel"].replace("|", "\\|")
            rationale = r["rationale"].replace("|", "\\|")
            lines.append(f"| [[{Path(note).stem}]] | {r['from']} | {r['to']} "
                         f"| {r['confidence']} | {rationale} |")
        lines.append("")
    try:
        REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  Warning: could not write report: {exc}")


# ─── Main ────────────────────────────────────────────────────────────────────

def collect_files(folders: list[str] | None) -> list[Path]:
    files: list[Path] = []
    for md in sorted(VAULT_ROOT.rglob("*.md")):
        rel = md.relative_to(VAULT_ROOT)
        if not rel.parts or rel.parts[0] in SKIP_TOP:
            continue
        if any(p.startswith(".") for p in rel.parts):
            continue
        if any(rel.as_posix().startswith(sub + "/") for sub in SKIP_SUBTREES):
            continue
        if folders and rel.parts[0] not in folders:
            continue
        files.append(md)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DLP-style classification assistant for an Obsidian vault")
    parser.add_argument("--vault", type=str, help="Override vault root")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate and report without writing to notes")
    parser.add_argument("--force", action="store_true",
                        help="Ignore tracking cache and classification_reviewed")
    parser.add_argument("--file", type=str, help="Evaluate one file")
    parser.add_argument("--folder", action="append", metavar="NAME",
                        help="Limit to this top-level folder (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N notes actually evaluated")
    parser.add_argument("--accept", action="store_true",
                        help="Accept queued proposals: set classification to the "
                             "proposed tier and retire the suggestion keys. "
                             "Narrow it with --tier / --folder / --file.")
    parser.add_argument("--reject", action="store_true",
                        help="Decline queued proposals: mark them ruled-on and "
                             "leave the tier alone. The proposal is kept as the "
                             "record of what was declined.")
    parser.add_argument("--set-tier", choices=TIERS, metavar="TIER",
                        dest="set_tier",
                        help="Record THIS tier on the notes in scope instead of "
                             "the one proposed — for when you agree the note "
                             "should move but not with where. Requires an "
                             "explicit scope (--tier / --folder / --file).")
    parser.add_argument("--tier", choices=TIERS, metavar="TIER",
                        help="Restrict --accept/--reject to proposals AT this "
                             "tier. Without it, every pending proposal in scope "
                             "is ruled on.")
    parser.add_argument("--rebaseline", action="store_true",
                        help="Raise notes to their folder's baseline tier "
                             "(FOLDER_BASELINE). Only ever raises, never lowers, "
                             "and is idempotent. Use after moving a folder's "
                             "floor, to bring notes created under the old "
                             "default into line. Requires --folder.")
    parser.add_argument("--reconcile", action="store_true",
                        help="Retire suggestion keys on notes whose tier now "
                             "satisfies the proposal, and mark them ruled-on. "
                             "Makes no model calls. Run after working the "
                             "review queue.")
    parser.add_argument("--detectors-only", action="store_true",
                        help="Run L0 detectors only — no API calls, no key needed")
    parser.add_argument("--workers", type=int,
                        default=int(os.environ.get("CLASSIFY_WORKERS", "6")),
                        help="Concurrent adjudications (default 6). Each note is "
                             "independent and writes only its own file; the shared "
                             "tracking map is mutex-guarded.")
    args = parser.parse_args()

    global VAULT_ROOT, TRACKING_FILE, REPORT_FILE
    if args.vault:
        VAULT_ROOT = Path(args.vault).resolve()
        TRACKING_FILE = VAULT_ROOT / ".classification_tracking.json"
        REPORT_FILE = VAULT_ROOT / "Templates" / "Scripts" / "last-classification-review.md"

    import script_lock
    lock = script_lock.acquire_or_exit("classify_notes", warn=print)

    if args.rebaseline:
        if not args.folder:
            print("Error: --rebaseline needs --folder (repeatable). Folders "
                  "without an explicit floor use the working default "
                  f"({DEFAULT_TIER}); explicit floors: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(FOLDER_BASELINE.items())))
            return 2
        targets = collect_files(args.folder)
        print("Re-baselining to folder floors")
        print(f"   Vault:  {VAULT_ROOT}")
        for f in args.folder:
            print(f"   Floor:  {f} -> "
                  f"{FOLDER_BASELINE.get(f, DEFAULT_TIER)}")
        print(f"   Mode:   {'DRY RUN' if args.dry_run else 'LIVE'}\n")
        raised, already, skipped = rebaseline(targets, args.dry_run)
        print(f"\nRaised {raised}; {already} already at or above the floor; "
              f"{skipped} outside a baselined folder.")
        if args.dry_run and raised:
            print("DRY RUN — nothing was modified.")
        del lock
        return 0

    if sum(bool(x) for x in (args.accept, args.reject, args.set_tier)) > 1:
        print("Error: --accept, --reject and --set-tier are mutually exclusive.")
        return 2

    if args.set_tier and not (args.tier or args.folder or args.file):
        # Without a scope this would rewrite the tier of every queued note in
        # the vault from one flag. Scope is not a convenience here.
        print("Error: --set-tier needs an explicit scope: --tier, --folder "
              "or --file.")
        return 2

    if args.accept or args.reject or args.set_tier:
        action = ("accept" if args.accept
                  else "reject" if args.reject else "set")
        targets = ([Path(args.file)] if args.file
                   else collect_files(args.folder))
        targets = [t if t.is_absolute() else VAULT_ROOT / t for t in targets]
        scope = args.tier or "every pending tier"
        headline = (f"SET -> {args.set_tier}" if action == "set"
                    else action.upper())
        print(f"Ruling on classification proposals: {headline}")
        print(f"   Vault: {VAULT_ROOT}")
        print(f"   Scope: {scope}"
              + (f", folder(s) {','.join(args.folder)}" if args.folder else "")
              + (f", file {args.file}" if args.file else ""))
        print(f"   Mode:  {'DRY RUN' if args.dry_run else 'LIVE'}\n")
        ruled, out_of_scope = rule_on(targets, action, args.tier, args.dry_run,
                                      set_tier=args.set_tier)
        past = {"accept": "Accepted", "reject": "Rejected", "set": "Set"}[action]
        print(f"\n{past} {ruled} proposal(s); "
              f"{out_of_scope} pending but outside --tier.")
        if args.dry_run and ruled:
            print("DRY RUN — nothing was modified.")
        del lock
        return 0

    if args.reconcile:
        targets = ([Path(args.file)] if args.file
                   else collect_files(args.folder))
        targets = [t if t.is_absolute() else VAULT_ROOT / t for t in targets]
        print("Reconciling accepted classification proposals")
        print(f"   Vault: {VAULT_ROOT}")
        print(f"   Mode:  {'DRY RUN' if args.dry_run else 'LIVE'}\n")
        accepted, still_open = reconcile(targets, args.dry_run)
        print(f"\nRetired {accepted} accepted proposal(s); "
              f"{still_open} still below the suggested tier and left open.")
        if args.dry_run and accepted:
            print("DRY RUN — nothing was modified.")
        del lock
        return 0

    client = None
    endpoint = ""
    if not args.detectors_only:
        # llm_endpoint owns WHERE calls go and WHICH stored secret opens the
        # door, so routing through an institutional gateway is .env config
        # rather than a code divergence between this file and its repo copy.
        import llm_endpoint
        try:
            client = llm_endpoint.client()
        except llm_endpoint.EndpointError as exc:
            print(f"Error: {exc}")
            return 1
        endpoint = llm_endpoint.describe()

    print("Obsidian Classification Assistant")
    print(f"   Vault: {VAULT_ROOT}")
    print(f"   Mode:  {'DRY RUN' if args.dry_run else 'LIVE'}"
          f"{' · detectors only' if args.detectors_only else ''}")
    if not args.detectors_only:
        print(f"   Model: {CLASSIFIER_MODEL}")
        print(f"   Auth:  {endpoint}")
    print()

    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = VAULT_ROOT / target
        target = target.resolve()
        if not target.is_file():
            # Silently evaluating nothing here reads as "clean", which is the
            # worst possible answer from a classification tool.
            print(f"Error: no such note: {args.file}")
            return 1
        files = [target]
    else:
        files = collect_files(args.folder)
    if not files:
        print("   No notes matched.")
        return 0

    tracking = {} if args.force else load_tracking()
    records: list[dict] = []
    deferred = skipped = 0

    # Decide the work list up front (cheap, serial) so --limit is deterministic
    # and the recent-edit / unchanged counts are exact.
    queue: list[Path] = []
    for filepath in files:
        if args.limit and len(queue) >= args.limit:
            break
        if RECENT_EDIT_GUARD_SECONDS > 0 and not args.force and not args.file:
            try:
                if time.time() - filepath.stat().st_mtime < RECENT_EDIT_GUARD_SECONDS:
                    deferred += 1
                    continue
            except OSError:
                pass
        try:
            key = str(filepath.relative_to(VAULT_ROOT))
            digest = content_hash(filepath.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not args.force and tracking.get(key) == digest:
            skipped += 1
            continue
        queue.append(filepath)

    evaluated = len(queue)
    guard = threading.Lock()
    marker = {"auto-applied": "!!", "suggested": "->", "backfilled": "..",
              "error": "??"}

    def handle(filepath: Path) -> None:
        try:
            record = process_file(filepath, client, args.dry_run,
                                  args.detectors_only, args.force)
        except Exception as exc:
            with guard:
                print(f"  Error on {filepath.name}: {exc}")
            return
        # One lock covers the shared list, stdout and the tracking map, so a
        # record and its detail line cannot be split by another worker.
        with guard:
            if record:
                records.append(record)
                print(f"  {marker[record['action']]} {record['rel']}")
                print(f"       {record['from']} -> {record['to']} "
                      f"({record['layer']}, {record['confidence']}) "
                      f"{record['rationale']}")
            if not args.dry_run:
                try:
                    tracking[str(filepath.relative_to(VAULT_ROOT))] = content_hash(
                        filepath.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass

    workers = 1 if args.detectors_only else max(1, args.workers)
    if workers == 1:
        for filepath in queue:
            handle(filepath)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(handle, queue))

    if not args.dry_run:
        save_tracking(tracking)
    write_report(records, evaluated, args.dry_run)

    counts = {a: sum(1 for r in records if r["action"] == a)
              for a in ("auto-applied", "suggested", "backfilled", "error")}
    print()
    print(f"Evaluated {evaluated} · unchanged {skipped} · deferred {deferred}")
    print(f"  auto-applied (L0): {counts['auto-applied']}")
    print(f"  suggested  (L1):   {counts['suggested']}")
    print(f"  backfilled:        {counts['backfilled']}")
    print(f"  errors:            {counts['error']}")
    print(f"Report: {REPORT_FILE}")
    if args.dry_run and records:
        print("\nDRY RUN — no notes were modified. Re-run without --dry-run to apply.")

    del lock
    return 0


if __name__ == "__main__":
    sys.exit(main())
