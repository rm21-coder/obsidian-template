---
tags:
  - tools
  - youtube
  - setup
  - optional
classification: public
---

# YouTube Summarizer (Optional)

A one-shot command-line tool that turns a YouTube video into a summarized markdown note. It pulls the video's captions via `yt-dlp`, sends the transcript to Claude for summarization, and writes the result into `~/Obsidian/Clippings/YouTube/` using the same frontmatter convention as Obsidian Web Clipper notes — so it's treated identically by the [[Semantic Auto-Tagger Setup|auto-tagger]] and everything downstream.

This is **optional** and **on-demand** — unlike the [[Voice Notes (Optional)|voice-notes]] or podcast pipelines, there is no background watcher. You run it by hand (or wire it into your own automation) whenever you want a video summarized.

## What it does

```bash
# macOS
Templates/Scripts/.venv/bin/python3 Templates/Scripts/youtube_summarize.py "https://www.youtube.com/watch?v=XXXX"
```
```powershell
# Windows
Templates\Scripts\.venv\Scripts\python.exe Templates\Scripts\youtube_summarize.py "https://www.youtube.com/watch?v=XXXX"
```

Naming the venv interpreter explicitly, as above, is still the clearest way to invoke it. But you no longer have to: the script re-execs itself into `Templates/Scripts/.venv` when it finds it is running outside a virtualenv. That matters because the shebang is `#!/usr/bin/env python3`, so any launcher with the stock macOS `PATH` — an Obsidian plugin, a `launchd` job, a `.app` wrapper — resolves to `/usr/bin/python3`, Apple's system Python 3.9, which carries none of the dependencies in `requirements.txt`. Before the guard existed that failed with `ModuleNotFoundError: No module named 'dotenv'` on the first third-party import. `tag_clippings_rag.py` carries the same guard.

1. Fetches the video's metadata and caption tracks via `yt-dlp` (no video/audio download — captions only).
2. Picks the best available English track: manual captions first, then auto-generated, preferring plain `en` over `en-*` variants.
3. Sends the transcript through [`llm_endpoint.py`](../Templates/Scripts/llm_endpoint.py) (`claude-sonnet-5` by default) with a fixed prompt asking for a short overview, a "Key takeaways" bullet list, a "Notable points" section, and 3–7 suggested topic tags. The call is metered in `usage_log`, so it shows up in the morning dashboard's cost view alongside the tagger and classifier.
4. Writes a markdown note with frontmatter (`title`, `source`, `author`, `published`, `duration`, `description`, `classification: public`, `tags`) into `Clippings/YouTube/`. The `tags` list always includes `youtube` plus whatever the model suggested.
5. Skips videos that already have a note in the output directory — safe to re-run over a list without duplicating work.

### Playlists

```bash
youtube_summarize.py --playlist "https://www.youtube.com/playlist?list=YYYY"
youtube_summarize.py --max 5 --playlist "https://www.youtube.com/playlist?list=YYYY"   # cap at 5 videos
```

Each video in the playlist is processed independently; one failure doesn't stop the rest. The summary line at the end (`done: N ok, M failed`) and exit code 2 tell you if anything needs attention.

### Flags

| Flag | Purpose |
|---|---|
| `--playlist` | treat the URL as a playlist and process every video in it |
| `--model NAME` | model to use (default `claude-sonnet-5`; or set `YOUTUBE_MODEL`) |
| `--out DIR` | output directory (default `~/Obsidian/Clippings/YouTube`) |
| `--max N` | cap a playlist run at N videos (default: no cap) |
| `--dry-run` | fetch the transcript but skip the model call and the file write — useful for checking a video has usable captions before spending an API call |
| `--verbose` | log progress to stderr |

### Exit codes

`0` success · `1` fatal error (missing credential, no transcript available, or the API call failed on a single-video run) · `2` partial failure (one or more videos failed in a `--playlist` run; the rest still succeeded).

## Setup

