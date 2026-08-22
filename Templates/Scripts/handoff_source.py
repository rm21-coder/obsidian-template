#!/usr/bin/env python3
"""handoff_source.py — pluggable handoff ingestion for the meeting
pre-population pipeline (and any consumer that speaks the Handoff Contract).

This module separates the *contract* (a schema-versioned, integrity-checked,
optionally-signed payload plus a fetch/ack interface) from the *transport*
(how the bytes actually arrive). A consumer depends only on the HandoffSource
interface, so the exact same downstream code runs against:

  - a relay / SFTP / manually-imported file drop today, and
  - a tenant MCP server tomorrow,

without any change to the consumer's business logic. See
docs/HANDOFF-ARCHITECTURE.md for the full contract and security model.

Design intent (why this exists): a cloud-drive sync client as the producer's
delivery mechanism is fragile in practice (sync-timing races, conflict
copies, online-only placeholders). Pinning the consumer to the *contract*
rather than to any one producer's delivery mechanism means the producer can
be swapped for an MCP server, a Graph pull, or a Power Automate flow with a
config change, not a rewrite — and the same package can be distributed to
other CIOs whose backends differ.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("handoff_source")

# The contract this module understands. Bump when the payload shape changes;
# a consumer advertises the versions it supports and rejects the rest.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Minimum top-level keys every schema-v1 payload must carry.
REQUIRED_TOP_LEVEL = (
    "schema_version", "source", "generated_at",
    "user", "week", "meetings", "contacts",
)


class HandoffError(Exception):
    """Raised when a handoff cannot be fetched, verified, or validated.

    Callers treat this as 'skip this handoff and keep going', never as a crash.
    """


@dataclass
class HandoffRecord:
    """A fetched, verified handoff ready for the consumer to process."""
    id: str                       # stable handoff name (e.g. the file stem)
    payload: dict                 # the parsed, schema-validated contract JSON
    handle: Any                   # opaque token handed back to source.ack()
    source: str = ""              # human-readable transport description
    raw_bytes: bytes | None = None


# ---------------------------------------------------------------------------
# Contract verification helpers (transport-agnostic; reused by every source)
# ---------------------------------------------------------------------------

def validate_schema(payload: dict,
                    supported: frozenset = SUPPORTED_SCHEMA_VERSIONS) -> None:
    """Raise HandoffError unless the payload matches a supported schema."""
    sv = payload.get("schema_version")
    if sv not in supported:
        raise HandoffError(
            f"unsupported schema_version={sv!r} (supported={sorted(supported)})")
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in payload]
    if missing:
        raise HandoffError(f"missing required top-level keys: {missing}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_sha256(data: bytes, expected_hex: str | None) -> None:
    """Integrity: bytes match the producer's checksum. No sidecar => skipped.

    A checksum proves the bytes were not corrupted in transit; it does NOT
    prove who produced them (see verify_signature for authenticity).
    """
    if not expected_hex:
        log.info("no sha256 provided — integrity check skipped")
        return
    actual = sha256_hex(data)
    if not hmac.compare_digest(actual, expected_hex.strip().lower()):
        raise HandoffError(
            f"sha256 mismatch: expected={expected_hex.strip()[:12]}… "
            f"actual={actual[:12]}…")


def verify_signature(data: bytes, signature_hex: str | None,
                     key: bytes | None, *, required: bool) -> None:
    """Authenticity: bytes were signed by a producer that holds the shared key.

    Same HMAC-envelope pattern used on the security allowlist. Modes:
      key is None, required False -> skip (authenticity not enforced)
      key is None, required True  -> configuration error (fail closed)
      key set                     -> a valid signature MUST be present
    Using hmac.compare_digest keeps the check constant-time.
    """
    if key is None:
        if required:
            raise HandoffError(
                "signature required but no HMAC key configured "
                "(set HANDOFF_HMAC_KEY or HANDOFF_HMAC_KEY_FILE)")
        return
    if not signature_hex:
        raise HandoffError("signature required but none supplied with handoff")
    expected = hmac.new(key, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_hex.strip().lower()):
        raise HandoffError(
            "HMAC signature mismatch — payload is not from a trusted producer")


def load_hmac_key() -> bytes | None:
    """Resolve the producer-authenticity key. Prefer a file over an inline env
    value so the secret is not exposed in the process environment table."""
    p = os.environ.get("HANDOFF_HMAC_KEY_FILE")
    if p:
        pth = Path(p).expanduser()
        if pth.is_file():
            return pth.read_bytes().strip()
        log.warning("HANDOFF_HMAC_KEY_FILE set but not readable: %s", pth)
    v = os.environ.get("HANDOFF_HMAC_KEY")
    return v.encode("utf-8") if v else None


# ---------------------------------------------------------------------------
# The interface every transport implements
# ---------------------------------------------------------------------------

class HandoffSource(ABC):
    """A pull-based handoff transport. discover() lists what is pending,
    load() returns a fully-verified record, ack() marks it consumed."""

    @abstractmethod
    def discover(self) -> list:
        """Return a list of opaque handles for pending handoffs (may be empty)."""

    @abstractmethod
    def load(self, handle) -> HandoffRecord:
        """Fetch + verify (integrity, signature, schema) one handoff.
        Raise HandoffError on any failure so the caller can skip it."""

    @abstractmethod
    def ack(self, handle, dry_run: bool = False) -> None:
        """Mark a handoff consumed (drop: archive; server: ack call)."""

    def describe(self) -> str:
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# Transport A — signed file drop (relay / synced folder / manual import)
# ---------------------------------------------------------------------------

class DropFolderSource(HandoffSource):
    """Generic drop-folder transport. Per handoff it expects:

        <name>.json          the payload (pretty-printed JSON)
        <name>.json.sha256   integrity (sha256sum format: hex, 2 spaces, name)
        <name>.sig           OPTIONAL producer HMAC-SHA256 over the json bytes
        <name>.ready         0-byte completion marker, written LAST by producer

    The .ready marker is the commit signal: a payload without it is treated as
    still-arriving and ignored. ack() archives the set into _processed/.

    No cloud client is required — point it at any folder a relay populates
    (rsync/SFTP pull, object-store sync, or a human dropping a signed file).
    """

    READY_SUFFIX = ".ready"
    PROCESSED_SUBDIR = "_processed"

    def __init__(self, folder, *, hmac_key: bytes | None = None,
                 require_signature: bool = False,
                 supported: frozenset = SUPPORTED_SCHEMA_VERSIONS) -> None:
        self.folder = Path(folder).expanduser()
        self.hmac_key = hmac_key
        self.require_signature = require_signature
        self.supported = supported

    def describe(self) -> str:
        return f"DropFolderSource({self.folder})"

    def discover(self) -> list:
        if not self.folder.is_dir():
            raise HandoffError(f"drop folder does not exist: {self.folder}")
        out = []
        for p in sorted(self.folder.glob("*" + self.READY_SUFFIX)):
            if "conflict" in p.stem.lower():   # sync-tool conflict copies
                log.warning("ignoring conflict file: %s", p.name)
                continue
            out.append(p)
        log.info("discovered %d ready handoff(s) in %s", len(out), self.folder)
        return out

    def load(self, ready_path: Path) -> HandoffRecord:
        json_path = ready_path.with_suffix(".json")
        if not json_path.is_file():
            raise HandoffError(f"missing payload for {ready_path.name}")
        data = json_path.read_bytes()

        # 1) integrity
        sha_path = json_path.with_suffix(".json.sha256")
        expected = None
        if sha_path.is_file():
            try:
                expected = sha_path.read_text(encoding="utf-8").split()[0]
            except (OSError, IndexError):
                log.warning("sha256 sidecar unreadable: %s", sha_path.name)
        verify_sha256(data, expected)

        # 2) authenticity (optional)
        sig_path = ready_path.with_suffix(".sig")
        sig = (sig_path.read_text(encoding="utf-8").strip()
               if sig_path.is_file() else None)
        verify_signature(data, sig, self.hmac_key,
                         required=self.require_signature)

        # 3) schema
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HandoffError(f"payload is not valid JSON: {e}")
        validate_schema(payload, self.supported)

        return HandoffRecord(id=ready_path.stem, payload=payload,
                             handle=ready_path, source=self.describe(),
                             raw_bytes=data)

    def ack(self, ready_path: Path, dry_run: bool = False) -> None:
        if dry_run:
            log.info("DRY-RUN: would archive %s set to %s/",
                     ready_path.name, self.PROCESSED_SUBDIR)
            return
        processed = ready_path.parent / self.PROCESSED_SUBDIR
        processed.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        json_path = ready_path.with_suffix(".json")
        for f in (json_path, ready_path,
                  json_path.with_suffix(".json.sha256"),
                  ready_path.with_suffix(".sig")):
            if f.exists():
                dst = processed / f"{f.stem}.{stamp}{f.suffix}"
                shutil.move(str(f), str(dst))
                log.info("archived %s -> %s", f.name, dst.name)


# ---------------------------------------------------------------------------
# Transport B — tenant MCP server (the endgame; stub until the endpoint exists)
# ---------------------------------------------------------------------------

class MCPSource(HandoffSource):
    """Pull the handoff from a purpose-built MCP server inside (or adjacent to)
    the tenant. The server performs the privileged M365 read and exposes it as
    scoped tools; the endpoint holds only a revocable token to the SERVER —
    never tenant credentials, never a sync client. This is the target state
    once the tenant's MCP option is enabled.

    Suggested server-side tool contract:
        handoff.list_pending()  -> [{"id": "<name>"}]
        handoff.fetch(id)       -> {"payload": {...},
                                    "sha256": "<hex>",   # over canonical bytes
                                    "sig": "<hmac hex>"} # optional
        handoff.ack(id)         -> {}

    This class is intentionally a stub: it validates its own configuration and
    raises a clear, actionable error until the three calls below are wired to
    your MCP client. The verification path (sha256 + signature + schema) is
    already written so wiring it is just the transport calls.
    """

    def __init__(self, endpoint: str, token: str, *,
                 hmac_key: bytes | None = None,
                 require_signature: bool = True,
                 supported: frozenset = SUPPORTED_SCHEMA_VERSIONS) -> None:
        self.endpoint = endpoint
        self.token = token
        self.hmac_key = hmac_key
        self.require_signature = require_signature
        self.supported = supported

    def describe(self) -> str:
        return f"MCPSource({self.endpoint or '<unconfigured>'})"

    def _stub(self) -> None:
        raise HandoffError(
            "MCPSource is a stub — wire discover()/load()/ack() to your tenant "
            "MCP endpoint. See docs/HANDOFF-ARCHITECTURE.md (Transport C).")

    def discover(self) -> list:
        if not self.endpoint or not self.token:
            raise HandoffError(
                "MCPSource requires HANDOFF_MCP_ENDPOINT and HANDOFF_MCP_TOKEN")
        self._stub()

    def load(self, handle) -> HandoffRecord:
        # Reference implementation once your MCP client is available:
        #   resp = mcp_call(self.endpoint, self.token, "handoff.fetch",
        #                   {"id": handle})
        #   data = json.dumps(resp["payload"],
        #                     separators=(",", ":"), sort_keys=True).encode()
        #   verify_sha256(data, resp.get("sha256"))
        #   verify_signature(data, resp.get("sig"), self.hmac_key,
        #                    required=self.require_signature)
        #   payload = resp["payload"]
        #   validate_schema(payload, self.supported)
        #   return HandoffRecord(id=handle, payload=payload, handle=handle,
        #                        source=self.describe(), raw_bytes=data)
        self._stub()

    def ack(self, handle, dry_run: bool = False) -> None:
        # When implemented: mcp_call(self.endpoint, self.token,
        #                            "handoff.ack", {"id": handle})
        self._stub()
