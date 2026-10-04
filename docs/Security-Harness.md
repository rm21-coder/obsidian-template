# Security Harness (Optional)

Two lightweight monitors that watch the parts of this workflow an attacker
would actually target: the community-plugin code Obsidian loads, and the
automation scripts and LaunchAgents themselves. They are standard-library
Python 3 (no dependencies), run **read-only** on a daily schedule, and make
**no network calls** — nothing leaves the machine. They are installed by
default as part of `./install.sh` (component `49-security-controls`).

## Process auditing is your EDR's job — deliberately not a control here

Earlier revisions of this harness shipped a third control that watched the
processes Obsidian spawns. It was retired on evidence, and the reasoning is
worth keeping because it applies to any user-space attempt at the same thing:

- **macOS:** the control scraped the unified log for `posix_spawn`/exec
  events. On macOS 26, Apple restricted those kernel events from user-level
  log queries — verified live: zero events visible over a 24-hour window
  with Obsidian active. A control whose data source is empty is worse than
  no control: it reports a clean pass forever, regardless of what actually
  happened (a *silent-failure* control).
- **Windows:** a CIM process-tree snapshot poll worked, but a poll can only
  see what is alive at the instant it runs — and Microsoft Defender ships
  on every Windows machine with continuous, kernel-level visibility of
  exactly these events.

Process-level monitoring belongs to the endpoint security layer: Microsoft
Defender for Endpoint, CrowdStrike, or any ESF/ETW-based EDR sees every
spawn with full arguments, continuously, at a fidelity user-space scripting
cannot reach. If this machine runs managed EDR, that requirement is already
met. If it runs nothing, enable the built-in protection your OS ships
(Defender on Windows; on macOS, consider an ESF-based tool) rather than
trusting a scraper that modern macOS has already blinded once.

## What it defends against

- **Supply-chain drift** — a community plugin auto-updates and its code changes,
  possibly maliciously, without your involvement.
  (Install-time complement: the installer only ever fetches plugin releases
  pinned by tag and SHA256 in `installers/plugin-pins.json` — upgrading a
  plugin is a deliberate re-pin + commit, never an ambient "latest".)
- **Tampering** — someone (or something) modifies your automation scripts, your
  LaunchAgents, or the security baseline itself.
- **Bulk data loss** — a large, unexpected deletion of vault notes.

