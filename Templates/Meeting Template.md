<%*
const options = ["Group", "Individual", "Ad-hoc"];
const choice = await tp.system.suggester(options, options, false, "Meeting type:");
const meetingType = choice || "Ad-hoc";
let peopleList = "";
let groupSection = "";
let titleLine = "";
if (meetingType === "Group") {
  groupSection = `group:\n`;
  const groupFolder = app.vault.getAbstractFileByPath("Groups");
  if (groupFolder && groupFolder.children) {
    const groupFiles = groupFolder.children
      .filter(f => f.extension === "md")
      .map(f => f.basename)
      .sort();
    const selected = await tp.system.suggester(groupFiles, groupFiles, false, "Select a meeting group:");
    if (selected) {
      groupSection = `group:\n  - "[[${selected}]]"\n`;
      const file = app.vault.getAbstractFileByPath(`Groups/${selected}.md`);
      const content = await app.vault.read(file);
      const names = content.match(/\[\[([^\]]+)\]\]/g);
      if (names) {
        const filtered = names.filter(n => !n.match(/\.(png|jpg|jpeg|gif|svg|webp|bmp)\|?\d*\]\]/i) && !n.match(/\.base[#\]]/i));
        peopleList = filtered.map(n => `  - "${n}"`).join("\n");
      }
    }
  }
} else if (meetingType === "Individual") {
  const peopleFolder = app.vault.getAbstractFileByPath("People");
  if (peopleFolder && peopleFolder.children) {
    const people = peopleFolder.children
      .filter(f => f.extension === "md")
      .map(f => f.basename)
      .sort();
    const selected = await tp.system.suggester(people, people, false, "Select a person:");
    if (selected) {
      peopleList = `  - "[[${selected}]]"`;
    }
  }
} else if (meetingType === "Ad-hoc") {
  let title = "";
  while (!title || !title.trim()) {
    const resp = await tp.system.prompt("Ad-hoc meeting title (required):");
    title = (resp == null) ? "" : resp;
  }
  const safe = title.trim().replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  titleLine = `title: "${safe}"\n`;
}
-%>
---
categories:
  - "[[Meetings]]"
type: <% meetingType %>
<% titleLine %><% groupSection %>people:
<% peopleList %>
classification: confidential
tags: []
---
