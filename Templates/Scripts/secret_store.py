#!/usr/bin/env python3
"""secret_store.py — one place to read and write workflow secrets.

Every script that needs an API key, app password, or token resolves it
through get_secret(), in this order:

  1. The platform keystore (or Azure Key Vault, below):
       macOS   — Keychain generic password (service = the secret's name
                 lowercased, account = the current user), read via
                 /usr/bin/security.
       Windows — a DPAPI-encrypted file under security_common.state_dir().
                 CryptUnprotectData only succeeds for the logged-in user.
       other   — a 0600 file under state_dir() (best effort, plainly a
                 weaker fallback).
  2. The environment (after the caller's own load_dotenv), only for a
     secret the keystore reports it does not hold, and never while Key
     Vault is on (fallback mode included). A stored value always wins over
     an env var of the same name, so a planted .env line cannot replace it
     (KV-1), and there is no switch that restores the old env-first order:
     any switch would be read from the same .env (review R1). A keystore
     read that fails, rather than finding nothing, returns None and does
     not fall through to the environment.

An install that keeps everything in .env still works, but every lookup asks
the keystore first. Moving a secret out of .env and into the keystore is
what removes the plaintext from disk. Whoever can edit .env can still switch
Key Vault off (KEYVAULT_URL lives there); see docs/Key-Vault.md.

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
    All four read the Key Vault settings from ~/dev/secrets/.env, as the
    jobs do. With the vault on, `get` reads the vault; `set` refuses (exit
    3) for a secret the vault holds, since no job would read the Keychain
    copy; `rm` removes only a Keychain copy. Only the secrets `status`
    lists are in the vault; every other name stays in the keystore.
  python3 secret_store.py status [NAME...]
                                       where each secret resolves from
                                       (keyvault, keystore, env); never
                                       prints a value. Exit 1 if any is
                                       missing.

Azure Key Vault (macOS, opt-in): set KEYVAULT_URL and its companions in
~/dev/secrets/.env and step 1 becomes the vault. See the Key Vault section
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
# answer. Bound it and report failure (a failed read returns nothing; it does
# not fall back to .env, see KV-1 below) — and never retry, because every retry queues another dialog onto a stack that
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


class _ReadFailed:
    """The keystore could not answer (a timeout, a locked Keychain, a denied
    ACL, an undecryptable file). Distinct from None, "it holds no such
    secret": only that may fall through to the environment (review R4)."""

    def __repr__(self) -> str:
        return "READ_FAILED"


READ_FAILED = _ReadFailed()

# `security find-generic-password` exits 44 for "no such item".
_KEYCHAIN_NOT_FOUND = 44


def _keychain_get(name: str):
    """The stored value, None when the Keychain holds no such item, or
    READ_FAILED when the read itself failed."""
    p = _security("find-generic-password",
                  "-a", _account(), "-s", _service(name), "-w")
    if p is None:
        return READ_FAILED
    if p.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    if p.returncode != 0:
        print(f"secret_store: {name}: the Keychain read failed (exit "
              f"{p.returncode})", file=sys.stderr)
        return READ_FAILED
    value = p.stdout.rstrip("\n")
    return value if value else None


# F09 (v2.6 assessment): writes used to pass the value as `-w <value>`, and
# another local account can read any process's arguments with `ps` on macOS.
# The add now goes to `security -i` on stdin, through the line builder shared
# with security_common (the limits and refusals are described there). What
# has been proven live, on the maintainer's login Keychain: read-only lookups
# of 100 to 9,000 characters (2026-11-06: up to 4,000 answer normally in
# ~0.02 s with no hang; longer lines split); and full write, read-back and
# delete round-trips of 40- and 3,400-character values drawn from
# [A-Za-z0-9-_+/=] (2026-11-06); and on 2026-11-10 round-trips of values
# holding a space, ', #, $, `, and the mixed set ;&|<>*?~!%^(){}[]=+,. --
# all read back byte for byte -- plus security_common's HMAC-key create and
# re-read. Every write is read back and compared before it counts, so a value
# the tool parses differently fails closed.
# Review S9: guarded, so this file beside an older security_common still
# imports and reads; only a write refuses. Deploy the two files together.
SECURITY_I_MAX_LINE = getattr(security_common, "SECURITY_I_MAX_LINE", 4000)


def _security_i_line(*args: str) -> str | None:
    builder = getattr(security_common, "security_i_line", None)
    if builder is None:
        print("secret_store: security_common.py is older than secret_store.py "
              "(no security_i_line); deploy both files together. Not writing.",
              file=sys.stderr)
        return None
    return builder(*args)


