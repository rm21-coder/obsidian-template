# Azure Blob Handoff Relay (Optional) — Tier B

A concrete Tier B implementation of the meeting-handoff pipeline's pluggable
transport (see [`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md)). Keeps
the consumer off any cloud-drive sync client entirely: a minimized,
signed-integrity payload lands in a dedicated Azure Blob Storage container,
and a small puller pulls it down to a plain local folder with a
read/list/delete-scoped SAS token — no tenant credentials on the endpoint, no
special filesystem permissions grant.

## Why a relay instead of a cloud-drive sync client

A cloud-drive sync client (OneDrive, SharePoint, etc.) as the transport
between producer and consumer is fragile in practice: sync-timing races,
conflict copies, online-only placeholders that don't hydrate in time, and —
on the consumer side, on macOS — a Full-Disk-Access grant because a
sync-client mount is TCC-protected. This is exactly the "Tier A" posture
described (but not implemented) in
[`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md).

This relay avoids all of that: the consumer reads a plain local folder — no
sync client, no special filesystem permissions — fed by a small puller over a
read-only-scoped SAS token.

**If your producer's environment has no direct network egress** (common for
sandboxed agent runtimes, which are often brokered entirely through a fixed
tool layer with no arbitrary outbound access), it can't push straight to
Blob. In that case you'll need a staging step of your own choosing between
the producer and the pusher below — whatever your producer already has write
access to. That staging mechanism is specific to your own environment and
is deliberately not prescribed here.

## Architecture

```
your producer  →  [pusher]  →  Azure Blob container  →  [puller]  →  local folder  →  meeting_prepopulate.py
                 (you build)      (Tier B relay)        handoff_blob_pull.py     MEETING_PREPOP_SOURCE=drop
```

- **Pusher** (tenant-side, you build this): forwards each completed
  `.json`/`.json.sha256`/`.ready` trio to Blob with a **write-only** SAS,
  from wherever your producer lands them. Runbook:
  [§4](#4-the-pusher-runbook) below (a Power Automate cloud flow is one way
  to build this; any tenant-side automation that can do an HTTP PUT works).
- **Puller** (shipped here): `Templates/Scripts/handoff_blob_pull.py`, run on
  a 5-minute poll. Pulls complete sets (a `.ready` blob must exist) into a
  local folder using a **read + list + delete** SAS, then deletes the blobs.
  It does not itself verify SHA-256 or HMAC signatures — that happens
  downstream in `handoff_source.DropFolderSource.load()`, exactly as it would
  for any other drop-folder relay (rsync, SFTP, manual import).
- **Consumer**: `meeting_prepopulate.py`, unchanged, pointed at the puller's
  local folder via `MEETING_PREPOP_SOURCE=drop`.

## 1. Storage account and container

One small storage account, one container, used for nothing else:

```bash
az storage account create \
  --name <youraccountname> --resource-group <your-rg> \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2

az storage container create \
  --account-name <youraccountname> --name meeting-handoff
```

No public access; both sides authenticate with a SAS scoped to this one
container (never an account key, never a tenant credential).

## 2. Two SAS tokens, least privilege per endpoint

Per the CISO checklist in `HANDOFF-ARCHITECTURE.md` §5: each side gets its own
revocable, minimally-scoped credential, independently rotatable.

**Pusher SAS — write only, no read/list/delete:**

