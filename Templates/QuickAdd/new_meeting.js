// new_meeting.js -- QuickAdd user script, first step of the "New Meeting" macro.
//
// Asks what kind of meeting it is and fills the variables that
// Templates/Meeting Template.md uses ({{VALUE:meetingType}} and friends):
//
//   Group       pick a note from Groups/; its people become the attendees
//   Individual  pick a note from People/
//   Ad-hoc      type a title (required)
//
// Replaces the Templater script that used to live inside Meeting Template.md
// (Templater retired 2026-10-03). Runs only when you choose New Meeting -- never
// because a note was opened or created.
//
// Text from note names, group notes and the ad-hoc title goes into YAML double
// quotes, so it is made inert there: line breaks become spaces (a newline could
// end the frontmatter early), backslashes and quotes are escaped, every brace
// is dropped (one pass over "{{" was bypassable: "{}}{FIELD:x}{{}" became
// "{{FIELD:x}}"), and "<%" is split with a zero-width space, as templater_guard
// does, for any install where Templater is still loaded.

const IMAGE_EMBED = /\.(png|jpg|jpeg|gif|svg|webp|bmp)\|?\d*\]\]/i;
const BASE_EMBED = /\.base[#\]]/i;

function yamlSafe(text) {
  return String(text)
    .replace(/[\r\n\u2028\u2029]+/g, " ")
    .replace(/[{}]/g, "")
    .replace(/<%/g, "<\u200b%")
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"');
}

function notesIn(app, folder) {
  const dir = app.vault.getAbstractFileByPath(folder);
  if (!dir || !dir.children) return [];
  return dir.children
    .filter((f) => f.extension === "md")
    .map((f) => f.basename)
    .sort((a, b) => a.localeCompare(b));
}

module.exports = async (params) => {
  const { app, quickAddApi } = params;

  // Escape at any picker cancels the command, so no note is made -- as Escape
  // does at New Note's picker and at the ad-hoc title. (Choosing a type and
  // then escaping the group list used to leave an empty "Group" meeting.)
  const pick = async (items, placeholder, what) => {
    if (!items.length) params.abort(`No ${what} to choose from`);
    let answer = null;
    try {
      answer = await quickAddApi.suggester(items, items, placeholder);
    } catch (e) {
      answer = null;
    }
    if (!answer) params.abort("New Meeting cancelled");
    return answer;
  };

  const meetingType = await pick(["Group", "Individual", "Ad-hoc"], "Meeting type:", "meeting types");
  let titleLine = "";
  let groupSection = "";
  let peopleList = "";

  if (meetingType === "Group") {
    const selected = await pick(notesIn(app, "Groups"), "Select a meeting group:", "notes in Groups/");
    groupSection = `group:\n  - "[[${yamlSafe(selected)}]]"\n`;
    const file = app.vault.getAbstractFileByPath(`Groups/${selected}.md`);
    const content = file ? await app.vault.read(file) : "";
    const links = content.match(/\[\[([^\]]+)\]\]/g) || [];
    peopleList = links
      .filter((link) => !IMAGE_EMBED.test(link) && !BASE_EMBED.test(link))
      .map((link) => `  - "${yamlSafe(link)}"`)
      .join("\n");
  } else if (meetingType === "Individual") {
    const selected = await pick(notesIn(app, "People"), "Select a person:", "notes in People/");
    peopleList = `  - "[[${yamlSafe(selected)}]]"`;
  } else {
    let title = "";
    while (!title.trim()) {
      let answer = null;
      try {
        answer = await quickAddApi.inputPrompt("Ad-hoc meeting title (required):");
      } catch (e) {
        answer = null;
      }
      // Escape cancels the whole command rather than asking forever.
      if (answer === null || answer === undefined) params.abort("No meeting title given");
      title = String(answer);
    }
    titleLine = `title: "${yamlSafe(title.trim())}"\n`;
  }

  params.variables.meetingType = meetingType;
  params.variables.titleLine = titleLine;
  params.variables.groupSection = groupSection;
  params.variables.peopleList = peopleList;
};
