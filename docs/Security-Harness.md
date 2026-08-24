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
  (Install-time complement: the installer only ever fetches plugin releases
  pinned by tag and SHA256 in `installers/plugin-pins.json` — upgrading a
  plugin is a deliberate re-pin + commit, never an ambient "latest".)
  possibly maliciously, without your involvement.
- **Tampering** — someone (or something) modifies your automation scripts, your
  LaunchAgents, or the security baseline itself.
- **Bulk data loss** — a large, unexpected deletion of vault notes.

(Unexpected process execution — Obsidian or an Electron helper spawning a
shell, interpreter, or network tool — is the EDR layer's job; see above.)

## The two controls

| Control | Script | Schedule | What it checks |
|---------|--------|----------|----------------|
| Plugin integrity | `plugin_integrity_check.py` | daily 06:30 + on change to the plugins folder | SHA-256 of each plugin's `main.js` and `manifest.json` under `<vault>/.obsidian/plugins/`, diffed against an HMAC-signed allowlist. |
| Workflow integrity | `integrity_monitor.py` | daily 06:35 + on change to scripts / LaunchAgents / state dir | SHA-256 of every `.py`/`.sh`/`.plist` in `Templates/Scripts/`, every plist in `~/Library/LaunchAgents/`, and the controls' own state files; plus a bulk-deletion guard on the vault's Markdown count. |

Both are wired as LaunchAgents (`com.obsidian.security.plugin-check`,
`com.obsidian.security.integrity`), each run
via `/usr/bin/python3` so they keep working even if the per-vault virtualenv is
broken.

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
("no baseline"). After installing, set the baselines:

```bash
/usr/bin/python3 ~/Obsidian/Templates/Scripts/plugin_integrity_check.py --update
/usr/bin/python3 ~/Obsidian/Templates/Scripts/integrity_monitor.py      --update
```

- The plugin allowlist lives at `~/.local/share/obsidian-security/plugin_allowlist.json`
  and is **HMAC-signed** with a random key stored in your macOS Keychain (service
  `obsidian-allowlist-hmac`). Editing the allowlist by hand is therefore detected
  as `ALLOWLIST_TAMPER` — the baseline can only be changed through `--update`.
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

Standard-library Python 3 only — no pip packages, no network. The controls read
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
