# Windows layer

PowerShell + Task Scheduler port of the macOS `launchd`/shell automation.
Validated end-to-end on Windows 11 (x64), including a clean bare-metal
rebuild. See [`../../../docs/Windows Setup.md`](../../../docs/Windows%20Setup.md)
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
- The pinned-plugin rewrite of `Install-Plugins.ps1` (2026-08-18) has been
  reviewed but not yet executed on a real Windows box (parser-checked only —
  no pwsh on the maintainer's Mac). Same for `graph_calendar_fetch.py` as a
  scheduled producer. Treat the first Windows run of either as unvalidated,
  same drill as above.
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
