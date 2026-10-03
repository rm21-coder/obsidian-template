// move_to_knowledge.js -- QuickAdd user script behind the "Move to Knowledge" command.
//
// Moves the open note into Knowledge/. Obsidian updates the links to it.
// Replaces the Templater action Templates/Move to Knowledge.md (Templater
// retired 2026-10-03). Refuses rather than overwrites when a note of the same
// name is already there.

module.exports = async (params) => {
  const { app, obsidian } = params;
  const file = app.workspace.getActiveFile();
  if (!file) {
    new obsidian.Notice("No active file to move");
    return;
  }
  if (file.parent && file.parent.path === "Knowledge") {
    new obsidian.Notice(`"${file.basename}" is already in Knowledge`);
    return;
  }
  const target = `Knowledge/${file.name}`;
  if (app.vault.getAbstractFileByPath(target)) {
    new obsidian.Notice(`Knowledge already has a note named "${file.basename}"; nothing moved`);
    return;
  }
  await app.fileManager.renameFile(file, target);
  new obsidian.Notice(`Moved "${file.basename}" to Knowledge`);
};
