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
# unconfigured external dependency (mailbox, Azure Blob relay, MCP calendar
# connector, ...) ship Enabled=$true -- they were validated end-to-end on a
# clean Windows 11 install by running the script by hand
# (.venv\Scripts\python.exe <script>) before flipping the flag. The three
# still shipping Enabled=$false each fail cleanly with a clear config error
# rather than silently no-op, because they depend on a per-user resource this
# template can't assume exists:
#   meeting-pull        needs the Claude CLI + an MCP calendar connector
#   handoff-blob-pull   needs a real Azure Blob SAS (see docs/Azure-Blob-Handoff-Relay.md)
#   source-mail-pull    needs a dedicated IMAP mailbox (see docs/Source-Mail-Transport.md)
# Set up the prerequisite, run the script by hand to confirm it works, then:
#   Enable-ScheduledTask -TaskName <name> -TaskPath '\Obsidian'

@{
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
        @{ Name='podcast-watch';        Script='podcast_watch.py';      Args=@('--once');    Trigger=@{ Type='MinuteInterval'; Minutes=15 }; Enabled=$true }
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
        @{ Name='handoff-blob-pull';    Script='handoff_blob_pull.py';  Args=@();            Trigger=@{ Type='MinuteInterval'; Minutes=5  }; Enabled=$false }
        @{ Name='group-photos';         Script='run_group_photos.py';   Args=@();            Trigger=@{ Type='Daily';  At='02:00' };        Enabled=$true }
        @{ Name='rag-sync';             Script='obsidian-rag-sync.py';  Args=@();            Trigger=@{ Type='Daily';  At='03:00' };        Enabled=$true }
        @{ Name='security-plugin-check';Script='plugin_integrity_check.py'; Args=@();        Trigger=@{ Type='Daily';  At='06:30' };        Enabled=$true }
        @{ Name='security-integrity';   Script='integrity_monitor.py';  Args=@();            Trigger=@{ Type='Daily';  At='06:35' };        Enabled=$true }
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
