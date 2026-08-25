# Windows layer

PowerShell + Task Scheduler port of the macOS `launchd`/shell automation.
Targets **Windows PowerShell 5.1** — the edition that ships with Windows.
PowerShell 7 works too but is not required or assumed.

Validated end-to-end on Windows 11 (x64), including a clean bare-metal
rebuild. A 2026-08-25 ARM64 re-validation found `Install-Plugins.ps1` and
`Send-Notification.ps1` unparseable under 5.1 — see the encoding note below —
so plugin pinning and toast alerts did not run on that edition. Fixed and **re-verified the same day on the same
hardware**: all 11 files parse under 5.1, and the plugin control
installed and hash-verified 13/13 against a live vault. See [`../../../docs/Windows Setup.md`](../../../docs/Windows%20Setup.md)
for the full setup guide.

## Conventions

- Vault root: `$env:USERPROFILE\Obsidian`. When the repo is cloned elsewhere
  (e.g. `%USERPROFILE%\obsidian-template`), `install.ps1` links `~\Obsidian` to
  it with a **directory junction** — the Windows analog of the Mac's `~/Obsidian`
  symlink. This keeps git in the repo folder while every `~\Obsidian\...` path
  (scripts, tasks, the Obsidian app) resolves. Override with `$env:OBSIDIAN_VAULT`
  if you must, but the junction is the canonical setup.
- Scripts: `<vault>\Templates\Scripts\`.
- Python venv: `<vault>\Templates\Scripts\.venv\Scripts\python.exe` (3.10+).
- Secrets: `$env:USERPROFILE\dev\secrets\.env` (gitignored, never committed).
- Runtime state: `$env:LOCALAPPDATA\obsidian-*\`.

## Files

- `install.ps1` — bootstrap a Windows box: preflight, vault junction, secrets
  stub, plugins, venv + deps, optional add-ons, register tasks.
- `common.ps1` — shared helpers (vault/repo path resolution, `.env` loading,
  `Invoke-Native` for exit-code-safe native calls, `Test-WSL2Present`).
- `schedules.psd1` — the job manifest (one entry per scheduled job).
- `Register-Tasks.ps1` / `Unregister-Tasks.ps1` — create/refresh or remove the
  scheduled tasks from the manifest.
- `Install-Plugins.ps1` — fetches the community plugins from their GitHub
  releases into `.obsidian\plugins\`.
- `setup-rag.ps1` — installs the local RAG stack (Ollama + `llama3.1:8b` +
  Open WebUI in Docker); called by `install.ps1 -WithRAG` or standalone.
- `Install-SendTo.ps1` — registers the right-click **Send to → "Markitdown to
  Obsidian"** shortcut.
- `sync-vault.ps1` — port of `sync-vault.sh` (RAG indexer wrapper).
- `Send-Notification.ps1` — toast helper replacing `osascript` notifications
  (BurntToast if installed, else a log-only fallback).
- `uninstall.ps1` — tears down what `install.ps1` set up; see
  `.\uninstall.ps1 -?` for flags.

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\install.ps1
```

Already bypassing execution policy for the whole window (e.g. mid-session,
running several `windows\*.ps1` scripts back to back)? Just call scripts
directly instead: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
once, then `.\Templates\Scripts\windows\install.ps1` with no wrapper needed.

## Notes

- Task Scheduler tasks that run at logon/interval do **not** need admin; some
  security-monitor tasks may. `Register-Tasks.ps1` registers per-user by default.
- `.env` parsing here is minimal (KEY=VALUE lines). Keep it in sync with the
  bash `set -a; source` behavior.
- The `meeting-pull` job needs a non-Python dependency the others don't: the
  Claude Code CLI on PATH (or at npm's global bin), authenticated, with an MCP
  calendar connector registered. `meeting_pull.py` itself is stdlib-only and
  platform-neutral — it looks for `claude.cmd` under `%APPDATA%\npm` as well as
  the Unix locations — but it has been exercised end-to-end on macOS only, so
  treat the first Windows run as unvalidated: `python meeting_pull.py --dry-run`
  first, then a real run, and only then enable the task. See
  [`docs/Meeting-Handoff-MCP-Producer.md`](../../../docs/Meeting-Handoff-MCP-Producer.md).
- **Shipped PowerShell must stay ASCII.** Windows PowerShell 5.1 decodes a
  BOM-less file as CP1252, so an em-dash (`U+2014`) reads as three characters
  ending in `U+201D` — which 5.1 accepts as a string terminator. Inside a
  double-quoted string that ends the string mid-line and the parser
  desynchronizes; the file becomes unparseable and the script never runs.
  This shipped: the 2026-08-18 pinned-plugin rewrite of `Install-Plugins.ps1`
  carried em-dashes in two `Write-Warning` strings (one of them the
  `HASH MISMATCH` refusal itself), so the supply-chain pinning control did not
  execute under 5.1 at all — while `install.ps1` caught the throw and exited 0.
  It was parser-checked before shipping and that check passed, because the file
  *is* valid UTF-8 PowerShell; only 5.1 executing it as ANSI breaks. Use `--`
  and `|` instead. Enforced by
  `Templates/Scripts/tests/test_static.py::TestPowerShellEncoding`.
- `graph_calendar_fetch.py` as a scheduled producer is still unvalidated on
  Windows. Treat the first run as such, same drill as above.
- Every native-command call site in `install.ps1` / `setup-rag.ps1` goes
  through `Invoke-Native` (`common.ps1`), which judges success by exit code
  alone — the installer is safe to log with `*>&1 | Tee-Object` or any other
  redirection style, since incidental stderr chatter can no longer be
  mistaken for a failure.
- Vault junction: `install.ps1` creates it automatically. To make one by hand:
  `New-Item -ItemType Junction -Path "$env:USERPROFILE\Obsidian" -Target "$env:USERPROFILE\obsidian-template"`.
  To remove it **without touching the repo**, delete only the link:
  `(Get-Item "$env:USERPROFILE\Obsidian").Delete()`. Do NOT `Remove-Item -Recurse`
  a junction — older PowerShell can recurse into the target and delete real files.
