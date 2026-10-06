# Drift Review (maintainer tool, optional)

Every push changes code, and the documents that describe the code drift
unless something re-checks them. The drift reviewer does that on every push:
a headless Claude Code session reads the change and checks the documents
against the code, a second session tries to disprove each finding, and what
survives lands as a task in your vault, so it shows in the Morning
Dashboard's to-do list.

It is maintainer tooling for this repository, not part of the vault workflow.
Nothing is installed for it, and it is off until you configure it.

## What it checks

- This repository's documents: `README.md`, `ONBOARDING.md`, `CLAUDE.md`,
  everything under `docs/` (including the workflow diagram's text), and
  `Templates/Scripts/windows/README.md`.
- Any **companion documents** you name: files kept outside the repository
  that describe this code, such as a security-review write-up or a vault
  note about sync scope.

It reports statements the pushed change made false, statements the change
touches that were already false, security-relevant behaviour the documents
no longer describe, and plain bugs it notices in the changed code. It does
not report style.

## Turning it on

1. Install and sign in to the Claude Code CLI (`claude`), the same CLI the
   meeting pull uses.
2. Copy `installers/lib/drift-review.example.json` to
   `~/.config/obsidian-drift-review/config.json` (Windows:
   `%APPDATA%\obsidian-drift-review\config.json`) and edit it:
   - `companions`: the outside documents, each with a `path` and a `label`
     saying what it is.
   - `report_dir`: where full reports go. It must be outside this
     repository: reports quote documents and name paths.
   - `task_note`: the vault note that collects open findings, for example
     `~/Obsidian/Actions/Drift Review.md`. It is created on first use.
   - `model`: leave `null` for the CLI's default.
3. Re-install the git hooks if you have not since updating:
   `./installers/install-git-hooks.sh`.

From then on, `pre-push` starts a review in the background once the security
suite has passed. The push does not wait for it, and a review that fails
cannot block a push.

To review a range by hand, or to see what would be staged without running a
session:

```bash
python3 installers/lib/drift_review.py --range <old>..<new>
python3 installers/lib/drift_review.py --range <old>..<new> --dry-run
```

## What you see

- **Findings:** each CONFIRMED or PLAUSIBLE finding becomes a `#task` line
  under a dated heading in the task note, naming the document, the location
  and the wrong text. PLAUSIBLE ones are marked "unverified". Tick a task
  when it is fixed, or when you judge it not worth fixing.
- **Reports:** one Markdown file per review in `report_dir`, with the
  evidence, the proposed correction, the verifier's reason, and the REJECTED
  findings too, so you can audit the verifier.
- **Failures:** a review that could not run (CLI missing, not signed in,
  timed out) writes a "Drift review FAILED" task with the reason and the
  command to re-run it. A clean review writes a report and no task.
- **Logs:** `~/.local/share/obsidian-drift-review/` (Windows:
  `%LOCALAPPDATA%\obsidian-drift-review\`).

## What the sessions can reach

Each review builds a private tree in a temporary directory: the repository at
the pushed commit (`git archive`, so no `.git`), the range's log and diff,
and copies of the companion documents. Nothing else is in it: no secrets
file, no keystore, and no vault beyond the companions you named. The tree is
deleted when the review ends.

The sessions run with `--restricted` (file tools confined to that tree; no
shell or code-running tools; settings files ignored), `--tools Read,Grep,Glob`,
`--strict-mcp-config` (no MCP servers, so no claude.ai connectors),
`--permission-mode dontAsk`, and every other built-in denied. A live probe
showed that a Read, Grep or Glob outside the tree was refused, and that no
shell or MCP tool was offered.

The diff and documents are text the tool did not write. The prompt tells the
session to treat them as data and to report any instruction it finds as a
finding. Before model text is written into the vault note, it goes through
the vault's own ingest guard (`templater_guard.neutralize`), and links and
tags are broken up, so a finding cannot run plugin code or forge a task.

Companion documents are sent to the model, so name only documents your
Claude account is entitled to process.
