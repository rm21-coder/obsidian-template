#!/usr/bin/env python3
"""
url_safety.py — SSRF-resistant URL validation and fetching.

Extracted from the article clipper (since retired) so every pipeline that fetches a URL shares one
implementation. That matters more than tidiness: the guard is subtle (DNS
rebinding, redirect re-validation, body caps), and a second copy would drift
from the tested one silently. Any new fetcher should route through here.

The threat model is a URL the operator did not personally choose. That is now
the normal case, not the exotic one: URLs arrive from RSS enclosures, from
iPad/iPhone Shortcut drops, and from the mail-drop transport. A URL from any of
those can point at loopback, link-local metadata endpoints (169.254.169.254),
or RFC1918 hosts — including this vault's own local services, e.g. the Ollama /
Open WebUI RAG stack.

Three entry points:

  is_safe_url(url)                  -> (ok, reason)
  safe_fetch(url, ...)              -> bytes | None   in-memory, small cap
  safe_download(url, dest, ...)     -> bool           streamed to disk, big cap

Why both fetch and download: an article is capped at 10 MB and held in memory,
while a podcast episode is routinely 50-150 MB and must be streamed to a file.
Sharing one cap would either truncate episodes or let a hostile endpoint
balloon a clipper's memory.

Never use urllib.request.urlopen or requests with allow_redirects=True on an
untrusted URL. Both follow redirects internally, so a first-hop check passes
and the redirect lands wherever the attacker likes. Every function here walks
redirects manually and re-validates each hop.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

# Hostnames and TLDs that can only mean "somewhere on this machine or LAN".
DISALLOWED_TLDS = (".local", ".internal", ".lan", ".intranet", ".corp",
                   ".home", ".localdomain")
LOOPBACK_NAMES = ("localhost", "ip6-localhost", "broadcasthost",
                  "ip6-loopback")

# safe_fetch tunables. MAX_REDIRECTS bounds the manual redirect walker;
# MAX_BODY_BYTES caps per-response memory so a malicious or runaway
# endpoint can't exhaust the process. 10 MB is well above any normal
# news article and below memory-pressure territory for a scheduled job.
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 10 * 1024 * 1024
SAFE_FETCH_TIMEOUT_SECONDS = 30
SAFE_FETCH_CHUNK_SIZE = 64 * 1024

# Streaming downloads are for media, so the ceiling is much higher; still
# bounded, so a hostile or misconfigured endpoint can't fill the disk.
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
SAFE_DOWNLOAD_TIMEOUT_SECONDS = 120

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

Logger = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative: rejects any URL that could
    plausibly target an internal resource.

    Checks, in order:
      1. URL parses cleanly.
      2. Scheme is http or https (no file://, gopher://, javascript:, etc.)
      3. Hostname is non-empty.
      4. Hostname is not a known loopback alias.
      5. Hostname does not end in a disallowed local TLD.
      6. If the hostname is an IP literal, it is not loopback / private /
         link-local / multicast / reserved.
      7. Otherwise resolve the hostname via socket.getaddrinfo and reject
         if ANY resolved IP is internal (DNS-rebinding defense). A
         resolution failure is treated as unsafe — we can't prove the
         target is public, so we refuse.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"disallowed scheme: {parsed.scheme!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "empty hostname"
    if host in LOOPBACK_NAMES:
        return False, f"loopback hostname: {host}"
    for tld in DISALLOWED_TLDS:
        if host.endswith(tld):
            return False, f"disallowed local TLD: {host}"
    # IP literal path: accept public IPs, reject internal IPs.
    try:
        ip = ipaddress.ip_address(host)
        if _ip_is_internal(ip):
            return False, f"disallowed IP literal: {ip}"
        return True, ""
    except ValueError:
        pass  # Not an IP literal — fall through to DNS resolution.
    # Hostname path: resolve and reject if ANY answer is internal.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for {host!r}: {e}"
    if not infos:
        return False, f"DNS resolution returned no records for {host!r}"
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(ip_str)
        except ValueError:
            # Unparseable sockaddr — treat as suspicious and refuse.
            return False, (f"DNS returned unparseable address {ip_str!r} "
                           f"for {host!r}")
        if _ip_is_internal(resolved):
            return False, (f"{host} resolves to internal address "
                           f"{ip_str}")
    return True, ""


def _walk(url: str, *, log: Logger, timeout: int):
    """Yield validated responses along a redirect chain.

    Each hop is re-validated by is_safe_url BEFORE its request issues, and
    allow_redirects=False is set so requests can never follow one for us.
    Yields (response, current_url) for the first non-redirect hop, or nothing
    if the chain is refused, errors, or runs too long.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ok, reason = is_safe_url(current)
        if not ok:
            log(f"refusing {current}: {reason}")
            return
        try:
            resp = requests.get(
                current,
                allow_redirects=False,
                stream=True,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            log(f"transport error on {current}: {e}")
            return
        status = resp.status_code
        if status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                log(f"redirect with no Location header: {current}")
                return
            # urljoin handles both absolute and relative redirects.
            current = urljoin(current, location)
            continue
        if not (200 <= status < 300):
            log(f"non-2xx ({status}) for {current}")
            resp.close()
            return
        yield resp, current
        return
    log(f"redirect chain longer than {MAX_REDIRECTS} hops from {url}")


def safe_fetch(url: str, *, log: Logger | None = None,
               max_bytes: int = MAX_BODY_BYTES) -> bytes | None:
    """Fetch a URL body into memory with SSRF guards and a size cap.

    Returns the raw body on a clean 2xx, or None on any refusal, transport
    error, non-2xx, malformed redirect, over-long chain, or oversize body.
    """
    log = log or _noop
    for resp, current in _walk(url, log=log, timeout=SAFE_FETCH_TIMEOUT_SECONDS):
        try:
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=SAFE_FETCH_CHUNK_SIZE):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    log(f"body exceeded {max_bytes} bytes on {current}")
                    return None
            return bytes(buf)
        finally:
            resp.close()
    return None


def safe_download(url: str, dest: Path, *, log: Logger | None = None,
                  max_bytes: int = MAX_DOWNLOAD_BYTES) -> bool:
    """Stream a URL to a file with SSRF guards and a size cap.

    Writes to a .part sibling and renames on success, so a partial download
    is never mistaken for a complete file by a later cache check. Returns
    True on success.
    """
    log = log or _noop
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for resp, current in _walk(url, log=log,
                               timeout=SAFE_DOWNLOAD_TIMEOUT_SECONDS):
        try:
            total = 0
            with open(tmp, "wb") as out:
                for chunk in resp.iter_content(chunk_size=SAFE_FETCH_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        log(f"download exceeded {max_bytes} bytes on {current}")
                        out.close()
                        tmp.unlink(missing_ok=True)
                        return False
                    out.write(chunk)
            tmp.replace(dest)
            log(f"downloaded {total:,} bytes -> {dest}")
            return True
        except OSError as e:
            log(f"write failed for {dest}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        finally:
            resp.close()
    tmp.unlink(missing_ok=True)
    return False
