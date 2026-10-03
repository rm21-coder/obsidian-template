"""
test_retire_templater.py -- Templater is gone, and QuickAdd does what it did.

Templater ran commands written in note text: dynamic commands (<%+ %>, and
<%*+ %> as JavaScript) in ANY note shown in reading view, with no setting or
folder limit, and every command in a new note when its creation trigger was
on. Retired 2026-10-03. Its jobs moved to QuickAdd: plain templates for notes
and people, and three user scripts (meeting pickers, Move to Knowledge, Clean
Filenames) that run only when their command is chosen. Updates remove the
plugin from existing installs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "Templates" / "Scripts"
TEMPLATES = REPO / "Templates"
QA = REPO / "Templates" / "QuickAdd"
QA_DATA = json.loads((REPO / ".obsidian" / "plugins" / "quickadd" / "data.json").read_text(encoding="utf-8"))
HOTKEYS = json.loads((REPO / ".obsidian" / "hotkeys.json").read_text(encoding="utf-8"))
CMDR = json.loads((REPO / ".obsidian" / "plugins" / "cmdr" / "data.json").read_text(encoding="utf-8"))
NODE = shutil.which("node")


def _choices(choices=None):
    for c in (QA_DATA["choices"] if choices is None else choices):
        yield c
        for step in (c.get("macro") or {}).get("commands", []):
            if step.get("type") == "NestedChoice":
                yield step["choice"]


# ---- Templater is gone -----------------------------------------------------

def test_no_shipped_template_uses_templater_syntax() -> None:
    offenders = [p.name for p in TEMPLATES.glob("*.md") if "<%" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_templater_is_not_enabled_pinned_or_configured() -> None:
    enabled = json.loads((REPO / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
    pins = json.loads((REPO / "installers" / "plugin-pins.json").read_text(encoding="utf-8"))
    assert "templater-obsidian" not in enabled
    assert "templater-obsidian" not in [p.get("id") for p in pins]
    assert not (REPO / ".obsidian" / "plugins" / "templater-obsidian").exists()
    assert "templater" not in json.dumps(HOTKEYS).lower()
    assert "templater" not in json.dumps(CMDR).lower()


def test_the_action_pseudo_templates_are_gone() -> None:
    assert not (TEMPLATES / "Move to Knowledge.md").exists()
    assert not (TEMPLATES / "Clean Filenames.md").exists()


# ---- QuickAdd's settings hold together -------------------------------------

def test_every_choice_points_at_files_that_exist() -> None:
    ids = [c["id"] for c in _choices()]
    assert len(ids) == len(set(ids))
    for c in _choices():
        if c["type"] == "Template":
            assert (REPO / c["templatePath"]).is_file(), c["name"]
        if c["type"] == "Capture":
            for path in re.findall(r"\{\{TEMPLATE:([^}]+)\}\}", c["format"]["format"]):
                assert (REPO / path).is_file(), path
        for step in (c.get("macro") or {}).get("commands", []):
            assert step["type"] in {"UserScript", "NestedChoice"}, step
            if step["type"] == "UserScript":
                assert (REPO / step["path"]).is_file(), step["path"]
                assert step["path"].startswith("Templates/QuickAdd/")


def test_every_hotkey_and_ribbon_button_runs_a_real_command() -> None:
    commands = {f"quickadd:choice:{c['id']}" for c in QA_DATA["choices"] if c.get("command")}
    used = [k for k in HOTKEYS if k.startswith("quickadd:")]
    used += [e["id"] for e in CMDR["leftRibbon"] if e["id"].startswith("quickadd:")]
    assert used and set(used) <= commands, set(used) - commands


def test_the_familiar_shortcuts_survive() -> None:
    by_name = {c["name"]: c for c in QA_DATA["choices"]}
    meeting = by_name["New Meeting"]
    assert meeting["id"] == "865f5ed1-38ae-42d0-9ed1-d4a5ce39e610"      # Cmd+Shift+M + ribbon
    assert meeting["type"] == "Macro"
    keys = {k: v[0]["key"] for k, v in HOTKEYS.items() if v}
    assert keys[f"quickadd:choice:{by_name['Insert People Template']['id']}"] == "P"
    assert keys[f"quickadd:choice:{by_name['Move to Knowledge']['id']}"] == "K"
    ribbon = {e["id"] for e in CMDR["leftRibbon"]}
    assert f"quickadd:choice:{by_name['Clean Filenames']['id']}" in ribbon


def test_the_meeting_template_uses_only_variables_the_script_sets() -> None:
    used = set(re.findall(r"\{\{VALUE:(\w+)\}\}", (TEMPLATES / "Meeting Template.md").read_text(encoding="utf-8")))
    script = (QA / "new_meeting.js").read_text(encoding="utf-8")
    set_ = set(re.findall(r"params\.variables\.(\w+) =", script))
    assert used and used == set_


def test_the_plugin_check_hashes_the_scripts_and_templates() -> None:
    import plugin_integrity_check as P
    refs = P._referenced_files(REPO, "quickadd", QA_DATA)
    for f in ("Templates/QuickAdd/new_meeting.js", "Templates/QuickAdd/move_to_knowledge.js",
              "Templates/QuickAdd/clean_filenames.js", "Templates/Meeting Template.md",
              "Templates/People Template.md"):
        assert f in refs, f


# ---- the scripts, run against a stand-in Obsidian --------------------------

HARNESS = r"""
const fs = require('fs');
const [script, scenario] = process.argv.slice(2);
const src = fs.readFileSync(script, 'utf8');
const module_ = { exports: null }; new Function('module', src)(module_);
const S = JSON.parse(scenario);
const notices = [], renamed = [], frontmatter = {};
const folder = (path, names) => ({ path, children: names.map(n => ({ basename: n.replace(/\.md$/, ''), name: n, extension: 'md', parent: { path } })) });
const tree = {};
for (const [p, names] of Object.entries(S.folders || {})) tree[p] = folder(p, names);
const existing = new Set(S.existing || []);
const app = {
  vault: {
    getAbstractFileByPath: (p) => tree[p] || (existing.has(p) ? { path: p } : null),
    read: async (f) => (S.contents || {})[f.path || f] || '',
  },
  workspace: { getActiveFile: () => S.active ? { name: S.active.split('/').pop(), basename: S.active.split('/').pop().replace(/\.md$/, ''), parent: { path: S.active.split('/').slice(0, -1).join('/') } } : null },
  fileManager: {
    renameFile: async (f, to) => { renamed.push([f.name, to]); },
    processFrontMatter: async (f, fn) => {
      if ((S.broken || []).includes(f.name)) throw new Error('YAML parse error');
      const fm = {}; fn(fm); frontmatter[f.name] = fm;
    },
  },
  metadataCache: { getFileCache: (f) => ((S.withProps || []).includes(f.name) || (S.broken || []).includes(f.name)) ? { frontmatter: {} } : {} },
};
// group files are read by path string in the script
app.vault.getAbstractFileByPath = ((orig) => (p) => orig(p) || ((S.contents || {})[p] !== undefined ? p : null))(app.vault.getAbstractFileByPath);
const answers = [...(S.answers || [])];
const params = {
  app,
  obsidian: { Notice: class { constructor(m) { notices.push(m); } } },
  quickAddApi: {
    suggester: async (items) => { const a = answers.shift(); if (a === '__cancel__') throw new Error('cancelled'); return a; },
    inputPrompt: async () => { const a = answers.shift(); return a === '__cancel__' ? null : a; },
  },
  variables: {},
  abort: (m) => { throw new Error('ABORT:' + m); },
};
module_.exports(params)
  .then(() => console.log(JSON.stringify({ ok: true, variables: params.variables, notices, renamed, frontmatter })))
  .catch((e) => console.log(JSON.stringify({ ok: false, error: String(e.message), notices, renamed })));
