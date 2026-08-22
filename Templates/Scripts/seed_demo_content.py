#!/usr/bin/env python3
"""
seed_demo_content.py — write a synthetic demo dataset into the vault.

Why this exists
---------------
The repo ships content-free: clone it and every content folder is empty, which
is correct for a public template but makes it hard to see how the pieces fit
together. Empty Bases, empty Topics pages, a Morning Dashboard with nothing on
it. This script fills the vault with an obviously-fictional dataset so the
whole workflow is visible at once, and removes it again cleanly when you are
ready to use the vault for real.

Everything is generated, nothing is committed. That is deliberate: the dataset
is anchored to the day you run it. The Morning Dashboard shows
`Meetings/<today>*.md` and files whose birth time is today, so a meeting note
committed with a fixed date would be correct for exactly one day and read as an
empty dashboard forever after. Generating the whole set from one place also
means there is a single mechanism to explain, rather than some content shipped
as files and some produced by a script.

The cast is invented: Nimbus Widgets Inc., its vendor Cumulus Cloud Co., and
people named after what they do — Wanda Widget the CIO, Rosie Rackmount the
CISO. Email addresses use the reserved `.example` TLD and phone numbers use the
`555` prefix, so nothing can route anywhere.

Usage
-----
    python3 seed_demo_content.py                  # seed, anchored to today
    python3 seed_demo_content.py --anchor 2026-09-01
    python3 seed_demo_content.py --dry-run
    python3 seed_demo_content.py --list           # what is currently seeded
    python3 seed_demo_content.py --remove         # take it all back out

Safety
------
Every file written carries `demo_seed: generated` in its frontmatter. `--remove`
deletes a file only if it carries that marker AND sits inside a known content
folder, so pointing this at a vault holding real notes cannot destroy them. (It
also recognizes the `demo_seed: static` marker used by an earlier version of
this script, so upgrading and running `--remove` still gives a clean sweep.)

All content is `classification: public` so it passes the repo's classification
audit (`installers/lib/check_classification.py`), and the dataset is kept clean
against `vault_lint.py` — no broken links, no off-taxonomy tags, no missing
frontmatter keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

# Script lives at <vault>/Templates/Scripts/. Override for testing.
VAULT = Path(
    os.environ.get("OBSIDIAN_VAULT")
    or Path(__file__).resolve().parent.parent.parent
)

MEETINGS_DIR = VAULT / "Meetings"
SERIES_DIR = MEETINGS_DIR / "Series"
DAILY_DIR = VAULT / "Daily"
CLIPPINGS_DIR = VAULT / "Clippings"
CREATIONS_DIR = VAULT / "Creations"
PEOPLE_DIR = VAULT / "People"
GROUPS_DIR = VAULT / "Groups"
CATEGORIES_DIR = VAULT / "Categories"
KNOWLEDGE_DIR = VAULT / "Knowledge"
TOPICS_DIR = VAULT / "Topics"
ACTIONS_DIR = VAULT / "Actions"

# Folders the classification auditor treats as user content. Removal is
# confined to these, as a second guard alongside the frontmatter marker.
CONTENT_FOLDERS = frozenset({
    "Actions", "Categories", "Clippings", "Creations", "Daily",
    "Excalidraw", "Groups", "Knowledge", "Meetings", "Notes",
    "People", "Topics",
})

# `static` is recognized but never written -- an earlier version of this script
# shipped part of the dataset as committed files carrying that marker, and
# --remove should still clean those up after an upgrade.
MARKER_RE = re.compile(r"(?m)^demo_seed\s*:\s*(generated|static)\s*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

MON, TUE, WED, THU, FRI = range(5)

NIMBUS = "Nimbus Widgets Inc."
CUMULUS = "Cumulus Cloud Co."


def back(anchor: dt.date, days: int) -> dt.date:
    return anchor - dt.timedelta(days=days)


def stamp(d: dt.date, hh: int = 9, mm: int = 0) -> str:
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}"


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------
# (stem, display, title, org, email, phone, tags, notes, bio)

PEOPLE = [
    ("Widget, Wanda", "Wanda Widget", "Chief Information Officer", NIMBUS,
     "wanda.widget@nimbuswidgets.example", "(555) 010-0101",
     ["Leadership", "Strategy"],
     "Runs the unified IT organization. This is the \"you\" persona in the demo "
     "vault — most meetings in `Meetings/` have Wanda as the organizer.",
     "Joined Nimbus Widgets as CIO after leading platform engineering at a "
     "mid-size manufacturer. Sponsors the AI governance council and the cloud "
     "migration program."),

    ("Megabyte, Milo", "Milo Megabyte", "VP & Deputy CIO", NIMBUS,
     "milo.megabyte@nimbuswidgets.example", "(555) 010-0102",
     ["Leadership", "Strategy"],
     "Closest strategic partner to the CIO. Owns cross-cutting initiatives and "
     "chairs the weekly leadership standup when Wanda travels.",
     "Fifteen years at Nimbus across applications and infrastructure. Runs the "
     "AI adoption bootcamp for divisional business officers."),

    ("Rackmount, Rosie", "Rosie Rackmount",
     "VP & Chief Information Security Officer", NIMBUS,
     "rosie.rackmount@nimbuswidgets.example", "(555) 010-0103",
     ["Leadership", "Cybersecurity"],
     "Owns the security program end to end, including incident response and the "
     "phishing-resistant MFA rollout.",
     "Came up through security operations. Chairs the incident review board and "
     "reports quarterly to the audit committee."),

    ("Gigabyte, Gus", "Gus Gigabyte", "VP, Infrastructure & Networking", NIMBUS,
     "gus.gigabyte@nimbuswidgets.example", "(555) 010-0104",
     ["Leadership", "Infrastructure"],
     "Accountable for data centers, compute, storage, and the network backbone. "
     "Co-sponsors the cloud migration program with Dot Matrix.",
     "Built out the current two-site data center footprint. Pushing hard on "
     "storage tiering to fund the migration."),

    ("Matrix, Dot", "Dot Matrix", "VP, Enterprise Applications", NIMBUS,
     "dot.matrix@nimbuswidgets.example", "(555) 010-0105",
     ["Leadership", "Operations"],
     "Owns the ERP, HR, and finance application portfolio. Co-sponsor of the "
     "cloud migration program.",
     "Led the last two ERP upgrades. Skeptical of lift-and-shift; wants "
     "application rationalization first."),

    ("Terabyte, Tilly", "Tilly Terabyte", "Director, Data & Analytics", NIMBUS,
     "tilly.terabyte@nimbuswidgets.example", "(555) 010-0106",
     ["Leadership", "Analytics"],
     "Runs the data platform and the analytics team. Primary voice on data "
     "governance in leadership meetings.",
     "Built the current warehouse from scratch. Currently evaluating whether the "
     "platform can support agentic AI workloads without a redesign."),

    ("Bandwidth, Barnaby", "Barnaby Bandwidth", "Director, Network Operations",
     NIMBUS, "barnaby.bandwidth@nimbuswidgets.example", "(555) 010-0107",
     ["Infrastructure", "Networking"],
     "Day-to-day owner of the network operations center and the wireless refresh.",
     "Twelve years in network operations. Runs the change advisory board."),

    ("Packet, Percy", "Percy Packet", "Network Engineer", NIMBUS,
     "percy.packet@nimbuswidgets.example", "(555) 010-0108",
     ["Infrastructure", "Networking"],
     "Hands-on engineer for the campus wireless refresh and segmentation work.",
     "Joined from a regional ISP. Holds the deepest working knowledge of the "
     "current segmentation design."),

    ("Pixel, Pip", "Pip Pixel", "Director, End-User Experience", NIMBUS,
     "pip.pixel@nimbuswidgets.example", "(555) 010-0109",
     ["Operations"],
     "Owns the service desk, endpoint management, and the unmanaged-device "
     "reduction effort.",
     "Reorganized the service desk around tiered escalation last year. Tracks "
     "satisfaction as the team's primary metric."),

    ("Cipher, Celia", "Celia Cipher", "Senior Manager, Security Operations",
     NIMBUS, "celia.cipher@nimbuswidgets.example", "(555) 010-0110",
     ["Cybersecurity"],
     "Runs the security operations center and the detection engineering backlog.",
     "Built the current detection pipeline. Pushing for more automation before "
     "headcount."),

    ("Firewall, Ferris", "Ferris Firewall", "Manager, Threat Intelligence",
     NIMBUS, "ferris.firewall@nimbuswidgets.example", "(555) 010-0111",
     ["Cybersecurity"],
     "Tracks the external threat landscape and briefs leadership monthly.",
     "Former analyst at a managed detection provider. Owns the threat brief that "
     "opens each security review."),

    ("Aggregate, Ada", "Ada Aggregate", "Senior Data Engineer", NIMBUS,
     "ada.aggregate@nimbuswidgets.example", "(555) 010-0112",
     ["Analytics"],
     "Builds and maintains the ingestion pipelines behind the data platform.",
     "Joined from the analytics consultancy that delivered the original "
     "warehouse. Now owns it internally."),

    ("Planner, Penny", "Penny Planner", "Executive Assistant to the CIO", NIMBUS,
     "penny.planner@nimbuswidgets.example", "(555) 010-0113",
     ["Operations"],
     "Manages the CIO's calendar and schedules the recurring series in "
     "`Meetings/Series/`. Penny is the demo vault's stand-in for an "
     "assistant address in `.config/meeting_prepopulate.json` — the meeting "
     "pre-population pipeline excludes those addresses from attendee counts "
     "and People linking, which is why Penny appears on the series roots but "
     "not in any individual meeting's `people:` list.",
     "Included so the assistant-exclusion behavior has something to act on."),

    ("Kilowatt, Kip", "Kip Kilowatt", "Enterprise Account Executive", CUMULUS,
     "kip.kilowatt@cumuluscloud.example", "(555) 020-0201",
     ["Vendors"],
     "Account executive for the Cumulus Cloud relationship. Attends the "
     "quarterly business review.",
     "External contact. Primary commercial point of contact for the cloud "
     "platform contract."),

    ("Vector, Vera", "Vera Vector", "Principal Solutions Architect", CUMULUS,
     "vera.vector@cumuluscloud.example", "(555) 020-0202",
     ["Vendors", "Cloud"],
     "Technical counterpart on the Cumulus Cloud account. Joins architecture "
     "reviews for the migration program.",
     "External contact. Has run three comparable migrations at similar scale."),
]

# Article authors. These exist so the `author:` wikilink on every clipping
# resolves, which is also what makes the Author view in Clippings.base work --
# that view filters on the author note being the active file, so with no note
# there is nothing to filter against.
AUTHORS = [
    ("Quotient, Quill", "Quill Quotient", "Contributing Editor",
     "The Synthetic Signal",
     "Writes on AI strategy and governance. Byline on two clippings in this "
     "vault.\n\nAuthor records like this one are what make the **Author** view "
     "in `Clippings.base` work: open this note and the view lists everything "
     "they wrote. Clippings saved by the Web Clipper create the `author:` "
     "wikilink automatically, but the person note itself does not exist until "
     "you make it — which is why an unresolved author link is normal and not "
     "an error."),
    ("Diligence, Dash", "Dash Diligence", "Security Correspondent",
     "The Synthetic Signal",
     "Covers identity, authentication, and enterprise security programs."),
    ("Lumen, Ledger", "Ledger Lumen", "Columnist", "Ledger & Line",
     "Writes on technology cost structures and cloud economics."),
    ("Marginalia, Marge", "Marge Marginalia", "Analyst", "The Synthetic Signal",
     "Covers data platforms and analytics adoption."),
]

WANDA = "Widget, Wanda"
MILO = "Megabyte, Milo"
ROSIE = "Rackmount, Rosie"
GUS = "Gigabyte, Gus"
DOT = "Matrix, Dot"
TILLY = "Terabyte, Tilly"
BARNABY = "Bandwidth, Barnaby"
PERCY = "Packet, Percy"
PIP = "Pixel, Pip"
CELIA = "Cipher, Celia"
FERRIS = "Firewall, Ferris"
ADA = "Aggregate, Ada"
PENNY = "Planner, Penny"
KIP = "Kilowatt, Kip"
VERA = "Vector, Vera"

LEADERSHIP = [WANDA, MILO, ROSIE, GUS, DOT, TILLY]
SECURITY = [ROSIE, CELIA, FERRIS]
INFRA = [GUS, BARNABY, PERCY]
ANALYTICS = [TILLY, ADA]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def slugify(subject: str) -> str:
    """Mirror meeting_prepopulate.slugify_subject so generated series roots
    land at the same paths the real pipeline would use."""
    s = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
    return s[:60] or "meeting"


def person_note(stem, display, title, org, email, phone, tags, notes, bio,
                created: dt.date) -> str:
    tag_block = "\n".join(f"  - {t}" for t in tags)
    return f"""---
