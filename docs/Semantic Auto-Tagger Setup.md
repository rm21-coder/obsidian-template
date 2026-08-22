---
tags:
  - note
  - setup
classification: public
---

# Semantic Auto-Tagger Setup

This template ships a Python LaunchAgent that auto-tags new and changed notes in `Clippings/`, `Creations/`, and `Meetings/` using the Claude API. It calls Claude one note at a time — about one API call per note actually changed — and only ever applies tags from your existing taxonomy. It never invents new tags.

This guide covers:

1. Putting the vault where the agent can reach it
2. Installing the LaunchAgent
3. (Optional) Adding a hard-allowlist tag taxonomy
4. (Optional) Retrieval-augmented variant for large taxonomies
5. On-demand maintenance prompts
6. Troubleshooting

---

## 1. Vault Location

Keep the vault on local disk so the script can read and edit it instantly. Recommended layout:

```
~/Obsidian/            ← open this folder as your Obsidian vault
├── Clippings/
├── Creations/
├── Meetings/
├── Knowledge/
├── Templates/
│   └── Scripts/
│       ├── tag_clippings.py
│       ├── com.tag-clippings.plist
│       └── ...
├── ... etc.
```

Open `~/Obsidian/` as the vault in Obsidian on your desktop. The script derives the vault root from its own location (`Templates/Scripts/` → two levels up), so as long as the scripts stay in that subfolder, it adapts to whatever vault path you choose.

---

## 2. Install the LaunchAgent

Full prerequisite list, install steps, and post-migration restoration are in [[LaunchAgents — Setup & Migration]]. The short version:

```bash
# 1) Per-vault venv with the script's dependencies
cd ~/Obsidian/Templates/Scripts
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) Anthropic key in ~/dev/secrets/.env
mkdir -p ~/dev/secrets
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/dev/secrets/.env
chmod 600 ~/dev/secrets/.env

# 3) Edit the plist (replace YOUR_USERNAME) and load
sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Obsidian/Templates/Scripts/com.tag-clippings.plist
cp ~/Obsidian/Templates/Scripts/com.tag-clippings.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tag-clippings.plist

# 4) Verify
launchctl list | grep tag-clippings
tail -f ~/Library/Logs/tag-clippings.log
```

The agent fires once on load (`RunAtLoad: true`) and then at :00 and :30 each hour (`StartCalendarInterval`). Logs land in `~/Library/Logs/tag-clippings.{log,err}`.

---

## 3. How tagging works

Every 30 minutes (at :00 and :30):

1. **Scan.** The script walks `Clippings/`, `Creations/`, and `Meetings/` (recursively, so `Meetings/History/` also gets tagged).
2. **Defer recent edits.** Notes modified within the last 2 minutes are skipped for this run — a very recent change usually means the note is open and being typed in, and because the tagger rewrites the whole file it would drop unsaved keystrokes in the editor. The note is picked up on a later run. Configurable via `TAG_RECENT_EDIT_GUARD_SECONDS` (default 120; set `0` to disable), and bypassed by `--force` / `--file`.
3. **Skip unchanged.** A `.tag_tracking.json` ledger at the vault root keys files by relative path with a body-content hash. Files whose body hasn't changed since last run are skipped.
4. **Body-only derivation.** For each in-scope file, the script strips the YAML frontmatter and sends only the body text to Claude (capped at 4 000 characters). The script is forbidden from inferring tags from `people:`, `group:`, `type:`, the filename, or any other metadata. The empty-body guard skips files with <50 chars of body content so freshly-templated meeting notes don't pick up tags from their participant lists.
5. **Allowlist-only.** Claude returns 1–6 candidate tags. The script normalizes case (PascalCase, with an acronym whitelist for things like `AI`, `GPU`, `AWS`) and validates each against the loaded allowlist. Tags that don't match are dropped and logged to `Templates/Scripts/tag-promotion-candidates.md` for weekly review.
6. **Write.** Validated tags are merged with any preserved base tags (the `clippings` base tag on `Clippings/` files) and written back to the file's YAML frontmatter. Other frontmatter keys are preserved in original order.

Two folder-specific rules:

- `Clippings/*` always keeps the `clippings` base tag.
- `Meetings/*` puts meeting type in the `type:` frontmatter field, not as a tag.

