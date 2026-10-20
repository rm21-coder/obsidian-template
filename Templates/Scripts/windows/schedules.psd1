# schedules.psd1 -- Windows Task Scheduler manifest.
# One hashtable per job. Register-Tasks.ps1 consumes this.
# Ported from the macOS launchd plists in Templates/Scripts/*.plist.
#
# Trigger types understood by Register-Tasks.ps1:
#   @{ Type='MinuteInterval'; Minutes=30 }
#   @{ Type='Daily';  At='03:00' }
#   @{ Type='Weekly'; At='07:00'; DaysOfWeek=@('Monday','Tuesday','Wednesday','Thursday','Friday') }
#   @{ Type='AtLogon' }
# 'Script' is relative to the vault Scripts dir; 'Args' is an array.
#
# Enabled=$true/$false is each job's registered starting state. Jobs with no
# unconfigured external dependency (mailbox, MCP calendar connector, ...) ship Enabled=$true -- they were validated end-to-end on a
# clean Windows 11 install by running the script by hand
# (.venv\Scripts\python.exe <script>) before flipping the flag. The four
# still shipping Enabled=$false each fail cleanly with a clear config error
# rather than silently no-op, because they depend on a per-user resource this
# template can't assume exists:
#   meeting-pull        needs the Claude CLI + an MCP calendar connector
#   source-mail-pull    needs a dedicated IMAP mailbox (see docs/Source-Mail-Transport.md)
#   email-tasks         needs the Claude CLI + the Microsoft 365 connector
#   classify            its first pass reads the whole vault: run it by hand,
#                       with Obsidian closed, before enabling (docs/Data-Classification.md)
# Set up the prerequisite, run the script by hand to confirm it works, then:
#   Enable-ScheduledTask -TaskName <name> -TaskPath '\Obsidian'

