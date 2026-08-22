---
tags:
  - note
  - setup
  - launchd
classification: public
---

# LaunchAgents — Setup & Migration

This page covers the core macOS LaunchAgents under `Templates/Scripts/` by hand. They are **not loaded automatically** when you clone the template, and they do **not** survive a Migration Assistant transfer to a new Mac.

`./install.sh` installs all of these for you — components `40-tagger`, `41-voice-cleanup`, `39-source-mail`, `47-podcast` and `50-llm-rag`. The manual steps below are the fallback for a partial setup, and the reference for what the installer actually does.

| Agent | Plist | Cadence | Purpose |
|---|---|---|---|
| Semantic auto-tagger | `com.tag-clippings.plist` | every 30 minutes | runs `tag_clippings.py` to apply taxonomy-based tags to new/changed notes in `Clippings/`, `Creations/`, `Meetings/` |
| Source mail pull | `com.obsidian.source-mail-pull.plist` | every 300 seconds | runs `source_mail_pull.py` to drain signed capture drops from a dedicated intake mailbox into `~/SourceMedia/` |
| Voice cleanup watcher | `com.voice-cleanup.plist` | continuous (`KeepAlive`), polls every 10 seconds | runs `voice_cleanup.py` to polish raw dictation files from `~/SourceMedia/VoiceInput/` into `Creations/` |
| Podcast watcher | `com.obsidian.podcast-watch.plist` | every 900 seconds | runs `podcast_watch.py` to transcribe drops in `~/SourceMedia/PodcastInput/` into `Clippings/` |
| RAG sync | `com.obsidian-rag-sync.plist` | daily at 03:15 | runs `obsidian-rag-sync.py` to push the vault into an Open WebUI Knowledge collection so a local Ollama-backed LLM can use the vault as RAG context |

Each agent is independent — you can install only the ones you actually use. The tagger, source-mail-pull and voice-cleanup are always-on automation; rag-sync only matters if you've stood up the local LLM stack ([[Local LLM with Obsidian Vault RAG]]). The vault ships other agents too (news clipping, strip-ads, meeting pre-population, morning dashboard, group photos, vault lint, the security harness); those are documented on their own pages and installed by their own components.

---

## Prerequisites (install once)

Before either agent will run successfully, the host Mac needs:

1. **Homebrew Python 3** at `/opt/homebrew/bin/python3` — installed via `brew install python@3.13` or by running `~/dev/scripts/setup-mac.sh`.
2. **Per-vault venv** at `Templates/Scripts/.venv/` with `anthropic`, `pyyaml`, `python-dotenv`, and `requests` installed:

   ```bash
   cd ~/Obsidian/Templates/Scripts
   /opt/homebrew/bin/python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. **Required secrets in `~/dev/secrets/.env`**:

   ```bash
   mkdir -p ~/dev/secrets
   cat > ~/dev/secrets/.env <<'ENVEOF'
   # Required by tag_clippings.py and voice_cleanup.py
   ANTHROPIC_API_KEY=sk-ant-...

   # Required by obsidian-rag-sync.py (only if you run the RAG agent)
   OPEN_WEBUI_API_KEY=sk-...
   OBSIDIAN_COLLECTION_ID=<uuid-of-your-WebUI-knowledge-collection>

   # Optional overrides — defaults shown
   # OPEN_WEBUI_URL=http://localhost:3000
   # OBSIDIAN_VAULT=~/Obsidian
   ENVEOF
   chmod 600 ~/dev/secrets/.env
   ```

   All three scripts call `load_dotenv(...)` from this path. If you keep secrets elsewhere, edit the path in each script (`tag_clippings.py`, `voice_cleanup.py`, `obsidian-rag-sync.py`).

4. **Log directory** at `~/Library/Logs/`:

   ```bash
   mkdir -p ~/Library/Logs
   ```

   Both plists write to this location (`~/Library/Logs/tag-clippings.{log,err}` and `~/Library/Logs/voice-cleanup.{log,err}`). macOS's Console.app surfaces these in its "User Reports" sidebar. If your plist is older and still points to `/tmp/`, edit it before loading — `/tmp/` clears on every reboot.

---

## Install: Semantic Auto-Tagger

```bash
# 1) Edit the plist: replace YOUR_USERNAME with your macOS short username
sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.tag-clippings.plist

