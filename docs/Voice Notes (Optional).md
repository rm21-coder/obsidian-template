---
tags:
  - note
  - setup
  - optional
classification: public
---

# Voice Notes (Optional)

This is an **optional** workflow. It is not required to use the vault or the [[Semantic Auto-Tagger Setup|auto-tagger]] — but if you want to dictate notes from your phone and have them land in `Creations/` as tagged markdown, this page describes how.

The current reference setup is a Python LaunchAgent that polls a local drop folder for new dictation files and uses the Claude API to clean each one before writing it into `Creations/`. The semantic auto-tagger LaunchAgent then tags the cleaned note on its next 30-minute pass.

Dictation reaches that folder by **mail**, not by iCloud Drive — the phone signs a payload and emails it to a dedicated intake mailbox, and [`source_mail_pull.py`](Source-Mail-Transport.md) drops it into `~/SourceMedia/VoiceInput/`. That page covers the transport in full; this one covers what happens once a `.txt` lands.

---

## Reference architecture

```
iPhone Shortcut ("Ask for Input" → Scriptable signs → Send Email)
        │
        ▼
   dedicated intake mailbox
        │ (IMAP, every 300 seconds)
        ▼
   com.obsidian.source-mail-pull  →  source_mail_pull.py
        │ verifies HMAC + sender, generates the filename locally
        ▼
   ~/SourceMedia/VoiceInput/*.txt
        │ (every 10 seconds)
        ▼
   com.voice-cleanup launchd agent  →  voice_cleanup.py
        │ cleans + polishes (Claude API), writes polished .md, deletes raw .txt
        ▼
   ~/Obsidian/Creations/YYYY-MM-DD_HHMMSS.md
        │ (next 30-minute LaunchAgent pass)
        ▼
   com.tag-clippings    ← Python LaunchAgent
        │ applies taxonomy-based tags
        ▼
   Tagged, queryable, first-class note
```

Worst-case latency from speaking to a polished note is the mail pull (≤5 min) plus the watcher poll (≤10 s), then up to 30 minutes for tags.

### Pieces you need

| Piece | Purpose |
|-------|---------|
| iPhone Shortcut ("Obsidian Note") | `Ask for Input` → sign with Scriptable → `Send Email` as a `type: voice` drop |
| A dedicated intake mailbox + `SOURCE_MAIL_*` in `~/dev/secrets/.env` | The transport; see [[Source-Mail-Transport]] |
| `com.obsidian.source-mail-pull` | Drains the mailbox into `~/SourceMedia/VoiceInput/` |
| `ANTHROPIC_API_KEY` in `~/dev/secrets/.env` | Used by `voice_cleanup.py` for the polish step |
| `Templates/Scripts/voice_cleanup.py` + `com.voice-cleanup.plist` | Bundled in this template |
| `~/Obsidian/Creations/` | Where polished voice notes land |

No iCloud Drive, and so no Full Disk Access: `~/SourceMedia/` is an ordinary folder in your home directory, which the venv's python can read without a TCC grant. On Windows that also means Apple iCloud for Windows is not needed at all. The old iCloud fallback was removed on 2026-08-18 — a fallback path preferred whenever it exists is one that silently wins over the real folder, which is worse than not having it.

### Why Python (and not a Claude desktop scheduled task)

Voice cleanup needs to run constantly at low cost. The Python script polls the drop folder every 10 seconds inside one long-lived process and costs one Claude API call per voice note. A Claude desktop scheduled task would spin up a fresh session on every poll — at cron's 1-minute minimum that's 1,440 sessions/day versus ~1 API call per actual voice note. The token-cost asymmetry is too large.

The same logic applies to the tagger: the [[Semantic Auto-Tagger Setup|tagger]] is also a Python LaunchAgent — running continuously is cheap with direct API calls, and a 30-minute cadence keeps per-tick overhead low while still tagging notes automatically.

### Activation

See [[LaunchAgents — Setup & Migration]] for full prerequisites (per-vault venv, `~/dev/secrets/.env`, `~/Library/Logs/`, Full Disk Access, post-migration restoration). The short version:

```bash
# 1) Create the config from the example
cp Templates/Scripts/voice_cleanup_config.yaml.example \
   Templates/Scripts/voice_cleanup_config.yaml
# Leave watch_folder commented out. Unset, it resolves through
# source_media.drop_dir("voice") — the same function the mail transport
# writes through, so the two cannot drift onto different folders.

# 2) Create the per-vault venv and install dependencies
cd ~/Obsidian/Templates/Scripts
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) Edit the plist (replace YOUR_USERNAME) and load
sed -i '' "s/YOUR_USERNAME/$USER/g" com.voice-cleanup.plist
cp com.voice-cleanup.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.voice-cleanup.plist
```

The script logs to `~/Library/Logs/voice-cleanup.log` and `~/Library/Logs/voice-cleanup.err`. Tail those if a voice note doesn't appear. (Older copies of the plist logged to `/tmp/`, which clears on reboot — update the plist if yours still points there.)

### Prerequisites worth flagging

- **Nothing lands until the transport is wired up.** `voice_cleanup.py` only drains a folder; it does not fetch mail. If notes never appear, check `~/Library/Logs/source-mail-pull.err` before `voice-cleanup.log` — a drop rejected on the sender allowlist or the HMAC gives the phone no indication anything went wrong.
- **The signing token is per-device.** Scriptable's Keychain does not sync, so a second phone or tablet must run the signing script standalone once before its Shortcut can send anything. See [[Source-Mail-Transport]].
- **An unreachable gateway pauses the watcher; it does not stop it.** If you route through an institutional gateway (`LLM_BASE_URL`), that hostname usually resolves only on the campus VPN. Off the VPN the watcher logs one `Paused —` line, leaves queued drops untouched, and picks up on its own when the endpoint comes back — no reload needed. It logs the outage once rather than once per poll, so a long trip does not bury the log. Under `--once` (the Windows scheduling, and any manual run) the same condition is a clean skip with exit 0 instead: a one-shot run has no later cycle to recover into.
- **A drop that keeps failing is set aside, never deleted.** A raw file is only removed once its note is written, so a permanent fault — a retired model id, a malformed drop — would otherwise be retried on every poll and block everything queued behind it. After three consecutive failures the file is renamed to `<name>.txt.failed` and the queue moves on. Nothing dictated is lost: fix the cause, rename it back to `.txt`, and it will be picked up on the next pass. Grep the err log for `Quarantined` to find them.

## Lightweight alternatives (no LaunchAgent, no Python)

You don't have to automate anything to get value from `Creations/`:

- **Dictate straight into Obsidian Mobile.** Use the phone's built-in dictation, save the note into `Creations/` by hand. The tagger LaunchAgent will tag it on its next 30-minute pass.
- **Paste Otter.ai / Voice Memos transcripts.** Copy the text, create a new note in `Creations/`, paste.
- **Any third-party transcription service that drops a file into the vault.** As long as a finished `.md` ends up in `Creations/`, the rest of the vault treats it identically.

---

## Related

- [[Source-Mail-Transport]] — how the dictation gets from the phone to `~/SourceMedia/VoiceInput/`
- [[Semantic Auto-Tagger Setup]] — the LaunchAgent that tags whatever lands in `Creations/`
- [[Markitdown Dropper]] — sibling pipeline for converting Word/PDF documents into the same `Creations/` folder
- [[Obsidian Configuration Guide]] — general vault setup
