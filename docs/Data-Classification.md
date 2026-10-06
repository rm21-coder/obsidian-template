# Data Classification — assistant and export gate

Two scripts, one property. `classify_notes.py` proposes a data-classification
tier for every note; `disclosure_check.py` refuses to export content above the
tier its audience is cleared for. Neither is useful without the other: a label
nothing enforces is decoration, and a gate with nothing to read is a no-op.

- Component: `58-classification`
- Agent: `com.obsidian.classify` — 05:00, with catch-ups at 08:00 and 12:30
- Log: `~/Library/Logs/obsidian-classify.log`
- Review queue: `Actions/Classification Review` (generated each run); the
  morning dashboard shows the pending count

## The problem this solves

Templates write `classification: internal-use-only` at creation. That is the
right default and it is also where the property stops, because nobody
remembers to elevate a note while writing it. On the vault this was built
against, 2,147 of 2,449 classified notes sat at the template default and only
77 were `confidential` — 74 of those written by a scheduled pipeline rather
than by a person. The scheme nominally had four tiers and effectively ran on
one.

Automating the *writing* of the property fixes nothing. Automating the
*judgement* is the actual problem.

## Three layers

**L0 — deterministic detectors.** A fixed table of high-precision patterns for
material that is regulated on sight: SSN, private key block, API token, AWS key
id, MRN followed by a value. These **auto-apply `classification: restricted`**
and record the previous tier in `classification_prior`. No model is consulted —
a regex is more trustworthy than an LLM for "is this a private key," and this
layer must keep working when the endpoint is down.

Every pattern must match an *instance*, never a topic. `HIPAA` is a topic; a
nine-digit SSN is an instance. Fenced and inline code is stripped before
matching, so a policy note quoting a placeholder credential does not classify
itself. Run this layer alone with `--detectors-only`: no model calls, no API
key needed.

**L1 — model adjudication.** Everything else goes to a Haiku-class model with
the four tier definitions and, centrally, the **topic-versus-instance** rule. A
vault like this discusses regulation, security, governance and personnel policy
constantly as subject matter; that is ordinary professional content. A note is
elevated only when it *contains* the sensitive thing.

This matters more than it sounds. Measured against a real vault, candidate
keyword rules fired on 121 of 2,200 notes and the top hits were the data-
classification policy note itself, the owner's published bio, and a job
description — notes *about* PHI, containing none. Keyword DLP alone was roughly
80% false positive. The rubric rejected all of them.

Set `CLASSIFIER_ORG_CONTEXT` in `~/dev/secrets/.env` to one line describing
whose vault this is and what field they work in. It is the highest-leverage
input to accuracy, because the topic-versus-instance rule depends on knowing
which sensitive-sounding subjects are simply this person's day job.

**L2 — human gate.** L1 **never writes `classification`.** It writes
`classification_suggested`, `classification_rationale` and
`classification_reviewed: false`, which surface as the review queue. Accepting
is a one-line edit; ignoring leaves the note where it was.

The queue is worked from a generated page, `Actions/Classification Review`,
for one reason: reviewing a backlog of seventy notes by opening seventy notes
is a workflow nobody completes. With the Meta Bind plugin installed, each
pending proposal on that page carries a tier dropdown and a Reject toggle bound
to the note itself, so ruling on it never means leaving the page. The template
does not ship Meta Bind; without it the page is a read-only list and you rule
from the command line (see "Working the queue" below). A Bases table
did this job first and was replaced: Obsidian has no pick-list property type,
so the tier could only be typed, and accepting took a second step.

| Key | Written by | Meaning |
| --- | --- | --- |
| `classification_suggested` | L1 | Proposed tier, awaiting review. Carries no force. |
| `classification_rationale` | L0 or L1 | One line naming the content that drove the verdict. |
| `classification_reviewed` | script writes `false`; **you set `true`** | `true` settles the note — excluded from future runs. |
| `classification_prior` | L0 | Tier held before an auto-application, so it is reversible. |

## Design decisions worth knowing

**Elevation only.** A verdict at or below the note's current tier is discarded,
never written. An automated demotion path would be a data-exfiltration
primitive. A note with no usable tier is judged against the tier it would have
received at creation, so "elevating" an unlabeled note down to `public` is not
reachable.