# 2) Copy into LaunchAgents/ and load
cp ~/Obsidian/Templates/Scripts/com.tag-clippings.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tag-clippings.plist

# 3) Verify
launchctl list | grep tag-clippings
# expected: <PID>  0  com.tag-clippings  (PID present, exit code 0)

# 4) Watch the first run land (RunAtLoad triggers immediately)
tail -f ~/Library/Logs/tag-clippings.log
```

The agent fires once on load (`RunAtLoad: true`) and then at :00 and :30 each hour (`StartCalendarInterval`).

---

## Install: Source Mail Pull

This is the transport the phone-originated pipelines sit on — voice notes and podcasts all arrive as signed email rather than through a cloud-drive client. Install it before the watchers it feeds. The reasoning, the message format and the producer-side Shortcut are in [[Source-Mail-Transport]]; this is the agent half.

1. **Create the dedicated mailbox** and put its credentials in `~/dev/secrets/.env`:

   ```bash
   SOURCE_MAIL_USER=intake-xxxx@gmail.com
   SOURCE_MAIL_APP_PASSWORD=abcdefghijklmnop
   SOURCE_MAIL_TOKEN=<40 random alphanumerics, no symbols>
   SOURCE_MAIL_ALLOWED_SENDERS=you@gmail.com
   ```

   Use a separate account, not a `+alias` on your main one, and generate one App Password per machine so a lost machine can be revoked on its own. The token must be alphanumeric — it has to survive quoted-printable encoding, mail line-wrapping, iOS smart punctuation and dotenv parsing, and every one of those failures is silent.

2. **Verify before wiring up a phone**, which separates "the transport is broken" from "the Shortcut is broken":

   ```bash
   cd ~/Obsidian/Templates/Scripts
   .venv/bin/python3 source_mail_pull.py --emit news "https://example.com/a"
   .venv/bin/python3 source_mail_pull.py --once --dry-run
   ```

   `--emit` prints a correctly signed body; `--once --dry-run` parses and verifies whatever is in the mailbox without writing anything or marking mail read.

3. **Install and load the agent**:

   ```bash
   sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.obsidian.source-mail-pull.plist
   cp ~/Obsidian/Templates/Scripts/com.obsidian.source-mail-pull.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.obsidian.source-mail-pull.plist
   launchctl list | grep source-mail-pull
   ```

The agent runs every 300 seconds (`StartInterval`) — an IMAP fetch of a few small text messages is cheap. Logs land in `~/Library/Logs/source-mail-pull.{log,err}`; rejections are logged there and nowhere else.

---

## Install: Podcast Watcher (Optional)

Drains `~/SourceMedia/PodcastInput/` — audio files, or link files holding an episode / RSS / Apple Podcasts URL — and writes verbatim transcripts into `Clippings/`, where the tagger treats them like any other clip. Only worth installing if you actually want transcripts; `podcast_transcribe.py` works on demand as a CLI without it.

1. **Install `ffmpeg` and a transcription backend.** `./install.sh --only 47-podcast` does both; by hand:

   ```bash
   brew install ffmpeg
   cd ~/Obsidian/Templates/Scripts
   .venv/bin/pip install -r requirements.txt   # faster-whisper, plus mlx-whisper on Apple Silicon
   ```

   Backends are tried in order: MLX Whisper (Apple Silicon GPU), faster-whisper (CPU), then ONNX Runtime. The first run downloads a model (~1.5 GB for `large-v3-turbo`).

2. **Smoke-test the CLI before scheduling anything**, since a bad backend install fails slowly:

   ```bash
   .venv/bin/python3 podcast_transcribe.py "https://example.com/episode.mp3"
   ```

3. **Install and load the agent**:

   ```bash
   sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.obsidian.podcast-watch.plist
   cp ~/Obsidian/Templates/Scripts/com.obsidian.podcast-watch.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.obsidian.podcast-watch.plist
   launchctl list | grep podcast-watch
   ```

The agent ticks every 900 seconds rather than the 300 the cheap watchers use, and takes **one** episode per run behind a single-instance lock: transcription costs real CPU for real minutes (roughly a quarter of the audio's duration), so a queue drains one episode per tick instead of seizing the machine on the first one. Processed drops are moved to `done/` or `failed/` (with a sibling `.error.log`), never deleted — a dropped audio file may be your only copy of a recording.

---

## Install: Voice Cleanup Watcher

This agent has additional setup beyond just loading the plist:

1. **Create the config from the template**:

   ```bash
   cp ~/Obsidian/Templates/Scripts/voice_cleanup_config.yaml.example \
      ~/Obsidian/Templates/Scripts/voice_cleanup_config.yaml
   ```

   Leave `watch_folder` commented out. Unset, it resolves through `source_media.drop_dir("voice")` — the same function `source_mail_pull.py` writes through, so the watcher and the transport cannot drift onto different folders. The resolved default is `~/SourceMedia/VoiceInput`.

2. **Wire up the transport** so dictation actually arrives: the iPhone Shortcut signs a payload and mails it, and `com.obsidian.source-mail-pull` (below) drops it into `~/SourceMedia/VoiceInput/`. See [[Source-Mail-Transport]] for the producer side and [[Voice Notes (Optional)]] for the pipeline design.

   No Full Disk Access is needed: `~/SourceMedia/` is an ordinary home-directory folder. (Run `python3 Templates/Scripts/source_media.py --apply` to create the drop folders if they are missing.)

3. **Install and load the agent**:

   ```bash
   sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.voice-cleanup.plist
   cp ~/Obsidian/Templates/Scripts/com.voice-cleanup.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.voice-cleanup.plist
   launchctl list | grep voice-cleanup
   ```

The agent runs continuously (`KeepAlive: true`) and polls every 10 seconds.

---

## Install: RAG Sync (Optional)

Only relevant if you're running the [[Local LLM with Obsidian Vault RAG]] stack — Ollama serving models locally and Open WebUI hosting the chat UI plus a Knowledge collection. Skip this section otherwise.

Prerequisites beyond the shared list above:

- Open WebUI running and reachable at `http://localhost:3000` (default Docker setup).
- A Knowledge collection already created in Open WebUI; copy its UUID into `OBSIDIAN_COLLECTION_ID` in `~/dev/secrets/.env`.
- An Open WebUI API key with collection-write permission; into `OPEN_WEBUI_API_KEY` in `~/dev/secrets/.env`.