def _security_stdin(verb: str, line: str) -> subprocess.CompletedProcess | None:
    """Run one command through `/usr/bin/security -i` on stdin, under the
    same hard timeout. Nothing secret is on the command line."""
    try:
        return subprocess.run(
            ["/usr/bin/security", "-i"], input=line + "\n",
            check=False, capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"secret_store: /usr/bin/security -i {verb} timed out after "
              f"{KEYCHAIN_TIMEOUT_SECONDS}s; Keychain appears wedged behind a "
              "consent prompt. Not retrying.", file=sys.stderr)
        return None


def _keychain_set(name: str, value: str) -> bool:
    # Hard-won constraints (verified live 2026-08-18 and 2026-11-06):
    #   1. The value travels on stdin to `security -i`, never in argv (F09,
    #      see SECURITY_I_MAX_LINE above for the limits that were probed).
    #   2. Delete-then-add, NOT `-U` — updating an existing item raises a
    #      GUI consent prompt whenever the item's ACL doesn't already cover
    #      the caller, and under launchd (or any unattended run) that prompt
    #      can never be answered: the call wedges silently. A fresh add
    #      never prompts. Deleting first loses nothing — we are about to
    #      write the authoritative value.
    #   3. Every call is bounded by KEYCHAIN_TIMEOUT_SECONDS. If the delete
    #      hangs we stop there rather than issuing the add: a wedged Keychain
    #      answers nothing, and a second call only queues a second dialog.
    #   4. The write is read back; a value the Keychain does not return
    #      byte for byte is removed and the write reports failure.
    # -T pre-authorizes /usr/bin/security itself so later reads by launchd
    # jobs (which all shell out to it) need no consent.
    line = _security_i_line("add-generic-password", "-a", _account(),
                            "-s", _service(name), "-w", value,
                            "-T", "/usr/bin/security")
    if line is None:
        print(f"secret_store: {name}: value cannot be written safely "
              "through security -i (a quote, a backslash, a character outside "
              f"printable ASCII, or over {SECURITY_I_MAX_LINE} bytes); "
              "not stored",
              file=sys.stderr)
        return False
    if _security("delete-generic-password",
                 "-a", _account(), "-s", _service(name)) is None:
        return False
    p = _security_stdin("add-generic-password", line)
    if p is None or p.returncode != 0:
        return False
    if _keychain_get(name) != value:
        _keychain_delete(name)
        print(f"secret_store: {name}: the Keychain did not return the value "
              "that was written; removed it, not stored", file=sys.stderr)
        return False
    return True


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
    if blob is READ_FAILED:
        # Review S1: READ_FAILED is truthy, and b64decode() on it raised a
        # TypeError that escaped every caller.
        raise KeyVaultError(f"certificate key {cfg['key_name']} could not be "
                            "read from the Keychain")
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


def _dpapi_get(name: str):
    """The stored value, None when there is no file, or READ_FAILED when the
    file exists but cannot be read or decrypted."""
    path = _file_path(name, dpapi=True)
    if not path.exists():
        return None
    try:
        raw = security_common._dpapi_crypt(path.read_bytes(), protect=False)
        return raw.decode("utf-8") or None
    except Exception as e:
        print(f"secret_store: {name}: the DPAPI file could not be decrypted "
              f"({type(e).__name__})", file=sys.stderr)
        return READ_FAILED


def _dpapi_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=True)
    try:
        blob = security_common._dpapi_crypt(value.encode("utf-8"), protect=True)
        path.write_bytes(blob)
        security_common.restrict_file(path)
        return True
    except Exception:
        return False


def _plainfile_get(name: str):
    path = _file_path(name, dpapi=False)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except (OSError, UnicodeDecodeError):
        return READ_FAILED


def _plainfile_set(name: str, value: str) -> bool:
    path = _file_path(name, dpapi=False)
    try:
        path.write_text(value + "\n", encoding="utf-8")
        security_common.restrict_file(path)
        return True
    except OSError:
        return False


# ---------- public API --------------------------------------------------------

# KV-1 (v2.6 assessment): the environment used to win over every stored
# secret, so one planted line in .env silently replaced a Keychain or Key
# Vault value. Now the stored value wins, always. The environment still
# supplies a secret the keystore reports it does not hold, so an install that
# keeps everything in .env keeps working, but never while Key Vault is on.
# There is deliberately no override switch: it would be read from the same
# environment the planted .env fills (review R1).


def _vault_active() -> bool:
    return keyvault_enabled() and sys.platform == "darwin"


def _keystore_get(name: str):
    if sys.platform == "darwin":
        return _keychain_get(name)
    if sys.platform == "win32":
        return _dpapi_get(name)
    return _plainfile_get(name)


