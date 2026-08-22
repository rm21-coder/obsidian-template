---
tags:
  - AI
  - Ollama
  - OpenWebUI
  - RAG
  - setup
  - optional
classification: public
---

# Local LLM with Obsidian Vault as RAG (Optional)

A local-only chat-with-your-vault setup running on Apple Silicon. Ollama serves the models, Open WebUI provides the chat UI and the RAG layer, and a sync script pushes the entire vault into Open WebUI's Knowledge feature on a nightly cadence. All inference happens on-device — no cloud calls, no telemetry, no data leaves the machine.

> [!info] Optional component
> This is **not** part of the core vault workflow. It requires capable hardware (Apple Silicon Mac with sufficient unified memory — see "Hardware footprint") and is documented here as an optional power-user layer for users who want vault-context queries that never leave their machine.

## Architecture at a glance

```
~/Obsidian  ──►  ~/Obsidian/Templates/Scripts/sync-vault.sh  ──►  Open WebUI Knowledge  ──►  RAG retrieval  ──►  Ollama model  ──►  Answer
                 (nightly via LaunchAgent)        (vector + BM25)       (Top K = 6)        (llama.cpp + Metal)
```

The model itself never sees the raw vault — Open WebUI retrieves relevant chunks at query time and injects them into the prompt context. This is conventional retrieval-augmented generation; the contribution is the wiring, the personalization layers, and the security posture, not the architecture.

## Inference & UI

### Ollama

Ollama is the inference backend. It wraps `llama.cpp`, which uses Metal Performance Shaders (MPS) on Apple Silicon to run inference on the GPU portion of the unified-memory chip. This is **not** the same path as Apple's MLX framework — MLX would be 10–30% faster on token generation and 1.5–2× faster on prompt processing for long contexts, but the migration cost (re-pull every model in MLX-community format, swap the inference layer, re-validate the RAG pipeline) is rarely worth it.

Install via the bundled bootstrap script `Templates/Scripts/setup-ollama.sh` (place it in `~/Obsidian/Templates/Scripts/setup-ollama.sh` and run):

```bash
chmod +x ~/Obsidian/Templates/Scripts/setup-ollama.sh
~/Obsidian/Templates/Scripts/setup-ollama.sh                  # full setup
~/Obsidian/Templates/Scripts/setup-ollama.sh --minimal        # only pull the 8B model
~/Obsidian/Templates/Scripts/setup-ollama.sh --skip-wired-limit  # don't touch iogpu.wired_limit_mb
~/Obsidian/Templates/Scripts/setup-ollama.sh --skip-models    # set up Ollama only, no model pulls
```

What the script does:

1. **Installs the Ollama macOS app** via Homebrew cask (`brew install --cask ollama-app`).
2. **Adds tuning env vars** to your shell profile (`~/.zshrc`):
    - `OLLAMA_FLASH_ATTENTION=1` — flash attention on long contexts.
    - `OLLAMA_KV_CACHE_TYPE=q8_0` — 8-bit KV cache, ~halved memory at negligible quality cost.
3. **Raises the Metal wired-memory cap** to 120 GB on a 128 GB machine (`iogpu.wired_limit_mb=122880`), persisted across reboots via a LaunchDaemon at `/Library/LaunchDaemons/com.local.iogpu.wired-limit.plist`.
4. **Pulls the model set** (see "Models" below).

The Ollama server listens on `127.0.0.1:11434`. Block outbound exposure on the LAN at the macOS application firewall — Ollama should never be reachable from anywhere except localhost.

### Open WebUI

Open WebUI is the chat front-end and the RAG layer (its built-in **Knowledge** feature). Run it in Docker, bound to localhost only:

```bash
docker run -d \
  -p 127.0.0.1:3000:8080 \
  --add-host=host.docker.internal:host-gateway \
  -v open-webui:/app/backend/data \
  --name open-webui \
  --restart always \
  ghcr.io/open-webui/open-webui:main
```

Notes:

- `-p 127.0.0.1:3000:8080` binds to localhost only. Do not drop the `127.0.0.1:` prefix — without it, Docker exposes the port on every interface.
- The first time you visit `http://localhost:3000` you'll be prompted to create an admin account. Create the admin first, then go to **Admin Panel → Settings → General** and set `Enable New Sign Ups` to OFF and `Default User Role` to `pending`. Do **not** set `ENABLE_SIGNUP=false` as a startup environment variable — it blocks all signups including the very first admin signup.
- Keep Open WebUI **localhost-only**. There is no supported remote-access path for the vault-RAG UI: do not open port 3000 to the LAN, and do not expose it over a mesh VPN such as Tailscale — the supply-chain surface of a third-party mesh isn't worth it for a personal index. To use the UI from another device, drive the host over its built-in **Screen Sharing** rather than exposing the port.