**Folder baselines — sensitive by class rather than by content.**
`FOLDER_BASELINE` floors whole folders, because for some the tier is a property
of the class of note and not of any individual note's text. The shipped defaults
floor `Meetings/` and `People/` at `confidential`: a meeting in a vault like this
routinely turns to personnel and undisclosed matters, and a person record
accumulates addresses and family names over time. Left at the global default,
the classifier proposes the same elevation across a third of the vault one note
at a time — a review burden with no judgement in it.

Set the floor and the reviewer only sees genuine exceptions. It also gives the
*absence* of an elevation meaning: a `Meetings/` note that is not confidential
was marked down deliberately.

Two other places encode the same folder defaults and must move together, or
one will write the old value back while another proposes raising it: `vault_lint.py`'s `SCHEMA_DEFAULTS` and `add_classification.py`'s
`FOLDER_DEFAULT`.

**Nothing defaults to `public`.** Clipped articles, YouTube summaries and
recipes are created `internal-use-only`, and no rule demotes a note to `public`
on the strength of an external `source:` URL. An earlier version had one,
reasoning that content already published elsewhere costs nothing to disclose.
That is true of the article and false of the note: the vault holds no
redistribution rights to a third-party piece whether or not it sat behind a
paywall — and paywall status is not something a classifier can determine anyway
— while *which* articles were clipped, annotated and tagged is itself signal
about their owner. A reading list is not public because each item on it is.

`public` remains a valid tier for a note deliberately marked safe to release; it
is simply never arrived at by default, and the model's rubric says so.

This is a statement about a **vault**, and it is the opposite of the rule
governing this **repository**: `installers/lib/check_classification.py` requires
`classification: public` on any content note committed here, because that note
is a genuinely public artifact. The two are not in conflict, and neither should
be "fixed" to match the other.

Note the knock-on for the export gate: with `Meetings/` at `confidential`, the
`internal` audience no longer passes meeting notes, and `cleared` becomes the
normal audience for them.

**Detectors run before any skip.** A note you marked `reviewed: true`, or one
already sitting in the queue, still gets scanned by L0 on every run. Skipping
that to honour a decision already made would mean a settled note that later
gains a credential is silently missed.

**Frontmatter is spliced, not round-tripped.** Reserialising thousands of notes
to add one key would rewrite quoting and date forms across the whole vault and
bury the real change in diff noise. Existing keys are replaced in place; new
ones are appended before the closing fence.

**The tracking hash covers the body only.** See the Obsidian interaction below —
this is not an optimisation, it is what stops an infinite rewrite loop.

**A queued note is not re-adjudicated.** It is waiting on a person, not on a
better verdict. Re-running would spend tokens to reword the rationale, and
because the model is not perfectly stable at tier boundaries it could silently
change what is being proposed while it sits in the queue.

## Running it with Obsidian open — read this before the first pass

Obsidian rewrites the frontmatter of any note an external process touches,
within milliseconds. Two distinct effects, both from Obsidian:

- The `update-time-on-edit` plugin bumps `updated:`. Its ignore list typically
  covers only `Templates`, `Z_attachments` and `.obsidian`, so content folders
  are exposed. A bulk pass with the app open stamps today's date on every note
  it touches and flattens the recency signal that dashboards and
  stale-content queries read. It re-bumps faster than a script can write the
  old value back. The script re-asserts the original `updated:` in its own
  write, which holds when the app is closed.
- The frontmatter serialiser strips "unnecessary" quotes and rewraps values.
  Two consequences the code handles: any free-text value is sanitised to be
  valid YAML *unquoted as well as quoted* before it is quoted (an unquoted
  `rationale: found [ssn]: a number` would otherwise parse as a flow sequence);
  and the tracking hash covers the body only, because hashing the whole file
  made reserialisation look like a content edit and brought every annotated
  note back for reprocessing forever.

**So: run the first full pass with Obsidian quit.** Nightly incremental runs are
fine either way — they only touch notes that genuinely changed.

## Usage

