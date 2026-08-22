# Demo Content

The repo ships content-free. Every content folder — `People/`, `Meetings/`,
`Knowledge/` and the rest — is empty on clone, which is correct for a public
template but makes the workflow hard to see: empty Bases, empty Topics pages, a
Morning Dashboard with nothing on it.

One command fills the vault with a synthetic dataset, and one command takes it
back out:

```bash
python3 Templates/Scripts/seed_demo_content.py
```

```bash
python3 Templates/Scripts/seed_demo_content.py --remove
```

That is the whole mechanism. Nothing is committed, nothing is shipped as files
— if it is demo data, this script wrote it.

## What it is

All of it is invented. The organization is **Nimbus Widgets Inc.**, its cloud
vendor is **Cumulus Cloud Co.**, and everyone is named after the thing they do
— Wanda Widget the CIO, Rosie Rackmount the CISO, Barnaby Bandwidth in network
operations. Email addresses use the reserved `.example` TLD and phone numbers
use the `555` prefix, so nothing can route anywhere. No real person, company, or
article is represented.

About 74 files across every content folder:

| Folder | Contents |
|---|---|
| `People/` | 19 records — a full IT leadership team, two vendor contacts, four article authors |
| `Groups/` | 2 static rosters with photos, 2 dynamic Dataview rosters |
| `Categories/` | The 5 index notes every template's `categories:` link points at |
| `Knowledge/` | Tag taxonomy, data classification, vendor playbook, incident runbook |
| `Topics/` | 4 tag aggregators |
| `Clippings/` | 5 tagged articles with resolving bylines |
| `Creations/` | Voice notes, a Software-type tracking record, a presentation outline |
| `Meetings/` | ~22 meetings over four weeks, plus 3 recurring-series roots |
| `Daily/` | 3 journal entries |
| `Actions/` | The aggregated to-do view |

## Why it is generated rather than committed

The dataset is anchored to the day you run it. The Morning Dashboard shows
`Meetings/<today>*.md` and files whose birth time is today, so a meeting note
committed with a fixed date would be correct for exactly one day and read as an
empty dashboard forever after.

Generating everything from one place also means there is a single mechanism to
explain. An earlier version of this shipped the timeless half as committed files
and generated only the dated half; having two sources for one dataset turned out
to be more confusing than the problem it solved.

Re-running is safe and idempotent — same anchor date, same output, no
accumulation. Run it again before recording anything.

Other flags:

```bash
python3 Templates/Scripts/seed_demo_content.py --dry-run
```

```bash
python3 Templates/Scripts/seed_demo_content.py --anchor 2026-09-07
```

```bash
python3 Templates/Scripts/seed_demo_content.py --list
```

`--anchor` is useful for rehearsing against a specific weekday; the generator
lays recurring meetings onto their real weekdays relative to whatever anchor you
give it.

## Seeing the Morning Dashboard against it

The dashboard defaults to `~/Obsidian`, which is your real vault. Point it here
instead:

```bash
OBSIDIAN_VAULT=$PWD python3 Templates/Scripts/morning_dashboard.py --no-open
```

It writes to `Z_dashboards/` inside this repo (gitignored), and the
`obsidian://` links it generates follow the override, so clicking one opens the
demo vault rather than your real one. Drop `--no-open` to have it launch in
Chrome.

## What the dataset exercises

**Today always has three meetings**, one of each type, because each type renders
a different label on the dashboard: a Group meeting shows the group name, an
Individual meeting shows `Last, First 1:1`, and an Ad-hoc meeting shows its
title. This holds no matter which day you seed on, weekends included.

**Meetings show both pipeline states.** Past meetings carry topical tags in
their frontmatter, as though the semantic auto-tagger had already run over them.
Today's carry `tags: []`, which is exactly what the meeting pre-population
pipeline writes before the tagger's next pass. Past meetings also have their
notes filled in; today's have an agenda and prep tasks but empty notes, which is
what your vault actually looks like at 7am.

**Action items age.** Tasks in meetings older than ten days are closed with a
`✅` done-date; meetings three to ten days old keep their last item open; recent
meetings stay fully open. Without this, every task the seeder ever wrote would
sit open forever and the To-Do list would show sixty-plus items, which is noise
rather than a demo.

**Tasks use the `#task` tag.** This vault sets the Tasks plugin's `globalFilter`
to `#task`, so a checkbox without that tag is not a task. The Morning Dashboard
applies the same rule. Every task in the demo data is tagged accordingly.

**Both group styles are present.** `IT Leadership Team` and `Security Team` are
static rosters, so the Meeting template can read their membership and pre-fill
attendees. `Infrastructure Team` and `Data & Analytics Team` are Dataview
rosters driven by the `#Infrastructure` and `#Analytics` tags on People notes —
which the Meeting template cannot pre-fill from. Having both makes the tradeoff
visible.

**The assistant-exclusion path has something to act on.** Penny Planner is the
stand-in for an assistant address in `.config/meeting_prepopulate.json`. She
appears on the series roots but in no individual meeting's `people:` list,
which is what the pre-population pipeline does with a configured assistant.

**Clipping bylines resolve.** The four fictional authors have People notes, so
the `Author` view in `Clippings.base` filters correctly. In a real vault an
author link is usually unresolved until you create the note; these exist so the
view has something to show.

**`Categories/` is populated.** Every note template stamps a `categories:` link
— `[[Categories/People]]`, `[[Meetings]]`, `[[Creations]]` — and without those
index notes the links resolve to nothing.

## It stays clean against the vault's own checks

The dataset is kept passing three things, and the test suite asserts all of them
so it cannot quietly rot:

- **`installers/lib/check_classification.py`** — every file is
  `classification: public`, which is what lets demo data exist in a public repo
  at all.
- **`Templates/Scripts/vault_lint.py`** — no broken wikilinks, no unreferenced
  notes, no missing frontmatter keys, and every topical tag drawn from
  `Knowledge/Tag Taxonomy.md`.
- **`Templates/Scripts/tests/test_seed_demo_content.py`** — 38 tests, weighted
  toward the removal guard.

## Removing it

```bash
python3 Templates/Scripts/seed_demo_content.py --remove
```

Safe to run in a vault containing your own notes. A file is deleted only if it
carries a `demo_seed:` marker in its frontmatter **and** sits inside a known
content folder. A note of your own in `Meetings/` has no marker and is never a
candidate; a marked file outside the content folders is out of scope. Add
`--dry-run` to see the list first.

It also recognizes the `demo_seed: static` marker used by the earlier
two-layer version of this script, so upgrading and running `--remove` still
gives a clean sweep rather than stranding those files.

The content folders themselves survive — each carries a `.gitkeep` — so removing
the demo data leaves you the vault structure and none of the contents.

## Changing it

Everything lives in `Templates/Scripts/seed_demo_content.py`. The cast is a list
of constants near the top, long-form content is in module-level strings, and
`build_plan()` assembles the file list. Add a note by appending a
`(path, content)` pair there; include `classification: public` and
`demo_seed: generated` in its frontmatter so the audit passes and `--remove`
can find it again.

Keep it obviously fictional — no real company names, real people, or real URLs.
A demo dataset that reads as plausibly real is one someone eventually mistakes
for real.
