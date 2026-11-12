# Azure Key Vault secret backend (optional, macOS)

By default `secret_store.get_secret()` reads the platform keystore (the
Keychain on macOS), and the environment only for a secret the keystore
reports it does not hold. An organisation that wants its
secrets held centrally, rotated in one place and every read logged can point
the store at an Azure Key Vault instead. It is off unless configured, and it
is macOS-only for now; Windows keeps DPAPI.

## What changes

With the vault on, the lookup order is:

1. The vault. The secret's name is lowercased with `_` turned into `-`
   (Key Vault names allow letters, digits and dashes): `SOURCE_MAIL_TOKEN`
   is read from `source-mail-token`.
2. Nothing. A vault that is unreachable, refuses the call or lacks the
   secret makes `get_secret` return `None`, with the cause on stderr. The
   Keychain is consulted only when `KEYVAULT_KEYCHAIN_FALLBACK=1`, which is
   meant for a parallel run while migrating and nothing else.

The environment, including `~/dev/secrets/.env`, is never a source of a
secret while the vault is on, fallback mode included: a copy left there is
ignored rather than overriding the vault, and `status` (below) names it.
There is no switch that restores the old env-first order, because any such
switch would be read from the same `.env` it is meant to protect against.
Configuration that is not secret (the mailbox address, the allowlist,
`LLM_BASE_URL`) is still read from `.env`.

One limit to state plainly: `KEYVAULT_URL` itself lives in `.env`. Whoever
can edit that file can remove the line, which turns the vault off; the jobs
then read the Keychain, and `.env` for any name the Keychain lacks. Once the
Keychain copies are deleted (step 5), that means `.env` supplies every
secret. The same editor can instead point `KEYVAULT_URL`, the tenant id and
the client id at a vault they control. The code only insists on an
`https://*.vault.azure.net` host, so the jobs would sign in there with the
certificate (whose public half is not secret) and read whatever that vault
holds. Protecting `.env` (mode 0600, outside any synced folder) is therefore
still part of the control, not made redundant by the vault.

Jobs sign in as a **service principal with a certificate**, not as a person,
so no interactive sign-in can expire under an unattended job. The
certificate's private key is the one credential left on the Mac, and it stays
in the Keychain. Values and the access token live in the process's memory and
are never written to disk.

The backend uses only the standard library and the Apple-signed
`/usr/bin/openssl`, which signs the client assertion; the private key reaches
it through a pipe, never a file or the command line. No Azure SDK and no `az`
CLI, so nothing joins the hash-locked dependencies, and the jobs that run on
Apple's `/usr/bin/python3` need nothing extra.

What this does not change: code running as you on the Mac can still use the
certificate to fetch the secrets, from any network the vault accepts. The
vault takes the secrets off the disk; it does not take trust off the
endpoint.

## Setup

What the vault's administrators provide:

- A vault with role-based access, soft delete and purge protection, reachable
  only from your organisation's networks (a firewall rule including the VPN
  ranges, or a private endpoint).
- An app registration for the jobs, with your certificate as its only
  credential, granted **Key Vault Secrets User** on this vault alone.
- **Key Vault Secrets Officer** for you, to load and rotate values.
- Ideally, diagnostic logs with an alert on any read by another identity.

On the Mac:

1. Generate the certificate and keep its private key in the Keychain. Send
   the administrators only the `.cer`, and confirm its SHA-1 thumbprint with
   them over a second channel:

   ```bash
   d=$(mktemp -d) && /usr/bin/openssl req -x509 -newkey rsa:3072 -nodes -keyout "$d/key.pem" -out ~/Downloads/kv-client.cer -days 365 -subj "/CN=obsidian-workflow-kv"
   base64 < "$d/key.pem" | tr -d '\n' | /usr/bin/python3 -I Templates/Scripts/secret_store.py set azure_kv_client_key
   rm "$d/key.pem" && rmdir "$d"
   /usr/bin/openssl x509 -in ~/Downloads/kv-client.cer -noout -fingerprint -sha1
   ```

   Keep the key at 3072 bits or smaller. A Keychain write goes to
   `security -i` on stdin as one command line, and lines over 4,000 bytes
   are refused (longer ones split). The base64 PEM of a 3072-bit key is
   about 3,300 characters; a 4096-bit key is about 4,400 and is refused with
   "value cannot be written safely".

2. Load each secret into the vault without putting it on a command line
   (`printf` is a shell builtin, so the value never reaches a process
   listing):

   ```bash
   printf %s "$(/usr/bin/security find-generic-password -s source_mail_token -w)" | az keyvault secret set --vault-name <vault> --name source-mail-token --file /dev/stdin --output none
   ```

3. Add the configuration to `~/dev/secrets/.env`. None of it is secret, but
   it names your tenant, so it never goes in this repository:

   ```
   KEYVAULT_URL=https://<vault>.vault.azure.net/
   KEYVAULT_TENANT_ID=<tenant id>
   KEYVAULT_CLIENT_ID=<app (client) id>
   KEYVAULT_CERT_THUMBPRINT=<40 hex, colons optional>
   # KEYVAULT_KEY_NAME=azure_kv_client_key      (the default)
   # KEYVAULT_KEYCHAIN_FALLBACK=1               (parallel run only)
   ```

4. Check it:

   ```bash
   /usr/bin/python3 -I Templates/Scripts/secret_store.py status
   ```

   This prints each secret's source (`keyvault`, `keystore`, `.env`,
   `MISSING`), never a value, names any `.env` copy that is being ignored, and
   exits 1 if any secret is missing from the vault. `UNAVAILABLE` means the
   vault (off VPN, the firewall, the sign-in, an unreadable certificate key)
   or the Keychain could not be asked: the secret may well be stored, so do
   not store it again; the line above it says why.

5. Once the jobs have run cleanly against the vault, rotate each key at its
   source, write the new value only to the vault, and delete the Keychain
   copies with `python3 Templates/Scripts/secret_store.py rm NAME`.

The plugin allowlist's HMAC key (`obsidian-allowlist-hmac`) is not a
`secret_store` secret and stays in the Keychain: it anchors a security
control that has to work with the network down.

## Failures and what they mean

| On stderr | Cause |
|---|---|
| `Key Vault refused the connection (ForbiddenByFirewall): off VPN?` | The vault firewall does not accept this network. |
| `Key Vault unreachable (...): off VPN?` | No route or DNS to the vault or to the sign-in endpoint. |
| `Key Vault HTTP 401 invalid_client AADSTS…` | The sign-in was refused: wrong client id, or a certificate or thumbprint that does not match the app registration. |
| `Key Vault HTTP 403 Forbidden…` | Signed in, but the identity lacks Key Vault Secrets User on this vault. |
| `NAME: not in Key Vault` | The vault has no secret under the dashed name. |
| `certificate key … is not in the Keychain` | Step 1 was not completed, or `KEYVAULT_KEY_NAME` points elsewhere. |
| `certificate key … could not be read from the Keychain` | The Keychain is locked, an ACL prompt is pending, or the read timed out. The key may well be stored: do not store it again; unlock the Keychain or dismiss the prompt, then re-run. |
| `Key Vault is configured but missing …` | `KEYVAULT_URL` is set without its companions. |

To roll back, comment out `KEYVAULT_URL` in `.env`: the store reverts to the
Keychain, provided its copies have not been deleted yet. The certificate
expires after the `-days` you gave it; renew it before then by repeating
step 1 and sending the new `.cer`.