```bash
V=~/Obsidian/Templates/Scripts/.venv/bin/python3
S=~/Obsidian/Templates/Scripts/classify_notes.py

$V $S --dry-run --workers 8        # preview the whole vault
$V $S --dry-run --limit 40         # sample first
$V $S --workers 8                  # apply
$V $S --folder Meetings            # one folder (repeatable)
$V $S --file "path/to/note.md"     # one note
$V $S --detectors-only             # L0 only: no model calls, no key
$V $S --force                      # ignore tracking, reviewed flags, and the queue
$V $S --reconcile                  # retire proposals you have accepted

$V $S --accept --tier confidential            # rule on a whole tier at once
$V $S --accept --tier confidential --folder Meetings
$V $S --reject --file "Knowledge/Some Note.md"
$V $S --accept --tier confidential --dry-run  # preview first
```

### Working the queue

**Work it from `Actions/Classification Review`.** Every run of the classifier
regenerates that page with one block per pending proposal: the note, its tier
now and the tier proposed, the reason the classifier gave, and two live fields
bound to the note itself.

- **Accept:** pick the suggested tier in the *Tier* dropdown. That writes
  `classification` on the note; nothing else is needed.
- **Reject:** switch on *Reject*. That sets `classification_reviewed: true`,
  so the note keeps its tier and is never proposed again. The declined
  proposal stays on the note as the record.
- **Later:** leave the block alone. Nothing is re-adjudicated while it waits.

The page does not change under you. The next run retires what you accepted
(the same pass as `--reconcile`, skipping notes edited in the last two
minutes) and drops what you rejected. The page lists only notes still below
their suggestion, and it inherits the highest tier it names, like the report.
The morning dashboard shows a "Classification review" card with the count
while anything is pending, and says so if the page has not been refreshed in
48 hours.