(Unexpected process execution — Obsidian or an Electron helper spawning a
shell, interpreter, or network tool — is the EDR layer's job; see above.)

### One deployed file is expected to differ from its pin

The pin guarantee is **at download time**: every plugin file is verified
against `installers/plugin-pins.json` before it is moved into place, and a
mismatch is a hard refusal with nothing written.

Exactly one file is then modified on purpose. Installer component 31
(`installers/lib/quickadd_patch.py`) rewrites a call inside QuickAdd's
`main.js` after install, so the deployed bytes of `quickadd/main.js` no
longer match the pinned hash. This is by design and is documented here
because the natural audit — hash every deployed plugin file against the pin
manifest — will report exactly one mismatch and it is not tampering. A
2026-08-25 independent verification did precisely that: 36 matched, 1
mismatched, and the one was this.

So: "all deployed plugin files match their pinned hashes" is **not** a true
statement about this workflow and should not be asserted as a control. The
true statements are that every plugin was verified against its pin before
installation, and that exactly one file is deliberately patched afterwards.

The plugin integrity monitor is unaffected — it baselines post-install state,
so the patched `main.js` is simply what it records as normal, and a later
unexplained change to it still alerts.

### Outside text is never plugin code

This is not one of the two controls, but it is one less thing for them to
watch. Templater runs dynamic commands (`<%+ … %>`, and `<%*+ … %>` as
JavaScript) in the rendered text of every note shown in reading view, with no
setting or folder limit. It also runs every command in a new note when
"Trigger Templater on new file creation" is on. The template stopped shipping Templater on 2026-10-03 and updates remove it (see the Obsidian Configuration Guide, 3.2); this stays for anyone who installs it themselves.

The scripts write other people's words into notes:

- invite subjects and attendee names;
- clipped pages;
- transcripts and converted documents;
- report lines that repeat any of these.

Each of those writers passes its text through `templater_guard.neutralize()`.
It puts a zero-width space between the `<` and the `%` in every rendered form
of the opener (`&lt;%`, `&#60;%`, `<&#37;`, `\<\%`, …). The text reads the
same, but Templater no longer sees a command.

Other plugins run code from note text too, and the guard defuses those
triggers the same way (since 2026-10-03):

- **Tasks.** A `tasks` block's `filter by function`, `sort by function` and
  `group by function` lines are JavaScript. Tasks runs them whenever the block
  renders, in Live Preview as well as reading view, and has no setting to turn
  them off.
- **Dataview.** `` `= …` `` inline queries are on by default, and their result
  renders as markdown. A crafted query could build a remote image URL out of
  another note's text, so opening the note sends that text to an outside
  server. `dataview` / `dataviewjs` blocks and `` `$= …` `` are covered too.
- **Dataview inside code blocks.** Dataview also runs an `=` query that
  begins a code block, any code block, by default.
- **Bases.** Obsidian's own `base` blocks can build an image URL from note
  properties, the same channel as a Dataview query.
- **Meta Bind, Metadata Menu, Excalidraw.** Their blocks (`meta-bind-*`,
  `mdm`, `excalidraw-script-install`) and Meta Bind's inline `INPUT[`,
  `VIEW[` and `BUTTON[` render controls that act on a click. Excalidraw's
  frontmatter keys would open a note as a drawing and offer to run its script.

A zero-width space goes between the fence and the language, after the
opening backticks of inline code, before a line that starts a code-block
query, and inside a raw `<code` tag. The language is matched the way Obsidian
finds it: entities and backslash escapes decoded, case ignored, leading
whitespace (JavaScript's `trim()` set) skipped, on any line, so fences inside
list items and quotes count. A language that would smuggle in a second CSS
class (a form feed, or an encoded line break, before `language-tasks`) is
rewritten so it cannot. Other code blocks keep their language, so syntax
highlighting still works.

`tests/test_templater_guard.py` lists every guarded writer. Any new script
that writes outside text into the vault belongs on that list.

The guard has five limits:

- **Copied text keeps the space.** Text copied out of a neutralised note
  carries the zero-width space, so a clipped JSP or ASP snippet (`<%= … %>`),
  an Excel formula in code (`` `=SUM(A1:A9)` ``) or an HTML sample using
  `<code>` no longer pastes cleanly.
- **"=" underlined headings show as text.** A heading written as a line of
  text over a line of `===` loses its heading formatting, because the guard
  defuses every line that starts with `=`. Converted and clipped pages use
  `#` headings, so this is rare.
- **New clippings are briefly raw.** The Web Clipper writes a note directly,
  and it stays unchanged until `strip_ads` rewrites it: a few seconds on a Mac,
  where a folder watch starts it, and up to 5 minutes on Windows, where it runs
  on a 5-minute schedule. For Templater this matters only if the note is
  switched to reading view in that window. A plugin block renders in Live
  Preview too, so opening a clipping of a hostile page in that window would
  run it.
- **Search blocks stay live.** Obsidian's core `query` block is left alone. It
  lists vault search results inside the note, on screen only, with no way to
  send them anywhere.
- **Exotic YAML keys are not parsed.** An Excalidraw key spelled with escapes
  in an explicit-key or multi-line flow layout (`? ` with the key on the next
  line, or `{? "…"` across lines) can still open a note as a drawing. Its
  onload script stays off: that setting ships disabled and asks before it runs.

Either way, the export gate blocks a dynamic command anywhere in an export
(see `Data-Classification.md`).

## The two controls

| Control | Script | Schedule | What it checks |
|---------|--------|----------|----------------|
| Plugin integrity | `plugin_integrity_check.py` | daily 06:30 + on change to the plugins folder | SHA-256 of each plugin's `main.js` and `manifest.json` under `<vault>/.obsidian/plugins/`, diffed against an HMAC-signed allowlist. |
| Workflow integrity | `integrity_monitor.py` | daily 06:35 + on change to scripts / LaunchAgents / state dir | SHA-256 of the scripts in `Templates/Scripts/` (and any module or bytecode planted beside them), every plist in `~/Library/LaunchAgents/`, the controls' own state files, the templates, the scripts' virtualenv, the Claude CLI's instructions and command-running settings, and the secrets file; every cached `.pyc` of the scripts checked against its source; plus a bulk-deletion guard on the vault's Markdown count. See "What the integrity monitor covers". |

Both are wired as LaunchAgents (`com.obsidian.security.plugin-check`,
`com.obsidian.security.integrity`), each run
via `/usr/bin/python3` so they keep working even if the per-vault virtualenv is
broken.

### What the integrity monitor covers

Each scope is hashed and compared with the baseline:

- **scripts** — `Templates/Scripts/`, including any `.pyc`, `.so`, `.pyd`,
  `.dylib` or `.pth` planted beside the scripts. The scripts' folder comes
  first on Python's import path, so a planted `requests.pyc` would shadow the
  real package.
- **launchagents** — the plists in `~/Library/LaunchAgents/` (on Windows, the
  `\Obsidian\` scheduled tasks).
- **state_dir** — the controls' own trust anchors.
- **script_config** — `Templates/Scripts/.config/*.json`, the jobs' own
  settings. `meeting_pull.json` names the tools the unattended Claude session
  may use; `meeting_pull.py` also refuses any tool name that is not a single
  identifier.
- **templates** — the `.md` and `.js` files under `Templates/` that QuickAdd
  fills or runs.
- **venv** — every code file in the scripts' virtualenv (`.py`, `.pyc`, `.pth`,
  `.so`, `.dylib`, …), `pyvenv.cfg` and the interpreter links. A `.pth` line
  or an edited package runs inside every job without touching a watched
  script. The CA bundle (`cacert.pem`) and plugin registries
  (`entry_points.txt`) count too. It changes legitimately when an update
  reinstalls the requirements.
- **user_site** — Python's per-user `site-packages` (`~/Library/Python/…`,
  `%APPDATA%\Python\…`), which every interpreter started without `-s` loads,
  Apple's `/usr/bin/python3` included. Normally empty.
- **agent_config** — `CLAUDE.md` in `~/.claude/` and in the vault, and only the
  settings that run commands or skip asking: `hooks`, `statusLine`,
  `apiKeyHelper`, `env`, `permissions`, and the MCP servers in `~/.claude.json`.
  The rest of those files changes all the time and is not compared.
- **secrets** — `~/dev/secrets/.env`, by hash. On macOS and Linux, a mode that
  lets group or others read it is a finding on every run.

**Bytecode** needs no baseline. Every cached `.pyc` of the scripts that an
interpreter would load instead of the source must be exactly what that source
compiles to. That includes Apple's `/usr/bin/python3`, which runs the security
controls and keeps its cache in `~/Library/Caches/com.apple.python`; every
`.pyc` there is checked, the standard library's included. A stale
`.pyc` doesn't count, since Python recompiles it; one for another Python
version is ignored by the interpreter that runs the job. The venv's cache is
checked by the venv's own interpreter, in a child process whose own imports
skip the cache it is checking.

A baseline taken before a scope existed reports `NOT_BASELINED` once for that
scope, not every file in it as new. Review, then adopt with `--update`.

### What the integrity monitor cannot see

It runs as you, so it is a tripwire, not a boundary. Code already running as
you can rewrite the monitor itself, or what it imports, and silence it. What
it buys is that a change to anything the jobs run, made while the monitor is
intact, is reported the next morning. The deploy and update steps, which
compare the vault with the repository, are the check from outside. Not
watched, by decision:

- **The Python install the virtualenv is built on.** Homebrew's (or a per-user
  Windows install's) standard library and `sitecustomize` are writable by you
  and change on every upgrade. The security controls run on Apple's
  `/usr/bin/python3`, whose library is not writable.
- **The Claude CLI's own binary.** It updates itself.
- **launchd's user environment** (`launchctl setenv`), which reaches every job
  without a file changing.
- **`CLAUDE.md` in a parent of the meeting pull's temporary working
  directory**, and `~/.claude/rules`.
- **Bytecode for a Python version that runs no job**, such as a Homebrew
  `python3` used by hand. Run the controls with `/usr/bin/python3`, as shown
  above.
- **On Windows**, the secrets file's ACL is not checked, and when the monitor
  runs under the venv interpreter it checks that interpreter's cache in
  process.

## Where alerts go

- **Alert log (append-only):** `~/.local/share/obsidian-security/alerts.log` —
  one JSON record per finding (`control`, `summary`, `findings`, `ts`).
- **macOS notification** (with sound) on each finding.
- **launchd output:** `~/Library/Logs/obsidian-security.log` — both
  controls redirect stdout and stderr here. Every line is stamped
  `YYYY-MM-DDTHH:MM:SS [tag] text`, so any line can be dated on its own;
  launchd adds no timestamps of its own, and before this the file could
  only be dated by cross-referencing `alerts.log`. Continuation lines
  (the `  - {...}` finding details) are stamped too, so slicing the file
  by date keeps a finding together with its header. `--json` output is
  deliberately left unstamped so it stays machine-parseable.
- **Exit codes:** `0` clean · `1` drift / suspicious activity · `2` hard error
  **or no baseline yet** (see below).

## Baselines

The plugin and workflow controls compare the current state against a **baseline
you establish once**. Until you do, they intentionally exit `2` and alert
("no baseline"). The installer offers to record both as its **last** step
(`88-security-baselines` on macOS, step 85 of `install.ps1` on Windows), after
every plugin, script and scheduled job is in place. Taken any earlier, the
first scheduled run reports the installer's own later steps as drift. Accept
only if nothing but the installer has touched the machine since it started;
otherwise, or after `--auto` / `-NonInteractive`, set them yourself, plugin
allowlist first:

```bash
/usr/bin/python3 ~/Obsidian/Templates/Scripts/plugin_integrity_check.py --update
/usr/bin/python3 ~/Obsidian/Templates/Scripts/integrity_monitor.py      --update
```

- The plugin allowlist lives at `~/.local/share/obsidian-security/plugin_allowlist.json`
  and is **HMAC-signed** with a random key stored in your macOS Keychain (service
  `obsidian-allowlist-hmac`). Editing the allowlist by hand is therefore detected
  as `ALLOWLIST_TAMPER` — the baseline can only be changed through `--update`.
- The plugin baseline also records:
  - which plugins are **enabled**, read as Obsidian reads `community-plugins.json`
    (`ENABLED_CHANGE`);
  - for Meta Bind, Dataview, Templater, QuickAdd, Excalidraw, Metadata Menu,
    Tasks and Omnisearch, the settings that let the plugin run code from note
    content or on its own (`enableJs`, `enableDataviewJs`,
    `trigger_on_file_creation`, QuickAdd startup macros, Excalidraw's startup
    script, Metadata Menu formulas, Tasks' global query, and so on), each as
    "set or not" plus a hash of its value (`SETTINGS_CHANGE`);
  - a hash of every script and template those settings point to, including
    Templater's templates folder (`(referenced) …`);
  - any other dot-folder at the vault root that holds Obsidian configuration:
    Obsidian can be pointed at one from outside the vault (`NEW Obsidian config
    folder`).

  An allowlist recorded before these fields existed reports `NOT_BASELINED`
  until you vet and run `--update`. A failure of the check itself raises
  `CONTROL_ERROR` instead of dying quietly.
- **Not covered:** a setting flipped and flipped back between two runs, for a
  plugin that reloads settings live (Meta Bind, Templater). On macOS the agent
  reruns when a watched settings file changes; on Windows the task runs daily
  only.
- The workflow baseline lives at `~/.local/share/obsidian-security/integrity_state.json`.
- `./install.sh --rebaseline` forces both to re-baseline.

## Responding to an alert

First question: **did you make the change?**

- **Yes** (you updated a plugin, edited a script, added an agent) — review the
  finding, then re-adopt the baseline with `--update` (or `./install.sh --rebaseline`).
- **No** — investigate before adopting anything.

Finding kinds you'll see:

- **Plugin:** `NEW`, `REMOVED`, `VERSION_CHANGE`, and `BUNDLE_CHANGE`.
  `BUNDLE_CHANGE` means a plugin's `main.js` changed **without a version bump** —
  the strongest supply-chain signal; investigate before adopting.
- **Workflow:** `NEW_FILE`, `CONTENT_CHANGE`, `DELETED`, and `BULK_DELETE`
  (vault Markdown count dropped by at least `max(50, 5%)`).
- **Process:** each suspicious spawn, with the offending binary path.

## Manual / on-demand use

Run any control by hand. Useful flags:

- `--update` — adopt the current state as the new baseline (plugin + integrity).
  Both controls also trigger a fresh scheduled run of their own launchd job
  afterwards. Without that, the scheduler keeps reporting the
  drift run that prompted the rebaseline (exit 1, recorded as
  `LastExitStatus` 256), and the morning dashboard's pipeline tile shows the
  control failing for as long as nothing else happens to trigger it — while
  the state on disk has in fact been clean since the moment you adopted.
- `--json` — machine-readable report, suppresses notifications and alert-log writes.
- `OBSIDIAN_SECURITY_STATE_DIR=/tmp/sandbox` — redirect the state directory
  for one invocation, so a manual run writes its baselines somewhere
  disposable instead of over the real ones. Use it whenever you are testing a
  change to a control: `--update` rewrites `plugin_allowlist.json`, including
  the `vetted_at` field that records when a **human** last reviewed each
  plugin bundle — that value is in no backup and no git history, so a test run
  against live state destroys it permanently.

  ```bash
  OBSIDIAN_SECURITY_STATE_DIR=/tmp/sandbox /usr/bin/python3 integrity_monitor.py --update
  ```

  The scheduled jobs never see this: neither plist declares
  `EnvironmentVariables`, and launchd does not pass a job your shell
  environment. Do **not** add it to the plists — that would let anything able
  to set a job's environment aim a security control at a baseline of its own
  choosing.
- `--vault PATH` (plugin + integrity), `--scripts-dir` / `--launchagents-dir`
  (integrity) — point at a non-default vault or layout.
- `--since 24h` and `--stream` (process audit) — set the retro window, or live-tail.

## Customization

- **Non-default vault name/location:** pass `--vault` (and `--scripts-dir` /
  `--launchagents-dir`) and adjust the paths in the two plists.
- **Silence a noisy auto-updater** in the workflow monitor: add its plist
  filename to `CONTENT_CHANGE_IGNORE` in `integrity_monitor.py` (it ships with
  `com.adobe.ccxprocess.plist` as an example; add your own vendor updaters).
- **Bulk-delete sensitivity:** `DELETION_FLOOR` (50) and `DELETION_RATIO` (0.05)
  in `integrity_monitor.py`.

## Footprint

Standard-library Python 3 only — no pip packages, no network. With the
transcription packages, the scripts' virtualenv is about 1.4 GB; hashing it
adds about 10 seconds to the integrity check, or under a minute from a cold
disk cache. The controls read
hashes and the local unified log, write only their own state and `alerts.log`,
and never modify your plugins, scripts, or notes. The state directory is created
mode `0700`; state files are written `0600`.

On Windows, `os.chmod` only toggles the read-only attribute — a `0600` call
there leaves whatever ACL the file inherited, and `stat()` still reports
`0o666`. `security_common.restrict_file()` therefore forks: `chmod 0600` on
POSIX, and on Windows an `icacls` pass that drops inheritance and grants only
the current user plus `SYSTEM`. Granting `SYSTEM` is the closest parallel to
macOS, where root reads a `0600` file freely; `Administrators` is dropped, so
an administrator must take ownership — a deliberate, auditable act — rather
than having ambient read access. It is best-effort by design: it returns
`False` rather than raising if `icacls` is unavailable, since this is
defense-in-depth beneath the HMAC envelope, and a failed hardening pass must
not stop a control from writing its state.

## Tests

`Templates/Scripts/tests/` has a pytest suite covering `url_safety.py`,
`integrity_monitor.py`, `plugin_integrity_check.py`, and `youtube_summarize.py`
— static AST invariants, unit tests, and the headline security scenarios
(DNS rebinding, redirect-to-private-IP, allowlist tamper detection, HMAC
forgery resistance). Fully mocked — safe to run repeatedly on a live Mac;
see `Templates/Scripts/tests/README.md`. Run it with
`Templates/Scripts/tests/run_tests.sh`.

## Uninstall

`./uninstall.sh` unloads the three agents and (by default) removes the security
state directory. `./uninstall.sh --newsyslog` also removes the sudo-installed log
rotation config at `/etc/newsyslog.d/obsidian-security.conf`, and `--secrets`
removes the Keychain HMAC key (`obsidian-allowlist-hmac`).
