# Source Mail Producer Setup

Building the phone-side half of the transport: a Shortcut that signs a drop
and mails it. Read [`Source-Mail-Transport.md`](Source-Mail-Transport.md)
first — it covers the wire format, why the payload carries an HMAC, and the
mailbox setup this assumes is already done.

## Why a helper script at all

Shortcuts has no HMAC action. macOS Shortcuts can *Run Shell Script* and call
`source_mail_pull.py --emit`, so the Mac is easy; iOS cannot. The practical
answer is a helper app that exposes hashing to Shortcuts —
[`producers/SourceMailDrop.js`](producers/SourceMailDrop.js) is that helper,
written for [Scriptable](https://scriptable.app) (free).

It signs and returns `{to, subject, body}`. It deliberately does **not** send:
Scriptable's `Mail.send()` presents a compose sheet, and UI presentation is
exactly what fails under Siri. Sending belongs in the Shortcut's own native
Send Email action.

## One-time setup, per device

1. Install Scriptable.
2. New script named `SourceMailDrop`, paste in `producers/SourceMailDrop.js`.
3. **Run it standalone once** — tap ▶ in the app, not via a Shortcut. It
   prompts for `SOURCE_MAIL_TOKEN` and the intake address and stores them in
   Scriptable's Keychain.

Step 3 is not optional and not once-per-account. Scriptable syncs *scripts*
through its own iCloud folder, but the Keychain is device-local and never
syncs. A second device receives the script, lists it, and cannot sign
anything. Worse, the setup prompt is an `Alert`, and an Alert cannot present
inside a "Run Script" action — so the Shortcut neither signs nor errors
visibly. It simply stops before Send Email: no mail, no compose sheet, no
error, and nothing in the Mac logs. Run it standalone once and it works.

## Building a Shortcut

One Shortcut per drop kind. They differ only in how the payload is obtained
and in the `type` value; everything from the Dictionary down is identical.

### Podcast — from the share sheet

Shortcut Settings → **Show in Share Sheet**, accepted input **URLs**.

| # | Action | Configuration |
|---|--------|---------------|
| 1 | Receive from Share Sheet | URLs and Apps; if no input: Get Clipboard |
| 2 | Get URLs from Input | Shortcut Input |
| 3 | **Get Item from List** | **First Item** |
| 4 | Dictionary | `type` = `podcast`, `payload` = Item from List |
| 5 | Run Script | `SourceMailDrop`, Input = Dictionary |
| 6 | Get Value for `to` in Output | → Set variable `Email-to` |
| 7 | Get Value for `subject` in Output | → Set variable `Email-subject` |
| 8 | Get Value for `body` in Output | → Set variable `Email-body` |
| 9 | Send Email | To `Email-to`, Subject `Email-subject`, Body `Email-body`, **Show Compose Sheet OFF** |

**Step 3 is not optional.** *Get URLs from Input* returns a **list**. Wired
straight into `payload`, a share yielding two URLs produces a multi-line
payload — and `parse_body()` on the Mac is line-based, so it reconstructs only
the first line while the phone signed the whole thing. The result is
`hmac mismatch`, and because a rejected message is marked `\Seen` and never
retried, the phone shows nothing wrong at all.

Steps 6–8 exist because Shortcuts cannot inline a script result's dictionary
keys into Send Email. Read each key into a variable first.

### Voice — from dictation

Same from step 4 down. Replace steps 1–3 with a **Dictate Text** action, and
set `type` = `voice`, `payload` = Dictated Text.

Note this path runs under Siri, where Scriptable cannot present any UI. The
helper throws plain `Error`s rather than showing Alerts for exactly this
reason — a thrown error surfaces in Shortcuts' own "Could Not Run" dialog,
which works everywhere.

## Two things that will bite you

**Show Compose Sheet must be off** for a hands-free capture — and iOS Mail's
silent send path composes HTML-only regardless of account settings. The Mac
ignores HTML parts by default, so set `SOURCE_MAIL_ALLOW_HTML_FALLBACK=1` in
`~/dev/secrets/.env`. See the transport doc for why the residual risk is
small; every other defence is unaffected.

**Each device picks its own From address.** `SOURCE_MAIL_ALLOWED_SENDERS` is
matched against the envelope From, and a phone and a tablet frequently default
to different mail accounts — an iPhone sending as `you@gmail.com` and an iPad
as `you@icloud.com`. The iPad's drop then arrives correctly signed and is
rejected on the allowlist, which reads as "it didn't work" from the device:

```
REJECT [podcast drop]: sender 'you@icloud.com' not in allowlist
```

The cleaner fix is to **point every device at one sending account** rather than
widening the allowlist: iOS Settings → Apps → Mail → Default Account, set to
the same account on each device. One From address on the allowlist means one
thing to keep true, and it keeps the allowlist as tight as it can be.

The alternative is to list every address you might send from:

```
SOURCE_MAIL_ALLOWED_SENDERS=you@gmail.com,you@icloud.com
```

That's a reasonable safety net if you'd rather not depend on each device's
default staying put — a restore or a newly added account can change it — but it
is strictly more permissive, and the allowlist is the only thing standing
between the mailbox and the noise floor.

Either way the device gives no indication anything went wrong, so check
`~/Library/Logs/source-mail-pull.err` first when a share seems to vanish. A
rejected message is marked `\Seen` and will not be retried — just re-share
from the device. Clearing the flag by hand is unreliable on Gmail, which
restores read state across a whole conversation, and every drop of a given
type shares one subject so they thread together.

## Verifying

On the Mac, before and after wiring up a phone:

```bash
python3 Templates/Scripts/source_mail_pull.py --emit podcast "https://example.com/ep.mp3"
python3 Templates/Scripts/source_mail_pull.py --once --dry-run
```

`--emit` prints a correctly signed body to compare against; `--dry-run` parses
and verifies without writing anything or marking mail read.

When a share seems to vanish, check `~/Library/Logs/source-mail-pull.err`
first — rejections are logged there, and the device never learns about them.

## Keeping the helper in step

`producers/SourceMailDrop.js` mirrors `source_mail_pull.py`: the canonical
string is length-prefixed per field counted in Unicode **code points** (not
UTF-16 units), the timestamp is integer-second UTC with a trailing `Z`, and
the valid `type` values match `TYPE_DIRS`. Change the wire format on one side
and you must change it on the other — a mismatch presents as `hmac mismatch`
with nothing to indicate which half moved.