The fields are [Meta Bind](https://github.com/mProjectsCode/obsidian-meta-bind-plugin)
inline fields in list form. Meta Bind is **not** in the template's pinned plugin
set: install it yourself to get the fields. Without it they render as inline
code and the page is a read-only list; rule from the command line. Bound fields inside a Markdown table were tried
and misbehaved, so the page never uses one. A note whose path contains a
character Meta Bind reserves (`{ } [ ] # ^ | : ?`, and a few that would break
the page: `` ` % $ < > \ ``) gets a plain entry telling you to set the property
in its Properties panel instead. Classifier rationales are model output about
note content, which can be a clipped web page, so they are stripped of inline
code, brackets, HTML and comment delimiters before they reach the page: a
rationale must not be able to plant a live field or hide the entries after it.

The command line does the same in bulk:

1. Decline the ones you disagree with, individually:
   `--reject --file "path/to/note.md"`.
2. Accept the rest in a batch: `--accept --tier confidential`. Add `--folder`
   to narrow it further, and `--dry-run` to see the list first.
3. `--set-tier` records a tier of your choosing when you agree the note should
   move but not with where.

The Bases table (`Templates/Bases/Classification Review.base`, which you can
embed in a note of your own, e.g. under `Topics/`) stays as the audit view: detector auto-applications,
the sensitive inventory, and what has been ruled on. Its cells can edit in
place too, but Obsidian has no pick-list property type, so a tier has to be
typed exactly, and accepting there still needs the next run's reconcile.

> **Editing in the table requires the property to have a registered type.**
> Obsidian will not offer a real editor for a property it has no type for, so
> `classification` renders as a dead cell showing only its current value. Fix
> it once: open any note that has the property, click the property's type icon
> in the Properties panel, and set it to **Text**. Registered types live in
> `.obsidian/types.json`. Note that a `metadata-menu` field definition does
> *not* satisfy this — Bases does not read that plugin's field types, so having
> `classification` defined there as a Select still leaves the table cell
> uneditable. The command-line path above is unaffected either way.
>
> The template's `.obsidian/types.json` registers `classification` and its
> companion keys as Text and `classification_reviewed` as a Checkbox, so a
> fresh clone works as shipped. A vault that predates that, or that keeps its
> own `.obsidian/`, needs the one-time fix above for both properties: without
> the Checkbox type, the pending view's `Ruled on` column is a dead cell too,
> and the table offers no way to reject.

Each run writes `Templates/Scripts/last-classification-review.md`, including
the notes it left alone. That file inherits the highest tier it names — it is a
concentrated index of the vault's most sensitive material and must not sit at
the default.

Concurrency is `--workers` (default 6, `CLASSIFY_WORKERS` in the environment).
Notes modified in the last 120 seconds are deferred, since the note is probably
open in the editor; override with `CLASSIFY_RECENT_EDIT_GUARD_SECONDS`.

`Meetings/_Runs` (retained only for installs predating its removal) and other
generated-log subtrees are skipped, matching
`vault_lint.py`'s treatment of them as logs rather than notes.

## The export gate

`disclosure_check.py` gates against an *audience ceiling* rather than one
threshold.

| Audience | Ceiling | For |
| --- | --- | --- |
| `public` | `public` | Leaving the organization — a public repo, a site, a talk |
| `internal` | `internal-use-only` | Circulating internally |
| `cleared` | `confidential` | A named, cleared distribution |

`restricted` is never exportable at any audience, and `--override` does not
lift that bar — the answer there is to move the specific material out of the
note.

```bash
G=~/Obsidian/Templates/Scripts/disclosure_check.py

$V $G check NOTE... --audience public
$V $G export NOTE... --to DIR --audience internal
$V $G export DIR --to OUT --audience public --skip-blocked   # + WITHHELD.md
$V $G check NOTE --audience public --override "reason"        # logged
```

Exit codes: `0` clear, `1` blocked, `2` usage error — so it composes into other
scripts.

**Transclusion closure is the point.** An embed `![[Note]]` pulls that note's
body into the exporting file, so exporting A discloses everything A embeds
regardless of A's own tier. The gate resolves embeds recursively (depth 6) and
judges the whole closure: a `public` note embedding a `public` note embedding a
`restricted` one is blocked. Plain `[[links]]` carry no content and are
reported for information only.

What counts as "renders into the note" is deliberately broad: `![[wiki]]`
embeds (including the `\|` alias form tables need), Markdown `![](Note.md)`
embeds, the file and text nodes of a `.canvas`, and every link inside an
Excalidraw drawing. A reference that could mean several notes — a duplicate
name, a relative or partial path — brings **all** of them into the closure,
so the gate never has to guess which one Obsidian would pick.

**Fail closed on what it cannot see** (since 2026-09-25). The gate used to
treat an unresolved embed as advisory, on the premise that nothing exists
behind it. That premise held only if its link resolution matched Obsidian's
exactly, and an adversarial review found several forms where it did not —
each one a restricted note exported as "clear". So now:

| Situation | Result |
|---|---|
| Embed resolves to nothing in the vault | **Blocks**; `--override` allowed (you can see the target does not exist) |
| Query block (`dataview`, `dataviewjs`, `tasks`, `base`, inline `` `= …` ``) or an embedded `.base` | **Blocks**; `--override` allowed — look at what it renders first; the gate cannot |
| An unparseable canvas, an unreadable dependency, a closure deeper than 6 | **Blocks; cannot be overridden** — a restricted note may be behind it |
| A declared tier the gate does not recognise — including beside a recognised one — or frontmatter it cannot read reliably (a JSON/flow mapping, a quoted key, a nested or multi-line `classification`) | **Blocks; cannot be overridden**, and `--treat-unclassified` does not apply |
| A query or base that names a restricted note, or a folder holding one | **Blocks; cannot be overridden** — quoted names, `[[links]]` and `file:`/`path:` tokens inside queries and bases are resolved and judged |
| A Templater **dynamic command** (`<%+ … %>`, `<%*+ … %>`) anywhere | **Blocks; cannot be overridden.** Templater runs these in every note shown in reading view, from a post-processor no setting or folder limits; `tp.file.include` renders any other note under a name the source need not spell out |
| A query in the export when some list item anywhere in the vault carries a Meta Bind field or raw `<code>` | **Blocks; cannot be overridden.** Tasks renders a task, and its child items, under the task's own path, so the host's folder does not decide whether Meta Bind renders it |
| A Meta Bind **block** (any `meta-bind…` code block: embed, button) anywhere | **Blocks; cannot be overridden.** These blocks ignore Meta Bind's excluded folders and an embed renders a whole other note |
| An inline Meta Bind field (`INPUT[`, `VIEW[`, `BUTTON[`), raw `<code>` HTML, or any query block, in a note (or canvas) Meta Bind renders in | **Blocks; cannot be overridden.** Meta Bind displays other notes' properties, finds fields in rendered `<code>` text, and can build a note's name from property values; a query (a Tasks block in `Actions/To-Do`, say) renders other notes' text, code spans included, into the page. The scope is Meta Bind's own **Excluded folders** setting; outside it inline fields are inert. Unreadable settings, or Meta Bind's JavaScript on, put every folder in scope; a folder whose name differs between Unicode forms never counts as excluded. The setting is watched by the plugin integrity check |
| Images, audio, video, PDF | Reported, advisory — media carries no tier |

**Reading the tier.** Every gate reads `classification:` through one reader
(`classification_tier.py`): YAML comment and quoting rules apply
(`restricted  # PHI` is `restricted`), and if the key appears more than once
the **most restrictive** value wins. Anything at the top level of the
frontmatter that is not a plain `key: value` line or a list item is treated as
unreadable rather than guessed at.

**Unlabelled notes.** A note with no `classification` value blocks.
`--treat-unclassified TIER` relaxes that only where something *other than the
label* already establishes the tier — repo documentation in an already-public
checkout, for instance. Never point it at a live vault.

**Audited.** Every decision, and every `--override` with its stated reason, is
appended to the security alert log as `control: disclosure-check`.

### Relationship to `02-classification-audit`

That component, backed by `installers/lib/check_classification.py` and a
pre-commit hook, is the primary gate on *this repo* carrying only public
content. It remains the gate. It checks files one at a time, so a `public` note
embedding a non-public one passes it; `disclosure_check.py` covers that case.
They are complementary.

What it checks, since 2026-10-04:

- **Every `.md` file, in any letter case, by default.** Only the template's
  own scaffolding is exempt: `Templates/`, `docs/`, `installers/`,
  `Z_attachments/`, dot-folders, and the root `README.md`, `CLAUDE.md` and
  `ONBOARDING.md`. Before that, it audited a list of content folders, so a
  note in `Z_archive/`, in a new top-level folder or at the root went
  unchecked.
- **At commit,** the staged copy of each file — what the commit will hold,
  not the working copy — including files whose names have accented characters
  (git quotes those, and they used to drop out of the list) and a symlink
  replaced by a file.
- **At push,** every version of every note in every commit being pushed. A
  note committed with `--no-verify` and removed or relabelled in a later
  commit is still published by the push, because history is public too. The
  identity gate does the same for real names and addresses.
- **In the full security suite** (`security-checks.sh`), the whole working
  tree, as the `content` pass.

## Endpoint and model

Both scripts resolve credentials through `llm_endpoint.py`: stock Anthropic with
`ANTHROPIC_API_KEY` by default, or set `LLM_BASE_URL` and `LLM_API_KEY_NAME` in
`~/dev/secrets/.env` to route through an institutional gateway. The key itself
is looked up by that name through `secret_store.get_secret`: the environment
(which includes `.env`) first, then the platform keystore (the macOS Keychain,
or DPAPI on Windows), where the installer stores it. Routing is
therefore configuration, not a code divergence between your vault's copy of
these files and this repo's.

| Variable | Default | Notes |
| --- | --- | --- |
| `CLASSIFIER_MODEL` | `claude-haiku-4-5-20251001` | Gateways often want their own alias |
| `CLASSIFIER_PROMPT_CACHE` | `1` | Set `0` where the endpoint ignores `cache_control` |
| `CLASSIFIER_ORG_CONTEXT` | generic | One line: whose vault, what field |
| `CLASSIFIER_MAX_CHARS` | `6000` | Body characters sent per note |
| `CLASSIFY_WORKERS` | `6` | Concurrent adjudications |

Set these in `.env`, not in the plist, so a manual CLI run and the scheduled run
resolve identically.

## Cost

Steady state is only notes whose body changed — a handful a day, cents a month.
A first pass over ~2,500 notes ran about $4 at Haiku prices. Cost is not the
design constraint here; accuracy is.

## Accuracy, measured

Against the vault this was built on: 7/7 rejections of notes that candidate
keyword rules falsely flagged, 6/6 recall on planted sensitive notes
(personnel/severance, vendor walk-away pricing, undisclosed incident, embargoed
board pre-read, PHI, privileged legal), 4/4 correct holds on ordinary content
written to bait it. PHI went to `restricted`, the rest to `confidential`.

Verdicts are **not** perfectly reproducible at tier boundaries. One note flipped
between `public` and `internal-use-only` across runs, and one borderline note
flagged once in six trials. That instability is confined to the low tiers; the
`confidential` findings reproduced identically across three runs at high
confidence. It is also the reason the human gate exists and the reason queued
notes are not re-adjudicated.
