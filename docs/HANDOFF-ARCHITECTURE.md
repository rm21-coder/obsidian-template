# Meeting Handoff — Reference Architecture

A backend-agnostic pattern for delivering an authenticated "handoff" of
scheduling + people data into a downstream workflow (here: the Obsidian
meeting pre-population pipeline), designed to be adopted by other CIOs whose
tenants and endpoints differ.

Status: v1, 2026-08-05. Reference implementation: `Templates/Scripts/handoff_source.py`
(sources) + `Templates/Scripts/meeting_prepopulate.py` (consumer).

## 1. The one idea: separate the contract from the transport

The consumer depends on a **contract** — a schema-versioned, integrity-checked,
optionally-signed payload plus a small fetch/ack interface — and nothing else.
*How the bytes arrive* (the transport) is pluggable behind that interface. This
is what makes the workflow portable and distributable:

- The producer can be a Claude Code session with an MCP calendar connector
  today, a tenant MCP server tomorrow, a Graph pull, or a Power Automate
  flow — swapped with a config change, not a rewrite.
- Each CIO wires their own backend to the same contract; the consumer code is
  identical everywhere.

Why this matters here specifically: a cloud-drive sync client as the producer
transport is fragile in practice (sync-timing races, conflict copies,
online-only placeholders that don't hydrate in time). None of that fragility
should be visible to the consumer or to the people you distribute this to.
Pin to the contract, not to any one producer's sync client — this repo's
recommended producer (Tier B below) writes the signed drop straight into the
local drop folder from an MCP-connector session (or LLM-free via a direct
Graph call, `graph_calendar_fetch.py`), with an Azure Blob Storage relay as
the optional variant when producer and vault are on different machines,
precisely to avoid that fragility.

## 2. The Handoff Contract (schema v1)

A handoff is one logical unit with three properties: **integrity** (the bytes
weren't corrupted), **authenticity** (a trusted producer created them), and a
**commit signal** (it's complete and safe to consume).

### 2.1 Artifacts per handoff

| Artifact | Required | Purpose |
|---|---|---|
| `<name>.json` | yes | The payload (schema-v1 JSON below). |
| `<name>.json.sha256` | recommended | Integrity. `sha256sum` format: lowercase hex, two spaces, filename. |
| `<name>.sig` | optional | Authenticity. Producer HMAC-SHA256 over the exact `.json` bytes, hex. |
| `<name>.ready` | yes (file transports) | 0-byte commit marker, written **last**. A payload without it is treated as still-arriving. |

`<name>` convention: `schedule-handoff-<YYYY-MM-DD>.v1`. For non-file transports
(MCP), the same four properties are fields on the response rather than files.

### 2.2 Payload — top level

`schema_version` (int, currently 1) · `source` (e.g. `microsoft-cowork`) ·
`source_version` · `generated_at` (ISO-8601) · `user{ display_name, email,
tenant, timezone }` · `week{ start, end }` · `meetings[]` · `contacts[]` ·
`notes[]`.

### 2.3 `meetings[]` (abbreviated)

`uid`, `series_uid`, `is_recurring_instance`, `subject`, `start`/`end` (true
UTC), `duration_minutes`, `is_all_day`, `organizer`, `my_response_status`,
`attendees[]` (with `role`, `response`, `is_resource`/`is_external`/
`is_optional`), `attendee_counts`, `location` (display, `is_teams_meeting`,
`teams_join_url`), `body_preview` (redacted when sensitivity is
private/confidential), `categories`, `sensitivity`, `is_cancelled`, timestamps,
and a `producer_classification_hint { class, rationale }` of
solo / individual / group / broadcast.

### 2.4 `contacts[]`

`email`, `display_name`, `given_name`, `surname`, `title`, `department`,
`office`, `phone`, `manager{ name, email }`, `company`, `is_external`,
`directory_source` (`tenant-gal` when resolved, else `invite-fallback`),
`directory_object_id`.

### 2.5 Producer responsibilities (the transform)

Times are normalized to **true UTC** (DST-aware), group/distribution/resource
mailboxes are excluded, and the payload is emitted by a **deterministic**
transform (never hand-written) so the same inputs yield byte-identical output —
which is what makes the SHA-256 and signature meaningful. Reference transform:
`Templates/Scripts/meeting_handoff_transform.js`.

## 3. The fetch/ack interface

The consumer speaks only this (see `handoff_source.py::HandoffSource`):

```
discover() -> [handle, ...]      # what is pending
load(handle) -> HandoffRecord    # fetch + verify (integrity, signature, schema)
ack(handle)                      # mark consumed (archive, or server ack)
```

`load()` performs all three verifications and raises `HandoffError` on any
failure, so a bad handoff is skipped, never fatal. The consumer's business
logic runs on `record.payload` and is transport-unaware.

## 4. Transport tiers — one contract, three postures

Adopters pick a tier by their device/tenant posture. All satisfy the same
interface, so the consumer is unchanged.

### Tier A — managed device + native connector
The producer writes to a tenant cloud drive (OneDrive/SharePoint or
equivalent); a **managed** endpoint reads it with the native sync client or a
scoped Graph pull. Appropriate only where the endpoint is managed and
sanctioned to hold tenant data. Described here for completeness — **not
implemented in this repo** (its sync-client fragility is exactly what Tier B
below was built to avoid); implement your own `HandoffSource` against
`handoff_source.py` if this posture fits your environment.

### Tier B — signed drop / relay (unmanaged-endpoint safe)
The producer (or a tenant-side pusher: Power Automate, a scheduled job) writes a
**minimized, signed, short-TTL** payload to a neutral drop the endpoint pulls
with a **relay-scoped** credential — never tenant credentials, no sync client.
Works over any relay: rsync/SFTP pull, object-store with a short-lived SAS, or a
manually imported signed file. This is the recommended posture for an unmanaged
endpoint that must not authenticate to the tenant.
Reference: `DropFolderSource` in `handoff_source.py`. A worked object-store
implementation — Azure Blob Storage, a Power Automate pusher, and
`handoff_blob_pull.py` as the puller — is documented end to end in
[`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md).

**Recommended producer for this tier:** rather than building a Cowork/Graph
job or Power Automate flow, a Claude Code session with an off-the-shelf MCP
connector to your calendar system (Microsoft 365, Google Workspace, etc.) can
do the privileged read and run the deterministic transform itself, writing
straight into the drop folder — no relay needed at all when it runs on the
same machine as the vault. This needs no custom server and no Tier C
build-out. Fully documented, with a ready-to-use M365 reference transform:
[`Meeting-Handoff-MCP-Producer.md`](Meeting-Handoff-MCP-Producer.md).

### Tier C — tenant MCP server (the endgame)
A purpose-built MCP server inside/adjacent to the tenant performs the privileged
M365 read server-side and exposes scheduling + people as **scoped tools**. The
endpoint holds only a **revocable OAuth token to the server**, not tenant
credentials and not a sync client. Smallest blast radius; also normalizes across
M365 / Google / on-prem behind one interface.
Reference: `MCPSource` in `handoff_source.py` (stub; suggested server tools:
`handoff.list_pending` / `handoff.fetch` / `handoff.ack`).

Selection is via `MEETING_PREPOP_SOURCE` = `drop` (default) | `mcp`.

## 5. Security requirements (the CISO checklist)

These are contract-level, independent of transport:

1. **Minimize at the producer, by classification.** Emit only the fields the
   workflow needs. Filter by your data-classification scheme *before* egress —
   restricted-class data should never leave the tenant toward an unmanaged
   endpoint, so drop it at the source, not scrub it at the consumer.
2. **Sign for authenticity.** Producer signs the exact payload bytes with
   HMAC-SHA256; the consumer verifies (constant-time). A compromised relay or
   drop then cannot inject or tamper with a handoff. Enable with
   `HANDOFF_HMAC_KEY_FILE` (+ `HANDOFF_REQUIRE_SIGNATURE=1` to fail closed).
3. **Checksum for integrity.** SHA-256 sidecar over the JSON bytes; catches
   corruption independent of authenticity.
4. **Encrypt + expire.** Where the drop is outside the tenant, envelope-encrypt
   so the relay is zero-knowledge; short TTL; delete after fetch (`ack`
   archives/removes).
5. **Revoke per endpoint.** Each distributed CIO/device holds its own
   credential (relay token or MCP OAuth), killable independently — no shared
   secret that forces fleet-wide rotation.
6. **Least standing access on the endpoint.** No tenant credentials and no sync
   client on unmanaged devices (Tiers B/C). The endpoint holds only a
   relay/server token scoped to the single handoff resource.

## 6. Adopting this (for another CIO)

1. Stand up a **producer** that emits the schema-v1 contract for your tenant
   (Cowork transform, a Graph job, or your MCP server). Reuse the reference
   transform's normalization rules.
2. Choose a **transport tier** for your endpoint posture (A/B/C above).
3. Run the **consumer** unchanged; point `MEETING_PREPOP_SOURCE` at your tier
   and set the signing key. The pluggable interface means your backend choice
   never touches the workflow logic.

Because the contract is backend-agnostic, a Google-Workspace or on-prem CIO
implements a different producer/transport but keeps the same consumer — which is
the whole point of separating the contract from the transport.