categories:
  - "[[Categories/People]]"
Title: {title}
Organization: {org}
Email: {email}
Mobile Phone: {phone}
preferred_name: {display.split()[0]}
aliases:
  - "{display}"
tags:
{tag_block}
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

## Photo

![[placeholder-person.png|150]]

## Notes

{notes}

## Bio

{bio}

## Meetings

![[Meetings.base#Person]]
"""


def author_note(stem, display, title, org, notes, created: dt.date) -> str:
    return f"""---
categories:
  - "[[Categories/People]]"
Title: {title}
Organization: {org}
Email:
Mobile Phone:
preferred_name: {display.split()[0]}
aliases:
  - "{display}"
tags: []
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

## Photo

![[placeholder-person.png|150]]

## Notes

{notes}

## Bio

External author. Not a colleague — included so clipping bylines resolve to a
real note. Every publication and person named here is invented.

## Clippings

![[Clippings.base#Author]]

## Meetings

![[Meetings.base#Person]]
"""


def static_group(members: list[str], blurb: str, created: dt.date) -> str:
    roster = "\n".join(f"![[placeholder-person.png|40]] [[{m}]]" for m in members)
    return f"""---
tags: []
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

{blurb}

{roster}

## Meetings

![[Meetings.base#Group]]
"""


def dataview_group(tag: str, blurb: str, created: dt.date) -> str:
    return f"""---
tags: []
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

{blurb}

```dataview
LIST
FROM "People"
WHERE contains(file.tags, "#{tag}")
SORT file.name ASC
```

## Meetings

![[Meetings.base#Group]]
"""


def category_note(title: str, base: str, blurb: str, created: dt.date) -> str:
    return f"""---
title: {title}
tags: []
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

{blurb}

![[{base}]]
"""


def topic_note(title: str, tag: str, blurb: str, created: dt.date) -> str:
    return f"""---
title: {title}
description: "Topic aggregator: notes tagged with #{tag}"
tags: []
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

{blurb}

```dataview
LIST
FROM ""
WHERE contains(file.tags, "#{tag}")
SORT file.name ASC
```
"""


def knowledge_note(title: str, tags: list[str], created: dt.date,
                   body: str) -> str:
    tag_block = "\n".join(f"  - {t}" for t in tags)
    return f"""---
title: {title}
tags:
{tag_block}
classification: public
demo_seed: generated
created: {stamp(created)}
updated: {stamp(created)}
---

{body}"""


def clipping_note(title, source, author, published: dt.date, created: dt.date,
                  tags, description, body) -> str:
    tag_block = "\n".join(f"  - {t}" for t in tags)
    return f"""---
categories:
  - "[[Clippings]]"
title: "{title}"
source: {source}
author:
  - "[[{author}]]"
published: {published.isoformat()}
created: {created.isoformat()}
description: {description}
tags:
{tag_block}
classification: public
demo_seed: generated
updated: {stamp(created, 8, 0)}
---

{body}"""


def render_meeting(mtype: str, people: list[str], *, group: str | None = None,
                   title: str | None = None, series: str | None = None,
                   created: dt.datetime, body: str = "",
                   tags: list[str] | None = None) -> str:
    """Compose a meeting note in the same canonical frontmatter shape
    `meeting_prepopulate.render_meeting_file` produces, plus the
    `classification` and `demo_seed` fields this vault requires.

    `tags` stands in for the semantic auto-tagger's output. The pre-population
    pipeline always writes `tags: []` and the tagger fills them in on its next
    pass, so past meetings here carry tags and today's do not — that is the
    same before/after the real pipeline produces, and it is what makes the
    Topics/ aggregators surface meetings rather than only clippings.
    """
    lines = [
        "---",
        "categories:",
        '  - "[[Meetings]]"',
        f"type: {mtype}",
    ]
    if title:
        lines.append(f'title: "{title}"')
    if group:
        lines += ["group:", f'  - "[[{group}]]"']
    lines.append("people:")
    lines += [f'  - "[[{p}]]"' for p in people]
    if series:
        lines.append(f"series_link: Meetings/Series/{series}")
    if tags:
        lines.append("tags:")
        lines += [f"  - {t}" for t in tags]
    else:
        lines.append("tags: []")
    lines.append("classification: public")
    lines.append("demo_seed: generated")
    st = created.strftime("%Y-%m-%dT%H:%M")
    lines.append(f"created: {st}")
    lines.append(f"updated: {st}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body


def render_series_root(subject: str, recurrence: str, people: list[str],
                       now: dt.datetime, agenda: str) -> str:
    people_yaml = "\n".join(f'  - "[[{p}]]"' for p in people)
    return f"""---
categories:
  - "[[Meetings]]"
type: meeting-series
subject: "{subject}"
series_uid: demo-{slugify(subject)}
recurrence_human: "{recurrence}"
people:
{people_yaml}
tags: []
classification: public
demo_seed: generated
created_by: seed_demo_content
created: {now.strftime('%Y-%m-%dT%H:%M')}
updated: {now.strftime('%Y-%m-%dT%H:%M')}
---

# {subject}

Recurrence: **{recurrence}**

Series roots are created once per recurring meeting and hold the things that
outlive any single instance. Each instance under `Meetings/YYYY-MM-DD HHMM.md`
links back here via `series_link:` in its frontmatter.

Scheduling and logistics: [[Planner, Penny]].

## Standing Agenda

{agenda}

## Ongoing Threads

- Cloud migration program — tracked in [[Cumulus Cloud Platform]]
- Tag hygiene against [[Tag Taxonomy]]

## Per-instance Notes

Use the instance notes for what happened; use this page for what persists.
"""


# ---------------------------------------------------------------------------
# Knowledge bodies
# ---------------------------------------------------------------------------

def tag_taxonomy_body(reviewed: dt.date) -> str:
    return f"""# Tag Taxonomy — Canonical Allowlist

**Last reviewed:** {reviewed.isoformat()} · **Total canonical tags:** 32

This is the authoritative tag list for the vault. `Templates/Scripts/tag_clippings.py`
reads this file as a **hard allowlist** — it will only ever apply tags that appear
below, and never invents new ones. Delete this file and the tagger falls back to
collecting whatever tags already exist in the vault, which drifts over time.
`Templates/Scripts/vault_lint.py` reports anything in use that is missing here.

## Conventions

1. **Casing:** PascalCase everywhere. Acronyms stay uppercase (`AI`, `MFA`, `IT`).
2. **Hierarchy depth:** max 2 levels (`Parent/Child`). No three-level chains.
3. **Plural:** singular for concepts (`AI`, `Strategy`), plural for collections (`Vendors`).
4. **Tag vs link:** products, places, people, and projects become wiki links,
   not tags. Tags are *axes*, not *entities*.
5. **Per-file count:** target 1–6 tags.

## How to add a new tag

1. The tagger logs tags it wanted but could not find here to
   `Templates/Scripts/tag-promotion-candidates.md` instead of writing them.
2. Review that file weekly, or once a candidate has three or more proposed uses.
3. Add the tag to this file under the right section.
4. The next tagger run starts applying it.

---

## Top-level (flat) tags

- AI
- Analytics
- Budget
- Cloud
- Compliance
- Cybersecurity
- DataGovernance
- Documentation
- Governance
- Hiring
- Infrastructure
- Leadership
- Networking
- Operations
- Procurement
- Roadmap
- Strategy
- Vendors
- Workforce

## Hierarchical tags

- AI/Adoption
- AI/Agents
- AI/Governance
- Analytics/DataPlatform
- Cloud/Cost
- Cloud/Migration
- Cybersecurity/Identity
- Cybersecurity/IncidentResponse
- Cybersecurity/Phishing
- Infrastructure/Compute
- Infrastructure/Storage
- Networking/Wireless
- Vendors/CumulusCloud
"""


DATA_CLASSIFICATION_BODY = """# Data Classification

Every note in an audited folder carries a `classification:` field in its
frontmatter. The public template repo enforces this: `installers/lib/check_classification.py`
runs as a pre-commit hook **and** as installer component `02-classification-audit`,
and it hard-fails on any `.md` file in `Actions/`, `Categories/`, `Clippings/`,
`Creations/`, `Daily/`, `Excalidraw/`, `Groups/`, `Knowledge/`, `Meetings/`,
`Notes/`, `People/`, or `Topics/` that is not marked `public`.

## Levels

| Value | Meaning | Safe to commit to a public repo? |
|---|---|---|
| `public` | No sensitive content. Structural templates, demo data, published material. | Yes |
| `internal-use-only` | Ordinary working notes: meetings, people, projects. | No |
| `confidential` | Personnel matters, contracts under negotiation, security findings. | No |
| `restricted` | Regulated data, credentials, anything with a legal handling obligation. | No |

## How this plays out in practice

Your real vault will be almost entirely `internal-use-only` and above. That is
the point of the split: the template repo is a **structure and automation**
share, not a content share. The templates default new notes to
`internal-use-only` precisely so that a slip of the wrist cannot publish
something — you have to deliberately downgrade a note to `public` before it can
be committed.

Everything in this demo vault is marked `public` because none of it is real.

## Tasks

- [ ] #task Review the classification of any note older than a year 🔺
- [ ] #task Confirm the pre-commit hook is installed on every clone you push from
"""

VENDOR_PLAYBOOK_BODY = """# Vendor Management Playbook

How the demo organization runs its major vendor relationships. Referenced from
the quarterly business review meetings with [[Kilowatt, Kip]] and [[Vector, Vera]]
at Cumulus Cloud Co.

## Cadence

| Tier | Example | Executive cadence | Owner |
|---|---|---|---|
| Strategic | Cumulus Cloud Co. | Quarterly business review | [[Widget, Wanda]] |
| Significant | Endpoint management suite | Semi-annual | [[Pixel, Pip]] |
| Transactional | Commodity hardware | Annual renewal only | [[Gigabyte, Gus]] |

## Standing QBR agenda

1. Service performance against the SLA — incidents, credits, trend
2. Spend to date vs. commitment, and the forecast to contract end
3. Roadmap items that affect our architecture decisions
4. Open escalations, with owners and dates
5. Renewal posture — leverage, alternatives, timing

## Rules of engagement

- Vendors do not present to the leadership team without a Nimbus owner in the room.
- No architecture commitment is made in a QBR. Decisions route through the
  architecture review, then to the sponsor.
- Every escalation gets a named owner and a date before the meeting ends.

## Tasks

- [ ] #task Draft the Cumulus Cloud renewal position ahead of the next QBR 🔺
- [ ] #task Refresh the tiering table — two vendors moved tier this year
"""

IR_RUNBOOK_BODY = """# Incident Response Runbook

Abbreviated runbook for the demo organization. Owned by [[Rackmount, Rosie]];
day-to-day execution sits with [[Cipher, Celia]] in the security operations center.

## Severity levels

| Level | Definition | Notification |
|---|---|---|
| SEV-1 | Confirmed compromise affecting production or regulated data | CIO and CISO immediately, executive team within one hour |
| SEV-2 | Confirmed compromise, contained, no regulated data | CISO within one hour |
| SEV-3 | Suspicious activity under investigation | Daily standup |

## First thirty minutes

1. Declare severity. When in doubt, declare higher — downgrading is cheap.
2. Open the incident channel and start the timeline. Every action gets a timestamp.
3. Assign an incident commander who is **not** doing hands-on technical work.
4. Preserve evidence before remediating. Snapshot, then act.
5. Notify per the table above. Do not wait for certainty to notify.

## Common pitfalls

- Remediating before scoping, which destroys the evidence needed to find the
  rest of the footprint.
- Letting the incident commander get pulled into the technical work, after which
  nobody is tracking the whole picture.
- Treating the all-clear as the end. The retrospective is where the value is.

## Tasks

- [ ] #task Schedule the next tabletop exercise with [[Cipher, Celia]] 🔺
- [ ] #task Update the notification tree — two names are stale
- [ ] #task Add a SEV-1 decision log template to this runbook
"""


# ---------------------------------------------------------------------------
# Meeting bodies
# ---------------------------------------------------------------------------

LEADERSHIP_BODIES = [
    """## Agenda

- Cloud migration — status against plan
- Security program update
- Budget: year-to-date against the reduction target

## Notes

Migration is tracking to plan on the infrastructure side. [[Matrix, Dot]] raised
that two application teams have not yet committed to their cutover windows, which
puts the Q4 wave at risk. Agreed to escalate through the sponsors rather than the
project team.

[[Rackmount, Rosie]] reported MFA coverage at 78% of privileged accounts. The
remaining tail is service accounts, which need a different approach.

Budget is running under plan, which gives room to absorb the storage refresh
without a supplemental request.

## Action Items

- [ ] #task Escalate the two uncommitted cutover windows to application sponsors 🔺
- [ ] #task Bring a service-account MFA proposal to the next leadership meeting
- [ ] #task Model the storage refresh against the remaining budget headroom
""",
    """## Agenda

- Agentic AI pilots — what is live and what governance sits under it
- Wireless refresh readiness
- Hiring plan

## Notes

Three agentic pilots are running. [[Megabyte, Milo]] walked through the tool
grants for each; two are read-only, one can file tickets. Agreed that anything
with write access needs a named business owner before it leaves pilot.

Wireless refresh is ready to start in the north campus buildings. [[Gigabyte, Gus]]
flagged a four-week lead time on access points that we should order against now.

Two open requisitions in security operations have been unfilled for a quarter.
Discussed whether to convert one to a contract role.

## Action Items

- [ ] #task Assign business owners to the write-capable agent pilot 🔺
- [ ] #task Place the access point order ahead of the wireless refresh
- [ ] #task Decide on converting the second SecOps requisition to contract
""",
    """## Agenda

- Quarterly board update — content review
- Vendor renewals landing this quarter
- Data platform roadmap

## Notes

Reviewed the draft board update. Consensus that the AI section should lead with
governance rather than pilot counts — the board's question is whether this is
controlled, not whether it is happening.

[[Kilowatt, Kip]] has asked to open Cumulus renewal discussions early. We are
under our committed spend, which is leverage; agreed not to engage until the
usage forecast is finalized.

[[Terabyte, Tilly]] presented the data platform roadmap. The semantic layer work
is the dependency everything else waits on.

## Action Items

- [ ] #task Restructure the board update's AI section to lead with governance 🔺
- [ ] #task Finalize the Cumulus usage forecast before renewal talks open
- [ ] #task Sequence the semantic layer work ahead of the downstream roadmap
""",
    """## Agenda

- Incident retrospective
- Unmanaged device count
- Q4 planning kickoff

## Notes

Walked the retrospective from last month's SEV-2. The detection worked; the
notification tree did not — two names on it had left the organization. Fix is
already in flight, see [[Incident Response Runbook]].

Unmanaged devices are down 40% year over year. [[Pixel, Pip]] credited tying
enrollment to the hardware refresh rather than running it as a standalone campaign.

Q4 planning starts in two weeks. Asked each leader for a one-page priority set,
not a project list.

## Action Items

- [ ] #task Send the Q4 one-page priority template to the leadership team 🔺
- [ ] #task Confirm the notification tree fix has landed
""",
]

SECURITY_BODIES = [
    """## Agenda

- Threat brief
- Detection engineering backlog
- MFA rollout status

## Notes

[[Firewall, Ferris]] opened with the threat brief. Credential phishing against
finance-adjacent roles is up sharply; the lures are noticeably better written
than a year ago, which tracks with what everyone else is reporting.

Detection backlog is at 14 items. [[Cipher, Celia]] wants to automate triage on
the top three noise sources before adding headcount, which is the right order.

MFA rollout is at 78% of privileged accounts.

## Action Items

- [ ] #task Automate triage for the top three detection noise sources 🔺
- [ ] #task Brief finance leadership on the credential phishing trend
""",
    """## Agenda

- Tabletop exercise planning
- Vulnerability management metrics
- Third-party risk review

## Notes

Tabletop is scheduled. Scenario is a compromised vendor credential with lateral
movement into the application tier — chosen because it exercises the seam
between the security team and application operations, which is where the last
real incident got slow.

Vulnerability metrics look better on mean time to remediate but the critical
count is flat. Worth understanding whether that is intake or throughput.

## Action Items

- [ ] #task Circulate the tabletop scenario to participants a week ahead 🔺
- [ ] #task Break down the flat critical count into intake vs. throughput
""",
    """## Agenda

- Post-incident review
- Identity roadmap
- Log retention

## Notes

Post-incident review on the SEV-2. Root cause was a service account with a
static credential and no rotation. Not a surprise, and not the only one — the
inventory shows a long tail of these.

Identity roadmap now sequences service account remediation ahead of the
passwordless work for end users. That is a reordering, and it will read as a
delay to people expecting passwordless this year.

Log retention is at the contractual minimum. Extending it has a real cost;
bringing options to the leadership meeting.

## Action Items

- [ ] #task Build the service account inventory with rotation status 🔺
- [ ] #task Communicate the identity roadmap reordering before it surprises anyone
- [ ] #task Cost out three log retention options for the leadership meeting
""",
    """## Agenda

- Threat brief
- Phishing simulation results
- Security awareness refresh

## Notes

Simulation click rate is down to 4%, but report rate is only 31% — people are
not falling for it and also not telling us, which limits our visibility.

Awareness refresh should emphasize reporting over avoidance. The current
material is all "don't click," which we have apparently landed.

## Action Items

- [ ] #task Rework the awareness refresh to emphasize reporting 🔺
- [ ] #task Add report rate to the monthly security metrics
""",
]

ONE_ON_ONE_BODIES = [
    """## Notes

Caught up on the migration cutover risk. Agreed the escalation should come from
the sponsor, not the project manager — it lands differently and it is the
sponsor's call anyway.

Talked through the agentic pilot tool grants. Comfortable with the two read-only
ones. The write-capable one needs an owner before it leaves pilot.

## Action Items

- [ ] #task Draft the sponsor escalation note on cutover windows 🔺
- [ ] #task Confirm the write-capable pilot has a named business owner
""",
    """## Notes

Career conversation. Interested in taking on the AI governance council as chair
once the charter is settled — good fit, and it gets the council out from under
the CIO's calendar.

Discussed the Q4 priority template. Wants it to force ranking rather than allow
a flat list, which is right; the whole point is the trade-offs.

## Action Items

- [ ] #task Revise the Q4 priority template to force ranking 🔺
- [ ] #task Settle the governance council charter so the chair handoff can happen
""",
    """## Notes

Reviewed the security metrics ahead of the board update. The report-rate number
is the one that needs framing — down click rate looks good, flat report rate
does not, and presenting one without the other would be misleading.

Discussed the unfilled SecOps requisitions. Leaning toward contract for one.

## Action Items

- [ ] #task Frame both phishing metrics together in the board update 🔺
- [ ] #task Come back with a contract-vs-hire recommendation for SecOps
""",
]

LEADERSHIP_TAGS = [
    ["Leadership", "Cloud/Migration", "Cybersecurity/Identity", "Budget"],
    ["AI", "AI/Governance", "Networking/Wireless", "Hiring"],
    ["Strategy", "Roadmap", "Vendors", "Analytics/DataPlatform"],
    ["Cybersecurity", "Cybersecurity/IncidentResponse", "Operations", "Workforce"],
]
SECURITY_TAGS = [
    ["Cybersecurity", "Cybersecurity/Phishing", "Cybersecurity/Identity"],
    ["Cybersecurity", "Cybersecurity/IncidentResponse", "Compliance"],
    ["Cybersecurity", "Cybersecurity/Identity", "Roadmap"],
    ["Cybersecurity", "Cybersecurity/Phishing", "Workforce"],
]
ONE_ON_ONE_TAGS = [
    ["Leadership", "Cloud/Migration", "AI/Agents"],
    ["Leadership", "AI/Governance", "Workforce"],
    ["Leadership", "Cybersecurity", "Hiring"],
]

OPEN_TASK_RE = re.compile(r"^(\s*-\s*)\[ \](\s*#task\s.*?)\s*$", re.MULTILINE)


def age_tasks(body: str, meeting_date: dt.date, anchor: dt.date) -> str:
    """Close out action items from older meetings.

    Without this, every task the seed ever wrote stays open and the To-Do list
    and Morning Dashboard show 60+ items — which is noise, not a demo. Real
    action items mostly get done, so age them by how long ago the meeting was:

        older than 10 days   all items closed
        3 to 10 days         all but the last item closed
        2 days or fewer      everything still open

    Closed items get the Tasks plugin's `✅ YYYY-MM-DD` done-date marker, three
    days after the meeting (never past the anchor), so the plugin's
    `sort by done` query in Actions/To-Do.md has something to order by.
    """
    age = (anchor - meeting_date).days
    if age <= 2:
        return body

    matches = list(OPEN_TASK_RE.finditer(body))
    if not matches:
        return body
    keep_open = set() if age > 10 else {len(matches) - 1}

    done_on = min(meeting_date + dt.timedelta(days=3), anchor)
    out, last = [], 0
    for i, m in enumerate(matches):
        if i in keep_open:
            continue
        # Drop any priority marker; a finished task carries no priority.
        text = m.group(2).replace(" 🔺", "").rstrip()
        out.append(body[last:m.start()])
        out.append(f"{m.group(1)}[x]{text} ✅ {done_on.isoformat()}")
        last = m.end()
    out.append(body[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Schedule construction
# ---------------------------------------------------------------------------

def weekdays_back(anchor: dt.date, count: int) -> list[dt.date]:
    """Return `count` weekdays ending at (and including) `anchor`, most recent
    first. If the anchor itself falls on a weekend it is still included, so a
    Saturday demo run still produces meetings for 'today'."""
    out = [anchor]
    d = anchor
    while len(out) < count:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def build_meetings(anchor: dt.date) -> list[tuple[dt.datetime, str]]:
    """Return (start_datetime, file_content) pairs for every meeting note.

    Recurring instances are laid onto their weekday across a four-week window;
    today's three meetings are added explicitly so the Morning Dashboard is
    never empty regardless of which day the demo is run.
    """
    out: list[tuple[dt.datetime, str]] = []
    window = weekdays_back(anchor, 20)
    past = [d for d in window if d != anchor]

    lead_i = sec_i = one_i = 0

    for d in sorted(past):
        if d.weekday() == MON:
            start = dt.datetime.combine(d, dt.time(9, 0))
            body = LEADERSHIP_BODIES[lead_i % len(LEADERSHIP_BODIES)]
            tags = LEADERSHIP_TAGS[lead_i % len(LEADERSHIP_TAGS)]
            lead_i += 1
            out.append((start, render_meeting(
                "Group", LEADERSHIP, group="IT Leadership Team",
                series="it-leadership-weekly", created=start, body=body,
                tags=tags)))

        if d.weekday() == WED:
            start = dt.datetime.combine(d, dt.time(10, 0))
            body = SECURITY_BODIES[sec_i % len(SECURITY_BODIES)]
            tags = SECURITY_TAGS[sec_i % len(SECURITY_TAGS)]
            sec_i += 1
            out.append((start, render_meeting(
                "Group", SECURITY, group="Security Team",
                series="security-operations-review", created=start, body=body,
                tags=tags)))

        if d.weekday() == THU:
            start = dt.datetime.combine(d, dt.time(14, 0))
            body = ONE_ON_ONE_BODIES[one_i % len(ONE_ON_ONE_BODIES)]
            tags = ONE_ON_ONE_TAGS[one_i % len(ONE_ON_ONE_TAGS)]
            one_i += 1
            out.append((start, render_meeting(
                "Individual", [MILO], series="cio-deputy-cio-1-1",
                created=start, body=body, tags=tags)))

    def bd(n: int) -> dt.date:
        return window[n]

    one_offs = [
        (bd(2), dt.time(13, 30), render_meeting(
            "Individual", [TILLY],
            created=dt.datetime.combine(bd(2), dt.time(13, 30)),
            tags=["Analytics", "Analytics/DataPlatform", "AI/Agents", "Roadmap"],
            body="""## Notes

Data platform roadmap review. The semantic layer is the long pole and everything
downstream is waiting on it. Agreed to sequence it first even though it is the
least visible item on the list.

Raised that the platform may not support agentic workloads without a redesign of
the access layer. Worth scoping before we commit to anything in the AI roadmap.

## Action Items

- [ ] #task Scope the access layer changes needed for agentic workloads 🔺
- [ ] #task Move the semantic layer to the front of the platform roadmap
""")),

        (bd(3), dt.time(11, 0), render_meeting(
            "Ad-hoc", [WANDA, GUS, DOT, VERA],
            title="Cloud Migration Steering Committee",
            created=dt.datetime.combine(bd(3), dt.time(11, 0)),
            tags=["Cloud", "Cloud/Migration", "Cloud/Cost", "Vendors/CumulusCloud"],
            body="""## Agenda

- Wave 3 readiness
- Application rationalization decisions
- Egress cost forecast

## Notes

Wave 3 is ready on infrastructure. Two application teams have not committed
cutover windows.

[[Vector, Vera]] walked through the egress forecast. Current architecture has
more cross-region traffic than the design assumed, which is where the variance
is coming from. Fixable, but it is a design change, not a configuration change.

Rationalization: eleven applications were flagged as retirement candidates. Four
have owners willing to commit. The rest need a push from the sponsors.

## Action Items

- [ ] #task Redesign the cross-region traffic path with [[Vector, Vera]] 🔺
- [ ] #task Get sponsor decisions on the seven unclaimed retirement candidates
- [ ] #task Lock cutover windows for the two outstanding application teams
""")),

        (bd(4), dt.time(9, 30), render_meeting(
            "Individual", [ROSIE],
            created=dt.datetime.combine(bd(4), dt.time(9, 30)),
            tags=["Cybersecurity", "Cybersecurity/IncidentResponse",
                  "Cybersecurity/Identity"],
            body="""## Notes

Walked the incident retrospective. The notification tree failure is the item
that matters — the detection worked exactly as designed.

Discussed the identity roadmap reordering. Service accounts ahead of end-user
passwordless is the right call on risk, and it will read as a delay to people
who were expecting passwordless this year. Better to say that plainly now.

## Action Items

- [ ] #task Communicate the identity roadmap reordering ahead of the roadmap review 🔺
- [ ] #task Confirm the notification tree has been rebuilt and tested
""")),

        (bd(6), dt.time(15, 0), render_meeting(
            "Ad-hoc", [WANDA, GUS, KIP, VERA],
            title="Cumulus Cloud Quarterly Business Review",
            created=dt.datetime.combine(bd(6), dt.time(15, 0)),
            tags=["Vendors", "Vendors/CumulusCloud", "Procurement", "Cloud/Cost"],
            body="""## Agenda

Standing QBR agenda per [[Vendor Management Playbook]].

## Notes

Service performance met SLA across the quarter, one credit issued for a
control-plane outage in the secondary region.

Spend is 8% under commitment. [[Kilowatt, Kip]] raised early renewal; we did not
engage, per the playbook — the usage forecast is not final and being under
commitment is leverage worth keeping.

[[Vector, Vera]] previewed a platform change that affects our egress design.
Useful, and exactly the kind of roadmap item the QBR is for.

Two open escalations, both with owners and dates.

## Action Items

- [ ] #task Finalize the usage forecast before reopening renewal discussions 🔺
- [ ] #task Assess the platform egress change against our current design
""")),

        (bd(8), dt.time(13, 0), render_meeting(
            "Group", INFRA, group="Infrastructure Team",
            created=dt.datetime.combine(bd(8), dt.time(13, 0)),
            tags=["Infrastructure", "Networking/Wireless",
                  "Infrastructure/Storage", "Operations"],
            body="""## Agenda

- Wireless refresh sequencing
- Storage tiering
- Change advisory backlog

## Notes

Wireless refresh starts in the north campus buildings. [[Packet, Percy]] has the
segmentation design ready; the dependency is access point lead time.

Storage tiering is ahead of schedule and is freeing more capacity than modeled,
which helps fund the migration.

Change advisory backlog is growing. [[Bandwidth, Barnaby]] proposed a standing
pre-approved category for low-risk repeatable changes.

## Action Items

- [ ] #task Define the pre-approved change category and get it ratified 🔺
- [ ] #task Confirm access point delivery dates before the refresh starts
""")),

        (bd(9), dt.time(9, 30), render_meeting(
            "Individual", [DOT],
            created=dt.datetime.combine(bd(9), dt.time(9, 30)),
            tags=["Cloud/Migration", "Operations", "Procurement"],
            body="""## Notes

Application rationalization. Eleven retirement candidates, four with committed
owners. The pattern in the other seven is that nobody wants to be the person who
turned something off.

Discussed making retirement a named deliverable in the migration plan rather
than a side effort, so it gets the same scrutiny as a cutover.

## Action Items

- [ ] #task Add application retirement as a tracked migration deliverable 🔺
- [ ] #task Tighten the sandbox spend-cap change process
""")),

        (bd(11), dt.time(10, 0), render_meeting(
            "Group", ANALYTICS, group="Data & Analytics Team",
            created=dt.datetime.combine(bd(11), dt.time(10, 0)),
            tags=["Analytics", "Analytics/DataPlatform", "DataGovernance",
                  "Operations"],
            body="""## Agenda

- Semantic layer scope
- Freshness guarantees
- Analyst support model

## Notes

Scoped the semantic layer to the twelve metrics that appear in executive
reporting. Broader coverage later; the goal now is that "revenue" means one
thing in every dashboard.

[[Aggregate, Ada]] proposed publishing freshness guarantees per dataset so
analysts know whether today's number is today's. Cheap to do and it removes a
recurring source of mistrust.

Support model: analytics questions currently land in the same queue as password
resets. That needs to change before adoption improves.

## Action Items

- [ ] #task Publish per-dataset freshness guarantees 🔺
- [ ] #task Split analytics support out of the general service desk queue
""")),

        (bd(13), dt.time(16, 0), render_meeting(
            "Ad-hoc", [WANDA, MILO, DOT, TILLY],
            title="FY27 Budget Planning Kickoff",
            created=dt.datetime.combine(bd(13), dt.time(16, 0)),
            tags=["Budget", "Strategy", "Roadmap", "Infrastructure/Compute"],
            body="""## Agenda

- Reduction target and where it lands
- Committed spend vs. discretionary
- Timeline

## Notes

The 10% reduction target is manageable but not evenly distributable — roughly
two-thirds of the base is committed contract spend that cannot move this cycle.
The reduction therefore lands almost entirely on discretionary, which is where
the roadmap lives.

Agreed the honest framing is a roadmap conversation, not a cost conversation.
Asking each leader for what they would stop, ranked, rather than a percentage
cut across the board.

## Action Items

- [ ] #task Circulate the "what would you stop, ranked" template 🔺
- [ ] #task Separate committed from discretionary spend in the base
- [ ] #task Set the planning milestone dates
""")),
    ]

    for d, t, content in one_offs:
        out.append((dt.datetime.combine(d, t), content))

    # Close out action items from older meetings. Applied to the whole rendered
    # note rather than the body alone -- frontmatter holds no task lines, so
    # there is nothing there for the pattern to match.
    out = [(start, age_tasks(content, start.date(), anchor))
           for start, content in out]

    # ---- Today: three meetings, one of each type. Agenda pre-filled with prep
    # tasks (realistic for a morning dashboard), notes left for the user.
    t0 = dt.datetime.combine(anchor, dt.time(9, 0))
    out.append((t0, render_meeting(
        "Group", LEADERSHIP, group="IT Leadership Team",
        series="it-leadership-weekly", created=t0,
        body="""## Agenda

- Q4 priorities — first read of the ranked lists
- Cloud migration wave 3 go/no-go
- Security metrics ahead of the board update

## Notes

## Action Items

- [ ] #task Bring the ranked Q4 priority lists to the leadership meeting 🔺
- [ ] #task Confirm wave 3 go/no-go criteria before the meeting
""")))

    t1 = dt.datetime.combine(anchor, dt.time(11, 30))
    out.append((t1, render_meeting(
        "Individual", [ROSIE], created=t1,
        body="""## Agenda

- Board update security section
- Service account remediation plan
- SecOps hiring decision

## Notes

## Action Items

- [ ] #task Review the security section of the board deck before this 1:1
""")))

    t2 = dt.datetime.combine(anchor, dt.time(14, 0))
    out.append((t2, render_meeting(
        "Ad-hoc", [WANDA, GUS, DOT, VERA],
        title="Cloud Migration Steering Committee", created=t2,
        body="""## Agenda

- Wave 3 go/no-go
- Cross-region traffic redesign
- Retirement candidate decisions

## Notes

## Action Items

- [ ] #task Get the egress redesign options from [[Vector, Vera]] before this meeting 🔺
""")))

    return out


def build_series_roots(now: dt.datetime) -> dict[str, str]:
    return {
        "it-leadership-weekly": render_series_root(
            "IT Leadership Weekly", "Every Monday at 9:00 AM", LEADERSHIP, now,
            "1. Round-robin: what changed, what is blocked\n"
            "2. Program status — migration, security, data platform\n"
            "3. Budget and headcount\n"
            "4. Decisions needed from this group"),
        "security-operations-review": render_series_root(
            "Security Operations Review", "Every Wednesday at 10:00 AM",
            SECURITY, now,
            "1. Threat brief\n"
            "2. Open incidents and escalations\n"
            "3. Detection engineering backlog\n"
            "4. Metrics review"),
        "cio-deputy-cio-1-1": render_series_root(
            "CIO / Deputy CIO 1:1", "Every Thursday at 2:00 PM",
            [WANDA, MILO], now,
            "1. Anything urgent\n"
            "2. Cross-cutting initiatives\n"
            "3. People and org\n"
            "4. What I am missing"),
    }


def build_daily(anchor: dt.date) -> dict[str, str]:
    """Three journal entries: today and the two prior weekdays."""
    days = weekdays_back(anchor, 3)
    entries = [
        """## Notes

Budget planning starts today. The framing I want to hold onto: this is a
roadmap conversation wearing a cost conversation's clothes. If I let it become a
percentage-cut exercise, every leader protects their base and nothing actually
gets stopped.

Ranked "what would you stop" lists force the trade-off into the open. Expect
resistance — ranking is the part nobody wants to do.

- [ ] #task Reread the ranked priority lists before the leadership meeting
""",
        """## Notes

The phishing metrics are a good reminder that a number moving in the right
direction is not automatically good news. Click rate down, report rate flat
means people learned to avoid and not to tell us. We measured the thing that was
easy to measure.

Worth checking where else we are doing that.

- [ ] #task Audit the security metrics set for measure-what-is-easy problems
""",
        """## Notes

Good conversation on the governance council charter. The instinct to review
prompts is exactly the trap — it feels like governance and it governs nothing.
Tool grants are the real control surface.

Also: build the sunset review in from the start. Every governance body I have
seen accumulates approvals and never revisits one.

- [ ] #task Add a standing sunset review item to the council charter 🔺
""",
    ]
    out = {}
    for d, body in zip(days, entries):
        # Build the heading without %-d / %#d, which differ between glibc and
        # the Windows CRT -- this script has to run on both.
        pretty = f"{d.strftime('%A, %B')} {d.day}, {d.year}"
        out[d.isoformat()] = f"""---
title: {d.isoformat()}
created: {d.isoformat()}T07:30
updated: {d.isoformat()}T07:30
tags:
  - note
  - journal
classification: public
demo_seed: generated
---

# {pretty}

{body}"""
    return out


# ---------------------------------------------------------------------------
# The full plan
# ---------------------------------------------------------------------------

def build_plan(anchor: dt.date) -> list[tuple[Path, str]]:
    """Every file the demo dataset consists of, as (path, content)."""
    plan: list[tuple[Path, str]] = []
    now = dt.datetime.combine(anchor, dt.time(7, 0))

    # --- People -----------------------------------------------------------
    made = back(anchor, 60)
    for row in PEOPLE:
        plan.append((PEOPLE_DIR / f"{row[0]}.md", person_note(*row, created=made)))
    for row in AUTHORS:
        plan.append((PEOPLE_DIR / f"{row[0]}.md", author_note(*row, created=made)))

    # --- Groups -----------------------------------------------------------
    plan += [
        (GROUPS_DIR / "IT Leadership Team.md", static_group(
            LEADERSHIP,
            "The CIO's direct leadership team. A **static** roster — membership "
            "is listed explicitly, so the Meeting template can read it and "
            "pre-fill attendees. Photos refresh nightly via "
            "`Z_attachments/refresh_groups.py`.", made)),
        (GROUPS_DIR / "Security Team.md", static_group(
            SECURITY, "Security leadership. Another **static** roster.", made)),
        (GROUPS_DIR / "Infrastructure Team.md", dataview_group(
            "Infrastructure",
            "A **dynamic** roster — membership is whoever carries the "
            "`#Infrastructure` tag in `People/`, resolved by Dataview at read "
            "time. Add the tag to a person and they appear here automatically. "
            "Note that the Meeting template cannot pre-fill attendees from a "
            "dynamic group; use a static roster when you need that.", made)),
        (GROUPS_DIR / "Data & Analytics Team.md", dataview_group(
            "Analytics",
            "A second **dynamic** roster, driven by the `#Analytics` tag.", made)),
    ]

    # --- Categories -------------------------------------------------------
    cat = back(anchor, 60)
    plan += [
        (CATEGORIES_DIR / "People.md", category_note(
            "People", "People.base",
            "Every contact record in the vault. Notes land here by carrying "
            "`categories: - \"[[Categories/People]]\"` in their frontmatter — "
            "the People template does this for you.", cat)),
        (CATEGORIES_DIR / "Meetings.md", category_note(
            "Meetings", "Meetings.base",
            "Every meeting note. The Meeting template stamps "
            "`categories: - \"[[Meetings]]\"`, which resolves to this note.", cat)),
        (CATEGORIES_DIR / "Clippings.md", category_note(
            "Clippings", "Clippings.base",
            "Articles saved from the web with the Obsidian Web Clipper, including "
            "paywalled sources. The semantic auto-tagger tags these on its "
            "next run.", cat)),
        (CATEGORIES_DIR / "Creations.md", category_note(
            "Creations", "Notes.base",
            "Things you made: authored notes, cleaned-up voice notes, converted "
            "documents, and software tracking records.", cat)),
    ]

    # --- Knowledge --------------------------------------------------------
    kn = back(anchor, 45)
    plan += [
        (KNOWLEDGE_DIR / "Tag Taxonomy.md", knowledge_note(
            "Tag Taxonomy", ["Documentation", "Governance"], kn,
            tag_taxonomy_body(kn))),
        (KNOWLEDGE_DIR / "Data Classification.md", knowledge_note(
            "Data Classification",
            ["Documentation", "Governance", "Compliance"], kn,
            DATA_CLASSIFICATION_BODY)),
        (KNOWLEDGE_DIR / "Vendor Management Playbook.md", knowledge_note(
            "Vendor Management Playbook",
            ["Documentation", "Procurement", "Vendors"], kn,
            VENDOR_PLAYBOOK_BODY)),
        (KNOWLEDGE_DIR / "Incident Response Runbook.md", knowledge_note(
            "Incident Response Runbook",
            ["Documentation", "Cybersecurity", "Cybersecurity/IncidentResponse"],
            kn, IR_RUNBOOK_BODY)),
    ]

    # --- Topics -----------------------------------------------------------
    plan += [
        (TOPICS_DIR / "AI.md", topic_note(
            "AI", "AI",
            "Everything tagged `#AI` — clippings, meeting notes, and authored "
            "work. Topic notes are pure views: they hold no content of their "
            "own, so adding the tag to a note is all it takes to surface it "
            "here.", cat)),
        (TOPICS_DIR / "Cybersecurity.md", topic_note(
            "Cybersecurity", "Cybersecurity",
            "Everything tagged `#Cybersecurity`. Note that child tags like "
            "`#Cybersecurity/Phishing` do **not** roll up automatically — add "
            "a second query if you want them.", cat)),
        (TOPICS_DIR / "Cloud Migration.md", topic_note(
            "Cloud Migration", "Cloud/Migration",
            "Everything tagged `#Cloud/Migration`. An example of a topic built "
            "on a hierarchical child tag rather than a top-level one.", cat)),
        (TOPICS_DIR / "Budget.md", topic_note(
            "Budget", "Budget",
            "Everything tagged `#Budget` — useful during planning season.", cat)),
    ]

    # --- Clippings --------------------------------------------------------
    plan += [
        (CLIPPINGS_DIR / "Why Agentic AI Needs a Governance Layer.md", clipping_note(
            "Why Agentic AI Needs a Governance Layer",
            "https://example.com/articles/agentic-ai-governance-layer",
            "Quotient, Quill", back(anchor, 58), back(anchor, 57),
            ["AI", "AI/Agents", "AI/Governance", "Governance"],
            "Agents that can act, not just answer, break the assumptions behind most AI review boards.",
            """*Synthetic sample article. Not a real publication.*

The review boards most organizations stood up in 2024 were designed for a world
where the model produced text and a human decided what to do with it. That
assumption is now wrong. An agent that can file a ticket, move money, or change
a configuration is not a content risk — it is an operational one, and it belongs
under change management, not communications review.

Three things follow. First, the unit of review shifts from the model to the
**tool grant**: what can this agent actually reach? Second, every agent needs an
identity, because an action with no attributable actor cannot be audited. Third,
the rollback path has to be designed before launch, not discovered during an
incident.

None of this is exotic. It is the same discipline applied to any system that
takes action on your behalf. The mistake is filing agentic AI under "AI policy"
rather than under the controls that already govern automated change.
""")),

        (CLIPPINGS_DIR / "Phishing-Resistant MFA - A Practical Rollout Guide.md", clipping_note(
            "Phishing-Resistant MFA: A Practical Rollout Guide",
            "https://example.com/articles/phishing-resistant-mfa-rollout",
            "Diligence, Dash", back(anchor, 76), back(anchor, 73),
            ["Cybersecurity", "Cybersecurity/Identity", "Cybersecurity/Phishing"],
            "Sequencing matters more than technology choice when moving off push-based MFA.",
            """*Synthetic sample article. Not a real publication.*

Most phishing-resistant MFA programs stall in the same place: the long tail of
users who cannot or will not carry a hardware key. The technology decision —
platform authenticators, security keys, or both — turns out to be the easy part.

The sequencing that works, in the order that works:

1. **Instrument first.** You cannot plan the rollout until you know which
   applications actually enforce MFA today and which merely offer it.
2. **Start with the privileged.** Administrators are a small population, a large
   share of the risk, and the group most able to absorb friction.
3. **Make enrollment a condition of something people want**, such as a hardware
   refresh, rather than a standalone ask.
4. **Kill the fallback last, and announce the date early.** Push-based fallback
   is what attackers target; leaving it enabled indefinitely negates the program.

Expect the tail to take longer than the first ninety percent. Budget for it
explicitly rather than declaring victory at the inflection point.
""")),

        (CLIPPINGS_DIR / "The Real Cost of Cloud Migration Sprawl.md", clipping_note(
            "The Real Cost of Cloud Migration Sprawl",
            "https://example.com/articles/cloud-migration-sprawl-cost",
            "Lumen, Ledger", back(anchor, 51), back(anchor, 50),
            ["Cloud", "Cloud/Migration", "Cloud/Cost", "Budget"],
            "Lift-and-shift migrations rarely fail on technology. They fail on the operating model nobody updated.",
            """*Synthetic sample article. Not a real publication.*

The cost overruns in cloud migration programs are remarkably consistent, and
they are almost never about compute pricing. They come from three places.

**Idle capacity nobody owns.** On-premises, unused capacity was a sunk cost. In
the cloud it is a monthly invoice, and it accrues to whoever forgot to tag the
resource. Without ownership tagging enforced at provisioning, the finance team
inherits a bill they cannot allocate.

**Applications migrated as-is that should have been retired.** Every
rationalization exercise finds candidates. Every migration program under
schedule pressure skips it, then pays to run the same portfolio somewhere more
expensive.

**Egress and inter-region traffic** designed by teams who never had to think
about the network as a metered resource.

The organizations that land these well treat the migration as an operating-model
change with a technology component, not the reverse.
""")),

        (CLIPPINGS_DIR / "Building a Data Platform Your Analysts Will Actually Use.md", clipping_note(
            "Building a Data Platform Your Analysts Will Actually Use",
            "https://example.com/articles/data-platform-adoption",
            "Marginalia, Marge", back(anchor, 37), back(anchor, 36),
            ["Analytics", "Analytics/DataPlatform", "DataGovernance", "Governance"],
            "Adoption is a product problem, not an architecture problem.",
            """*Synthetic sample article. Not a real publication.*

Platform teams measure success in pipelines delivered. Analysts measure it in
questions answered without filing a ticket. Those two metrics diverge quickly,
and the gap is where adoption dies.

What closes it, in rough order of impact: a semantic layer so that "revenue"
means one thing; documented freshness guarantees, so an analyst knows whether
today's number is today's; and a genuine support path that is not the same queue
as password resets.

Architecture matters, but it is downstream of the question of whether anyone
trusts the numbers enough to put them in front of an executive.
""")),

        # Dated today, so the dashboard's "New today" section is populated.
        (CLIPPINGS_DIR / "What Executives Get Wrong About AI Pilots.md", clipping_note(
            "What Executives Get Wrong About AI Pilots",
            "https://example.com/articles/ai-pilot-mistakes",
            "Quotient, Quill", anchor, anchor,
            ["AI", "AI/Adoption", "Strategy", "Leadership"],
            "A pilot that cannot fail teaches you nothing. Most AI pilots are designed not to fail.",
            """*Synthetic sample article. Not a real publication.*

Most AI pilots are structured to succeed, which is why so few of them teach
anything. The scope is narrow, the users are volunteers, the data is clean, and
the success criterion is "did people like it." All four of those choices remove
the exact conditions that determine whether the thing works at scale.

A pilot worth running has a falsifiable claim attached: this will reduce
handle time by fifteen percent, this will let us close the books two days
earlier. If you cannot state what result would cause you to stop, you are not
running a pilot. You are running a demo with a longer timeline.

The second failure is treating adoption as the outcome. Adoption is an input.
Plenty of tools get adopted and produce nothing measurable, and a pilot that
reports usage numbers instead of outcome numbers is usually hiding that.
""")),
    ]

    # --- Creations --------------------------------------------------------
    vn = back(anchor, 43)
    sw_created, sw_updated = back(anchor, 65), back(anchor, 30)
    po = back(anchor, 25)
    plan += [
        (CREATIONS_DIR / f"{vn.isoformat()} 0815.md", f"""---
categories:
  - "[[Creations]]"
title: "Voice note — AI governance council scope"
type: Authored
created: {stamp(vn, 8, 15)}
updated: {stamp(vn, 8, 15)}
status: Draft
tags:
  - AI
  - AI/Governance
  - Governance
  - Strategy
classification: public
demo_seed: generated
source: voice-cleanup
---

## Notes

*This note is a sample of what the voice-note pipeline produces: raw phone
dictation, cleaned up through the Claude API by `Templates/Scripts/voice_cleanup.py`,
written into `Creations/` where the semantic auto-tagger picks it up on its next
run. The filename is the capture timestamp.*

Thinking through the scope of the AI governance council before proposing it to
the leadership team.

The failure mode I want to avoid is a body that reviews prompts. That is the
wrong altitude and it will be ignored within two quarters. What actually needs a
decision forum is the tool-grant question — which systems an agent is allowed to
touch, and who signs off. That is a real approval, it has a real owner, and it
maps onto change management we already run.

Second thing: membership. If it is all of IT, it becomes a status meeting. It
needs the business owners of the systems in scope, or the approvals mean nothing.

Third: I want a standing agenda item on what we have **turned off**. Every
governance body accumulates approvals and never revisits them. Build the
sunset review in from the start rather than bolting it on after the portfolio
gets unmanageable.

Action for me — draft a one-page charter and walk [[Megabyte, Milo]] through it
before it goes to the full team.

- [ ] #task Draft the AI governance council charter 🔺
- [ ] #task Walk the charter through with [[Megabyte, Milo]] before the leadership meeting
"""),

        (CREATIONS_DIR / "Cumulus Cloud Platform.md", f"""---
categories:
  - "[[Creations]]"
title: "Cumulus Cloud Platform"
type: Software
created: {stamp(sw_created, 14, 0)}
updated: {stamp(sw_updated, 11, 30)}
status: In Production
vendor: Cumulus Cloud Co.
product: Cumulus Cloud Platform
version: "2026.2"
license-type: Enterprise Agreement, committed spend
contract-expiration: {dt.date(anchor.year + 1, 3, 31).isoformat()}
annual-cost: 1450000
owner: "[[Gigabyte, Gus]]"
tags:
  - Cloud
  - Vendors
  - Vendors/CumulusCloud
  - Procurement
classification: public
demo_seed: generated
---

## Overview

Primary infrastructure-as-a-service platform, and the destination for the cloud
migration program. Commercial relationship is managed by [[Kilowatt, Kip]];
technical counterpart is [[Vector, Vera]].

This note is a sample of the **Software** note type — pick it from the Note
template's type selector and you get the vendor, product, version, licensing,
cost, and owner fields above, plus the Overview / Environment / Integrations
sections below. Software notes are how the demo vault tracks the portfolio
without a separate asset system.

## Environment

- Two regions, active/passive. Failover tested semi-annually; last test passed.
- Production, staging, and a sandbox tenant with a hard monthly spend cap.
- Identity federated to the corporate directory. No local accounts in production.

## Integrations

- Corporate directory — SSO and group-based authorization
- Security operations pipeline — control-plane audit logs
- Finance — monthly cost allocation export, tag-driven

## Notes

Renewal is {dt.date(anchor.year + 1, 3, 31).isoformat()}. Committed spend is
currently tracking about eight percent under the commitment, which is leverage
worth preserving going into the negotiation — see [[Vendor Management Playbook]].

Open item: the sandbox tenant's spend cap has been raised twice this year without
going through procurement. Worth tightening the process before it becomes a
pattern.

- [ ] #task Draft the renewal position for Cumulus Cloud 🔺
- [ ] #task Tighten the sandbox spend-cap change process with [[Matrix, Dot]]
"""),

        (CREATIONS_DIR / f"{po.isoformat()} 0900.md", f"""---
categories:
  - "[[Creations]]"
title: "Quarterly Technology Update"
type: Authored
created: {stamp(po, 9, 0)}
updated: {stamp(po, 9, 0)}
status: Draft
tags:
  - Strategy
  - Roadmap
  - Leadership
  - Budget
classification: public
demo_seed: generated
---

## Notes

Outline for the quarterly technology update to the board. Twenty minutes,
followed by questions — so four topics at most, and the budget slide goes last
because that is where the questions land.

### 1. Cloud migration progress

Where we are against the plan, what moved this quarter, and the two workloads
that slipped and why. Sponsor view, not a project list. See [[Cumulus Cloud Platform]].

### 2. Cybersecurity posture

Phishing-resistant MFA coverage, unmanaged-device count trend, and the tabletop
results. [[Rackmount, Rosie]] presents this section.

### 3. AI adoption

Where the agentic pilots are, what governance now sits underneath them, and the
one place we said no. Ties back to the council charter.

### 4. Budget performance

Year-to-date against plan, the ten percent reduction target, and where the
remaining risk sits.

- [ ] #task Get [[Rackmount, Rosie]] the security slide template 🔺
- [ ] #task Pull the year-to-date budget actuals from [[Matrix, Dot]]
- [ ] #task Dry-run the deck with the leadership team
"""),

        # Dated today, so the dashboard's "New today" section is populated.
        (CREATIONS_DIR / f"{anchor.isoformat()} 0645.md", f"""---
categories:
  - "[[Creations]]"
title: "Voice note — framing the budget conversation"
type: Authored
created: {stamp(anchor, 6, 45)}
updated: {stamp(anchor, 6, 45)}
status: Draft
tags:
  - Budget
  - Strategy
  - Leadership
classification: public
demo_seed: generated
source: voice-cleanup
---

## Notes

*Sample voice-note output, dated today so the Morning Dashboard's "New today"
section has something in it.*

Driving in, thinking about how to open the budget conversation.

The temptation is to lead with the number, because the number is what everyone
already knows is coming. But leading with the number makes it a negotiation, and
in a negotiation everybody defends their base.

Better opening: here is the roadmap we said we would deliver, here is what it
costs, and here is the gap. Then the question is which commitments we are
withdrawing — which is a decision this group can actually make together, rather
than a cut I impose.

Two-thirds of the base is committed contract spend anyway. Pretending the
reduction is evenly distributable would be dishonest and everyone in the room
knows enough to catch it.

- [ ] #task Rewrite the budget kickoff opening around withdrawn commitments 🔺
"""),
    ]

    # --- Actions ----------------------------------------------------------
    plan.append((ACTIONS_DIR / "To-Do.md", """---
title: To-Do
classification: public
demo_seed: generated
---

# To-Do

Every open task across the vault, aggregated by the Tasks plugin. Tasks are
written inline wherever the work is discussed — in a meeting note, a knowledge
note, a voice note — and surface here automatically.

This vault sets the Tasks plugin's `globalFilter` to `#task`, so **a checkbox
only counts as a task if it carries the `#task` tag**. That keeps grocery lists
and packing lists out of your work queue. The Morning Dashboard applies the same
rule.

## Open

```tasks
not done
short mode
```

## Completed recently

```tasks
done
sort by done reverse
limit 15
short mode
```
"""))

    # --- Meetings, series roots, journal ----------------------------------
    for start, content in build_meetings(anchor):
        plan.append((MEETINGS_DIR / f"{start.strftime('%Y-%m-%d %H%M')}.md", content))
    for slug, content in build_series_roots(now).items():
        plan.append((SERIES_DIR / f"{slug}.md", content))
    for day, content in build_daily(anchor).items():
        plan.append((DAILY_DIR / f"{day}.md", content))

    plan.sort(key=lambda kv: str(kv[0]))
    return plan


# ---------------------------------------------------------------------------
# Seed / remove
# ---------------------------------------------------------------------------

def seeded_files() -> list[tuple[Path, str]]:
    """Every file under a content folder carrying a `demo_seed:` marker.

    Both guards matter: the folder check keeps us out of Templates/, docs/, and
    installers/, and the marker check means a real note that happens to sit in
    Meetings/ is never a candidate for deletion.
    """
    out: list[tuple[Path, str]] = []
    for folder in sorted(CONTENT_FOLDERS):
        root = VAULT / folder
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fm = FRONTMATTER_RE.match(text)
            if not fm:
                continue
            m = MARKER_RE.search(fm.group(1))
            if m:
                out.append((p, m.group(1)))
    return out


def seed(anchor: dt.date, dry_run: bool) -> int:
    plan = build_plan(anchor)

    for path, content in plan:
        rel = path.relative_to(VAULT)
        print(f"  {'would write' if dry_run else 'wrote'}  {rel}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    meetings = sum(1 for p, _ in plan if p.parent == MEETINGS_DIR)
    print(f"\n{len(plan)} file(s) {'would be ' if dry_run else ''}written "
          f"({meetings} meetings, anchored to {anchor.isoformat()}).")
    if not dry_run:
        print("\nRemove it all again with:  python3 seed_demo_content.py --remove")
    return 0


def remove(dry_run: bool) -> int:
    targets = seeded_files()
    if not targets:
        print("no demo content found.")
        return 0
    for p, marker in targets:
        rel = p.relative_to(VAULT)
        print(f"  {'would remove' if dry_run else 'removed'}  {rel}")
        if not dry_run:
            p.unlink()
    # Prune Meetings/Series only. The seed creates that folder itself, so
    # removing it when empty restores the tree exactly. Every other content
    # folder ships with the repo (each carries a .gitkeep) and must survive a
    # teardown -- clearing the demo data still leaves the vault structure.
    if not dry_run and SERIES_DIR.is_dir() and not any(SERIES_DIR.iterdir()):
        SERIES_DIR.rmdir()
    print(f"\n{len(targets)} file(s) {'would be ' if dry_run else ''}removed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed or remove the synthetic demo dataset.")
    ap.add_argument("--anchor", metavar="YYYY-MM-DD",
                    help="Anchor date for 'today'. Defaults to the current date.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen without touching the vault.")
    ap.add_argument("--remove", action="store_true",
                    help="Remove all demo content.")
    ap.add_argument("--list", action="store_true",
                    help="List demo content currently in the vault.")
    args = ap.parse_args()

    if not VAULT.is_dir():
        print(f"error: vault not found at {VAULT}", file=sys.stderr)
        return 2

    if args.list:
        found = seeded_files()
        if not found:
            print("no demo content found.")
            return 0
        for p, _ in found:
            print(f"  {p.relative_to(VAULT)}")
        print(f"\n{len(found)} file(s).")
        return 0

    if args.remove:
        return remove(args.dry_run)

    if args.anchor:
        try:
            anchor = dt.date.fromisoformat(args.anchor)
        except ValueError:
            print(f"error: bad --anchor date: {args.anchor}", file=sys.stderr)
            return 2
    else:
        anchor = dt.datetime.now().astimezone().date()

    return seed(anchor, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