def get_secret(name: str, *, use_env: bool = True) -> str | None:
    """Resolve a secret by NAME. Azure Key Vault when KEYVAULT_URL is set
    (macOS), else the platform keystore; the environment only when the
    keystore reports it holds no such secret, and never while the vault is
    on. Returns None when nowhere has it. A vault failure is reported on
    stderr by cause and returns None, unless KEYVAULT_KEYCHAIN_FALLBACK sends
    it on to the Keychain; a keystore read that fails returns None too.
    use_env=False never consults the environment."""
    env = os.environ.get(name, "").strip() if use_env else ""
    if keyvault_enabled() and sys.platform != "darwin":
        print("secret_store: the Key Vault backend is macOS-only; "
              "KEYVAULT_URL ignored", file=sys.stderr)
    vault = _vault_active()
    vault_failed = False
    if vault:
        try:
            value = _keyvault_get(name)
        except KeyVaultError as e:
            print(f"secret_store: {name}: {e}", file=sys.stderr)
            value = None
            vault_failed = True
        else:
            if value is not None:
                _warn_env_ignored(name, env, value)
                _last_source[name] = "keyvault"
                return value
            print(f"secret_store: {name}: not in Key Vault", file=sys.stderr)
        if not _keyvault_fallback():
            _note_env_unused(name, env)
            # T1: a vault that could not be asked (off VPN, firewall, sign-in,
            # an unreadable certificate key) is not a missing secret, and
            # storing it again is never the fix.
            _last_source[name] = "vault-error" if vault_failed else "missing"
            return None
        print(f"secret_store: {name}: falling back to the Keychain "
              "(KEYVAULT_KEYCHAIN_FALLBACK)", file=sys.stderr)
    value = _keystore_get(name)
    if value is READ_FAILED:
        print(f"secret_store: {name}: the keystore could not be read; not "
              "falling back to the environment", file=sys.stderr)
        _last_source[name] = "unavailable"
        return None
    if value is not None:
        _warn_env_ignored(name, env, value)
        _last_source[name] = "keystore"
        return value
    if env and not vault:
        _last_source[name] = "env"
        return env
    if vault:
        # Fallback mode reaches here: the vault and the Keychain both lack
        # it. The environment is still not a source while the vault is on.
        # A vault that could not be asked stays the reason (review B8).
        _note_env_unused(name, env)
        _last_source[name] = "vault-error" if vault_failed else "missing"
        return None
    _last_source[name] = "missing"
    return None


def _note_env_unused(name: str, env: str) -> None:
    if env:
        print(f"secret_store: {name}: an environment copy exists but is not "
              "used while Key Vault is on", file=sys.stderr)


def _warn_env_ignored(name: str, env: str, stored: str) -> None:
    """Say so (by name, never value) when an environment copy differs from
    the stored value it no longer overrides."""
    if env and env != stored:
        print(f"secret_store: {name}: ignoring a different value in the "
              "environment; the stored value wins", file=sys.stderr)


def last_source(name: str) -> str | None:
    """Where the last get_secret(name) in this process found it: env,
    keyvault, keystore, unavailable (the keystore read failed), vault-error
    (Key Vault could not be asked) or missing. A name, never a value."""
    return _last_source.get(name)


def not_found_hint(name: str) -> str:
    """What to tell an operator when get_secret(name) returned None: store it,
    or, when the keystore read itself failed, do NOT store it again (review
    S2: re-writing to a Keychain that just failed is the one thing never to
    do after a hang)."""
    if last_source(name) == "unavailable":
        return (f"the keystore could not be read, so {name} may well be "
                "stored; do not store it again. Fix the keystore (the "
                "secret_store line above says why) and re-run")
    if last_source(name) == "vault-error":
        return (f"Key Vault could not be asked, so {name} may well be "
                "stored there; nothing to store. The secret_store line above "
                "says why (off VPN, the vault firewall, or the sign-in)")
    if _vault_held(name):
        # F-20261125: `set` writes only the Keychain, which no job reads
        # while the vault is on, so it is the wrong advice here.
        try:
            vname = _vault_name(name)
        except KeyVaultError:
            vname = name
        return (f"add it to Key Vault as {vname} (docs/Key-Vault.md shows "
                "how); the Keychain is not read while the vault is on")
    return f"store it with: python3 secret_store.py set {name}"


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


# Configuration the CLI takes from .env, never a secret value.
_CLI_CONFIG_KEYS = ("KEYVAULT_URL", "KEYVAULT_TENANT_ID", "KEYVAULT_CLIENT_ID",
                    "KEYVAULT_CERT_THUMBPRINT", "KEYVAULT_KEY_NAME",
                    "KEYVAULT_KEYCHAIN_FALLBACK", "LLM_API_KEY_NAME")

# The secrets the jobs read, which Key Vault holds when it is on. Every
# other name (the certificate key, the Open WebUI admin password, ...) stays
# in the keystore either way.
def _vault_names() -> tuple:
    return ((os.environ.get("LLM_API_KEY_NAME", "").strip()
             or "ANTHROPIC_API_KEY").upper(), *_STATUS_NAMES)