**No API key of its own.** Summarization goes through
[`llm_endpoint.py`](../Templates/Scripts/llm_endpoint.py), the same endpoint and
credential the auto-tagger already uses — `ANTHROPIC_API_KEY`, or
`LLM_BASE_URL` + `LLM_API_KEY_NAME` on an institutional gateway. If the tagger
works, this works.

This script previously called Gemini, and the reasoning for the change is worth
recording because the obvious objection is wrong. Google produces most of these
captions, so it seems natural that Gemini would summarize them better — but
`yt-dlp` fetches the caption track and this script parses it to plain text
locally. The model receives text and never the URL, so Google had no privileged
access to the transcript and no advantage from having generated it. On a blind
three-arm comparison over real ASR text (Gemini, Claude with this prompt, and
Claude with the instructions moved to a system prompt), Claude with this prompt
was preferred, so the second API key bought nothing and was dropped.

Two details in that comparison are now pinned by tests, because both cost a
production failure to learn:

- The prompt stays a **single user turn**. Moving it to a system prompt is the
  obvious refactor and it measurably regressed the output — that arm was the
  only one to break the format spec, opening with a title heading the prompt
  forbids.
- **No `temperature`.** Current models reject it, and through a LiteLLM-style
  gateway it surfaces as a 400 reading "`temperature` is deprecated for this
  model", which looks like a gateway problem and sends you debugging the wrong
  layer.

`yt-dlp` ships in `requirements.txt` and is already installed in the per-vault venv by `install.sh` / `install.ps1`.

## Known limitations

- **No transcript, no summary.** If a video has neither manual nor auto-generated English captions, the script raises `no transcript available` and moves on (or exits 1 for a single video). There's no audio-transcription fallback wired in here — `podcast_transcribe.py` can produce a plain transcript from an audio URL, but it's a separate, podcast-oriented, first-step tool (no vault write, no summary, no frontmatter) rather than a drop-in fallback for this script.
- **No scheduling.** There's no LaunchAgent/Task Scheduler entry for this script — it's meant to be run when you actually want a specific video summarized, not polled against a feed. If you want it automated (e.g., a "watch this channel" pipeline), that's a straightforward `yt-dlp --flat-playlist` + cron/Task Scheduler wrapper you'd build yourself; nothing bundled does that today.
- **English-only caption preference.** The track-selection logic only looks for `en`/`en-*` tracks; videos without an English caption track (manual or auto) of some kind will fail even if other languages are available.

## Troubleshooting

- **`[yt-sum] error: Connection error.`** — the old symptom, and the reason the
  check below exists. The Anthropic SDK raises `APIConnectionError` with that
  exact three-word message whenever it cannot open a connection, and this script
  printed it verbatim. A gateway that was merely unreachable therefore looked
  like a broken credential or a misconfigured endpoint, which is a long way from
  the actual cause.
- **`[yt-sum] error: gateway host ... did not resolve`** — what you get instead
  now, from the preflight in
  [`llm_endpoint.py`](../Templates/Scripts/llm_endpoint.py). An institutional
  gateway is usually an internal-only address that resolves on the campus
  network and nowhere else, so the overwhelmingly common cause is being off the
  VPN. Connect and retry. Confirm independently with `host <gateway-hostname>`,
  or with `python3 llm_endpoint.py`, which prints the resolved endpoint on
  stdout and its reachability on stderr. Stock `api.anthropic.com` installs are
  never preflighted — a resolution failure there means the machine has no
  internet, and the VPN advice would send you the wrong way. If your egress is
  proxied the proxy resolves the hostname rather than you, so the check stands
  down automatically on any `HTTPS_PROXY`/`ALL_PROXY`; `LLM_SKIP_PREFLIGHT=1`
  disables it outright for a split-DNS setup where local resolution genuinely
  cannot predict the call.

## Related

- [[Semantic Auto-Tagger Setup]] — tags the note on its next 30-minute pass
- [[Voice Notes (Optional)]] — sibling on-demand-vs-watcher comparison: iPhone dictation pipeline
- [[Windows Setup]] — Windows-specific notes (no Keychain fallback; venv invocation)