---

## 4. (Optional) Hard allowlist via Tag Taxonomy.md

By default the script's allowlist comes from a fallback vault scan — every tag currently in use anywhere in the vault becomes a candidate. That works fine when you start out, but the tag space drifts over time (case variants, near-synonyms, one-off typos).

To lock the taxonomy down, create `Knowledge/Tag Taxonomy.md`. The script reads it as the canonical allowlist whenever the file exists:

```markdown
# Tag Taxonomy

This is the authoritative tag list for the vault. The auto-tagger reads this
file as a hard allowlist — it will only ever apply tags listed here.

## Standalone tags

- AI
- Programming
- Self-improvement

## Hierarchical: Vendors/

- Vendors/AWS
- Vendors/Azure
- Vendors/GCP

## Hierarchical: AI/

- AI/Agents
- AI/LLMs

## Filtered out (reference only — never applied)

- clippings
- note
- journal
```

Format rules:

- Each tag is a `- tag` bullet on its own line.
- Section headings (`##`) group tags for human readability — they don't change the allowlist semantics.
- The `## Filtered out` section is the one exception: anything listed under it is **not** added to the allowlist (used for documenting structural tags you don't want the tagger to pick up).
- Hierarchical tags use `/` (`Vendors/AWS`), preserved verbatim.

To **add a new tag**: edit `Knowledge/Tag Taxonomy.md` directly. The next run (within 30 minutes) picks it up.

To **retire a tag**: remove it from the file. The tagger stops applying it. Existing uses on files remain until you sweep them.

To extend the **acronym whitelist** (so `gpu`/`Gpu`/`GPU` all canonicalize to `GPU`), edit `ACRONYM_MAP` near the top of `tag_clippings.py`.

When the file is missing, the script falls back to the vault-scan behavior. Migration is non-disruptive: a populated `.tag_tracking.json` continues to work either way.

---

## 5. (Optional) Retrieval-augmented variant for large taxonomies

`tag_clippings_rag.py` is an alternative tagging engine for when your taxonomy grows large (say 100+ tags). The default tagger puts the whole allowlist into one Claude call; past a certain size that single pass tends to satisfice on broad tags and overlook the specific child tags whose vocabulary already exists. The RAG variant fixes that by ranking the taxonomy first.

How it differs:

1. **Local embedding pre-rank.** It embeds each tag (plus a short gloss) with a local Ollama model and embeds the note body, then hands Claude only the ~30 most relevant tags instead of the full list. Embeddings run on your machine — the only cloud call is the same final tagging request the default tagger already makes.
2. **Augment, never gate.** The candidate list is always the top-N by similarity *unioned with the note's current tags*, so pre-ranking can only add options, never hide one a note already carries.
3. **Additive mode.** `--additive` only ever *adds* tags and never removes an existing one — ideal for enriching notes you've already reviewed. `--max-add N` keeps just the strongest N additions (by similarity), so reviewed notes gain a few precise tags rather than many marginal ones.
4. **Surgical, reversible writes.** Only the frontmatter `tags:` block is rewritten; bodies and other frontmatter stay byte-for-byte identical. Every `--apply` run writes a rollback manifest, and `--rollback <manifest>` restores every touched file.

It keeps the core guarantees of the default tagger: body-only, allowlist-only, never invents tags.

Setup adds two things to Section 2:

```bash
# embedding model for the local pre-rank
ollama pull nomic-embed-text
# the RAG script also needs requests
~/Obsidian/Templates/Scripts/.venv/bin/pip install requests
```

Glosses live in `tag_glosses.py` (ships with a small example). A gloss is the words a *note about that topic* would use — `FinancialReporting → "chargeback, cost recovery, internal pricing"` — which is what lets the embedding step rank the right tags. Tags without a gloss fall back to an auto-derived one (the de-CamelCased name).

Usage:

```bash
P=~/Obsidian/Templates/Scripts/.venv/bin/python3
S=~/Obsidian/Templates/Scripts/tag_clippings_rag.py

# preview only, never writes:
$P "$S" --dry-run --additive 'Clippings' 'Creations'
# enrich a reviewed folder, adding at most 3 tags per note:
$P "$S" --apply --additive --max-add 3 'Knowledge'
# undo the last apply:
$P "$S" --rollback tag_rag_manifest_YYYYMMDD_HHMMSS.json
```

It runs on demand (no LaunchAgent by default). For a large run, watch progress with `grep -c 'current :' your.log` (notes processed) versus `grep -c '>> written'` (notes changed) — writes lag well behind, since additive tagging leaves well-tagged notes unchanged.

A companion tool, `merge_tags.py`, performs taxonomy consolidations (fold a flat tag into a hierarchy, rename a tag everywhere) with the same surgical, idempotent, rollback-manifest approach. Edit its `MERGES` map, dry-run, then `--apply`.

---

## 6. On-Demand Maintenance Prompts

These don't need the LaunchAgent — paste them into a Claude desktop chat when you want a one-off:

**Re-tag a single file**
> *Re-tag the file `Creations/2026-04-01 Strategy Draft.md` using the vault taxonomy. Same rules as the LaunchAgent tagger — body-only, allowlist-only, log unknown candidates.*

**Force a full re-tag of one folder**
> *Re-run the auto-tagger against every file in `Meetings/`, not just new/changed ones — body-only, allowlist-only.*

**Dry run**
> *Dry run: what tags would the tagger set on every file in `Clippings/` modified in the last 7 days? Don't write anything — just show me the proposed changes.*

**Build a Topic page**
> *Put all the [TagName] tags in the [TagName] Topic file — create a Dataview query in `Topics/` that lists every note with that tag.*

Or trigger the LaunchAgent to run immediately (skip the wait):

```bash
launchctl kickstart gui/$(id -u)/com.tag-clippings
```

Or run the script directly with options the agent doesn't pass:

```bash
~/Obsidian/Templates/Scripts/.venv/bin/python3 \
    ~/Obsidian/Templates/Scripts/tag_clippings.py --force            # ignore tracking
~/Obsidian/Templates/Scripts/.venv/bin/python3 \
    ~/Obsidian/Templates/Scripts/tag_clippings.py --dry-run          # preview only
~/Obsidian/Templates/Scripts/.venv/bin/python3 \
    ~/Obsidian/Templates/Scripts/tag_clippings.py --file "Clippings/foo.md"
```

---

## 7. Troubleshooting

**Agent isn't running.** `launchctl list | grep tag-clippings` should show the agent. If it's missing, reload from the plist in `Templates/Scripts/`. Check `~/Library/Logs/tag-clippings.err` for Python tracebacks. See [[LaunchAgents — Setup & Migration]] for the full diagnostic ladder.

**`ANTHROPIC_API_KEY not found`.** The `.env` at `~/dev/secrets/.env` is missing the key, or the venv's `python3` can't read it. Verify both: `cat ~/dev/secrets/.env | grep ANTHROPIC` and `ls -l ~/Obsidian/Templates/Scripts/.venv/bin/python3`.

**`ModuleNotFoundError` for `anthropic` / `yaml` / `dotenv`.** The venv may have been wiped. Recreate per the install steps above.

**Tags being invented instead of reused.** With a Tag Taxonomy.md file in place, this can't happen — proposals outside the allowlist go to `Templates/Scripts/tag-promotion-candidates.md`. Without that file, the fallback vault-scan still uses the existing tag space, but no allowlist guard. If you see invention without the taxonomy file, create the file and re-run.

**Tagger touching files it shouldn't.** Edit the `WATCH_FOLDERS` list near the top of `tag_clippings.py` and reload the agent.

**You want to see what would change before it writes.** Use `--dry-run` (above).

**You want to pause tagging.** `launchctl unload ~/Library/LaunchAgents/com.tag-clippings.plist`. Resume with `launchctl load`.

---

## Related

- [[LaunchAgents — Setup & Migration]] — host-side launchd install for the tagger and voice-cleanup, plus post-migration restoration
- [[Voice Notes (Optional)]] — feeding voice transcripts into `Creations/` so the tagger picks them up
- [[Markitdown Dropper]] — drag-drop converter for Word / PDF / Excel / etc. that lands cleaned markdown in `Creations/`
- [[Obsidian Configuration Guide]] — plugins, templates, general vault setup
- [[Local LLM with Obsidian Vault RAG]] — optional layer that uses the tagged vault as a retrieval-augmented knowledge base