def _vault_held(name: str) -> bool:
    """True when Key Vault is on and NAME is one of the secrets it holds."""
    return _vault_active() and name.strip().upper() in _vault_names()


# `set` exits with this when Key Vault is on and the name lives there: the
# installer must not take it for a Keychain failure and fall back to .env.
EXIT_VAULT_REFUSED = 3


def _load_cli_config() -> dict:
    """Bring the Key Vault settings from .env into this process, as every
    scheduled job has them, and return the file's keys.

    F-20261125: only `status` did this. Run by hand from a plain shell,
    `get` then never asked the vault and reported a stored secret "not
    found", and `set` wrote a Keychain item no job reads. A setting already
    in the environment wins, as before."""
    file_keys = _env_file_keys(SECRETS_ENV)
    for key in _CLI_CONFIG_KEYS:
        if key in file_keys and key not in os.environ:
            os.environ[key] = file_keys[key]
    return file_keys


def _status(names: list[str]) -> int:
    """Report where each secret resolves from. Never prints a value.

    Exit 1 when any is missing from where it will be read: the keystore or
    vault, or (vault off) .env. A copy in .env next to a stored value is
    reported, since it is ignored (KV-1) and only invites confusion."""
    file_keys = _load_cli_config()
    if not names:
        names = [os.environ.get("LLM_API_KEY_NAME", "").strip()
                 or "ANTHROPIC_API_KEY", *_STATUS_NAMES]
    vault = _vault_active()
    print("backend:", "keyvault " + os.environ["KEYVAULT_URL"].strip()
          if vault else "platform keystore")
    if vault and _keyvault_fallback():
        print("note: KEYVAULT_KEYCHAIN_FALLBACK is on (parallel run only)")
    bad = 0
    for name in names:
        copy = ""
        if file_keys.get(name):
            copy = ".env"
        elif os.environ.get(name, "").strip():
            copy = "environment"
        found = get_secret(name, use_env=False) is not None
        if last_source(name) == "unavailable":
            print(f"{name}: UNAVAILABLE (the keystore could not be read; "
                  "jobs get nothing until it can)")
            bad += 1
        elif last_source(name) == "vault-error":
            print(f"{name}: UNAVAILABLE (Key Vault could not be asked; the "
                  "line above says why)")
            bad += 1
        elif found:
            print(f"{name}: {last_source(name)}"
                  + (f"  (a {copy} copy exists and is ignored; remove it)"
                     if copy else ""))
        elif copy and not vault:
            print(f"{name}: {copy} (not in the keystore)")
        elif copy:
            print(f"{name}: MISSING from Key Vault (a {copy} copy exists "
                  "but is not used while the vault is on)")
            bad += 1
        else:
            print(f"{name}: MISSING")
            bad += 1
    return 1 if bad else 0


def _cli(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        return _status(argv[1:])
    if len(argv) != 2 or argv[0] not in ("set", "get", "rm"):
        print(__doc__.split("CLI", 1)[1], file=sys.stderr)
        return 2
    op, name = argv
    _load_cli_config()
    # The vault holds this secret: the Keychain copy is not what jobs read.
    in_vault = _vault_held(name)
    if op == "set":
        if in_vault and not _keyvault_fallback():
            # Refused before any prompt, so no value is read for nothing.
            print(f"secret_store: {name}: not stored. Key Vault is on and "
                  "the jobs read it from the vault, never the Keychain; load "
                  "the new value into Key Vault instead (docs/Key-Vault.md)",
                  file=sys.stderr)
            return EXIT_VAULT_REFUSED
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
        if ok and in_vault:
            print(f"secret_store: {name}: this is the Keychain fallback copy "
                  "(KEYVAULT_KEYCHAIN_FALLBACK); jobs read Key Vault first",
                  file=sys.stderr)
        return 0 if ok else 1
    if op == "get":
        if _vault_active() and not in_vault:
            # Vault on, but not a vault secret: the keystore, which is where
            # `set` puts it. get_secret would ask only the vault.
            value = _keystore_get(name)
            if value is READ_FAILED:
                print(f"secret_store: {name}: the keystore could not be read",
                      file=sys.stderr)
                return 1
        else:
            value = get_secret(name)
        if value is None:
            print(f"{name}: not found; {not_found_hint(name)}", file=sys.stderr)
            return 1
        print(value)
        return 0
    ok = delete_secret(name)
    print("removed" if ok else "nothing removed", name, file=sys.stderr)
    if in_vault:
        print(f"secret_store: {name}: only a Keychain copy is ever removed "
              "here; the Key Vault secret is unchanged", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
