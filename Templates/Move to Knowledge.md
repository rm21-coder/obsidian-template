<%*
const file = app.workspace.getActiveFile();
if (file) {
  const newPath = `Knowledge/${file.name}`;
  await new Promise(r => setTimeout(r, 200));
  await app.fileManager.renameFile(file, newPath);
  new Notice(`Moved "${file.basename}" to Knowledge folder`);
} else {
  new Notice("No active file to move");
}
-%>
