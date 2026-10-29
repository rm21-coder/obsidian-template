#!/usr/bin/env python3
"""secret_store.py — one place to read and write workflow secrets.

Every script that needs an API key, app password, or token resolves it
through get_secret(), in this order:

  1. The environment (after the caller's own load_dotenv). An explicitly
     set env var always wins, so .env files, CI, and one-off overrides
     keep working exactly as before.
  2. The platform keystore:
       macOS   — Keychain generic password (service = the secret's name
                 lowercased, account = the current user), read via
                 /usr/bin/security.
       Windows — a DPAPI-encrypted file under security_common.state_dir().
                 CryptUnprotectData only succeeds for the logged-in user.
       other   — a 0600 file under state_dir() (best effort, plainly a
                 weaker fallback).

Nothing here is required: an install that keeps everything in .env never
touches the keystore. Moving a secret out of .env and into the keystore is
what removes the plaintext from disk.

Why the macOS reads shell out to /usr/bin/security instead of a native
Keychain binding: Keychain ACLs bind to the *calling binary*. Homebrew's
python is ad-hoc signed, so every `brew upgrade python` would orphan the
grant, and under launchd the re-consent prompt can never be shown — the
job would hang. /usr/bin/security is Apple-signed and stable across python
upgrades. The tradeoff: the item is readable by anything that can execute
`security` as you. That is still strictly better than a plaintext .env —
reading requires code execution as the user, not a mere file read.

The service name is the secret's name lowercased, which deliberately
matches the manual convention the installer has always used (a lowercased
name, e.g. `anthropic_api_key`), so an install that already keeps a key in the
Keychain by hand needs no migration.

CLI (for setup and the installer):
  python3 secret_store.py set NAME     value read from stdin or a hidden
                                       prompt — never from argv
  python3 secret_store.py get NAME     prints the value (careful in shells
                                       with history expansion of output)
  python3 secret_store.py rm NAME
  python3 secret_store.py status [NAME...]
                                       where each secret resolves from
                                       (env, keyvault, keystore); never
                                       prints a value. Exit 1 if any is
                                       missing or shadowed by .env.

Azure Key Vault (macOS, opt-in): set KEYVAULT_URL and its companions in
~/dev/secrets/.env and step 2 becomes the vault. See the Key Vault section
below and docs/Key-Vault.md.
"""
from __future__ import annotations

import base64
import getpass
import http.client
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

# security_common owns the platform forks for state_dir, DPAPI, and file
# permission hardening; reuse rather than re-implement.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import security_common  # noqa: E402


def _service(name: str) -> str:
    return name.strip().lower()


def _account() -> str:
    return getpass.getuser()


def _secrets_dir() -> Path:
    d = security_common.state_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- macOS (Keychain via /usr/bin/security) ---------------------------

# Hard ceiling on every /usr/bin/security call. A Keychain op that has not
# returned by now is not slow, it is wedged: something is waiting on a GUI
# consent dialog that an unattended run (launchd, --auto, CI) can never
# answer. Bound it, report failure, let the caller fall back to .env — and
# never retry, because every retry queues another dialog onto a stack that
# blocks all later Keychain ops on the machine, git-over-HTTPS through the
# osxkeychain helper included.
KEYCHAIN_TIMEOUT_SECONDS = 15


def _security(*args: str) -> subprocess.CompletedProcess | None:
    """Run /usr/bin/security under a hard timeout. None means it hung (and
    was killed) — a distinct outcome from a non-zero exit, and never a
    reason to try again."""
    try:
        return subprocess.run(
            ["/usr/bin/security", *args],
            check=False, capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"secret_store: /usr/bin/security {args[0]} timed out after "
              f"{KEYCHAIN_TIMEOUT_SECONDS}s; Keychain appears wedged behind a "
              "consent prompt. Not retrying.", file=sys.stderr)
        return None


def _keychain_get(name: str) -> str | None:
    p = _security("find-generic-password",
                  "-a", _account(), "-s", _service(name), "-w")
    if p is None or p.returncode != 0:
        return None
    value = p.stdout.rstrip("\n")
    return value if value else None


