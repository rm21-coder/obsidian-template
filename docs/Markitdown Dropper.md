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

- `markitdown_dropper.py` — the PySide6 GUI app (place in `/Applications/` or any always-accessible folder)
- `Markitdown Dropper.command` — double-click launcher that creates and maintains the venv
- `Templates/Scripts/markitdown_cleanup.py` — post-conversion cleanup module (imported by the dropper)
- `~/.markitdown-dropper-venv/` — isolated Python 3.13 environment with `markitdown[all]` and `PySide6`
- `~/.markitdown_dropper.json` — saved destination folder

## Cleanup pass

After Markitdown converts a file, the dropper imports `markitdown_cleanup.py` from `~/Obsidian/Templates/Scripts/` (you place a copy there during setup) and runs the converted text through `clean()` before writing it. The cleanup is intentionally conservative — it only changes things it can be confident about, so it doesn't mangle nuance in dictation-style notes or pasted email content.

**What it does:**

1. **Extracts inline base64 images** — Markitdown converts embedded images in `.docx` / `.pdf` files into giant `![Image](data:image/png;base64,...)` blobs. The cleanup decodes each, saves it as `<source-stem>-img-N.png` in `Z_attachments/`, and replaces the inline blob with an Obsidian wiki-link `![[name.png]]`. Images still render in Obsidian and the markdown becomes RAG-indexable.
2. **Recovers images from source archives when Markitdown emits stubs** — sometimes Markitdown can't extract an image and emits `![](data:image/png;base64...)` (literal `...`, no real data). For `.docx`, `.pptx`, and `.xlsx` sources, the cleanup opens the source as a ZIP, pulls images from `word/media/` (or `ppt/media/` / `xl/media/`), saves them as `<source-stem>-source-img-N.<ext>`, and uses them to replace stubs in document order. Any extracted images that didn't match a stub are appended in a `## Images from source` section so nothing silently disappears. Stubs without a matching extracted image become a clear text placeholder. Non-renderable formats (WMF, EMF) are skipped.
3. **Normalizes bullet markers** — converts Outlook/Word's `•`, `○`, `▪`, `▸`, `▹`, `‣`, `◦`, `●`, `⁃` to standard `-`. Tab indentation becomes two-space indentation while preserving nesting depth.
4. **Promotes two strict heading patterns to `##`** — used because RAG chunkers (Open WebUI's Markdown Header Splitter, etc.) need header anchors:
    - `N. **Heading text**` on its own line (numbered + entirely bold)
    - `**Heading text:**` on its own line (bold label ending in colon)
   Anything ambiguous is left alone. Inline `**bold emphasis**` inside paragraphs is never promoted.
5. **Normalizes whitespace** — strips trailing spaces, collapses any run of blank lines to a single blank, trims leading and trailing blanks.
6. **Adds minimal YAML frontmatter** when the file has none:
    ```yaml
    ---
    title: <derived from source filename>
    created: <today>
    source: markitdown
    source_file: <original filename with extension>
    tags: []
    ---
    ```
   The empty `tags: []` lets the semantic auto-tagger fill it in on its next 30-minute LaunchAgent pass.

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

Then:

1. Copy `Templates/Scripts/markitdown_cleanup.py` from this vault to `~/Obsidian/Templates/Scripts/markitdown_cleanup.py` (the dropper's launcher inserts `~/Obsidian/Templates/Scripts/` on `sys.path` so the module is importable).
2. Place `markitdown_dropper.py` and `Markitdown Dropper.command` together in a stable folder — `/Applications/` (or any always-accessible folder). They must live next to each other; the launcher resolves the script via `dirname`.
3. Double-click `Markitdown Dropper.command`. The first launch builds the venv and installs `markitdown[all]` plus `PySide6` (~30–60 seconds). Subsequent launches take a few seconds because markitdown loads its [magika](https://github.com/google/magika) ONNX model once at startup; after that, drops are near-instant.
4. On first launch the app prompts for the destination folder — point it at this vault's `Creations/` folder. The choice persists in `~/.markitdown_dropper.json`.

A Terminal window stays open behind the app while it's running — that's the launcher process. Closing it quits the app.

## Updating markitdown

```bash
~/.markitdown-dropper-venv/bin/pip install --upgrade 'markitdown[all]'
```

The `[all]` extra pulls converters for every supported format. Plain `pip install markitdown` installs only a slim core and will throw `MissingDependencyException` for files like `.docx`.

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
