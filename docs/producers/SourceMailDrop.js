// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: pink; icon-glyph: magic;
// SourceMailDrop.js — Scriptable script that signs and sends a source-media
// drop (voice/podcast) to the mail-drop transport described in
// docs/Source-Mail-Transport.md.
//
// One script shared by the drop Shortcuts, each of which calls this via
// "Run Script" with different Shortcut input. Mirrors the exact wire format
// and canonicalization in Templates/Scripts/source_mail_pull.py:
//   - ts is UTC, integer-second precision, trailing "Z" (no milliseconds)
//   - canonical string is length-PREFIXED per field, counted in Unicode code
//     points (matching Python's len()), not UTF-16 code units
//   - HMAC-SHA256, hex-encoded, lowercase
//
// One-time setup on this phone:
//   1. Install Scriptable (free, App Store).
//   2. Open Scriptable, create a new script, paste this file's contents in,
//      name it "SourceMailDrop".
//   3. Run it once standalone (tap the play button) — it will prompt for
//      your SOURCE_MAIL_TOKEN and INTAKE_ADDRESS and store them in the
//      Keychain (Scriptable's Keychain, not iCloud Keychain — stays on this
//      device, never synced). You only do this once; subsequent runs read
//      the stored values silently.
//
// Then build a Shortcut per drop kind, each doing "Run Script" ->
// SourceMailDrop and passing Shortcut Input as described below.
// Step-by-step build instructions, including the two failure modes that
// look like a broken Shortcut and are not, are in
// docs/Source-Mail-Producer-Setup.md.

const KEYCHAIN_TOKEN_KEY = "sourceMailToken"
const KEYCHAIN_ADDRESS_KEY = "sourceMailIntakeAddress"
const PROTOCOL_VERSION = "1"

// ---------------------------------------------------------------------
// One-time Keychain setup (run standalone once; skipped on later runs)
// ---------------------------------------------------------------------
async function ensureSetup() {
  if (!Keychain.contains(KEYCHAIN_TOKEN_KEY)) {
    const a = new Alert()
    a.title = "Source Mail Drop — one-time setup"
    a.message = "Paste your SOURCE_MAIL_TOKEN (the same value in ~/dev/secrets/.env on your Mac). Stored only in this device's Scriptable Keychain."
    a.addTextField("SOURCE_MAIL_TOKEN")
    a.addAction("Save")
    await a.presentAlert()
    const token = a.textFieldValue(0).trim()
    if (!token) throw new Error("No token entered; aborting setup.")
    Keychain.set(KEYCHAIN_TOKEN_KEY, token)
  }
  if (!Keychain.contains(KEYCHAIN_ADDRESS_KEY)) {
    const a = new Alert()
    a.title = "Source Mail Drop — intake address"
    a.message = "The dedicated mailbox address (SOURCE_MAIL_USER in .env)."
    a.addTextField("intake-xxxx@gmail.com")
    a.addAction("Save")
    await a.presentAlert()
    const addr = a.textFieldValue(0).trim()
    if (!addr) throw new Error("No address entered; aborting setup.")
    Keychain.set(KEYCHAIN_ADDRESS_KEY, addr)
  }
}

// ---------------------------------------------------------------------
// Canonicalization — must match source_mail_pull.py's canonical_string()
// exactly, including counting by Unicode code point (not UTF-16 code unit,
// which is what JS .length gives you and would silently diverge for any
// payload containing an emoji or other supplementary-plane character).
// ---------------------------------------------------------------------
function codePointLength(s) {
  return Array.from(s).length
}

function canonicalString(type, ts, payload) {
  const parts = [type, ts, payload]
  return "v" + PROTOCOL_VERSION + "|" +
    parts.map(p => `${codePointLength(p)}:${p}`).join("|")
}

// ---------------------------------------------------------------------
// Pure-JavaScript SHA-256 / HMAC-SHA256. Deliberately NOT using a WebView
// bridge to crypto.subtle: that path depends on Web Crypto actually being
// available inside a bare loadHTML() WebView (Web Crypto normally requires
// a "secure context", which a plain loadHTML page may not establish), and
// when it isn't, the async function hangs or throws with nothing able to
// surface the failure back through the completion() bridge -- exactly the
// silent spinner this replaces. This implementation has no dependency on
// WebView, crypto.subtle, TextEncoder (also not present in Scriptable's own
// JS context, confirmed the hard way), or any other global beyond bare
// JavaScript -- it hand-encodes UTF-8 and does the SHA-256/HMAC bit math
// itself. Verified byte-for-byte against Node's crypto
// module across SHA-256's block-boundary edge cases (55/56/64/119-byte
// inputs) and HMAC's short-key/long-key branches before shipping.
// ---------------------------------------------------------------------
function rotr(x, n) { return (x >>> n) | (x << (32 - n)) }