```bash
# 1) Edit the plist (replace YOUR_USERNAME) and load
sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.obsidian-rag-sync.plist
cp ~/Obsidian/Templates/Scripts/com.obsidian-rag-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.obsidian-rag-sync.plist

# 2) Verify
launchctl list | grep obsidian-rag-sync
# expected: -  0  com.obsidian-rag-sync  (PID dash because nothing scheduled until 03:15)

# 3) Smoke-test by running the script manually
~/Obsidian/Templates/Scripts/.venv/bin/python3 \
    ~/Obsidian/Templates/Scripts/obsidian-rag-sync.py --dry-run
# Expect: "scanned N markdown files (X indexable, Y below threshold)" and a
# diff line. Drop --dry-run to actually push.
```

State lives at `~/.local/share/obsidian-rag-sync/state.json` and is auto-backed up before each real run as `state.bak.YYYYMMDD-HHMMSS.json`. The script enforces deletion ceilings — if a run wants to remove >50 files (or >5% of indexed corpus, whichever's larger), it aborts with exit code 3 unless `--allow-bulk-delete` is passed. That's the safeguard catching mass moves/renames; investigate the diff before overriding.

The agent runs once daily at 03:15 (`StartCalendarInterval`).

---

## Post-migration restoration

After moving to a new Mac via Migration Assistant or Time Machine restore, your loaded LaunchAgents will be **gone** even though the plist source files in `~/Obsidian/Templates/Scripts/` came along. Why:

- macOS launchd's job database is per-machine. The `launchctl load` registration does not migrate.
- On Sonoma+ / Apple Silicon, Migration Assistant sometimes drops files out of `~/Library/LaunchAgents/` because the new System Settings → General → Login Items model requires explicit re-approval.
- `setup-mac.sh` handles dev-environment bootstrap (Homebrew, Python, Claude Code) but **does not install LaunchAgents** — that's still manual.
- TCC permissions (Full Disk Access, etc.) do not migrate either. This no longer affects voice-cleanup, whose drop folder is an ordinary home-directory path, but it still bites any legacy install left pointing at iCloud Drive.
- The `obsidian-rag-sync` agent depends on Open WebUI (running in Docker) and Ollama. Both must be re-installed and started on the new Mac before the agent will succeed; the Open WebUI Knowledge collection's UUID will be different on a fresh stack, so update `OBSIDIAN_COLLECTION_ID` in `.env`.

**Restoration checklist for a migrated Mac:**

```bash
# 1) See what survived
ls ~/Library/LaunchAgents/
launchctl list | grep -v com.apple

# 2) Re-create the per-vault venv (Python path / arch may have changed)
cd ~/Obsidian/Templates/Scripts
rm -rf .venv
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) Confirm the secrets file is there and has the right keys
ls ~/dev/secrets/.env
grep -c "ANTHROPIC_API_KEY\|OPEN_WEBUI_API_KEY\|OBSIDIAN_COLLECTION_ID" ~/dev/secrets/.env

# 4) Confirm the log directory exists
mkdir -p ~/Library/Logs

# 5) Re-install the LaunchAgents you actually use, using the steps above

# 6) For voice-cleanup: confirm voice_cleanup_config.yaml exists
#    (only the .yaml.example is committed to the repo)
# 7) For source-mail-pull: confirm SOURCE_MAIL_USER, SOURCE_MAIL_APP_PASSWORD,
#    SOURCE_MAIL_TOKEN and SOURCE_MAIL_ALLOWED_SENDERS are in .env, then
#    verify with `source_mail_pull.py --once --dry-run`. App Passwords are
#    per-machine, so a new Mac needs its own — generate and revoke the old one.
# 8) For obsidian-rag-sync: re-stand-up Open WebUI + Ollama, recreate the
#    Knowledge collection, update OBSIDIAN_COLLECTION_ID in .env, and
#    consider running --dry-run first since the new collection will look
#    empty and trigger a giant "new files" diff
```

---

## Verifying everything is healthy

```bash
# All agents loaded?
launchctl list | grep -E "tag-clippings|source-mail-pull|voice-cleanup|podcast-watch|obsidian-rag-sync"

# Logs growing?
ls -la ~/Library/Logs/tag-clippings.* \
       ~/Library/Logs/source-mail-pull.* \
       ~/Library/Logs/voice-cleanup.* \
       ~/Library/Logs/podcast-watch.* \
       ~/Library/Logs/obsidian-rag-sync.*

# tag-clippings ran cleanly recently?
tail -10 ~/Library/Logs/tag-clippings.log
# expected: a "Done! Updated: N | Skipped (unchanged): M | Deferred (recent edit): D | Total: T" line within the last 30 minutes

# voice-cleanup is watching?
tail -10 ~/Library/Logs/voice-cleanup.log

# obsidian-rag-sync ran cleanly last night?
tail -10 ~/Library/Logs/obsidian-rag-sync.err
# expected: a "sync complete. errors=0" line from the most recent 03:15 run
```

---

## Troubleshooting

**Logs are missing entirely.** The agent isn't loaded, or it's loaded but never fired. Confirm with `launchctl list | grep tag-clippings`. If empty, re-run `launchctl load -w ~/Library/LaunchAgents/com.tag-clippings.plist` (the `-w` flag persists the registration through reboots).

**`launchctl list` shows the agent with a non-zero exit code.** The script crashed on its last run. Check `~/Library/Logs/tag-clippings.err` for the traceback. Common causes:

- `ModuleNotFoundError: anthropic` (or `yaml`, `dotenv`) — the venv at `Templates/Scripts/.venv/` is missing dependencies. Recreate it per Prerequisites step 2.
- `Error: ANTHROPIC_API_KEY not found` — `~/dev/secrets/.env` is missing or unreadable. Check the path and permissions (`chmod 600 ~/dev/secrets/.env`).
- `anthropic.AuthenticationError: Unauthorized` — your API key is rejected. Confirm the key is valid and the script is reading the right `.env` file.

**A dictation never appears, and the voice-cleanup log shows nothing.** The watcher only drains a folder — it does not fetch mail. Check `~/Library/Logs/source-mail-pull.err` first: a drop rejected on the sender allowlist or the HMAC is logged there and nowhere else, and the sending device gives no indication anything went wrong. A rejected message is marked `\Seen` and is not retried; just re-share from the device.

**Voice cleanup log is empty.** Check that `~/SourceMedia/VoiceInput/` exists (`python3 Templates/Scripts/source_media.py` reports where each kind resolves, `--apply` creates them) and that the mail transport is accepting drops — see `~/Library/Logs/source-mail-pull.err`.

**Pause the tagger.** `launchctl unload ~/Library/LaunchAgents/com.tag-clippings.plist`. Resume with `launchctl load`. To remove it entirely: `launchctl unload ~/Library/LaunchAgents/com.tag-clippings.plist && rm ~/Library/LaunchAgents/com.tag-clippings.plist`.

**`obsidian-rag-sync` exits with code 3 / `ABORT: safeguards triggered`.**
The deletion ceiling tripped — usually because you reorganized the vault between runs (renamed/moved >50 files in one shot). Inspect the `would-delete:` list in `~/Library/Logs/obsidian-rag-sync.err`; if it's expected, run once with `--allow-bulk-delete`:

```bash
~/Obsidian/Templates/Scripts/.venv/bin/python3 \
    ~/Obsidian/Templates/Scripts/obsidian-rag-sync.py --allow-bulk-delete
```

The script auto-backs up `state.json` before mutating, so if you change your mind there's a `state.bak.YYYYMMDD-HHMMSS.json` to restore from.

**`obsidian-rag-sync` reports `OPEN_WEBUI_API_KEY env var not set` on a manual run.**
The script reads from `~/dev/secrets/.env` via `load_dotenv()`. Confirm the key is there (`grep OPEN_WEBUI_API_KEY ~/dev/secrets/.env`) and that the venv has `python-dotenv` installed (`.venv/bin/pip show python-dotenv`).

**A specific note got tagged with only the base tag (`clippings`) and no topical tags.** Older versions of `tag_clippings.py` could fail to parse Claude's response if Claude appended reasoning text after the JSON array. Affected files end up tracked as "processed" even though they have no real tags. Fix: delete those entries from `~/Obsidian/.tag_tracking.json` and the next run will re-tag them with the current parser, or run `tag_clippings.py --force` once to re-tag everything.

---

## Related

- [[Semantic Auto-Tagger Setup]] — design rules and on-demand prompts for the tagger LaunchAgent installed above
- [[Source-Mail-Transport]] — the signed-email transport behind source-mail-pull, and the producer-side Shortcut
- [[Voice Notes (Optional)]] — iPhone Shortcut + dictation pipeline design
- [[Obsidian Configuration Guide]] — vault, plugin, and template setup