@{
    # Jobs this template once registered and has since retired. Register-Tasks
    # (so every install and update) unregisters them from \Obsidian\: nothing
    # else would, and a retired job only ever fails or runs code that no longer
    # ships. An explicit list rather than "anything not in Jobs", so a task a
    # user added under \Obsidian\ themselves is never touched.
    #   handoff-blob-pull   Azure Blob relay, removed 2026-09-30
    RetiredJobs = @('handoff-blob-pull')
    Jobs = @(
        @{ Name='tag-clippings';        Script='tag_clippings.py';      Args=@();            Trigger=@{ Type='MinuteInterval'; Minutes=30 }; Enabled=$true }
        @{ Name='voice-cleanup';        Script='voice_cleanup.py';      Args=@('--once');    Trigger=@{ Type='MinuteInterval'; Minutes=5  }; Enabled=$true }
        # Mail-drop transport: pulls authenticated drops into ~/SourceMedia/<Type>/
        # for the watchers above to consume. Cheap (an IMAP fetch of small text),
        # so it ticks with the other 5-minute watchers rather than the podcast job.
        @{ Name='source-mail-pull';     Script='source_mail_pull.py';   Args=@('--once');    Trigger=@{ Type='MinuteInterval'; Minutes=5  }; Enabled=$false }
        # 15 minutes, not 5: transcription costs real CPU for real minutes, so
        # this job is deliberately slower-ticking than the other watchers. It
        # takes one drop per tick (podcast_watch's own --max-per-run default)
        # and holds a single-instance lock, so a long episode can't stack runs.
        # TimeLimitMinutes (see Register-Tasks.ps1): a long episode transcribed
        # on CPU can legitimately take well over the default 30 minutes.
        @{ Name='podcast-watch';        Script='podcast_watch.py';      Args=@('--once');    Trigger=@{ Type='MinuteInterval'; Minutes=15 }; Enabled=$true; TimeLimitMinutes=180 }
        @{ Name='strip-ads';            Script='strip_ads.py';          Args=@();            Trigger=@{ Type='MinuteInterval'; Minutes=5  }; Enabled=$true }
        @{ Name='meeting-prep';         Script='meeting_prep.py';       Args=@();            Trigger=@{ Type='MinuteInterval'; Minutes=5  }; Enabled=$true }
        @{ Name='meeting-prepopulate';  Script='meeting_prepopulate.py';Args=@();            Trigger=@{ Type='MinuteInterval'; Minutes=30 }; Enabled=$true }
        # Producer half of meeting pre-population: shells out to the Claude CLI
        # to read the day's calendar over an MCP connector, then writes a
        # handoff into the folder meeting-prepopulate watches. Needs the CLI
        # installed and .config\meeting_pull.json present (see
        # docs/Meeting-Handoff-MCP-Producer.md); validate with
        # `python meeting_pull.py --dry-run` before enabling. Weekdays 05:00,
        # ahead of morning-dashboard's 07:00 so notes exist first. The task
        # settings already set -StartWhenAvailable, which covers a missed run
        # on a machine that was off; skip-if-fresh keeps that catch-up cheap.
        @{ Name='meeting-pull';         Script='meeting_pull.py';       Args=@('--skip-if-fresh'); Trigger=@{ Type='Weekly'; At='05:00'; DaysOfWeek=@('Monday','Tuesday','Wednesday','Thursday','Friday') }; Enabled=$false }
        # Weekday email requests -> #task lines in Actions\Email Tasks.md (see
        # docs/Email-Tasks.md). Needs the CLI and .config\email_tasks.json;
        # validate with `python email_tasks.py --dry-run` before enabling.
        # 06:45, ahead of morning-dashboard. A missed run loses nothing: each
        # run reads from the last successful one.
        @{ Name='email-tasks';          Script='email_tasks.py';        Args=@();            Trigger=@{ Type='Weekly'; At='06:45'; DaysOfWeek=@('Monday','Tuesday','Wednesday','Thursday','Friday') }; Enabled=$false }
        # Data-classification assistant (docs/Data-Classification.md). Proposes
        # tiers into classification_suggested; never writes classification
        # except restricted from a detector. Disabled at registration: the first
        # pass reads every note and must run by hand with Obsidian closed. Daily
        # 05:00; -StartWhenAvailable covers a run missed while off or off-VPN
        # only when the machine was off, so a gateway behind a VPN can still
        # skip a day (a skip costs nothing: the next run catches up).
        @{ Name='classify';             Script='classify_notes.py';     Args=@();            Trigger=@{ Type='Daily';  At='05:00' };        Enabled=$false; TimeLimitMinutes=240 }
        @{ Name='group-photos';         Script='run_group_photos.py';   Args=@();            Trigger=@{ Type='Daily';  At='02:00' };        Enabled=$true }
        # Only where the optional local-LLM RAG layer is set up (rag_status.py:
        # OBSIDIAN_COLLECTION_ID configured). Elsewhere it would fail every
        # night unseen, so Register-Tasks enables it exactly when RAG is set up.
        # A first index of a large vault takes hours, so more than the default 2.
        @{ Name='rag-sync';             Script='obsidian-rag-sync.py';  Args=@();            Trigger=@{ Type='Daily';  At='03:00' };        Enabled=$true; RequiresRag=$true; TimeLimitMinutes=240 }
        # Isolated: run from the installed copy in %LOCALAPPDATA%\obsidian-security\bin
        # under the base interpreter with -I -S, never from the vault they watch
        # (finding 5278112; see Register-Tasks.ps1 and Get-ControlPython).
        @{ Name='security-plugin-check';Script='plugin_integrity_check.py'; Args=@();        Trigger=@{ Type='Daily';  At='06:30' };        Enabled=$true; Isolated=$true }
        @{ Name='security-integrity';   Script='integrity_monitor.py';  Args=@();            Trigger=@{ Type='Daily';  At='06:35' };        Enabled=$true; Isolated=$true }
        @{ Name='morning-dashboard';    Script='morning_dashboard.py';  Args=@();            Trigger=@{ Type='Weekly'; At='07:00'; DaysOfWeek=@('Monday','Tuesday','Wednesday','Thursday','Friday') }; Enabled=$true }
        # Weekly content lint (see docs/Vault-Lint.md). Read-only: no fixing
        # flags, so an unattended run reports and never rewrites. --exit-zero
        # keeps findings from registering as a failed task, the same reason the
        # macOS LaunchAgent passes it. Mondays 07:00, after morning-dashboard.
        # Stdlib-only with no external dependency, hence Enabled=$true; note it
        # has not been hand-run on Windows hardware, so if it misbehaves the
        # fix is to disable this one task, not the whole manifest.
        @{ Name='vault-lint';           Script='vault_lint.py';         Args=@('--exit-zero'); Trigger=@{ Type='Weekly'; At='07:00'; DaysOfWeek=@('Monday') }; Enabled=$true }
    )
}
