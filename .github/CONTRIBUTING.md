# Contributing

This is a public repository, and it is also an Obsidian vault: the same
working tree that holds the template holds a contributor's real notes. Most
of what follows exists to keep the second out of the first.

## The one rule

Nothing personal or institutional is ever committed. No real notes, People,
Meetings or Groups; no employer email domains, internal hostnames, colleague
names or meeting titles; no machine paths with a real username; no secrets.
The content folders are gitignored for this reason. Demo content comes from
`Templates/Scripts/seed_demo_content.py`, never from committed files (see
[`docs/Demo-Content.md`](../docs/Demo-Content.md)).

## Setup, once per clone

1. **Commit under your GitHub noreply address**, not a work address. Commit
   headers are public and permanent. In the repo:

       git config user.email "<id>+<username>@users.noreply.github.com"

   and turn on *Settings → Emails → Block command line pushes that expose my
   email* on GitHub.
2. **Install the hooks:** `./installers/install-git-hooks.sh`. Re-run it
   whenever `installers/lib/hooks/` changes, or the next commit is refused.
3. **Build your own identity deny-list:**
   `installers/lib/check_identity_leak.py --init`. It is gitignored. Put in
   it the strings that must never leave *your* machine: your employer's
   domains, colleagues' names, internal hostnames.

On Windows, Git for Windows runs the hooks under its bash. A scanner you have
not installed is reported as a named SKIP rather than a failure; the PR check
installs every one of them.

## Making a change

- Branch from `main` (`fix/...`, `feat/...`, `docs/...`) and open a pull
  request. `main` accepts changes only through a reviewed PR.
- Keep a PR to one change. Update the docs that describe the behaviour in the
  same PR; if you change a count or list of jobs, components or plugins, grep
  every doc that states it.
- Treat every script in `Templates/Scripts/` as Python 3.9 code: many jobs run
  on Apple's `/usr/bin/python3`. No `match`, no runtime `X | Y`, no nested
  same-quote f-strings.
- Prefer one platform-neutral `.py` over separate macOS and Windows copies.
  Fork only the OS glue (schedulers, wrappers, installers).
- Dependencies: edit the `requirements*.txt`, re-lock with
  `installers/lib/lock_requirements.py`, commit both. Never hand-edit a lock.
- Say in the PR which platforms you tested on. A Windows-layer change needs a
  Windows run; a macOS-layer change needs a Mac run.

## What happens to a PR

The **PR checks** workflow runs the full security suite (secrets, SAST, SCA,
shell, content gates, dynamic checks) and the test suite on macOS. It must
pass. Its artifacts are not uploaded, because on a public repo an artifact is
public; read the failing step's log.

CI cannot see the deny-lists, so the maintainer checks out the PR locally,
reruns the suite and the identity gate with the private lists, and merges
from there. A finding is recorded (file, line, severity, failure scenario)
before it is fixed; accepted findings go in
`installers/lib/security-suppressions.txt` with a rationale and a re-check
date.
