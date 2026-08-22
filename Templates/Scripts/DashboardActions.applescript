-- DashboardActions.applescript
--
-- Compiled into DashboardActions.app by build_dashboard_actions_app.sh, which
-- also registers it for the obsidian-dashboard:// URL scheme. The Morning
-- Dashboard (a static file:// HTML page, see morning_dashboard.py) can't run
-- local commands from a plain button — browsers never let a page spawn
-- processes, file:// origin or not — so its buttons instead link to
-- obsidian-dashboard://run/<action>. macOS routes that to this app, which
-- shells out to dashboard_actions.sh and surfaces a notification.
--
-- The dispatcher backgrounds long actions itself, so this handler returns
-- quickly; a "Finished" notification for a backgrounded action means
-- "started successfully" — watch ~/Library/Logs/dashboard-actions.log for
-- the action's own output.
--
-- Vault location follows the platform convention (~/Obsidian). If your
-- vault lives elsewhere, edit scriptsDir below and re-run
-- build_dashboard_actions_app.sh.
on open location theURL
	set actionName to my extractAction(theURL)
	set scriptsDir to (POSIX path of (path to home folder)) & "Obsidian/Templates/Scripts/"
	set dispatcher to quoted form of (scriptsDir & "dashboard_actions.sh")
	set logFile to quoted form of ((POSIX path of (path to home folder)) & "Library/Logs/dashboard-actions.log")

	display notification "Started…" with title ("Dashboard: " & actionName)

	try
		do shell script "/bin/bash " & dispatcher & " " & quoted form of actionName & " >> " & logFile & " 2>&1"
		display notification "Finished" with title ("Dashboard: " & actionName)
	on error errMsg number errNum
		display notification ("Failed (exit " & errNum & ") — see dashboard-actions.log") with title ("Dashboard: " & actionName)
	end try
end open location

on extractAction(theURL)
	-- theURL looks like: obsidian-dashboard://run/pull-meetings
	set AppleScript's text item delimiters to "/"
	set parts to text items of theURL
	set AppleScript's text item delimiters to ""
	return item -1 of parts
end extractAction
