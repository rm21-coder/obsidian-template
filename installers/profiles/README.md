# Install profiles

A profile is a small env file that pre-answers `install.sh`.

```bash
./install.sh --profile gateway          # installers/profiles/gateway.env
./install.sh --profile ~/ours.env       # a file someone handed you
./install.sh --list-profiles            # what's available here
```

## Why this exists

Three components ship **off** — meeting pre-population (52), the meeting-pull
producer (54), and the dashboard's action buttons (57) — and several prompts
have no universal default. Not because they are half-finished, but because
they depend on things an installer cannot provision: an AI gateway with a key
issued to you, an MCP calendar connector your tenant has approved, the list
of domains that count as "internal" here.

That default is right for a stranger cloning this repo. It is wrong for a
colleague inside the same institution, who has all three of those things and
should end up with the setup you actually run rather than the reduced one. A
profile is how you hand them the answers: one file, one flag, same result.

## Two things a profile carries

**1. Where Claude calls go.** Setting `LLM_BASE_URL` and `LLM_API_KEY_NAME`
routes every Claude-calling script through an institutional AI gateway
instead of a personal `api.anthropic.com` key — so the traffic is billed and
governed by the institution, and nobody expenses their own key for work. The
installer persists both to `~/dev/secrets/.env`, which is what the scheduled
LaunchAgents read (they do not inherit your shell). See
`Templates/Scripts/llm_endpoint.py` for the contract; the gateway has to
speak the Anthropic Messages API.

**2. Which opt-in components to run, and their answers.** `PROFILE_*` keys
below.

## Keys

Everything is optional. An unset key leaves the installer's own default —
which for the opt-in components means "ask", or "skip" under `--auto`.

### Endpoint (non-secret config, persisted to `~/dev/secrets/.env`)

None of these is a credential. They say *where* calls go and *which name*
holds the key; the key's value is prompted for separately and stored in the
platform keystore (macOS Keychain), never in a file. They live in `.env`
because the scheduled LaunchAgents start from a bare environment — launchd
does not source your shell — and each script reads `.env` for config, then
resolves the named secret out of the keystore.

| Key | Meaning |
|---|---|
| `LLM_BASE_URL` | Gateway base URL. Unset = stock `api.anthropic.com`. |
| `LLM_API_KEY_NAME` | Name of the secret holding the gateway key (default `ANTHROPIC_API_KEY`). The *name*, never the value. |
| `LLM_GATEWAY_HELP_URL` | Where a colleague requests a key. Shown in the prompt. |
| `TAGGER_MODEL` | Model id or gateway alias for the tagger. |
| `TAGGER_PROMPT_CACHE` | `0` if the gateway rejects `cache_control`. |
| `CLASSIFIER_MODEL` | Same, for the nightly data-classification agent. |
| `CLASSIFIER_PROMPT_CACHE` | Same, for the classifier. |

A profile must never contain a key, token, or password. It is a config file
people forward to each other; the credential belongs in the Keychain, which
is where the installer puts what you type at the prompt.

### Opt-in components

| Key | Effect when `1` |
|---|---|
| `PROFILE_MEETING_PREPOPULATE` | Run 52 (consumer: handoff JSON → meeting notes + People stubs). |
| `PROFILE_MEETING_PULL` | Run 54 (producer: MCP calendar fetch → handoff JSON). |
| `PROFILE_DASHBOARD_ACTIONS` | Run 57 (registers the `obsidian-dashboard://` URL scheme). |

`0` declines without asking. Either way the answer is announced in the log,
so a profile can't silently install something. 52 and 54 are the two halves
of one pipeline: 54 alone produces handoffs nothing consumes, 52 alone waits
for a producer that never runs.

### Prompt pre-fills

| Key | Prompt it answers |
|---|---|
| `PROFILE_DISPLAY_NAME` | Your name, as it appears in meeting notes. |
| `PROFILE_EMAIL` | The calendar owner's address. Per-person — leave unset. |
| `PROFILE_TENANT` | Primary domain. |
| `PROFILE_TENANT_DOMAINS` | Comma-separated internal domains. **Every** domain, or attendees get misfiled as external. |
| `PROFILE_TIMEZONE` | IANA timezone. |
| `PROFILE_MCP_PREFIX` | MCP tool-name prefix, as `claude mcp list` reports it. |
| `PROFILE_SEARCH_TOOL` | Calendar-search tool name. |
| `PROFILE_READ_TOOL` | Resource-read tool name. |
| `PROFILE_OUT_DIR` | Drop folder the producer writes and the consumer watches. |
| `PROFILE_ADMIN_EMAILS` | Assistant/EA addresses to exclude from attendee counts. |
| `PROFILE_DESCRIPTION` | One line echoed when the profile loads. |

In an interactive run these appear as editable defaults, so a colleague can
overtype anything. Under `--auto` they are taken as-is — and `PROFILE_EMAIL`
becomes load-bearing: component 54 requires the calendar owner's address and
has no one to ask, so an unattended run without it installs the consumer half
and skips the producer, which looks successful and produces nothing. Run
interactively, or give each person their own copy of the profile with those
fields filled in.

## Windows

`--profile` is macOS-only today: `install.ps1` has no equivalent flag. The
gateway half still works there, because `llm_endpoint.py` is cross-platform
and reads the same two keys — uncomment `LLM_BASE_URL` and `LLM_API_KEY_NAME`
in `%USERPROFILE%\dev\secrets\.env`, then store the key with
`python secret_store.py set <NAME>`. The opt-in half is the three scheduled
tasks `install.ps1` registers DISABLED for the same reason 52/54/57 ship off;
enable them in Task Scheduler once the connector exists. See
`docs/Windows Setup.md`.

## Writing one

Copy `gateway.env.example`, fill it in, and hand it over with the repo URL.

Two cautions. A profile is **sourced**, so it is shell code running at the
same trust level as `install.sh` — read one before you run it, and only run
profiles from someone you trust. And `installers/profiles/*.env` is
gitignored on purpose: a real profile names internal endpoints, tenant
domains, and connector names. Sharing it with a colleague is fine; publishing
it in a public repo is a disclosure decision, so make it deliberately
(`git add -f`) rather than by accident.
