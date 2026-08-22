<%*
const clippingsFolder = app.vault.getAbstractFileByPath("Clippings");
if (!clippingsFolder || !clippingsFolder.children) {
  new Notice("Clippings folder not found or empty");
  return;
}

const files = clippingsFolder.children.filter(f => f.extension === "md");
if (files.length === 0) {
  new Notice("No markdown files in Clippings folder");
  return;
}

// Replacement map
const replacements = {
  '\u2022': '-',    // bullet •
  '\u2605': 'star', // ★
  '?': '',
  '\u2018': "'",    // left single quote
  '\u2019': "'",    // right single quote
  '\u201C': "'",    // left double quote
  '\u201D': "'",    // right double quote
  '\u2014': '-',    // em dash
  '\u2013': '-',    // en dash
  '\u00e8': 'e',    // è
  '\u00e9': 'e',    // é
  '\u00f1': 'n',    // ñ
  '\u00fc': 'u',    // ü
  '\u00e4': 'a',    // ä
  '\u00f6': 'o',    // ö
  '\u00e7': 'c',    // ç
  '\u0107': 'c',    // ć
  '\u00bd': '1-2',  // ½
  '\u00bc': '1-4',  // ¼
  '\u00be': '3-4',  // ¾
};

function cleanName(name) {
  let newName = name;
  for (const [from, to] of Object.entries(replacements)) {
    newName = newName.split(from).join(to);
  }
  newName = newName.replace(/[\\/:*?"<>|]/g, '');
  newName = newName.replace(/  +/g, ' ').trim();
  return newName;
}

let renamed = 0;
let skipped = 0;

for (const file of files) {
  const oldName = file.basename;
  const newName = cleanName(oldName);

  if (newName === oldName) {
    skipped++;
    continue;
  }

  // Add title property with original name if not already present
  const content = await app.vault.read(file);
  if (content.startsWith('---')) {
    const endIdx = content.indexOf('---', 3);
    if (endIdx !== -1) {
      const fm = content.substring(0, endIdx);
      if (!fm.includes('\ntitle:') && !fm.includes('\nTitle:')) {
        const newContent = fm + 'title: "' + oldName + '"\n' + content.substring(endIdx);
        await app.vault.modify(file, newContent);
      }
    }
  }

  // Rename the file (Obsidian auto-updates wiki links)
  const newPath = file.parent.path + '/' + newName + '.' + file.extension;
  await new Promise(r => setTimeout(r, 100));
  await app.fileManager.renameFile(file, newPath);
  renamed++;
}

new Notice(`Cleaned ${renamed} filenames, ${skipped} already clean`);
-%>