```bash
az storage container generate-sas \
  --account-name <youraccountname> --name meeting-handoff \
  --permissions c --https-only \
  --expiry 2026-12-31T00:00Z \
  --auth-mode login --as-user
```
(`--permissions c` = create/write only; a compromised pusher credential can
drop payloads but can't read or delete existing ones.)

**Puller SAS — read, list, delete, no write:**

```bash
az storage container generate-sas \
  --account-name <youraccountname> --name meeting-handoff \
  --permissions rld --https-only \
  --expiry 2026-12-31T00:00Z \
  --auth-mode login --as-user
```
(`--permissions rld` = read + list + delete; the puller can consume and clean
up but can never inject a payload.)

Rotate both before `--expiry`; a short-lived, renewable SAS beats a
long-lived one per the "encrypt + expire" requirement in the architecture
doc. Store each in `~/dev/secrets/.env`, never in the flow definition or the
repo.

## 3. Consumer setup (this Mac, and the Windows box)

Add to `~/dev/secrets/.env` (`%USERPROFILE%\dev\secrets\.env` on Windows):

```
HANDOFF_BLOB_ACCOUNT_URL=https://<youraccountname>.blob.core.windows.net
HANDOFF_BLOB_CONTAINER=meeting-handoff
HANDOFF_BLOB_SAS=<the puller's rld SAS query string, no leading ?>
```

Then point the consumer at the puller's landing folder:

```
MEETING_PREPOP_SOURCE=drop
MEETING_PREPOP_HANDOFF_DIR=<same path as HANDOFF_BLOB_LOCAL_DIR, default ~/MeetingIngest>
```

Optional: `HANDOFF_BLOB_LOCAL_DIR` to move the landing folder (defaults to
`~/MeetingIngest`); `HANDOFF_HMAC_KEY_FILE` +
`HANDOFF_REQUIRE_SIGNATURE=1` if you've enabled producer signing (§5).

**macOS:** `cp Templates/Scripts/com.obsidian.handoff-blob-pull.plist
~/Library/LaunchAgents/` (edit `YOUR_USERNAME` first), then `launchctl load
-w ...`. No Full Disk Access needed — the target folder is a plain vault
subfolder, not `~/Library/CloudStorage`.

**Windows:** enable the `handoff-blob-pull` task registered from
`Templates/Scripts/windows/schedules.psd1` (`Enable-ScheduledTask -TaskName
handoff-blob-pull -TaskPath '\Obsidian'`) once validated by hand.

Test manually first: `python3 Templates/Scripts/handoff_blob_pull.py
--dry-run --verbose`.

## 4. The pusher runbook

A scheduled cloud flow (or any tenant-side automation that can watch a
location and issue an HTTP PUT), separate from your producer:

1. **Trigger:** whenever a new `.json`, `.json.sha256`, or `.ready` file
   lands wherever your producer (or its staging step) writes them. Handle
   each of the three independently and idempotently — they don't need to
   arrive together.
2. **Get file content** for the triggering file.
3. **HTTP** action: `PUT
   https://<youraccountname>.blob.core.windows.net/meeting-handoff/<filename>?<pusher SAS>`
   with header `x-ms-blob-type: BlockBlob` and the file content as the body.
4. **Delete or archive** the source file once the PUT succeeds, so the
   staging location doesn't accumulate stale copies — `meeting_prepopulate.py`
   never has to see them if you stay on `MEETING_PREPOP_SOURCE=drop`.

The three files don't need to arrive at Blob in a particular order: the
puller's `.ready`-gates-the-set logic already tolerates them landing in any
order, as long as all three eventually show up before the poll that matters.

## 5. Open decision: signing across the pusher hop

Depending on your producer, you may only be computing the SHA-256 checksum
today, not an HMAC signature — the checksum still travels through this relay
unchanged (integrity is preserved end-to-end), but authenticity (proof the
pusher, not an attacker with the write-SAS, produced the bytes) isn't
enforced across this hop unless you add one of:

- **(a) Ship without it for now.** The write-SAS is itself a credential only
  you hold; the residual risk is anyone who obtains that specific SAS could
  inject an unsigned payload into the container. Simpler, and consistent
  with a checksum-only posture.
- **(b) Sign at the producer instead.** Compute an HMAC-SHA256 over the JSON
  bytes with a shared key and write `<name>.sig` alongside the checksum — the
  verification path already exists (`HANDOFF_HMAC_KEY_FILE` /
  `HANDOFF_REQUIRE_SIGNATURE=1`, checked in `DropFolderSource.load()`). This
  requires giving your producer access to the shared secret at generation
  time, which may be a materially different (and more sensitive) trust
  boundary than what it does today.

This is a judgment call on your threat model, not a default picked here —
decide before enabling `HANDOFF_REQUIRE_SIGNATURE=1`.

## Troubleshooting

- **Puller finds nothing** → confirm the pusher flow is actually running and
  check its run history in Power Automate; then `az storage blob list
  --account-name <youraccountname> --container-name meeting-handoff --sas-token
  <sas>` to see what's actually in the container.
- **403 on list/download** → the puller's SAS lacks `r`/`l` permission, or has
  expired; regenerate per §2.
- **Sets never look "ready"** → the flow's three triggers ran out of order
  and the `.ready` blob's PUT hasn't landed yet; the puller just skips the
  set until it does (checked again next 5-minute poll).
- **Logs** → `<scripts>/logs/handoff_blob_pull.log`.

## Where things live

| Thing | Location |
|-------|----------|
| Puller | `Templates/Scripts/handoff_blob_pull.py` |
| Puller LaunchAgent | `Templates/Scripts/com.obsidian.handoff-blob-pull.plist` |
| Puller Windows task | `handoff-blob-pull` in `Templates/Scripts/windows/schedules.psd1` |
| Local landing folder | `HANDOFF_BLOB_LOCAL_DIR` (default `~/MeetingIngest`) |
| Pusher | Power Automate cloud flow (you build; not shipped in this repo) |
| Contract + tiers overview | [`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) |
| Consumer + JSON schema | [`Meeting-Pre-Population.md`](Meeting-Pre-Population.md) |