function sha256(bytes) {
  const K = [
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
  ]
  let h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a,
      h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19

  const len = bytes.length
  const bitLen = len * 8
  const withOne = len + 1
  const padLen = (withOne + 8 + 63) & ~63
  const padded = new Uint8Array(padLen)
  padded.set(bytes)
  padded[len] = 0x80
  const dv = new DataView(padded.buffer)
  dv.setUint32(padLen - 4, bitLen >>> 0, false)
  dv.setUint32(padLen - 8, Math.floor(bitLen / 0x100000000), false)

  const w = new Uint32Array(64)
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = (padded[offset+i*4]<<24) | (padded[offset+i*4+1]<<16) |
             (padded[offset+i*4+2]<<8) | (padded[offset+i*4+3])
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >>> 3)
      const s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >>> 10)
      w[i] = (w[i-16] + s0 + w[i-7] + s1) | 0
    }
    let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25)
      const ch = (e & f) ^ (~e & g)
      const temp1 = (h + S1 + ch + K[i] + w[i]) | 0
      const S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const temp2 = (S0 + maj) | 0
      h=g; g=f; f=e; e=(d+temp1)|0; d=c; c=b; b=a; a=(temp1+temp2)|0
    }
    h0=(h0+a)|0; h1=(h1+b)|0; h2=(h2+c)|0; h3=(h3+d)|0
    h4=(h4+e)|0; h5=(h5+f)|0; h6=(h6+g)|0; h7=(h7+h)|0
  }

  const out = new Uint8Array(32)
  const hs = [h0,h1,h2,h3,h4,h5,h6,h7]
  for (let i = 0; i < 8; i++) {
    out[i*4]   = (hs[i] >>> 24) & 0xff
    out[i*4+1] = (hs[i] >>> 16) & 0xff
    out[i*4+2] = (hs[i] >>> 8) & 0xff
    out[i*4+3] = hs[i] & 0xff
  }
  return out
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length)
  out.set(a, 0)
  out.set(b, a.length)
  return out
}

function hmacSha256(keyBytes, msgBytes) {
  const blockSize = 64
  if (keyBytes.length > blockSize) keyBytes = sha256(keyBytes)
  const key = new Uint8Array(blockSize)
  key.set(keyBytes)
  const ipad = new Uint8Array(blockSize)
  const opad = new Uint8Array(blockSize)
  for (let i = 0; i < blockSize; i++) {
    ipad[i] = key[i] ^ 0x36
    opad[i] = key[i] ^ 0x5c
  }
  const inner = sha256(concatBytes(ipad, msgBytes))
  return sha256(concatBytes(opad, inner))
}

function toHex(bytes) {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("")
}

// Hand-rolled UTF-8 encode -- no TextEncoder in Scriptable's JS context.
// codePointAt() (not charCodeAt()) so a supplementary-plane character
// (surrogate pair in UTF-16) is read as one code point instead of two
// mis-encoded halves; the loop index is bumped past the low surrogate that
// codePointAt() already consumed.
function utf8Encode(str) {
  const bytes = []
  for (let i = 0; i < str.length; i++) {
    const code = str.codePointAt(i)
    if (code > 0xFFFF) i++
    if (code < 0x80) {
      bytes.push(code)
    } else if (code < 0x800) {
      bytes.push(0xC0 | (code >> 6), 0x80 | (code & 0x3F))
    } else if (code < 0x10000) {
      bytes.push(0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F),
                 0x80 | (code & 0x3F))
    } else {
      bytes.push(0xF0 | (code >> 18), 0x80 | ((code >> 12) & 0x3F),
                 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F))
    }
  }
  return new Uint8Array(bytes)
}

function hmacSha256Hex(keyStr, message) {
  return toHex(hmacSha256(utf8Encode(keyStr), utf8Encode(message)))
}

// ---------------------------------------------------------------------
// Build the signed body. Deliberately does NOT send anything -- see the
// note on main() below for why sending is left to the Shortcut's own
// Send Email action instead of Scriptable's Mail.send().
// ---------------------------------------------------------------------
function buildDrop(type, payload) {
  // Kept in step with TYPE_DIRS in source_mail_pull.py. `news` was retired
  // 2026-08-18 when the Obsidian Web Clipper replaced the NewsInput
  // pipeline; the Mac now rejects it, so signing one here would produce a
  // drop that sends cleanly from the phone and is silently discarded.
  if (!["voice", "podcast"].includes(type)) {
    throw new Error(`type must be voice/podcast, got ${type}`)
  }
  payload = payload.trim()
  if (!payload) throw new Error("empty payload — nothing to send")

  const token = Keychain.get(KEYCHAIN_TOKEN_KEY)
  const intakeAddress = Keychain.get(KEYCHAIN_ADDRESS_KEY)

  // Integer-second precision, trailing Z — matches
  // source_mail_pull.py --emit exactly. Do NOT use toISOString() directly;
  // it includes milliseconds, which is still parseable Python-side but
  // needlessly diverges from the reference format.
  const ts = new Date().toISOString().split(".")[0] + "Z"

  const mac = hmacSha256Hex(token, canonicalString(type, ts, payload))

  const body =
    `v: ${PROTOCOL_VERSION}\n` +
    `type: ${type}\n` +
    `ts: ${ts}\n` +
    `payload: ${payload}\n` +
    `hmac: ${mac}\n`

  return { to: intakeAddress, subject: `${type} drop`, body: body }
}