Update the container after a new release:

```bash
docker pull ghcr.io/open-webui/open-webui:main
docker stop open-webui
docker rm open-webui
# re-run the docker run command above
```

## Models

The bootstrap script pulls a tiered set automatically. Each base model gets wrapped as a "Custom Model" in **Open WebUI → Workspace → Models** with the vault Knowledge collection pre-attached and a tailored system prompt — those wrappers appear in the model picker as **`<model> + Vault`**:

| Custom model | Base model tag | Loaded size | Speed | Use it for |
|---|---|---|---|---|
| **Llama 8B + Vault** | `llama3.1:8b-instruct-q8_0` | ~8 GB | Fastest | Quick lookups, name/date pulls |
| **Gemma 4 26B + Vault** | `gemma4:26b` (MoE) | ~16 GB | Fast (MoE fires ~4–8B params/token) | Everyday default |
| **Llama 70B + Vault** | `llama3.3:70b-instruct-q5_K_M` | ~50 GB | Slow (~15s first-load) | Synthesis, multi-source reasoning |
| **Nemotron + Vault** | `nvidia/nemotron-...` *(pulled separately)* | ~86 GB | Slowest | Grounding-critical work; strict citations |

Verify the live set with `ollama list`. The 70B's first-load time is dominated by reading 50 GB into unified memory; subsequent queries against an already-loaded model are fast. Treat the 70B and Nemotron as "premium" tiers — Gemma 4 26B MoE is the practical everyday default (the MoE architecture only fires ~4–8B parameters per token, so it is much faster than the 70B at comparable quality).

### Embedding model

Used for the RAG side, not for chat:

```bash
ollama pull nomic-embed-text
```

Configured in **Open WebUI → Admin Panel → Settings → Documents → Embedding Model**.

### Optional reranker

Adding a cross-encoder reranker materially improves retrieval on short colloquial queries:

In **Admin Panel → Settings → Documents → Reranking Model**, enter:

```
BAAI/bge-reranker-v2-m3
```

Open WebUI will pull the model on first use (~600 MB, one-time). Adds a small per-query latency that's negligible on capable hardware.

## RAG configuration

In **Admin Panel → Settings → Documents**:

| Setting | Value |
|---|---|
| Embedding Model | `nomic-embed-text` |
| Hybrid Search | ON |
| Enrich Hybrid Search Text | ON |
| Top K | 6 |
| Top K Reranker | 6 (only meaningful if the reranker is set) |
| Relevance Threshold | 0  (don't filter — let Top K decide) |
| Markdown Header Splitter | ON  (chunk on `#` boundaries, not arbitrary character counts) |
| Reranking Model | `BAAI/bge-reranker-v2-m3`  *(optional, recommended)* |

Why these defaults:

- **Hybrid search** combines BM25 (lexical) with vector (semantic). A pure vector setup misses queries with strong lexical signals; a pure BM25 setup misses paraphrases.
- **Top K = 6** is enough context for most queries without bloating the prompt.
- **Relevance threshold = 0** is counter-intuitive but correct: the threshold filter cuts chunks that score below a cutoff, which on short queries can leave the model with 1–2 fragments of context. Trust Top K.
- **Markdown header splitter** preserves the semantic boundaries of the vault's structure. The [[Markitdown Dropper]] cleanup pipeline ensures imported documents have proper `##` headings so the chunker has something to split on.

## Sync: vault → Open WebUI Knowledge

Two files do this work (place both in `~/Obsidian/Templates/Scripts/`):

- `sync-vault.sh` — a thin shell wrapper that loads `OPEN_WEBUI_API_KEY` and `OBSIDIAN_COLLECTION_ID` from `~/dev/secrets/.env` and execs the Python indexer.
- `obsidian-rag-sync.py` — the indexer itself.

### Usage

```bash
~/Obsidian/Templates/Scripts/sync-vault.sh                       # push new + modified, remove deleted
~/Obsidian/Templates/Scripts/sync-vault.sh --dry-run             # preview only
~/Obsidian/Templates/Scripts/sync-vault.sh --reset-quarantine    # retry files that hit the failure threshold
~/Obsidian/Templates/Scripts/sync-vault.sh --allow-bulk-delete   # explicit consent for large deletes
```

Dry-run output prints a per-file diff: `NEW`, `MODIFIED`, `DELETED`, `QUARANTINED (skip)`, plus a summary line `diff: new=N modified=N deleted=N unchanged=N quarantined_skip=N`.

### What the script does

The script keeps state at `~/.local/share/obsidian-rag-sync/state.json` — a map of `relative-path → {sha256, mtime, size, body_chars, file_id}`. Each run scans the vault for `.md` files, hashes them, diffs against state, and pushes only the changes. Open WebUI stores each note as a file (uploaded via `POST /api/v1/files/`) and adds it to the Knowledge collection.

### Excluded directories

The sync skips `.obsidian/`, `.trash/`, `Templates/`, `Excalidraw/`, and `Z_attachments/`. `Z_archive/` is **not** excluded — archived content is still indexed and findable by RAG queries.

### Thin-content filtering

Files whose extractable body text is below `MIN_BODY_CHARS = 100` (after stripping frontmatter, wikilinks, and image embeds) are skipped on **fresh adds only**. This prevents most People notes — which are typically a frontmatter block + photo + Meetings dataview query and almost no prose — from being indexed as semantic noise.

To make a thin People note (or any sparse file) indexable, add a paragraph of real content.

### Classification gating (optional)

If you tag notes with a `classification` frontmatter property, the sync will
exclude anything classified `restricted` from the local index — a middle
tier like `confidential` is still indexed by default, on the reasoning that
the RAG is a single-user index on your own authorized hardware, so material
you're already cleared to view doesn't need re-gating. `restricted` is meant
for the tier where mere presence in an LLM context carries its own
compliance implications (PHI/PII/credentials/regulated data), independent of
who's authorized to view it. A previously-indexed file later elevated to a
blocked tier is deindexed on the next sync (logged as `DEINDEXED
(classification)`, distinct from a true filesystem deletion).

This is entirely optional and degrades gracefully: if you don't use a
`classification` property at all, every file falls back to the default
(`internal-use-only`) and is indexable as before. Unknown classification
values fail secure — excluded, with a warning logged so you can fix the
typo. Adjust `INDEXABLE_CLASSIFICATIONS` / `EXCLUDED_CLASSIFICATIONS` in
`obsidian-rag-sync.py` to match whatever scheme you actually use.

### Safety guardrails

The script will refuse to proceed under three conditions:

1. **Pre-mutation backup.** Before any real run that would change state, `state.json` is copied to `state.bak.YYYYMMDD-HHMMSS.json` (last 10 retained).
2. **Deletion ceiling.** A run that would delete more than `max(50, 5% of indexed corpus)` files refuses without `--allow-bulk-delete`.
3. **Insanity check.** If a run would delete more files than it leaves untouched (when the corpus is >50 files), it refuses regardless of flags.

### Quarantine

After three consecutive failures on the same content hash, a file is quarantined and skipped on subsequent runs until either its content changes or `--reset-quarantine` is invoked.

### Nightly schedule

Wire the nightly LaunchAgent by placing this at `~/Library/LaunchAgents/com.obsidian-rag-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.obsidian-rag-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USERNAME/Obsidian/Templates/Scripts/sync-vault.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/obsidian-rag-sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/obsidian-rag-sync.err</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Replace `YOUR_USERNAME`, then `launchctl load ~/Library/LaunchAgents/com.obsidian-rag-sync.plist`.

## Personalization layers

The "+ Vault" custom models combine three layers of context:

1. **Open WebUI Personalization** — first-class user prefs (name, role, tone) set in your account settings.
2. **System prompt** — per-custom-model instructions ("You are an executive assistant; cite sources from the vault Knowledge collection; be concise; …").
3. **Vault profile + RAG retrieval** — a `Knowledge/About <Your Name>.md` file works as the anchor: enumerate strategic priorities, the leadership team, major programs, and working-style notes. RAG also pulls in topical files matching the query.

If a response is generic or hallucinated, click the **Sources** panel under the answer to see which files retrieval pulled. That tells you which layer to fix.

## Known limitations and how to work around them

- **Short colloquial queries fail** more often than direct queries. *"Who reports to me?"* won't reliably retrieve `About <Name>.md`; *"What are the names of my eight direct reports?"* will. Use the file's actual vocabulary.
- **Sparse files don't retrieve well.** Most People notes are templates with frontmatter + photo + Meetings dataview query — almost no prose. Add a 100–200 word bio paragraph to make a person queryable.
- **Avoid duplicate profile files.** Two near-identical files will both rank highly and consume retrieval slots. Keep one.
- **Synthesis across many notes is RAG's weakest pattern.** "Which meetings are most relevant to my AI strategy work?" is hard. Use the 70B for this; phrase the query specifically.

## Hardware footprint reference

| Tier | Models that fit at production quantization | Approx loaded size | Hardware |
|---|---|---|---|
| Edge | Llama 8B, Phi-4-reasoning 14B | ~8–10 GB | Any Apple Silicon Mac with 16+ GB |
| Single-H100 / mid-Mac | Qwen 14B, Gemma 4 31B Dense, Qwen 3 32B | ~12–62 GB | Mac Studio M4 Max 64 GB or H100 80 GB |
| 128 GB workstation | Llama 70B, Qwen 3 72B | ~50 GB at Q4 | Mac Studio 128 GB unified memory |
| Top tier | Nemotron, Llama 4 Scout 109B (MoE) | ~86 GB+ | Mac Studio 128 GB or multi-node cluster |

If you only have 16–32 GB of unified memory, stop at the edge tier. The full multi-tier "+ Vault" stack assumes 128 GB.

## Optional: a second machine for larger models (LM Studio + LM Link)

The "+ Vault" RAG stack should live on **one** node — your primary machine, the one that holds the vault. If you also own a second capable Apple Silicon Mac (e.g. a Mac Studio) and want to run the 70B / Nemotron-class models without taxing your daily driver, the cleaner pattern is to keep that second machine **out of the RAG pipeline entirely** and reach it only for *non-vault* queries.

> [!warning] The vault lives on exactly one node
> Do not sync the vault, attach the Knowledge collection, or run `obsidian-rag-sync` against the second machine. The entire premise of the local setup is that vault context never leaves a single device. A second inference host is for general or personal queries that carry no vault content.

How to wire it:

- On the second Mac, install **LM Studio** and enable **LM Link** (LM Studio's built-in remote-inference feature). LM Link provisions its own end-to-end-encrypted mesh between your machines and gates discovery on your LM Studio account — you don't open a port or stand up your own VPN.
- On your primary machine, add the second Mac as an LM Studio remote host. Larger models you can't run comfortably on the RAG node are now available for ad-hoc, non-vault chat.
- This path is deliberately **separate** from Open WebUI and the vault. Open WebUI stays localhost-only (see "Open WebUI" above); LM Link carries only personal, non-vault traffic between your own devices, with no cloud calls.

This is the recommended way to use a high-memory second machine: rather than replicating the vault onto it to run bigger models, run the big models there for everything *except* the vault, and let the RAG node stay the sole holder of vault context. Note that LM Link bundles its own mesh networking; it is scoped here to personal, non-vault inference and is distinct from the vault-RAG UI, which stays localhost-only with no remote-access path.

## Files on disk

| Path | Purpose |
|---|---|
| `~/Obsidian/Templates/Scripts/setup-ollama.sh` | One-shot Ollama bootstrap |
| `~/Obsidian/Templates/Scripts/sync-vault.sh` | Wrapper that loads secrets and execs the indexer |
| `~/Obsidian/Templates/Scripts/obsidian-rag-sync.py` | The indexer |
| `~/.local/share/obsidian-rag-sync/state.json` | Per-file state |
| `~/.local/share/obsidian-rag-sync/state.bak.*.json` | Auto backups |
| `~/.local/share/obsidian-rag-sync/sync.log` | Append-only run log |
| `/Library/LaunchDaemons/com.local.iogpu.wired-limit.plist` | Persists the Metal wired-memory cap (system-wide) |
| `~/Library/LaunchAgents/com.obsidian-rag-sync.plist` | Nightly sync schedule |

### Secrets configuration

Required by `obsidian-rag-sync.py` (set by `sync-vault.sh` from `~/dev/secrets/.env`):

| Variable | Purpose |
|---|---|
| `OPEN_WEBUI_API_KEY` | Open WebUI API key — generate in Open WebUI → Settings → Account → API Keys |
| `OBSIDIAN_COLLECTION_ID` | Knowledge collection UUID — find in the URL when inside the Knowledge collection |
| `OBSIDIAN_VAULT` | Vault root (default: `~/Obsidian`) |
| `OPEN_WEBUI_URL` | Base URL (default: `http://localhost:3000`) |

## Related

- [[Semantic Auto-Tagger Setup]] — the source-of-truth for tags that power retrieval
- [[Markitdown Dropper]] — produces well-structured markdown that the chunker handles cleanly