def _keychain_set(name: str, value: str) -> bool:
    # Two hard-won constraints (both verified live, 2026-08-18):
    #   1. Direct argv, NOT `security -i` — interactive mode hangs on
    #      realistic key lengths. Cost: the value is visible in the process
    #      table for the milliseconds the command runs; acceptable on a
    #      single-user machine, and how the installer's keychain_set has
    #      always worked.
    #   2. Delete-then-add, NOT `-U` — updating an existing item raises a
    #      GUI consent prompt whenever the item's ACL doesn't already cover
    #      the caller, and under launchd (or any unattended run) that prompt
    #      can never be answered: the call wedges silently. A fresh add
    #      never prompts. Deleting first loses nothing — we are about to
    #      write the authoritative value.
    #   3. Every call is bounded by KEYCHAIN_TIMEOUT_SECONDS. If the delete
    #      hangs we stop there rather than issuing the add: a wedged Keychain
    #      answers nothing, and a second call only queues a second dialog.
    # -T pre-authorizes /usr/bin/security itself so later reads by launchd
    # jobs (which all shell out to it) need no consent.
    if _security("delete-generic-password",
                 "-a", _account(), "-s", _service(name)) is None:
        return False
    p = _security("add-generic-password",
                  "-a", _account(), "-s", _service(name), "-w", value,
                  "-T", "/usr/bin/security")
    return p is not None and p.returncode == 0


def _keychain_delete(name: str) -> bool:
    p = _security("delete-generic-password",
                  "-a", _account(), "-s", _service(name))
    return p is not None and p.returncode == 0


# ---------- Azure Key Vault (macOS, opt-in) -----------------------------------

# Optional, and off unless KEYVAULT_URL is set (in the caller's environment,
# normally via ~/dev/secrets/.env). With it set, every secret resolves from
# the vault instead of the platform keystore, as a service principal that
# signs in with a certificate:
#
#   KEYVAULT_URL               https://<vault>.vault.azure.net/
#   KEYVAULT_TENANT_ID         Entra tenant id
#   KEYVAULT_CLIENT_ID         the service principal's app (client) id
#   KEYVAULT_CERT_THUMBPRINT   SHA-1 thumbprint of its certificate, 40 hex
#   KEYVAULT_KEY_NAME          Keychain item holding the certificate's private
#                              key as one base64 line of the PEM
#                              (default azure_kv_client_key)
#   KEYVAULT_KEYCHAIN_FALLBACK 1 = on any vault failure, fall back to the
#                              Keychain. Meant for a parallel-run period only.
#
# The private key is the one credential left on the Mac, and it stays in the
# Keychain. Nothing fetched from the vault is written to disk: values are held
# in memory for the life of the process, as is the access token.
#
# Why no Azure SDK and no az CLI: both are large dependency trees in a workflow
# whose dependencies are hash-locked, and many callers run on Apple's
# /usr/bin/python3, which has neither. Everything here is the standard library
# plus /usr/bin/openssl (Apple-signed, like /usr/bin/security) to sign the
# client assertion. The key reaches openssl through a pipe, never a file.
#
# Off the vault's allowed networks its firewall answers HTTP 403
# ForbiddenByFirewall; that is reported by name, as "off VPN?", and the caller
# gets None exactly as it would for a missing secret.

KEYVAULT_TIMEOUT_SECONDS = 20
KEYVAULT_API_VERSION = "7.4"
_KEYVAULT_DEFAULT_KEY_NAME = "azure_kv_client_key"
_OPENSSL = "/usr/bin/openssl"

_kv_token: dict = {}          # {"value": str, "expires": epoch seconds}
_kv_cache: dict = {}          # vault secret name -> value, this process only
_last_source: dict = {}       # secret NAME -> where get_secret found it


class KeyVaultError(Exception):
    """A Key Vault lookup failed. The message names the cause, never a value."""


def keyvault_enabled() -> bool:
    return bool(os.environ.get("KEYVAULT_URL", "").strip())


def _keyvault_fallback() -> bool:
    return os.environ.get("KEYVAULT_KEYCHAIN_FALLBACK", "").strip().lower() in (
        "1", "true", "yes")


def _vault_name(name: str) -> str:
    """Key Vault names allow letters, digits and dashes only."""
    vname = name.strip().lower().replace("_", "-")
    if not vname or len(vname) > 127 or not all(
            c.isascii() and (c.isalnum() or c == "-") for c in vname):
        raise KeyVaultError(f"{name!r} cannot be a Key Vault secret name")
    return vname


