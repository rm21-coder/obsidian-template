// clean_filenames.js -- QuickAdd user script behind the "Clean Filenames" command.
//
// Renames every note in Clippings/ whose name carries characters that trip up
// links and other tools (curly quotes, dashes, bullets, accents, characters
// that are illegal in file names), and keeps the original name in a `title`
// property. Obsidian updates the links to each renamed note.
//
// Replaces the Templater action Templates/Clean Filenames.md (Templater
// retired 2026-10-03). Two fixes on the way: the title goes in through
// Obsidian's own frontmatter writer, so a name containing a quote no longer
// breaks the YAML, and a rename that would collide with an existing note is
// skipped rather than failing half-way.

const REPLACEMENTS = {
  "\u2022": "-",   // bullet
  "\u2605": "star",
  "?": "",
  "\u2018": "'",
  "\u2019": "'",
  "\u201C": "'",
  "\u201D": "'",
  "\u2014": "-",   // em dash
  "\u2013": "-",   // en dash
  "\u00E8": "e",
  "\u00E9": "e",
  "\u00F1": "n",
  "\u00FC": "u",
  "\u00E4": "a",
  "\u00F6": "o",
  "\u00E7": "c",
  "\u0107": "c",
  "\u00BD": "1-2",
  "\u00BC": "1-4",
  "\u00BE": "3-4",
};

function cleanName(name) {
  let out = name;
  for (const [from, to] of Object.entries(REPLACEMENTS)) out = out.split(from).join(to);
  out = out.replace(/[\\/:*?"<>|]/g, "");
  out = out.replace(/  +/g, " ").trim();
  // A leading dot would hide the note in Obsidian and in Finder/Explorer.
  return out.replace(/^\.+/, "").trim();
}

module.exports = async (params) => {
  const { app, obsidian } = params;
  const folder = app.vault.getAbstractFileByPath("Clippings");
  if (!folder || !folder.children) {
    new obsidian.Notice("Clippings folder not found");
    return;
  }
  const files = folder.children.filter((f) => f.extension === "md");
  if (!files.length) {
    new obsidian.Notice("No notes in Clippings");
    return;
  }

  // Taken names, compared without case: macOS and Windows file systems treat
  // "Cafe's.md" and "cafe's.md" as the same file.
  const taken = new Set(folder.children.map((f) => f.name.toLowerCase()));

  let renamed = 0;
  let clean = 0;
  let collided = 0;
  let unnamed = 0;
  let failed = 0;
  for (const file of files) {
    const oldName = file.basename;
    const newName = cleanName(oldName);
    if (newName === oldName) {
      clean++;
      continue;
    }
    if (!newName) {
      unnamed++;
      continue;
    }
    const newFile = `${newName}.${file.extension}`;
    if (taken.has(newFile.toLowerCase())) {
      collided++;
      continue;
    }
    try {
      // Keep the original name as a `title` property -- only in a clipping
      // that already has frontmatter, as before, and only if it has no title.
      const cache = app.metadataCache.getFileCache(file);
      if (cache && cache.frontmatter) {
        await app.fileManager.processFrontMatter(file, (fm) => {
          if (fm.title === undefined && fm.Title === undefined) fm.title = oldName;
        });
      }
      await app.fileManager.renameFile(file, `${file.parent.path}/${newFile}`);
      taken.delete(file.name.toLowerCase());
      taken.add(newFile.toLowerCase());
      renamed++;
    } catch (e) {
      // Malformed frontmatter, or a rename the file system refused: leave
      // this note as it is and carry on with the rest.
      failed++;
    }
  }

  const parts = [`Cleaned ${renamed} filename${renamed === 1 ? "" : "s"}`, `${clean} already clean`];
  if (collided) parts.push(`${collided} skipped (a note with the clean name exists)`);
  if (unnamed) parts.push(`${unnamed} skipped (nothing would be left of the name)`);
  if (failed) parts.push(`${failed} could not be renamed`);
  new obsidian.Notice(parts.join(", "));
};
