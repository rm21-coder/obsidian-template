---
tags:
  - note
  - setup
  - windows
classification: public
created: 2026-08-15T13:53
updated: 2026-08-15T13:54
---

# Windows Setup

This template runs natively on Windows. `install.ps1` is the PowerShell
counterpart to `install.sh`: Windows Task Scheduler replaces `launchd`, the
same Python automation runs under a per-vault venv, and the local LLM RAG
stack (Ollama + Open WebUI) is fully self-contained — no Mac required for any
part of the workflow. Validated end-to-end on Windows 11 (x64), including a
clean bare-metal rebuild, and on Windows 11 ARM64 with native ARM64 Python —
see [Windows on ARM64](#windows-on-arm64) for the details. A 2026-08-25
re-validation on ARM64 found that plugin installation and toast notifications
did not run at all under Windows PowerShell 5.1 (an encoding defect, fixed the
same day); both are pending re-verification on real hardware.

## Quick Start

Three commands on a fresh Windows machine:

```powershell
git clone https://github.com/rm21-coder/obsidian-template.git $env:USERPROFILE\Obsidian
cd $env:USERPROFILE\Obsidian
powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\install.ps1
```

The `-ExecutionPolicy Bypass -File` form runs the installer in one line with
no separate policy step — it relaxes the policy only for that one process,
nothing system-wide or permanent. (If you're going to keep running other
scripts in `windows\` in the same PowerShell window afterward — `setup-rag.ps1`,
`uninstall.ps1` — it's more convenient to set the bypass once for that whole
window instead: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`,
then call scripts directly, e.g. `.\Templates\Scripts\windows\install.ps1`.)

`install.ps1` runs straight through and will:

- Check for Python 3.10+ and run the classification audit (refuses to
  install if the repo somehow carries non-public content)
- Install Obsidian via `winget` if it isn't already present
- Link `%USERPROFILE%\Obsidian` to wherever you cloned the repo, via a
  directory junction (see [Architecture](#architecture) below)
- Create a secrets stub at `%USERPROFILE%\dev\secrets\.env`
- Fetch the 13 community plugins from their GitHub releases into
  `.obsidian\plugins\`
- Patch QuickAdd's folder-picker fall-through and apply the canonical ribbon
  icon order
- Create a per-vault Python venv and install `requirements.txt`
- Register all 15 scheduled jobs in Task Scheduler under `\Obsidian\` — 13
  ship **enabled** by default (validated end-to-end on a clean Windows 11
  install) and begin firing on their triggers as soon as the installer
  finishes, the other 3 ship **disabled** because they each need a per-user
  resource this template can't assume exists (a dedicated mailbox, an Azure
  Blob relay, or an MCP calendar connector) — see
  [Scheduled jobs](#scheduled-jobs) below
- Print a final status table of every registered task

One optional add-on stays off unless you ask for it (a console run offers it
interactively; add the switch for a non-interactive install):

```powershell
.\Templates\Scripts\windows\install.ps1 -WithRAG
```

- **`-WithRAG`** — installs the fully-local RAG stack: Ollama, the
  `llama3.1:8b` model, and Open WebUI in Docker. See
  [Local LLM RAG](#local-llm-rag) below for the one-time manual signup this
  still needs afterward.

The iPhone/iPad pipelines (voice-note cleanup,
podcast-watch) don't need anything installed for them at all: the recommended
path is the mail-drop transport (`source_mail_pull.py` — see
[Source Mail Transport](Source-Mail-Transport.md)), which needs nothing from
Apple. `install.ps1` never installs or prompts for Apple iCloud for Windows,
and the watchers read only the local `~/SourceMedia/` drop folders.

### After it finishes

1. **Launch Obsidian**, open `%USERPROFILE%\Obsidian` as a vault, click
   **Trust author and enable plugins**, then quit and reopen so the ribbon
   order and hotkeys bind correctly.
2. **Fill in your API keys** — see [Getting your API keys](#getting-your-api-keys).
3. **Validate a script by hand before enabling its task.** `tag-clippings` is
   the recommended first one:
   ```powershell
   & "$env:USERPROFILE\Obsidian\Templates\Scripts\.venv\Scripts\python.exe" `
     "$env:USERPROFILE\Obsidian\Templates\Scripts\tag_clippings.py"
   Enable-ScheduledTask -TaskName tag-clippings -TaskPath '\Obsidian\'
   ```
   Repeat for whichever other jobs you want running (see
   [Scheduled jobs](#scheduled-jobs) below for the full list).

### Common flags

```powershell
.\Templates\Scripts\windows\install.ps1 -WithRAG                 # optional local RAG stack
.\Templates\Scripts\windows\install.ps1 -SkipTasks              # everything except registering tasks
.\Templates\Scripts\windows\install.ps1 -SkipAudit               # skip the classification audit
.\Templates\Scripts\windows\install.ps1 -NonInteractive           # never prompt (for scripted/unattended installs)
.\Templates\Scripts\windows\install.ps1 -Profile ours            # pre-answer the prompts from a profile
.\Templates\Scripts\windows\install.ps1 -ListProfiles             # what's available here
.\Templates\Scripts\windows\install.ps1 -Profile ours -Force      # let the profile overwrite existing .env values
```

### Install profiles

Windows takes the **same** `installers\profiles\<name>.env` files the macOS
installer takes, so one profile serves a mixed-platform team. A bare name
resolves inside `installers\profiles\`; anything with a separator or an
`.env` suffix is treated as a path, so a profile a colleague handed you works
without being copied into the repo first:

```powershell
.\Templates\Scripts\windows\install.ps1 -Profile ours
.\Templates\Scripts\windows\install.ps1 -Profile $env:USERPROFILE\ours.env
```

What a profile does here:

- **Points every Claude call at your institution's gateway.** `LLM_BASE_URL`
  and `LLM_API_KEY_NAME` are written to `%USERPROFILE%\dev\secrets\.env`,
  which is what the scheduled tasks read — they start from a bare environment,
  so a gateway set only in your shell would work now and quietly fall back to
  stock Anthropic auth at 03:00. The key itself is never in the profile or in
  `.env`: the run ends by printing the exact `secret_store.py set <NAME>`
  command for it.
- **Turns on the meeting pipeline.** Writes `.config\meeting_pull.json` and
  `.config\meeting_prepopulate.json`, creates the drop folder, and enables the
  `meeting-pull` task (which otherwise ships disabled, since it needs a Claude
  CLI and an approved MCP calendar connector). Per-person answers — display
  name, email, assistant's address — arrive as editable prompt defaults.
- **Says what it cannot do.** `PROFILE_DASHBOARD_ACTIONS` is macOS-only (it
  registers a URL-scheme handler app); the run reports that rather than
  silently ignoring the key.

Two cautions. Under `-NonInteractive` the profile *is* the consent for the
opt-in jobs — that is the only way an unattended run installs them — but a
profile with no `PROFILE_EMAIL` cannot name the calendar owner, so the
producer is left disabled and says so. And a profile names internal endpoints
and tenant domains, so read one before you run it and only accept one from
someone you trust.

One difference from macOS worth knowing: `install.sh` *sources* a profile,
which makes it shell code running at the installer's trust level. `install.ps1`
**parses** it as data and never evaluates a value, so a profile cannot execute
anything on Windows. The trade-off is that shell constructs in a value have no
meaning here — `$HOME` is translated, and a value carrying a command
substitution is refused rather than taken literally.

Full key reference: [`installers/profiles/README.md`](../installers/profiles/README.md).

## Prerequisites

**Required:**

- **Windows 10/11**, x64 or ARM64 (see [Windows on ARM64](#windows-on-arm64))
- **Windows PowerShell 5.1** — the edition that ships with Windows, and the
  one this layer targets. PowerShell 7 (`pwsh`) also works but is *not*
  required and is not assumed anywhere. The distinction matters: 5.1 decodes
  a file without a byte-order mark as ANSI/CP1252 rather than UTF-8, so every
  shipped `.ps1`/`.psd1` is held ASCII-only by
  `Templates/Scripts/tests/test_static.py::TestPowerShellEncoding`. Keep it
  that way — a stray em-dash inside a double-quoted string makes 5.1 stop
  parsing mid-file, and the script simply never runs.
- **Python 3.10+** — installed automatically by `install.ps1` if missing
  (`winget install Python.Python.3.12`); the scripts use PEP 604 `X | None`
  unions, so 3.10 is the floor
- **`winget`** — ships with modern Windows; used to install Obsidian, Ollama,
  and Docker Desktop
- **API keys** (collected in `.env` after install): **Anthropic** (or your institutional gateway)
  — see [Getting your API keys](#getting-your-api-keys)

**Optional:**

- **Docker Desktop** — installed automatically by `setup-rag.ps1` if you use
  `-WithRAG`. Installing it is unattended, but its engine will not start
  without WSL2 (next item)
- **WSL2** — **required for `-WithRAG`, and it is not installed by default on
  a fresh Windows image.** Docker Desktop runs its engine inside WSL2, so
  without it the engine fails to start and the Open WebUI container never
  comes up. Neither `install.ps1` nor `setup-rag.ps1` can install it for you:
  it needs an elevated shell and a reboot. Do it *before* running with
  `-WithRAG`, from an **administrative** PowerShell:

  ```powershell
  wsl --install
  ```

  Then reboot, launch Docker Desktop once to accept its license and wait for
  "Engine running", and only then run `-WithRAG`. If WSL2 is already present,
  no reboot is needed. On **ARM64 this is a hard requirement with no
  workaround** — Docker Desktop for Windows on Arm supports only the WSL2
  backend, so unlike x64 there is no Hyper-V backend to fall back on. Skipping
  this is the single most likely way for a `-WithRAG` install to fail; the
  rest of the install (vault, venv, plugins, scheduled jobs, Ollama and the
  model pull) completes fine without it

## Getting your API keys

Add these to `%USERPROFILE%\dev\secrets\.env` (created as a stub by
`install.ps1`; this file is gitignored — never commit it):

- **`ANTHROPIC_API_KEY`** — powers `tag_clippings.py` (the semantic
  auto-tagger) and `voice_cleanup.py`. Get one at
  [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
  (sign up / log in, then "Create Key"). Requires adding billing/credits to
  the account before keys will make live calls.
- `youtube_summarize.py` needs **no key of its own** — it summarizes through
  `llm_endpoint.py`, so the `ANTHROPIC_API_KEY` above (or your gateway's
  equivalent) already covers it. See
  [`YouTube Summarizer.md`](YouTube%20Summarizer.md) for usage — it's an
  on-demand CLI, not a scheduled task, so there's no Windows Task Scheduler
  entry for it.
- **`OPEN_WEBUI_API_KEY` / `OBSIDIAN_COLLECTION_ID`** — only if you're using
  `-WithRAG`; see [Local LLM RAG](#local-llm-rag) below.

## Not on Windows (yet)

The Morning Dashboard's action buttons are macOS-only: they rely on a URL-
scheme handler app (`DashboardActions.app`, component `57-dashboard-actions`)
that has no Windows counterpart. The dashboard detects this and simply
renders without the button bar — everything the buttons do can be run
directly (`python meeting_pull.py`, `python obsidian-rag-sync.py`, etc.).

The nightly classification assistant (`classify_notes.py`, macOS agent
`com.obsidian.classify`) is also not yet a Windows scheduled task. The
script itself is cross-platform — run it manually or register your own
task if you want nightly classification on Windows; `install.ps1` already
seeds the `LLM_*`/`CLASSIFIER_*` config it reads.

## Scheduled jobs

All 15 jobs are registered by `Register-Tasks.ps1` under the Task Scheduler
path `\Obsidian\`, from the manifest in `schedules.psd1`. 13 ship **enabled**
by default — each was validated end-to-end on a clean Windows 11 install by
running its script by hand before its task was enabled. Because they are
enabled at registration time, they start firing as soon as `install.ps1`
finishes, so fill in `.env` first or expect logged errors until you do. The
other 3 ship
**disabled** because each needs a per-user resource this template can't
assume exists (a dedicated mailbox, an Azure Blob relay, or an MCP calendar
connector); set up the prerequisite, validate the script by hand, then
`Enable-ScheduledTask -TaskName <name> -TaskPath '\Obsidian\'`.

| Task | Script | Trigger | Default | Notes |
|---|---|---|---|---|
| `tag-clippings` | `tag_clippings.py` | every 30 min | enabled | Semantic auto-tagger. |
| `voice-cleanup` | `voice_cleanup.py --once` | every 5 min | enabled | Watches `~/SourceMedia/VoiceInput/` — see [Source Mail Transport](Source-Mail-Transport.md). Runs cleanly against an empty queue. |
| `source-mail-pull` | `source_mail_pull.py --once` | every 5 min | **disabled** | Mail-drop transport that fills `~/SourceMedia/<Type>/` for `voice-cleanup` and `podcast-watch` from a dedicated, HMAC-authenticated mailbox — replaces iCloud Drive as a transport. Needs `SOURCE_MAIL_USER`, `SOURCE_MAIL_APP_PASSWORD`, `SOURCE_MAIL_TOKEN`, `SOURCE_MAIL_ALLOWED_SENDERS` in `.env` first. See [Source Mail Transport](Source-Mail-Transport.md). |
| `podcast-watch` | `podcast_watch.py --once` | every 15 min | enabled | Watches `~/SourceMedia/PodcastInput/`. Runs cleanly against an empty queue. |
| `strip-ads` | `strip_ads.py` | every 5 min | enabled | Cleans ad cruft from clipped articles. |
| `meeting-prep` | `meeting_prep.py` | every 5 min | enabled | Inserts/refreshes open follow-up task callouts into today's individual meeting notes already in the vault. Gated to weekday business hours; a no-op the rest of the time. |
| `meeting-prepopulate` | `meeting_prepopulate.py` | every 30 min | enabled | Reads a schedule-handoff JSON from a local drop folder via a pluggable source (`drop` or `mcp`) — see [`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md). The producer side is yours to build; the consumer runs cleanly and reports "no handoffs to process" until it's wired up. |
| `meeting-pull` | `meeting_pull.py` | weekdays 5:00 AM | **disabled** | Producer side of meeting pre-population: shells out to the Claude CLI over an MCP calendar connector. Exercised end-to-end on macOS only — needs the Claude CLI on `PATH` and a registered MCP calendar connector before it can even reach its own config; validate with `python meeting_pull.py --dry-run` first. |
| `handoff-blob-pull` | `handoff_blob_pull.py` | every 5 min | **disabled** | Tier B handoff relay: pulls meeting-handoff sets from Azure Blob Storage via SAS-authenticated REST — see [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md). Fails loud (exit 1, clear message) until `HANDOFF_BLOB_ACCOUNT_URL`, `HANDOFF_BLOB_CONTAINER`, and `HANDOFF_BLOB_SAS` are set in `.env` — leave disabled until you have real Azure Blob credentials. |
| `group-photos` | `run_group_photos.py` | daily 2:00 AM | enabled | Runs `Z_attachments/insert_group_placeholders.py` then `refresh_groups.py` in sequence, mirroring the macOS `insert && refresh` pipeline. |
| `rag-sync` | `obsidian-rag-sync.py` | daily 3:00 AM | enabled | Pushes the vault into Open WebUI's Knowledge collection. Needs `-WithRAG` set up first — safe to leave enabled otherwise, since it detects a missing/unconfigured stack and skips cleanly. |
| `security-plugin-check` | `plugin_integrity_check.py` | daily 6:30 AM | enabled | Community-plugin code integrity, HMAC-signed baseline (DPAPI-backed key on Windows). Run once with `--update` to set the initial baseline before relying on its output. |
| `security-integrity` | `integrity_monitor.py` | daily 6:35 AM | enabled | Automation-script and Task Scheduler-task integrity. Run once with `--update` to set the initial baseline before relying on its output. |
| `morning-dashboard` | `morning_dashboard.py` | weekdays 7:00 AM | enabled | Self-contained HTML dashboard: open to-dos, today's meetings, new notes, pipeline health. |
| `vault-lint` | `vault_lint.py --exit-zero` | Mondays 7:00 AM | enabled | Weekly content lint: duplicate/malformed tags, taxonomy drift, near-duplicate notes, frontmatter gaps, broken wikilinks, vault-vs-repo script drift. Read-only — it passes no fixing flags, so it reports and never rewrites. Stdlib-only with no external dependency, but not yet hand-run on Windows hardware; disable this one task if it misbehaves. See [`Vault-Lint.md`](Vault-Lint.md). |

## Architecture

How the Windows layer maps onto the macOS original:

| Concern | macOS | Windows |
|---|---|---|
| Scheduler | `launchd` | Task Scheduler (`Register-Tasks.ps1` + `schedules.psd1`) |
| Notifications | `osascript` toast | `Send-Notification.ps1` — BurntToast if installed, else a log-only fallback |
| Vault path | `~/Obsidian` | `%USERPROFILE%\Obsidian` (override with `$env:OBSIDIAN_VAULT`) |
| Runtime state | `~/.local/share/*` | `%LOCALAPPDATA%\*` |
| State-file permissions | `chmod 0600` | `icacls`: inheritance dropped, owner + `SYSTEM` only (`chmod` alone is a no-op on Windows) |
| Trust-anchor key | Keychain | DPAPI-encrypted file under `%LOCALAPPDATA%` |
| Secrets | `~/dev/secrets/.env` | `%USERPROFILE%\dev\secrets\.env` |
| Interpreter | Homebrew python3 venv | `Templates\Scripts\.venv\Scripts\python.exe` |
| Vault link | symlink (`10-vault-bootstrap.sh`) | directory junction (no admin / Developer Mode needed) |

**The vault junction.** When the repo is cloned somewhere other than
`%USERPROFILE%\Obsidian` (e.g. `%USERPROFILE%\obsidian-template`),
`install.ps1` creates `%USERPROFILE%\Obsidian` as a directory junction
pointing at the clone. Every `~\Obsidian\...` path — scripts, tasks, the
Obsidian app itself — then resolves correctly, and the Python scripts
self-locate the vault root the same way on both platforms
(`Path(__file__).parent.parent.parent.resolve()`, which follows the junction
exactly as it follows the macOS symlink).

**No TCC/Full-Disk-Access scaffolding needed on either platform:** the
meeting-prepopulate consumer only ever reads a plain local drop folder (fed
directly or via a relay — see
[`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md)), never a
TCC-protected cloud-sync mount, so there's nothing scoped-access-wrapper-like
to port. Scripts just run with the user's own permissions.

## Local LLM RAG

`install.ps1 -WithRAG` (or `setup-rag.ps1` directly) installs a fully local
retrieval-augmented-generation stack — nothing leaves the machine:

1. **Ollama**, via `winget`, pulls `llama3.1:8b` (~4.9 GB)
2. **Docker Desktop**, via `winget` if not already present. Its engine runs
   inside WSL2; if WSL2 isn't installed yet, `setup-rag.ps1` tells you to run
   `wsl --install` in an administrative PowerShell and reboot once. If WSL2
   is already there, no reboot is needed — just launch Docker Desktop once
   and accept the license.
3. **Open WebUI**, the official Docker image, on `http://localhost:3000`,
   pointed at the host's Ollama.

One-time manual setup at `http://localhost:3000` (an interactive admin
signup can't be scripted):

1. Create the local admin account (first user to sign up becomes admin).
2. **Workspace → Knowledge → "+" / Create Knowledge Base** → name it
   `Obsidian` → **hit Save**. Open it — the UUID in the URL
   (`.../workspace/knowledge/<uuid>`) is your `OBSIDIAN_COLLECTION_ID`.
3. **Admin Panel → Settings → Authentication → "Enable API Key"** (hidden
   until turned on) → **hit Save at the bottom of the page** (toggling the
   switch alone doesn't persist it). Then **Settings → Account → API
   Keys → create one** for `OPEN_WEBUI_API_KEY` — the key is only shown
   once, so copy it before navigating away.
4. Set `OPEN_WEBUI_URL=http://localhost:3000`, `OPEN_WEBUI_API_KEY`, and
   `OBSIDIAN_COLLECTION_ID` in `.env`, then
   `Enable-ScheduledTask -TaskName rag-sync -TaskPath '\Obsidian\'`.

Easy to miss: several of Open WebUI's settings pages (Authentication, and
the Knowledge Base name/description) don't save on blur — there's a
**Save** button you must click, usually at the bottom of the panel. If a
change doesn't seem to have taken effect (the API Key section reappears
hidden, or the collection name reverts), you likely navigated away before
hitting Save.

`setup-rag.ps1` re-run later (e.g. after a rebuild) checks your `.env` and
Open WebUI's own state and tells you exactly which case you're in: already
configured, genuinely fresh, or an unrecognized existing account in the
Docker volume (pass `-ResetWebUI` to force a clean signup in that last case —
this destroys anything already in that volume).

## Uninstall

`Templates\Scripts\windows\uninstall.ps1` tears down what `install.ps1` set
up — the Windows counterpart to `uninstall.sh`. One line, no separate
execution-policy step (same `-ExecutionPolicy Bypass -File` form as install):

```powershell
# Safe defaults (interactive prompts for anything data-bearing)
powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\uninstall.ps1

# Complete uninstall: everything this repo installed, non-interactive
powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\uninstall.ps1 -All -RemoveApps -PurgeModels -Yes
```

Already bypassing execution policy for the whole window? Call the script
directly instead — same flags apply:

```powershell
.\Templates\Scripts\windows\uninstall.ps1              # interactive; safe defaults
.\Templates\Scripts\windows\uninstall.ps1 -Yes          # non-interactive, safe defaults
.\Templates\Scripts\windows\uninstall.ps1 -All -RemoveApps -PurgeModels -Yes   # complete uninstall
.\Templates\Scripts\windows\uninstall.ps1 -DryRun       # print what would happen, change nothing
```

By default it removes the scheduled tasks, the venv, `%LOCALAPPDATA%` state,
the Send To shortcut, and the `OBSIDIAN_VAULT` env var. It only removes the
`%USERPROFILE%\Obsidian` **junction itself** — the repo underneath is never
touched. It never touches your vault notes, the Obsidian app, or Ollama
models unless you opt in — note that **`-All` alone does not cover those
two**: it sets `-RemoveRAG -RemovePlugins` only. `-RemoveApps`
(winget-installed apps) and `-PurgeModels` (pulled Ollama models) are always
separate switches, on top of `-All`, for a genuinely complete teardown. See
`.\uninstall.ps1 -?` for the full flag list.

`%USERPROFILE%\dev\secrets\.env` is **never** deleted by this script, at any
flag combination — other tools on the machine may share that file, so the
uninstaller doesn't own it and won't touch it. If you want it gone, delete it
by hand.

## Windows on ARM64

Validated on Windows 11 ARM64 (Snapdragon-class) with **native ARM64 Python
3.12**. `install.ps1` runs clean end-to-end, every scheduled job registers and
runs, and Obsidian ships a native Windows-arm64 build. Every dependency in
`requirements.txt` resolves to a native `win_arm64` wheel with no source
builds — including `onnxruntime` and `magika`, the two heaviest compiled ones.

**Podcast transcription uses a different backend here.** `ctranslate2`, the
compiled backend behind `faster-whisper`, publishes no `win_arm64` wheel *and
no sdist*, and `torch` has no `win_arm64` wheel either — so neither
`faster-whisper` nor `openai-whisper` can run on a native ARM64 interpreter.
`requirements.txt` marker-guards `faster-whisper` accordingly:

```
faster-whisper; platform_machine != "ARM64"
```

That guard is load-bearing, not cosmetic. Left unguarded, the requirement is
simply unsatisfiable, and pip responds by backtracking through every older
`faster-whisper` until it reaches versions pinning `av==10.*`/`11.*` — which
have no ARM64 wheels either, so it tries to compile them and dies in a wall of
Cython errors about `av\logging.pyx`. The `av` output is a red herring; the
real cause is `ctranslate2` several steps earlier.

ONNX Runtime *does* ship a native ARM64 build, so `podcast_transcribe.py`
falls back to a third backend, `whisper_onnx.py`, which drives a pre-exported
Whisper ONNX model directly — feature extraction in numpy, greedy decoding
against the merged decoder, `tokenizers` for the vocab. No torch anywhere.
Nothing in it is ARM- or Windows-specific; it is simply the torch-free option,
and it runs wherever ONNX Runtime does.

Backends are tried in order: **MLX** (Apple Silicon) → **faster-whisper**
(anywhere ctranslate2 has a wheel) → **ONNX Runtime**. ONNX is last because
faster-whisper is better optimised where it can be installed at all.

Measured on a Snapdragon X Elite (12 cores, CPU only):

| Model | Speed | ONNX on disk | Check sentence |
|---|---|---|---|
| `base` | 11.7x realtime | ~290 MB | 15/16 words |
| `small` | 4.2x realtime | ~1 GB | 16/16, word-exact |

`small` is the default for this backend — roughly 14 minutes for a one-hour
episode. Override with `--model base` for speed or `--model large-v3-turbo`
for quality; the first run downloads the model. The MLX default
(`large-v3-turbo`) is deliberately *not* reused here, because a good choice
for a GPU backend is a poor one on CPU.

Two caveats worth knowing. Decoding runs in `<|notimestamps|>` mode, so
timestamps land on 30-second window boundaries rather than per utterance —
enough to navigate a transcript, not enough for subtitles. And **ffmpeg must
be on PATH** (`winget install Gyan.FFmpeg`, which puts it there); it decodes
the audio before transcription. `install.ps1` does not install ffmpeg, since
transcription is an optional, manually-run feature with no scheduled task.

Two related notes on the arch markers, both working as intended:

- `mlx-whisper; platform_machine == "arm64"` correctly **skips** here. The
  marker is case-sensitive and Apple-Silicon-only: macOS reports `arm64`
  lowercase, Windows ARM64 reports `ARM64` uppercase.
- The installer's MSVC-runtime step is **skipped** for a native ARM64
  interpreter, since `ctranslate2` was its only consumer. It gates on the
  interpreter's own `sysconfig.get_platform()` rather than the machine's
  architecture, so an x64 Python running under emulation still gets the x64
  redistributable it genuinely needs.

**Why not just run x64 Python under emulation?** That was the original plan,
on the reasoning that these are lightweight background scripts so the
emulation cost is irrelevant. It doesn't hold for transcription specifically:
it is the one genuinely compute-bound job in the workflow, and `ctranslate2`'s
x64 build leans on AVX2/AVX-512, which the emulation layer handles poorly. So
the choice would have been to run *everything* emulated in order to speed up
the one thing emulation is worst at. The ONNX backend keeps the whole
workflow native instead. If you do want the x64 route anyway, install x64
Python, delete `Templates\Scripts\.venv`, and re-run `install.ps1`.

The Snapdragon's NPU is not used yet. `onnxruntime-qnn` (the Qualcomm
execution provider) installs cleanly on this machine, so routing the encoder
to the NPU is a plausible next step — it needs a model quantised and compiled
for Hexagon, which is a larger piece of work than the CPU path.

### RAG on ARM64

The `-WithRAG` stack works on ARM64, but **WSL2 must be installed first** —
see [Prerequisites](#prerequisites). This bites harder here than on x64:
Docker Desktop for Windows on Arm ships only the WSL2 backend, so there is no
Hyper-V backend to fall back on. A fresh ARM64 Windows image has no WSL, so
`-WithRAG` on a clean machine will install Docker Desktop successfully and
then fail when the engine can't start. Run `wsl --install` from an elevated
PowerShell and reboot before you use the switch.

Everything else in the stack is genuinely ARM64-native — verified on a
Snapdragon X Elite (Surface Laptop 7, Windows 11 build 26200):

| Component | ARM64 status |
|---|---|
| Docker Desktop (winget `Docker.DockerDesktop`) | native ARM64 binary |
| Ollama (winget `Ollama.Ollama`) | native ARM64 binary |
| `llama3.1:8b` | pulls and serves normally (~4.9 GB) |
| `ghcr.io/open-webui/open-webui:main` | multi-arch image, publishes `linux/arm64` |

So no part of the RAG stack needs emulation and no image needs a
platform override. Note that Ollama has no Adreno GPU or Hexagon NPU backend,
so inference is **CPU-only** on Snapdragon — expect it to be slower than an
x64 box that can offload to a discrete GPU.

## Known limitations

- **`meeting_prep.py`** (the meeting-notes follow-up enrichment pass) runs
  cleanly on Windows with no meetings scheduled, but has not yet had a live
  validation pass against a real day of individual meetings.
- **`meeting_prepopulate.py`** is fully ported and unit/e2e-tested (11 + 8
  cases) on its pluggable handoff-source design, but wiring up a real
  producer (a relay into its local drop folder, or an MCP transport) is left
  to you — see [`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) for the
  contract, same as the equivalent macOS feature. A cloud-drive-sync-client
  transport (e.g. OneDrive) is deliberately not implemented — see
  `HANDOFF-ARCHITECTURE.md`'s Tier A note for why.
- **`handoff_blob_pull.py`** (the Azure Blob Tier B relay) fails loud with a
  clear message until `HANDOFF_BLOB_ACCOUNT_URL`, `HANDOFF_BLOB_CONTAINER`,
  and `HANDOFF_BLOB_SAS` are set — see
  [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md). Not
  validated against a real Azure Blob Storage account on Windows yet.
- **QuickAdd's folder-picker patch** rewrites a minified call in `main.js`.
  It matches structurally rather than by a fixed string (the fall-through is
  the only `getOrCreateFolder` call whose first argument is its own
  `allowedRoots`), so it survives upstream re-minification — but if a future
  QuickAdd restructures that function, the installer safely skips the patch
  rather than risk corrupting the file (cosmetic only — QuickAdd still works,
  you may just see an extra folder picker in one flow). Both platforms share
  one implementation, `installers/lib/quickadd_patch.py`.
- **`Templates/Scripts/tests/`** — the security-controls pytest suite now
  runs on Windows. Last measured 2026-08-25 on Windows 11 ARM64 at commit
  `75986f8`: **573 passed, 4 skipped, 9 failed, 0 errors** of 586 collected,
  against a macOS reference of 577 passed / 9 skipped on the same suite.
  Collection totals match exactly, so the whole gap is Windows-specific.

  That run was itself a re-measurement. The previous one, at `2d97440`, was
  545 passed / 3 skipped / 4 failed / **33 errors**. The 33 were a single
  fixture redirecting only `HOME`, when this platform derives its state
  directory from `%LOCALAPPDATA%` — so the fixture's own guard correctly
  refused to run rather than write to real state. Clearing it let seven
  previously-inert tests execute and expose a real product bug: RAG-sync
  state keys were built with `str(Path)`, so they were backslash-separated
  here and forward-slash on macOS, and the state file was not portable
  between the two. Failures went 4 to 9 as a result, which was progress
  rather than regression.

  Fixes for every remaining failure landed 2026-08-25 after that measurement,
  so the figure above is again **due a re-measurement**. It is the last result
  actually observed, not a projection. `pytest`
  and `pytest-cov` are deliberately not in `requirements.txt`, so install
  them first, then run from the repo root:

  ```powershell
  .\Templates\Scripts\.venv\Scripts\python.exe -m pip install pytest pytest-cov
  .\Templates\Scripts\.venv\Scripts\python.exe -m pytest Templates\Scripts\tests
  ```

  `run_tests.sh` is macOS-only (`pip3 install --break-system-packages`); the
  invocation above is the Windows equivalent, and needs no `PYTHONPATH` —
  `conftest.py` resolves `Templates/Scripts/` onto `sys.path` itself. Add
  `-rs` to print the skip reasons. The five skips are platform-conditional,
  not disabled tests: four macOS-only branches (Keychain trust anchor, the
  Keychain fallback in `get_api_key`) and one `bash -n` syntax check, which
  skips only when no `bash` is on `PATH` and runs and passes with Git Bash
  there.

## Troubleshooting

- **A venv looks broken** (present but missing `Scripts\python.exe`) — delete
  `Templates\Scripts\.venv` and re-run `install.ps1`.
- **Docker won't start** — `setup-rag.ps1` checks whether WSL2 is present and
  only asks for a reboot if it's genuinely missing; otherwise, just launch
  Docker Desktop once and accept the license, then re-run
  `setup-rag.ps1 -SkipModel`. Reinstalling Docker Desktop right after
  uninstalling it (e.g. a `uninstall.ps1 -RemoveApps` teardown-and-rebuild)
  can fail outright with a generic installer error — that's Docker Desktop's
  own known behavior around leftover driver/service state, not something
  `setup-rag.ps1` can work around; a reboot before reinstalling clears it.
- **`install.ps1` reports Docker Desktop's install failed with "the operation
  was canceled by the user," but nobody was at the keyboard to cancel
  anything** — Docker Desktop's installer requests admin elevation
  (`Expect a prompt`), and that UAC prompt runs on the secure desktop with its
  own timeout; if you'd walked away by the time it appeared, Windows
  auto-cancels it unanswered and the install step fails — no one actually
  declined it. Step 06 installs Docker Desktop immediately after Obsidian,
  before the long unattended stretch (plugins, pip installs, the Ollama model
  pull), specifically so this prompt lands early while you're still there —
  but if you still miss it (or walked away before even that), just re-run:
  `.\Templates\Scripts\windows\setup-rag.ps1 -SkipModel`.
- **`docker` (or `ollama`) not found, even in a run that should see a prior
  install** — `install.ps1` and `setup-rag.ps1` both call `Sync-Path`
  (`common.ps1`) at startup specifically so this doesn't happen: a
  PowerShell process's `PATH` is a snapshot from its own startup, and a
  *child* process (including one spawned by
  `powershell -ExecutionPolicy Bypass -File ...` from an old, long-lived
  console) inherits that stale snapshot rather than re-reading the registry
  — so a tool a *previous, separate* run of these scripts installed can be
  invisible to `Get-Command` in a later run even though it's genuinely
  installed. If you still hit this (e.g. in your own ad-hoc commands, not
  through these scripts), refresh manually:
  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
  ```
- **`docker info` fails once right after Docker Desktop reports "Engine
  running"** — the engine can take a few more seconds to finish initializing
  even after the tray icon looks ready; just retry.
- **`setup-rag.ps1` warns it couldn't reach Open WebUI right after a fresh
  image pull** — the container needs a few extra seconds to boot the first
  time. Not a failure; wait a moment and open http://localhost:3000 by hand.
- **Open WebUI shows a plain "Sign in" screen you don't recognize** — that
  container's Docker volume already has an admin account from an earlier
  run (volumes outlive `uninstall.ps1` unless you pass `-RemoveRAG`). Run
  `setup-rag.ps1 -SkipModel -ResetWebUI` for a guaranteed-clean signup.

## Related

- [`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) — the meeting-prepopulate
  handoff-source contract (drop folder / MCP)
  there is no longer a Windows-specific difference here
- [`Voice Notes (Optional).md`](Voice%20Notes%20%28Optional%29.md) — the
  sibling mail-drop pipeline behind the `voice-cleanup` task
- [`YouTube Summarizer.md`](YouTube%20Summarizer.md) — the on-demand
  `youtube_summarize.py` CLI (no scheduled task on either platform)
- [`Local LLM with Obsidian Vault RAG.md`](Local%20LLM%20with%20Obsidian%20Vault%20RAG.md) —
  the RAG design in full, including the macOS Tailscale remote-access option
- [`Security-Harness.md`](Security-Harness.md) — what the security
  monitors defend against and how to respond to alerts
- `Templates/Scripts/windows/README.md` — quick file-by-file reference for
  the PowerShell layer itself
