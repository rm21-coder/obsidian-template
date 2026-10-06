---
tags:
  - tools
  - markdown
  - python
  - markitdown
  - setup
  - optional
classification: public
---

# Markitdown Dropper (Optional)

A small always-on-top macOS window that converts dropped files into Markdown using Microsoft's [markitdown](https://github.com/microsoft/markitdown) library and writes the output into the vault's `Creations/` folder. An inline cleanup pass extracts embedded images, normalizes Outlook/Word bullet markers, promotes strict heading patterns, and adds minimal YAML frontmatter so the [[Semantic Auto-Tagger Setup|semantic auto-tagger]] picks up the file on its next run.

This is **optional**. The vault works fine without it — you can paste content directly into `Creations/` or use any other conversion tool. The dropper is convenient when you regularly receive Word docs / PDFs / agendas from people who don't use markdown.

## What it does

Drag any supported file (Word, Excel, PowerPoint, PDF, HTML, audio, image, etc.) onto the drop zone. The file is converted to Markdown in-process, run through the [cleanup pass](#cleanup-pass), and written to `~/Obsidian/Creations/<original-name>.md`. Extracted images land in `~/Obsidian/Z_attachments/`. Originals are not touched. If a file with the same name already exists in `Creations/`, a timestamp is appended so nothing gets overwritten.

## Files

- `Templates/Scripts/markitdown_dropper.py` — the PySide6 GUI app
- `~/Applications/Markitdown Dropper.app` — the launcher the installer builds (component 43): an AppleScript app bundle carrying its own copy of `markitdown_dropper.py`, which it starts with the dropper venv's Python
- `Templates/Scripts/markitdown_cleanup.py` — post-conversion cleanup module (imported by the dropper)
- `~/.markitdown-dropper-venv/` — isolated Python 3.13 environment with `markitdown[all]` and `PySide6`, installed only from `Templates/Scripts/requirements-dropper.lock` (every package hash-checked, wheels only)
- `~/.markitdown_dropper.json` — saved destination folder

## Cleanup pass

After Markitdown converts a file, the dropper imports `markitdown_cleanup.py` from `~/Obsidian/Templates/Scripts/` (where the template already ships it) and runs the converted text through `clean()` before writing it. The cleanup is intentionally conservative — it only changes things it can be confident about, so it doesn't mangle nuance in dictation-style notes or pasted email content.

**What it does:**

1. **Extracts inline base64 images** — Markitdown converts embedded images in `.docx` / `.pdf` files into giant `![Image](data:image/png;base64,...)` blobs. The cleanup decodes each, saves it as `<source-stem>-img-N.png` in `Z_attachments/`, and replaces the inline blob with an Obsidian wiki-link `![[name.png]]`. Images still render in Obsidian and the markdown becomes RAG-indexable.
2. **Recovers images from source archives when Markitdown emits stubs** — sometimes Markitdown can't extract an image and emits `![](data:image/png;base64...)` (literal `...`, no real data). For `.docx`, `.pptx`, and `.xlsx` sources, the cleanup opens the source as a ZIP, pulls images from `word/media/` (or `ppt/media/` / `xl/media/`), saves them as `<source-stem>-source-img-N.<ext>`, and uses them to replace stubs in document order. Any extracted images that didn't match a stub are appended in a `## Images from source` section so nothing silently disappears. Stubs without a matching extracted image become a clear text placeholder. Non-renderable formats (WMF, EMF) are skipped.

   Both steps are bounded, because a dropped file usually came from someone else: at most 200 inline images and 100 MB decoded per document (any past that become an "Embedded image omitted" placeholder), and from an archive at most 500 media members, 25 MB per decompressed image and 200 MB in total (anything over is skipped, so its stub gets the placeholder). Sizes are counted while streaming, not taken from the archive's own header. If a member turns out to be damaged partway through, the images already recovered from that archive are removed again rather than left orphaned in `Z_attachments/`.

   Those limits apply to the cleanup pass only. MarkItDown itself reads zip members whole (and recurses into nested zips), so before conversion a zip container (`.zip`, `.docx`, `.xlsx`, `.pptx`) is checked by `archive_limits.py`. It is refused, with a "refused: archive …; not converted" line in the log, if it has more than 5,000 members, any member that inflates past 100 MB, more than 500 MB inflated in total, or zips nested more than two deep. Other formats are not size-checked.
3. **Normalizes bullet markers** — converts Outlook/Word's `•`, `○`, `▪`, `▸`, `▹`, `‣`, `◦`, `●`, `⁃` to standard `-`. Tab indentation becomes two-space indentation while preserving nesting depth.
4. **Promotes two strict heading patterns to `##`** — used because RAG chunkers (Open WebUI's Markdown Header Splitter, etc.) need header anchors:
    - `N. **Heading text**` on its own line (numbered + entirely bold)
    - `**Heading text:**` on its own line (bold label ending in colon)
   Anything ambiguous is left alone. Inline `**bold emphasis**` inside paragraphs is never promoted.
5. **Normalizes whitespace** — strips trailing spaces, collapses any run of blank lines to a single blank, trims leading and trailing blanks.
6. **Writes the note's YAML frontmatter:**
    ```yaml
    ---
    title: "<derived from source filename>"
    created: <today>
    source: markitdown
    source_file: "<original filename with extension>"
    classification: internal-use-only
    tags: []
    ---
    ```
   The empty `tags: []` lets the semantic auto-tagger fill it in on its next 30-minute LaunchAgent pass. `title` and `source_file` are always quoted: the filename is the sender's, and names like `[DRAFT] Budget` or `Report #3` are otherwise invalid YAML.

   If the converted document starts with its own `---` frontmatter block, that block does **not** become the note's frontmatter: a document could otherwise declare its own `classification: public`. It is kept, visibly, as a fenced `yaml` code block at the top of the body under "Frontmatter from the source document (kept as text, not applied)". The standalone CLI below is the exception: it re-cleans notes already in the vault, so it keeps the file's existing frontmatter.

**What it does NOT do** (intentionally):

- Aggressive heading inference from inline labels surrounded by prose. False-positive risk too high.
- Smart-quote / em-dash / ellipsis normalization. They render fine in Obsidian and read better in print.
- Touch fenced code blocks, tables, or links.
- Backups. Markitdown leaves the source file untouched — your originals are still wherever they came from.

**Per-drop log:**

Each successful drop logs `<source>  →  <output>  (<size> bytes)  [cleaned: 2 img, 1 stubs→img, 6 bullets, 4 headings]` in the dropper window so you can see at a glance what the cleanup did.

**Standalone CLI** — useful for retroactive cleanup of existing files:

```bash
~/.markitdown-dropper-venv/bin/python3 ~/Obsidian/Templates/Scripts/markitdown_cleanup.py path/to/file.md             # print cleaned text to stdout
~/.markitdown-dropper-venv/bin/python3 ~/Obsidian/Templates/Scripts/markitdown_cleanup.py path/to/file.md --in-place  # rewrite in place

# Recover images from the original source archive (handy if a previous drop
# ended up with a placeholder because the dropper hadn't been restarted yet):
~/.markitdown-dropper-venv/bin/python3 ~/Obsidian/Templates/Scripts/markitdown_cleanup.py \
    "Creations/Some Note.md" --source ~/Downloads/original.docx --in-place
```

## Setup

Prerequisite: Homebrew Python 3.13.

```bash
brew install python@3.13
```

Then run the installer: component 43 (`installers/components/43-markitdown-dropper.sh`) does the rest.

1. It builds the venv from the lock:

   ```bash
   /opt/homebrew/bin/python3.13 -m venv ~/.markitdown-dropper-venv
   ~/.markitdown-dropper-venv/bin/python3 -m pip install --require-hashes --no-deps --only-binary :all: -r ~/Obsidian/Templates/Scripts/requirements-dropper.lock
   ```

2. It compiles `~/Applications/Markitdown Dropper.app` and bundles a copy of `markitdown_dropper.py` inside it, so a change to that script reaches the app only when component 43 runs again. The bundled script still imports `markitdown_cleanup.py` from `~/Obsidian/Templates/Scripts/`, which is on its `sys.path`.
3. Launch it from Spotlight (type "Markitdown") or Finder. If Gatekeeper warns on the first launch, right-click it and choose Open. Launches take a few seconds because markitdown loads its [magika](https://github.com/google/magika) ONNX model once at startup; after that, drops are near-instant. Launching it while it is already running brings the open window forward.
4. On first launch the app prompts for the destination folder — point it at this vault's `Creations/` folder. The choice persists in `~/.markitdown_dropper.json`.

The app runs in the background with no Terminal window; quit it from its own window.

## Updating markitdown

Not with `pip install --upgrade`: that would fetch whatever PyPI holds, unchecked. A maintainer moves the pins (`python3 installers/lib/lock_requirements.py --upgrade`, review the diff, commit); then re-run the installer, or update, which reinstalls the venv from `requirements-dropper.lock`.

The `[all]` extra in `requirements-dropper.txt` pulls converters for every supported format. A plain `markitdown` installs only a slim core and will throw `MissingDependencyException` for files like `.docx`.

## Restart the dropper after cleanup-module updates

Python caches imported modules at process start, so changes to `~/Obsidian/Templates/Scripts/markitdown_cleanup.py` only take effect when the dropper is relaunched.

## Design notes

- **PySide6 instead of tkinter.** The first build used `tkinterdnd2`, but its prebuilt `tkdnd` binary fails to load on Apple Silicon (`tkdnd_Init` symbol not found). PySide6 ships native arm64 wheels with first-class drag-and-drop support.
- **In-process markitdown, not subprocess.** Each `markitdown` CLI invocation reloads magika's ONNX model from scratch, which adds 10–30 seconds per file. Calling the Python API in-process amortizes that cost over the lifetime of the app.
- **Python 3.13 specifically.** The venv is pinned to 3.13 because some markitdown dependencies don't yet ship 3.14 wheels and need to compile from source on Python 3.14.
- **Cleanup is fail-loud.** If the cleanup pass raises an exception, the file is NOT written — you'll see `cleanup failed: <ErrorType>: <message>` in the dropper log. This is deliberate: better than getting a half-cleaned file and not knowing.
- **Cleanup is import-optional.** If `markitdown_cleanup.py` can't be imported for any reason, the dropper still works — files just land without the cleanup pass and you'll see no `[cleaned: ...]` annotation in the log.

## Known quirks

Python sometimes throws an at-exit warning when the window closes — a benign race between Qt's cleanup and onnxruntime's tear-down inside markitdown. It happens after all conversions complete and can be ignored.

## Related

- [[Semantic Auto-Tagger Setup]] — picks up the cleaned file on its next 30-minute LaunchAgent pass
- [[Voice Notes (Optional)]] — sibling pipeline for iPhone-dictated voice notes
