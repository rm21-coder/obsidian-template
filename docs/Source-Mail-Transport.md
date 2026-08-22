# Source Mail Transport

How a voice note or podcast link gets from a phone into the vault
without iCloud Drive.

## Why this exists

The watchers (`voice_cleanup.py`,
`podcast_watch.py`) read local drop folders. Historically those folders lived
in iCloud Drive, because an iOS Shortcut can write there and the Mac would sync
it down. That conflates two separate things — the **drop-folder contract** and
the **transport** — and pins the whole workflow to one vendor's sync client.

[`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) already rules on this for
meeting handoff: a cloud-drive sync client is fragile as a transport (sync
races, conflict copies, online-only placeholders that don't hydrate in time),
which is exactly why Tier B exists. The same reasoning applies to source media,
with an extra cost on Windows: iCloud Drive drop folders force Apple iCloud for
Windows onto a machine that otherwise needs nothing from Apple.

`source_mail_pull.py` is the same idea as `handoff_blob_pull.py` with a mailbox
as the relay:

```
iOS Shortcut ──email──► dedicated mailbox ──IMAP──► source_mail_pull.py
                                                          │
                                                          ▼
                                            ~/SourceMedia/<Type>/
                                                          │
                                        ┌─────────────────┴─────────────────┐
                                        ▼                                   ▼
                                    voice_cleanup.py            podcast_watch.py
```

The drop-folder contract is unchanged, so the watchers needed no modification.

## Why a mailbox needs authentication

An inbox is **world-writable**. Anyone who learns the address can post to it,
and whatever survives verification is written to your filesystem and then read
by an LLM (`tag_clippings.py`). iCloud Drive was at least authenticated — only
you could put a file there. Email is not, so the payload has to carry its own
proof.

Defences, layered so no single failure is fatal:

1. **Unguessable address.** Free, and it means the token isn't the only thing
   standing between a stranger and your vault.
2. **Sender allowlist.** Weak alone — `From` is trivially spoofed — but it
   removes the noise floor. Fails closed: an empty allowlist rejects everything
   rather than allowing everything.
3. **HMAC-SHA256**, keyed with `SOURCE_MAIL_TOKEN`, compared with
   `hmac.compare_digest`. This is the real control. **The token is never
   transmitted** — sending it would leave a long-lived secret sitting in
   Gmail's storage forever; an HMAC proves knowledge of it without disclosing
   it.
4. **The routing type comes from inside the MAC**, never from the Subject line,
   so an authentic podcast drop can't be retargeted at the voice pipeline.
5. **A timestamp inside the MAC** bounds replay to 7 days.
6. **Filenames are generated locally**, never taken from the message, so path
   traversal is impossible by construction rather than by filtering.
7. **`text/plain` only**, size-capped. HTML parts are ignored outright rather
   than parsed — recovering a payload from hostile markup is a needless attack
   surface when the producer controls the format and can simply send plain
   text.

### If your producer can't guarantee plain text

Some mail clients' silent, no-user-interaction send paths compose HTML-only
regardless of account settings — this bit an iOS Shortcuts "Send Email"
action with "Show Compose Sheet" turned off, which is exactly the mode a
hands-free capture Shortcut needs. If your producer hits the same wall, set

```
SOURCE_MAIL_ALLOW_HTML_FALLBACK=1
```

in `~/dev/secrets/.env`. This is **off by default** and the default for
anyone else who clones this repo should stay off — turn it on only if you've
confirmed (via `--emit` + `--once --dry-run`, same as any other setup step
here) that your specific producer needs it.

What it actually does, and why the residual risk is small: `text/plain` is
still always preferred: `text/html` is read only when *no* `text/plain` part
exists at all, so the fallback code path doesn't even run for a normal
plain-text producer. When it does run, tags are stripped with Python's
stdlib `html.parser` — not a third-party HTML library, and not an XML
parser, so there's no external-entity/DTD class of bug possible here at all.
The contents of `<script>`/`<style>` tags are discarded along with the tags
themselves (a mail client never renders that text, so a naive "strip tags,
keep everything between them" approach would let a sender smuggle content
into the recovered payload that was never actually displayed). Every other
defense — sender allowlist, HMAC, replay window, generated filenames — is
completely unaffected: this only changes which MIME part supplies the raw
text before that same validation runs, not what counts as authentic.

## Message format

```
v: 1
type: podcast
ts: 2026-08-09T21:15:00Z
payload: https://example.com/episode.mp3
hmac: 3db2a9fe896c295e...
```

`type` is one of `voice` or `podcast`. `payload` is a URL for `podcast`, or the
dictation text for `voice`. The Subject line is cosmetic and
is not used for routing.

The HMAC covers a **length-prefixed** canonical string:

```
v1|{len(type)}:{type}|{len(ts)}:{ts}|{len(payload)}:{payload}
```

Length prefixes rather than plain delimiters, because a plain delimiter is
ambiguous when a field can contain it — `("T", "x|y")` and `("T|x", "y")` would
otherwise flatten to the same signed string, letting one MAC authenticate two
different field sets.

Unknown keys, comments (`#`) and trailing signature blocks are ignored, so
"Sent from my iPhone" is harmless. **The first occurrence of a key wins**, so
appending a second `payload:` line to a captured message doesn't override the
signed one.

## Setup

### 1. Dedicated mailbox

Use a **dedicated** account, not a `+alias` on your main one: the credential on
the vault machine should unlock exactly one mailbox containing exactly these
payloads. Make the address unguessable.

Enable 2-Step Verification first, then generate an App Password
(`https://myaccount.google.com/apppasswords`) — the option doesn't appear until
2SV is on, and it stays hidden if 2SV is passkey- or security-key-only. Create
**one App Password per machine** so a lost machine can be revoked on its own.

> Google is phasing App Passwords out during 2026 in favour of OAuth 2.0. When
> that lands, only `_secret()` in `source_mail_pull.py` needs to change; the
> routing, verification and sanitising logic is independent of how the mailbox
> is authenticated.

### 2. Secrets

In `~/dev/secrets/.env` (gitignored):

```
SOURCE_MAIL_USER=intake-xxxx@gmail.com
SOURCE_MAIL_APP_PASSWORD=abcdefghijklmnop
SOURCE_MAIL_TOKEN=<40 random alphanumerics>
SOURCE_MAIL_ALLOWED_SENDERS=you@icloud.com,you@gmail.com
```

Generate the token with 40 alphanumeric characters and **no symbols** — it has
to survive quoted-printable encoding, email line-wrapping, iOS smart
punctuation, and dotenv parsing (`#` starts a comment in some parsers, `$` can
interpolate). The entropy difference is irrelevant; the corruption modes are
silent.

### 3. Verify before wiring up a phone

```bash
# Print a correctly signed body for any type
python3 Templates/Scripts/source_mail_pull.py --emit podcast "https://example.com/ep.mp3"

# Parse and verify without writing anything or marking mail read
python3 Templates/Scripts/source_mail_pull.py --once --dry-run
```

### 4. Enable the job

macOS: `com.obsidian.source-mail-pull.plist` (300s interval).
Windows: `source-mail-pull` in `schedules.psd1`, registered disabled like
everything else.

```powershell
Enable-ScheduledTask -TaskName source-mail-pull -TaskPath '\Obsidian\'
```

## The producer side

**This is the fiddly part**, and it's worth knowing before you start: Shortcuts
has no native HMAC action.

The build steps live in [`Source-Mail-Producer-Setup.md`](Source-Mail-Producer-Setup.md), along with the
helper script itself at [`producers/SourceMailDrop.js`](producers/SourceMailDrop.js). The rest of
this section is the reasoning behind them.

- **macOS Shortcuts** can *Run Shell Script*, so the Mac is easy — call
  `source_mail_pull.py --emit <type> <payload>` and mail the output.
- **iOS Shortcuts** cannot. The practical options are a helper app that exposes
  hashing to Shortcuts (Scriptable is the usual choice, and free), or
  Pushcut/Toolbox Pro. Note the shipped helper implements SHA-256/HMAC in plain
  JavaScript rather than calling Web Crypto: `crypto.subtle` needs a secure
  context that a bare `loadHTML()` WebView may not establish, and when it isn't
  there the call hangs with nothing able to surface the failure.

If the HMAC proves too painful on iOS, the fallback is a shared-token line
instead of a MAC: weaker against interception and replay, but it still stops
anyone who doesn't know the token. That's a deliberate downgrade, not the
default — decide it explicitly rather than drifting into it.

### Two things that bite once a second device is involved

The signing secret is per-device even though the script is not, and each device
picks its own From address. Both look like a broken Shortcut and are neither;
both are written up with their fixes in
[`Source-Mail-Producer-Setup.md`](Source-Mail-Producer-Setup.md).

## Folder layout

```
~/SourceMedia/
├── VoiceInput/     ← voice_cleanup.py
└── PodcastInput/   ← podcast_watch.py
```

One local root, no cloud client. Subfolders rather than one flat folder because
both drop kinds are `.txt`: in a flat folder a `.txt` would be ambiguous and
the watchers would race for it, each mis-handling the other's input.

`podcast_watch.py` reads only `~/SourceMedia/PodcastInput` — there is no legacy
or iCloud fallback location. `source_media.py --apply` (run by the installers)
creates the folders.