def _kv_config() -> dict:
    cfg = {k: os.environ.get(k, "").strip() for k in (
        "KEYVAULT_URL", "KEYVAULT_TENANT_ID", "KEYVAULT_CLIENT_ID",
        "KEYVAULT_CERT_THUMBPRINT")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise KeyVaultError("Key Vault is configured but missing "
                            + ", ".join(missing))
    url = cfg["KEYVAULT_URL"].rstrip("/")
    if not (url.startswith("https://") and url.endswith(".vault.azure.net")):
        raise KeyVaultError("KEYVAULT_URL must be https://<name>.vault.azure.net")
    cfg["KEYVAULT_URL"] = url
    thumb = cfg["KEYVAULT_CERT_THUMBPRINT"].replace(":", "")
    try:
        cfg["x5t"] = bytes.fromhex(thumb)
    except ValueError:
        cfg["x5t"] = b""
    if len(cfg["x5t"]) != 20:
        raise KeyVaultError("KEYVAULT_CERT_THUMBPRINT must be a 40-hex SHA-1")
    cfg["key_name"] = (os.environ.get("KEYVAULT_KEY_NAME", "").strip()
                       or _KEYVAULT_DEFAULT_KEY_NAME)
    return cfg


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(message: bytes, key_pem: bytes) -> bytes:
    """RS256 over message with /usr/bin/openssl; the key goes in on a pipe fd."""
    if len(key_pem) > 16384:          # one pipe buffer; a 3072-bit key is ~2.5k
        raise KeyVaultError("certificate private key is unexpectedly large")
    r, w = os.pipe()
    try:
        os.write(w, key_pem)
        os.close(w)
        w = -1
        try:
            p = subprocess.run(
                [_OPENSSL, "dgst", "-sha256", "-sign", f"/dev/fd/{r}"],
                input=message, capture_output=True, pass_fds=(r,),
                timeout=KEYCHAIN_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeyVaultError(f"openssl could not sign: {type(e).__name__}")
    finally:
        os.close(r)
        if w != -1:
            os.close(w)
    if p.returncode != 0 or not p.stdout:
        raise KeyVaultError(f"openssl could not sign (exit {p.returncode})")
    return p.stdout


_LOGIN_HOST = "login.microsoftonline.com"
_KV_MAX_RESPONSE = 1 << 20


def _kv_http(url: str, *, data: bytes | None = None,
             headers: dict | None = None) -> dict:
    """One HTTPS call to the Entra sign-in host or a vault. Raises
    KeyVaultError naming the failure, never a value.

    http.client rather than urllib on purpose: urllib follows redirects
    (re-sending the bearer token wherever a 30x points), honours proxy
    variables from the environment, and carries file: and ftp: handlers.
    Here there is one fixed host per call, HTTPS only, and none of those."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.port not in (None, 443) or not (
            host == _LOGIN_HOST or host.endswith(".vault.azure.net")):
        raise KeyVaultError(f"refusing a Key Vault call to {parts.scheme}://{host}")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    conn = http.client.HTTPSConnection(
        host, context=ssl.create_default_context(),
        timeout=KEYVAULT_TIMEOUT_SECONDS)
    try:
        conn.request("POST" if data is not None else "GET", path,
                     body=data, headers=dict(headers or {}))
        resp = conn.getresponse()
        status, raw = resp.status, resp.read(_KV_MAX_RESPONSE)
    except (OSError, http.client.HTTPException) as e:
        raise KeyVaultError(f"Key Vault unreachable ({e or type(e).__name__}): off VPN?")
    finally:
        conn.close()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise KeyVaultError(f"Key Vault HTTP {status}: response is not JSON")
    if not isinstance(body, dict):
        raise KeyVaultError(f"Key Vault HTTP {status}: unexpected response")
    if status == 200:
        return body
    err = body.get("error")
    if isinstance(err, dict):               # Key Vault: {"error": {"code": ...}}
        code = str(err.get("code", ""))
        inner = err.get("innererror") or {}
        if isinstance(inner, dict) and inner.get("code"):
            code = f"{code}/{inner['code']}"
    else:                                   # Entra: {"error": "...", ...}
        code = str(err or "")
        ids = body.get("error_codes") or []
        if ids:
            code = f"{code} AADSTS{ids[0]}"
    if "ForbiddenByFirewall" in code:
        raise KeyVaultError("Key Vault refused the connection "
                            "(ForbiddenByFirewall): off VPN?")
    raise KeyVaultError(f"Key Vault HTTP {status} {code}".rstrip())


def _kv_access_token(cfg: dict) -> str:
    now = time.time()
    if _kv_token.get("value") and _kv_token.get("expires", 0) - 120 > now:
        return _kv_token["value"]
    blob = _keychain_get(cfg["key_name"])
    if not blob:
        raise KeyVaultError(f"certificate key {cfg['key_name']} is not in "
                            "the Keychain")
    try:
        key_pem = base64.b64decode(blob, validate=True)
    except ValueError:
        raise KeyVaultError(f"{cfg['key_name']} is not base64 of a PEM key")
    token_url = (f"https://{_LOGIN_HOST}/"
                 f"{urllib.parse.quote(cfg['KEYVAULT_TENANT_ID'])}/oauth2/v2.0/token")
    iat = int(now)
    header = {"alg": "RS256", "typ": "JWT", "x5t": _b64url(cfg["x5t"])}
    claims = {"aud": token_url, "iss": cfg["KEYVAULT_CLIENT_ID"],
              "sub": cfg["KEYVAULT_CLIENT_ID"], "jti": str(uuid.uuid4()),
              "nbf": iat - 60, "exp": iat + 600}
    signing_input = (_b64url(json.dumps(header).encode()) + "."
                     + _b64url(json.dumps(claims).encode())).encode("ascii")
    assertion = (signing_input.decode("ascii") + "."
                 + _b64url(_sign_rs256(signing_input, key_pem)))
    body = urllib.parse.urlencode({
        "client_id": cfg["KEYVAULT_CLIENT_ID"],
        "scope": "https://vault.azure.net/.default",
        "grant_type": "client_credentials",
        "client_assertion_type":
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion}).encode("ascii")
    resp = _kv_http(token_url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    token = resp.get("access_token")
    if not token:
        raise KeyVaultError("Entra returned no access token")
    _kv_token["value"] = token
    _kv_token["expires"] = now + float(resp.get("expires_in") or 300)
    return token


def _keyvault_get(name: str) -> str | None:
    """The secret's current value from the vault; None when the vault has no
    such secret. Any other failure raises KeyVaultError."""
    vname = _vault_name(name)
    if vname in _kv_cache:
        return _kv_cache[vname]
    cfg = _kv_config()
    token = _kv_access_token(cfg)
    try:
        resp = _kv_http(
            f"{cfg['KEYVAULT_URL']}/secrets/{vname}?api-version={KEYVAULT_API_VERSION}",
            headers={"Authorization": f"Bearer {token}"})
    except KeyVaultError as e:
        if "SecretNotFound" in str(e):
            return None
        raise
    value = resp.get("value")
    if not isinstance(value, str) or not value:
        return None
    _kv_cache[vname] = value
    return value


# ---------- Windows (DPAPI file) / other (0600 file) --------------------------

def _file_path(name: str, *, dpapi: bool) -> Path:
    suffix = ".dpapi" if dpapi else ".txt"
    return _secrets_dir() / (_service(name) + suffix)


def _dpapi_get(name: str) -> str | None:
    path = _file_path(name, dpapi=True)
    if not path.exists():
        return None
    try:
        raw = security_common._dpapi_crypt(path.read_bytes(), protect=False)
        return raw.decode("utf-8") or None
    except Exception:
        return None


def _dpapi_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=True)
    try:
        blob = security_common._dpapi_crypt(value.encode("utf-8"), protect=True)
        path.write_bytes(blob)
        security_common.restrict_file(path)
        return True
    except Exception:
        return False


def _plainfile_get(name: str) -> str | None:
    path = _file_path(name, dpapi=False)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _plainfile_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=False)
    try:
        path.write_text(value + "\n", encoding="utf-8")
        security_common.restrict_file(path)
        return True
    except OSError:
        return False


# ---------- public API --------------------------------------------------------

def get_secret(name: str, *, use_env: bool = True) -> str | None:
    """Resolve a secret by NAME. Environment first (unless use_env=False),
    then Azure Key Vault when KEYVAULT_URL is set (macOS), else the platform
    keystore. Returns None when nowhere has it; a vault failure is reported on
    stderr by cause and also returns None, unless KEYVAULT_KEYCHAIN_FALLBACK
    sends it on to the Keychain."""
    if use_env:
        env = os.environ.get(name, "").strip()
        if env:
            _last_source[name] = "env"
            return env
    if keyvault_enabled():
        if sys.platform != "darwin":
            print("secret_store: the Key Vault backend is macOS-only; "
                  "KEYVAULT_URL ignored", file=sys.stderr)
        else:
            try:
                value = _keyvault_get(name)
            except KeyVaultError as e:
                print(f"secret_store: {name}: {e}", file=sys.stderr)
                value = None
            else:
                if value is not None:
                    _last_source[name] = "keyvault"
                    return value
                print(f"secret_store: {name}: not in Key Vault",
                      file=sys.stderr)
            if not _keyvault_fallback():
                _last_source[name] = "missing"
                return None
            print(f"secret_store: {name}: falling back to the Keychain "
                  "(KEYVAULT_KEYCHAIN_FALLBACK)", file=sys.stderr)
    if sys.platform == "darwin":
        value = _keychain_get(name)
    elif sys.platform == "win32":
        value = _dpapi_get(name)
    else:
        value = _plainfile_get(name)
    _last_source[name] = "keystore" if value is not None else "missing"
    return value


def last_source(name: str) -> str | None:
    """Where the last get_secret(name) in this process found it: env,
    keyvault, keystore or missing. A name, never a value."""
    return _last_source.get(name)


def set_secret(name: str, value: str) -> bool:
    """Store a secret in the platform keystore. Returns False on failure —
    callers should treat that as 'keep using .env', not an exception."""
    if not value:
        return False
    if sys.platform == "darwin":
        return _keychain_set(name, value)
    if sys.platform == "win32":
        return _dpapi_set(name, value)
    return _plainfile_set(name, value)


def delete_secret(name: str) -> bool:
    if sys.platform == "darwin":
        return _keychain_delete(name)
    path = _file_path(name, dpapi=(sys.platform == "win32"))
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---------- CLI ----------------------------------------------------------------

SECRETS_ENV = Path.home() / "dev" / "secrets" / ".env"

# The secrets the workflow itself reads. The model key's name is configurable
# (LLM_API_KEY_NAME), so it is resolved at status time rather than listed.
_STATUS_NAMES = ("SOURCE_MAIL_APP_PASSWORD", "SOURCE_MAIL_TOKEN",
                 "OPEN_WEBUI_API_KEY")


def _env_file_keys(path: Path) -> dict:
    """KEY -> value for every assignment in a .env file, parsed by hand
    because the CLI runs on /usr/bin/python3, which has no python-dotenv."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = val.strip().strip('"').strip("'")
    return out


def _status(names: list[str]) -> int:
    """Report where each secret resolves from. Never prints a value.

    Exit 1 when any is missing, or sits in the environment or .env while the
    vault is on: an env value always wins, so a stale copy there would
    silently override the vault."""
    file_keys = _env_file_keys(SECRETS_ENV)
    for key in ("KEYVAULT_URL", "KEYVAULT_TENANT_ID", "KEYVAULT_CLIENT_ID",
                "KEYVAULT_CERT_THUMBPRINT", "KEYVAULT_KEY_NAME",
                "KEYVAULT_KEYCHAIN_FALLBACK", "LLM_API_KEY_NAME"):
        if key in file_keys and key not in os.environ:
            os.environ[key] = file_keys[key]
    if not names:
        names = [os.environ.get("LLM_API_KEY_NAME", "").strip()
                 or "ANTHROPIC_API_KEY", *_STATUS_NAMES]
    vault = keyvault_enabled()
    print("backend:", "keyvault " + os.environ["KEYVAULT_URL"].strip()
          if vault else "platform keystore")
    if vault and _keyvault_fallback():
        print("note: KEYVAULT_KEYCHAIN_FALLBACK is on (parallel run only)")
    bad = 0
    for name in names:
        if os.environ.get(name, "").strip() or file_keys.get(name):
            where = ".env" if file_keys.get(name) else "environment"
            print(f"{name}: {where}" + ("  <-- overrides Key Vault; remove it"
                                        if vault else ""))
            bad += 1 if vault else 0
            continue
        found = get_secret(name, use_env=False) is not None
        print(f"{name}: {last_source(name) if found else 'MISSING'}")
        bad += 0 if found else 1
    return 1 if bad else 0


def _cli(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        return _status(argv[1:])
    if len(argv) != 2 or argv[0] not in ("set", "get", "rm"):
        print(__doc__.split("CLI", 1)[1], file=sys.stderr)
        return 2
    op, name = argv
    if op == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"Value for {name}: ").strip()
        else:
            value = sys.stdin.readline().strip()
        if not value:
            print("empty value; nothing stored", file=sys.stderr)
            return 1
        ok = set_secret(name, value)
        print("stored" if ok else "FAILED to store", name,
              file=sys.stderr)
        return 0 if ok else 1
    if op == "get":
        value = get_secret(name)
        if value is None:
            print(f"{name}: not found", file=sys.stderr)
            return 1
        print(value)
        return 0
    ok = delete_secret(name)
    print("removed" if ok else "nothing removed", name, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