// Used only by manualTestPrompt() below, which runs from a direct foreground
// tap in the app -- never under Siri -- so Scriptable's own Mail.send() is
// fine there.
async function sendDropDirectly(type, payload) {
  const built = buildDrop(type, payload)
  const mail = new Mail()
  mail.toRecipients = [built.to]
  mail.subject = built.subject
  mail.body = built.body
  mail.isBodyHTML = false
  await mail.send()
}

// ---------------------------------------------------------------------
// Manual test mode: prompts for type + payload and sends, so you can
// verify a real send without building a Shortcut first. Reachable only
// when running the script standalone (tap the play button in the app)
// after setup is already done.
// ---------------------------------------------------------------------
async function manualTestPrompt() {
  const choice = new Alert()
  choice.title = "No Shortcut input"
  choice.message = "This script is meant to be called from a Shortcut " +
    "(Run Script -> Input -> Shortcut Input). Since you ran it directly, " +
    "you can send a one-off test drop instead."
  choice.addAction("Send a test drop")
  choice.addCancelAction("Cancel")
  if (await choice.presentAlert() !== 0) return

  const t = new Alert()
  t.title = "Test drop"
  t.addTextField("type: voice / podcast", "voice")
  t.addTextField("payload")
  t.addAction("Send")
  t.addCancelAction("Cancel")
  if (await t.presentAlert() !== 0) return

  const type = t.textFieldValue(0).trim()
  const payload = t.textFieldValue(1)
  await sendDropDirectly(type, payload)

  const done = new Alert()
  done.title = "Sent"
  done.message = `Signed and mailed a ${type} drop. Verify on the Mac with: ` +
    "python3 source_mail_pull.py --once --dry-run"
  done.addAction("OK")
  await done.presentAlert()
}

// ---------------------------------------------------------------------
// Entry point. Expects Shortcut input as a dictionary:
//   { "type": "voice" | "podcast", "payload": "<text or URL>" }
// Set this via "Run Script" -> Input -> Shortcut Input, after building that
// dictionary earlier in the Shortcut (see the build steps doc).
//
// A standalone run (tapping play in the app) has no Shortcut input at all
// -- args.shortcutParameter is only populated when a Shortcut's Run Script
// action actually supplies one. The first-ever standalone run does the
// one-time Keychain setup and stops there, cleanly, rather than falling
// through into "no input" and erroring on a run that was never supposed to
// send anything.
// ---------------------------------------------------------------------
async function main() {
  const alreadySetUp = Keychain.contains(KEYCHAIN_TOKEN_KEY) &&
    Keychain.contains(KEYCHAIN_ADDRESS_KEY)
  await ensureSetup()

  let input = args.shortcutParameter

  // Some Shortcuts/Scriptable combinations hand a Dictionary through as a
  // JSON string rather than a native object -- accept either rather than
  // assuming one.
  if (typeof input === "string") {
    try { input = JSON.parse(input) } catch (e) { /* leave as-is; falls through below */ }
  }

  if (!input || typeof input !== "object") {
    if (!alreadySetUp) {
      const a = new Alert()
      a.title = "Setup complete"
      a.message = "Token and intake address saved to this device's " +
        "Keychain. Now build your Shortcuts: Dictionary -> Run Script -> " +
        "SourceMailDrop."
      a.addAction("OK")
      await a.presentAlert()
      return
    }
    await manualTestPrompt()
    return
  }

  // Diagnose a malformed Dictionary explicitly rather than letting
  // sendDrop's blunt validation error swallow what was actually received --
  // a key-name mismatch (e.g. "Type" instead of "type") produces exactly
  // this shape (input is an object, but .type is undefined), and seeing the
  // raw JSON is the fastest way to spot it.
  //
  // A thrown Error, not an Alert: this branch runs on every real Shortcut
  // invocation, and a Shortcut built around Dictate Text runs under Siri,
  // where Scriptable's Alert UI cannot present at all ("Alerts are not
  // supported in Siri") -- confirmed the hard way. A thrown Error surfaces
  // as plain text in Shortcuts' own "Could Not Run Run Script" dialog
  // instead, which works in every context including Siri.
  if (typeof input.type !== "string" || !input.type ||
      typeof input.payload !== "string" || !input.payload) {
    throw new Error(
      "Expected a dictionary with text keys 'type' and 'payload' " +
      "(both lowercase). Got: " + JSON.stringify(input) +
      " -- check the Dictionary action: key names must be exactly " +
      "'type' / 'payload', both Text, 'type' one of voice/podcast, " +
      "'payload' non-empty.")
  }

  // Return the built { to, subject, body } for the Shortcut's own Send
  // Email action to actually send -- not sent from here. Scriptable's
  // Mail.send() presents an interactive compose sheet, and any UI
  // presentation is exactly the category of thing Siri blocked for Alert
  // above; a native Shortcuts action is what Apple engineered to work in
  // that context, so sending belongs there instead.
  const built = buildDrop(input.type, input.payload)
  Script.setShortcutOutput(built)
}

await main()
Script.complete()