"""


def _run(tmp_path, script: str, scenario: dict) -> dict:
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    p = subprocess.run([NODE, str(h), str(QA / script), json.dumps(scenario)],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@needs_node
def test_group_meeting_takes_the_groups_people_without_images(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "new_meeting.js", {
        "folders": {"Groups": ["Board.md"]},
        "contents": {"Groups/Board.md": '![[Ann.png|40]] [[Ann Smith]]\n[[Bo "Q" Lee]] ![[Meetings.base#Person]]'},
        "answers": ["Group", "Board"]})
    v = out["variables"]
    assert out["ok"] and v["meetingType"] == "Group"
    assert v["groupSection"] == 'group:\n  - "[[Board]]"\n'
    assert v["peopleList"] == '  - "[[Ann Smith]]"\n  - "[[Bo \\"Q\\" Lee]]"'    # quote escaped, embeds gone
    assert v["titleLine"] == ""


@needs_node
def test_individual_and_cancelled_type(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "new_meeting.js", {"folders": {"People": ["Ann Smith.md"]}, "answers": ["Individual", "Ann Smith"]})
    assert out["variables"]["peopleList"] == '  - "[[Ann Smith]]"'
    out = _run(tmp_path, "new_meeting.js", {"answers": ["Ad-hoc", "Lunch {{DATE}} \\ talk"]})
    assert out["variables"]["meetingType"] == "Ad-hoc"
    assert out["variables"]["titleLine"] == 'title: "Lunch DATE \\\\ talk"\n'    # token braces dropped, backslash escaped


@needs_node
def test_adhoc_escape_cancels_instead_of_asking_forever(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "new_meeting.js", {"answers": ["Ad-hoc", "  ", "__cancel__"]})
    assert out["ok"] is False and "ABORT:No meeting title given" in out["error"]


@needs_node
def test_move_to_knowledge_moves_and_never_overwrites(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "move_to_knowledge.js", {"active": "Clippings/Idea.md"})
    assert out["renamed"] == [["Idea.md", "Knowledge/Idea.md"]]
    out = _run(tmp_path, "move_to_knowledge.js", {"active": "Clippings/Idea.md", "existing": ["Knowledge/Idea.md"]})
    assert out["renamed"] == [] and "already has a note named" in out["notices"][0]
    out = _run(tmp_path, "move_to_knowledge.js", {})
    assert out["notices"] == ["No active file to move"]


@needs_node
def test_clean_filenames_renames_keeps_the_title_and_skips_collisions(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "clean_filenames.js", {
        "folders": {"Clippings": ["Why ‘AI’ — now?.md", "Plain.md", "A•B.md", "a-b.md"]},
        "withProps": ["Why ‘AI’ — now?.md"]})
    assert out["renamed"] == [["Why ‘AI’ — now?.md", "Clippings/Why 'AI' - now.md"]]
    assert out["frontmatter"]["Why ‘AI’ — now?.md"] == {"title": "Why ‘AI’ — now?"}
    # "A-B.md" collides with "a-b.md" on a case-insensitive file system
    assert out["notices"] == ["Cleaned 1 filename, 2 already clean, 1 skipped (a note with the clean name exists)"]


@needs_node
def test_clean_filenames_edge_cases(tmp_path, allow_subprocess) -> None:
    out = _run(tmp_path, "clean_filenames.js", {
        "folders": {"Clippings": ["No props—here.md", "??.md", "?.env.md", "Bad•yaml.md"]},
        "broken": ["Bad•yaml.md"]})
    assert ["No props—here.md", "Clippings/No props-here.md"] in out["renamed"]
    assert "No props—here.md" not in out["frontmatter"]           # no properties: none added
    assert ["?.env.md", "Clippings/env.md"] in out["renamed"]           # no leading dot
    assert all(r[0] != "Bad•yaml.md" for r in out["renamed"])
    assert out["notices"] == ["Cleaned 2 filenames, 0 already clean, "
                              "1 skipped (nothing would be left of the name), 1 could not be renamed"]


# ---- updates remove the plugin ---------------------------------------------

def test_both_platforms_retire_the_same_plugins() -> None:
    lib = (REPO / "installers" / "lib" / "update.sh").read_text(encoding="utf-8")
    common = (SCRIPTS / "windows" / "common.ps1").read_text(encoding="utf-8")
    mac = re.search(r"RETIRED_PLUGINS=\(([^)]*)\)", lib).group(1).split()
    win = re.findall(r"'([\w-]+)'", re.search(r"\$RetiredPlugins = @\(([^)]*)\)", common).group(1))
    assert mac == win == ["templater-obsidian"]
    assert "Remove-RetiredPlugins -Vault $vault" in (SCRIPTS / "windows" / "update.ps1").read_text(encoding="utf-8")
    upd = (REPO / "update.sh").read_text(encoding="utf-8")
    assert 'retire_plugins "$VAULT"' in upd[upd.index("== 4/6 plugins =="):upd.index("# ---- 5.")]


def _retire(tmp_path, dry: int):
    vault = tmp_path / "vault"
    (vault / ".obsidian" / "plugins" / "templater-obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "plugins" / "templater-obsidian" / "main.js").write_text("x", encoding="utf-8")
    (vault / ".obsidian" / "plugins" / "quickadd").mkdir(parents=True)
    backup = tmp_path / "backup"
    p = subprocess.run(
        ["bash", "-c", 'source "$1/installers/lib/common.sh"; source "$1/installers/lib/update.sh";'
                       ' retire_plugins "$2" "$3" "$4"', "_", str(REPO), str(vault), str(backup), str(dry)],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    return p, vault, backup


def test_macos_update_moves_the_retired_plugin_out_of_the_vault(tmp_path, allow_subprocess) -> None:
    p, vault, backup = _retire(tmp_path, 0)
    assert p.returncode == 0, p.stderr
    assert not (vault / ".obsidian" / "plugins" / "templater-obsidian").exists()
    assert (backup / "plugins" / "templater-obsidian" / "main.js").exists()       # kept, outside the vault
    assert (vault / ".obsidian" / "plugins" / "quickadd").exists()
    assert "templater-obsidian: retired upstream; removed" in p.stdout + p.stderr


def test_macos_dry_run_leaves_it(tmp_path, allow_subprocess) -> None:
    p, vault, backup = _retire(tmp_path, 1)
    assert (vault / ".obsidian" / "plugins" / "templater-obsidian").exists() and not backup.exists()
    assert "dry run: would remove the plugin" in p.stdout + p.stderr


def test_windows_removal_moves_rather_than_deletes() -> None:
    common = (SCRIPTS / "windows" / "common.ps1").read_text(encoding="utf-8")
    fn = common[common.index("function Remove-RetiredPlugins"):]
    fn = fn[:fn.index("\n}\n")]
    assert "Move-Item -LiteralPath $dir" in fn and "Remove-Item" not in fn
    assert "Write-Warning" in fn
    common.encode("ascii")


def test_the_guide_no_longer_tells_anyone_to_use_templater() -> None:
    guide = (REPO / "docs" / "Obsidian Configuration Guide.md").read_text(encoding="utf-8")
    assert "Templater: Insert" not in guide and "Configure Templater" not in guide


@needs_node
@pytest.mark.parametrize("hostile,forbidden", [
    ("[[{}}{FIELD:classification}{{}]]", "{"),                      # one-pass brace stripping was bypassable
    ("[[x\n---\nclassification: public\nfoo]]", "\n"),          # a newline ended the frontmatter
    ("[[<%* app.vault %>]]", "<%"),                                   # Templater, while still loaded
])
def test_group_note_content_is_inert_in_the_yaml(tmp_path, allow_subprocess, hostile, forbidden) -> None:
    out = _run(tmp_path, "new_meeting.js", {
        "folders": {"Groups": ["G.md"]}, "contents": {"Groups/G.md": hostile}, "answers": ["Group", "G"]})
    people = out["variables"]["peopleList"]
    assert people and forbidden not in people.replace("<\u200b%", "")
    assert people.count("\n") == 0


def test_updaters_refuse_while_obsidian_is_open() -> None:
    upd = (REPO / "update.sh").read_text(encoding="utf-8")
    pull = upd[upd.index('info "== 1/6 pull =="'):]
    assert pull.index("obsidian_running") < pull.index('git -C "$REPO_ROOT" pull --ff-only')
    assert pull.index("obsidian_running") < pull.index("dirty=")
    ps = (SCRIPTS / "windows" / "update.ps1").read_text(encoding="utf-8")
    first = ps[ps.index("if (-not $AfterPull) {"):]
    assert first.index("Get-Process -Name Obsidian") < first.index("git -C $vault pull --ff-only")
    assert ps.index("Remove-RetiredPlugins -Vault $vault") < ps.index("== 3/3 scheduled tasks ==")


def test_obsidian_running_detection(tmp_path, allow_subprocess) -> None:
    for code, expect in ((0, 0), (1, 1)):
        fake = tmp_path / f"pgrep{code}"
        fake.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
        fake.chmod(0o755)
        p = subprocess.run(["bash", "-c", 'source "$1/installers/lib/common.sh"; source "$1/installers/lib/update.sh"; obsidian_running',
                            "_", str(REPO)], env={**os.environ, "OBSIDIAN_PGREP": str(fake)})
        assert p.returncode == expect


def test_quickadd_settings_will_not_dirty_the_tree_on_first_launch() -> None:
    raw = (REPO / ".obsidian" / "plugins" / "quickadd" / "data.json").read_text(encoding="utf-8")
    assert not raw.endswith("\n")                    # Obsidian writes it without one
    pins = json.loads((REPO / "installers" / "plugin-pins.json").read_text(encoding="utf-8"))
    pinned = [p["ref"] for p in pins if p["id"] == "quickadd"][0]
    assert QA_DATA["version"] == pinned               # else QuickAdd re-stamps it on first launch


def test_no_stale_templater_entries_remain() -> None:
    assert "templater-obsidian" not in (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "templater" not in (REPO / ".obsidian" / "ribbon-config.json").read_text(encoding="utf-8").lower()


# ---- the ARM laptop run of 9b39b8a (2026-10-03) -----------------------------

@needs_node
@pytest.mark.parametrize("scenario,marker", [
    ({"answers": ["__cancel__"]}, "ABORT:New Meeting cancelled"),                                  # type picker
    ({"folders": {"Groups": ["G.md"]}, "answers": ["Group", "__cancel__"]}, "ABORT:New Meeting cancelled"),
    ({"folders": {"People": ["P.md"]}, "answers": ["Individual", "__cancel__"]}, "ABORT:New Meeting cancelled"),
    ({"answers": ["Group"]}, "ABORT:No notes in Groups/ to choose from"),                          # empty folder
])
def test_escape_at_any_meeting_picker_makes_no_note(tmp_path, allow_subprocess, scenario, marker) -> None:
    """Escape at the group list used to leave an empty "Group" meeting note."""
    out = _run(tmp_path, "new_meeting.js", scenario)
    assert out["ok"] is False and marker in out["error"]


def test_no_hotkey_pairs_ctrl_with_mod() -> None:
    """Mod is Ctrl on Windows, so ["Alt","Ctrl","Mod"] read as Ctrl+Ctrl+Alt there and
    could not be pressed (Insert People Template, Move to Knowledge, Tasks' edit task)."""
    for command, bindings in HOTKEYS.items():
        for b in bindings:
            assert not {"Ctrl", "Mod"} <= set(b.get("modifiers", [])), command


def test_quickadd_settings_are_already_in_222s_format() -> None:
    """QuickAdd 2.22 migrated the 2.12-format file on first launch and saved it, so
    every install's tree was dirty and the next update refused. The shipped file is
    2.22's own output, with every migration recorded: verified 2026-10-03 by running
    the pinned 2.22.0 bundle's load path against it, which then saved nothing."""
    assert all(QA_DATA["migrations"].values()) and len(QA_DATA["migrations"]) >= 16
    assert "templateFolderPath" not in QA_DATA and QA_DATA["templateFolderPaths"] == []
    by_name = {c["name"]: c for c in _choices()}
    for name in ("New Note", "New Software Note", "Meeting note"):
        assert by_name[name]["fileExistsBehavior"] == {"kind": "apply", "mode": "duplicateSuffix"}, name
        assert "fileExistsMode" not in by_name[name] and "setFileExistsBehavior" not in by_name[name]
    assert by_name["New Person"]["fileExistsBehavior"] == {"kind": "prompt"}


def test_other_settings_files_match_what_obsidian_writes() -> None:
    omni = (REPO / ".obsidian" / "plugins" / "omnisearch" / "data.json").read_text(encoding="utf-8")
    keys = list(json.loads(omni))
    assert keys.index("indexFilesWithoutExtension") == keys.index("indexedFileTypes") + 1
    assert not (REPO / ".obsidian" / "types.json").read_text(encoding="utf-8").endswith("\n")
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert ".obsidian/**/*.json text eol=lf" in attrs
