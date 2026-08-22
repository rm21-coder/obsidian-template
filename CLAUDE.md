# CLAUDE.md — Obsidian Second-Brain Template

This file gives Claude Code the context it needs to work in this repository.
Read it first. The project context lives here, in the repo, where every
machine (macOS **and** Windows) can see it.

## What this repository is

A public, shareable **template** for an Obsidian "second brain" plus the
automation that runs on top of it. **The repo IS the vault**: you clone it into
your vault location and its top-level folders (`Actions/`, `Categories/`,
`Clippings/`, `Creations/`, `Daily/`, `Groups/`, `Knowledge/`, `Meetings/`,
`Notes/`, `People/`, `Templates/`, `Topics/`, `Z_archive/`, `Z_attachments/`)
become the vault structure. `.obsidian/` config ships with the repo too.

The automation lives in `Templates/Scripts/` (so scripts version alongside the
templates that call them). It is a set of Python jobs — semantic auto-tagger,
voice-note cleanup, signed mail-drop transport, podcast transcription,
meeting prep + pre-population, a morning HTML dashboard, a weekly vault lint,
an optional local-LLM RAG sync, and two security controls — scheduled by
macOS `launchd` or, on Windows, Task Scheduler.

## CRITICAL: this is a PUBLIC repo

Zero personal data. Only structural templates and shareable scripts/docs are
allowed. Never commit real notes, People, Meetings, Groups, API keys, or
machine-specific paths with a real username. Secrets are read at runtime from
`~/dev/secrets/.env` (macOS) / `%USERPROFILE%\dev\secrets\.env` (Windows) and
are **gitignored** (`.env`). A `02-classification-audit` installer component
exists to check the tree carries only public content — respect it.

## The content folders are empty on purpose — demo data is generated

Every content folder (`People/`, `Meetings/`, `Knowledge/`, …) ships holding
nothing but a `.gitkeep`. **Never commit content into them.** The `.gitkeep`
files are what preserve the vault structure through a demo teardown; leave them.

A synthetic dataset is written on demand by
`Templates/Scripts/seed_demo_content.py` — an invented cast at Nimbus Widgets
Inc. with `.example` addresses and `555` phone numbers, about 74 notes across
every folder. `--remove` takes it back out. It is generated rather than
committed because the dataset is anchored to today: the Morning Dashboard reads
`Meetings/<today>*.md`, so a fixed-date meeting note would leave it permanently
empty. Everything it writes carries `demo_seed: generated`, which is also what
makes removal safe.

If you find content files in those folders, they are either seeded demo data
(check for the `demo_seed:` marker) or a mistake — real notes belong only in a
user's private vault, never here.

To change the demo data, edit `seed_demo_content.py` rather than adding files:
the cast is a list of constants near the top and `build_plan()` assembles the
file list. Anything you add needs `classification: public` in its frontmatter
(the audit hard-fails otherwise) and `demo_seed: generated` (so `--remove` can
find it). Keep it obviously fictional — no real company names, people, or URLs.

The dataset is held clean against `installers/lib/check_classification.py`,
`Templates/Scripts/vault_lint.py`, and
`Templates/Scripts/tests/test_seed_demo_content.py`; run all three after
touching it. Full detail in [`docs/Demo-Content.md`](docs/Demo-Content.md).

## Repository layout

- `install.sh` / `uninstall.sh` + `installers/` — the macOS installer. 27
  numbered, idempotent components (`installers/components/NN-*.sh`) run in
  order; shared helpers in `installers/lib/`.
- `Templates/Scripts/*.py` — the automation logic (mostly cross-platform).
- `Templates/Scripts/*.plist` — macOS `launchd` LaunchAgents (the schedulers).
- `Templates/Scripts/*.sh` — macOS shell wrappers (`sync-vault.sh`, etc.).
- `Templates/Scripts/windows/` — the **Windows port** (this branch). PowerShell
  equivalents of the schedulers/wrappers/installer. See its `README.md`.
- `docs/` — setup + architecture docs (LaunchAgents, RAG, security harness,
  the workflow diagram, etc.).

## Platform conventions

- **Vault root:** `~/Obsidian` (macOS) / `%USERPROFILE%\Obsidian` (Windows).
- **Scripts:** `<vault>/Templates/Scripts/` on both platforms.
- **Python:** a shared venv at `Templates/Scripts/.venv`. Needs **3.10+**
  (code uses PEP 604 `X | None` unions). macOS uses Homebrew python, NOT
  `/usr/bin/python3` (that is 3.9). Windows uses python.org / winget python.
  Deps: `requirements.txt` (install ALL of it — `requests` is required by the
  RAG sync). `mlx-whisper` is Apple-Silicon-only and is guarded out elsewhere.
- **Secrets/state:** secrets in `dev/secrets/.env`; runtime state under
  `~/.local/share/...` (macOS) / `%LOCALAPPDATA%\...` (Windows).

## Working agreements

- **Git:** the maintainer (Rich) normally drives `commit`/`push`. Make edits and
  stage them; only commit/push when explicitly asked.
- **Cross-platform first:** when touching a `.py` script, prefer making it
  platform-neutral (use `pathlib`, `platform.system()` guards, `shutil.which`)
  over forking a Windows copy. Fork only the OS-specific glue (schedulers,
  wrappers, notifications, installer).
- **Windows is a fully supported platform**, validated end-to-end on Windows 11
  (x64) including a clean bare-metal rebuild. Setup docs live in
  [`docs/Windows Setup.md`](docs/Windows%20Setup.md); a quick file-by-file
  reference for the PowerShell layer is in `Templates/Scripts/windows/README.md`.

## When working on the Windows layer

Start from [`docs/Windows Setup.md`](docs/Windows%20Setup.md) and
`Templates/Scripts/windows/README.md`. Test any change on a Windows machine,
commit to the `windows-port` branch, push, and merge cross-platform
improvements back to `main` when solid (most fixes belong in the shared `.py`
files anyway — see "Cross-platform first" above).
