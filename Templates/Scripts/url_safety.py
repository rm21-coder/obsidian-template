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

Each hop also CONNECTS to the address that was validated. Checking a name and
then letting requests resolve it again is a time-of-check/time-of-use gap: a
hostile name with a zero TTL answers a public address to the check and
127.0.0.1 to the connect (DNS rebinding). _pinned_get resolves once, through
_check, and hands urllib3 that address for the TCP connect while the URL, the
Host header, TLS SNI and certificate verification all keep the original name.
Environment proxies are ignored for the same reason: a proxy resolves the name
itself (see _pinned_get).

Every fetch also has a wall-clock deadline, because the requests timeout is per
socket operation and a server dripping one byte at a time never trips it.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
import urllib3.exceptions
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

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
# Whole-fetch wall-clock budgets, redirects and body included.
SAFE_FETCH_DEADLINE_SECONDS = 60
SAFE_DOWNLOAD_DEADLINE_SECONDS = 30 * 60   # a 150 MB episode at ~0.7 Mbit/s

# Streaming downloads are for media, so the ceiling is much higher; still
# bounded, so a hostile or misconfigured endpoint can't fill the disk.
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024


_USERINFO_IN_PATH = re.compile(r"[^/]*@")


def redact_url(url: str) -> str:
    """A URL made safe to log: userinfo dropped, query string elided.

    Every refusal and error in this module used to log the full URL. A URL can
    carry credentials in its userinfo (https://user:pass@host/) and bearer-like
    material in its query (signed CDN links, SAS tokens), so a log written for
    diagnosis became a place secrets accumulate. Scheme, host and path survive
    because they are what diagnosis actually needs. Microsoft M-DASH 2026-09-23
    (CWE-532) flagged two of these call sites; all of them had it.
    """
    if not isinstance(url, str):
        return "<unparseable url>"
    try:
        parts = urlparse(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        # A bad port raises from .port, not from urlparse -- outside the old
        # try, so a hostile redirect Location crashed the refusal log itself.
        return "<unparseable url>"
    if port:
        host = f"{host}:{port}"
    # Malformed forms put the userinfo in the PATH: "http:user:pass@host/p"
    # and "http:///user:pass@host/p" both parse with an empty netloc. Drop
    # anything before an "@" in any segment, wherever it landed.
    path = _USERINFO_IN_PATH.sub("<redacted>@", parts.path)
    query = "?…" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{query}"
SAFE_DOWNLOAD_TIMEOUT_SECONDS = 120

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

Logger = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


# Ranges Python's ipaddress still calls global, but which are not a public
# web server: the 6to4 relay anycast block (RFC 7526, deprecated) is routed to
# whatever relay is nearest. fec0::/10 (site-local, deprecated) is caught by
# is_site_local below.
_EXTRA_INTERNAL = (ipaddress.ip_network("192.88.99.0/24"),)


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not a public unicast address.

    `not ip.is_global` is the base test: it catches the ranges the explicit
    flags missed, e.g. 100.64.0.0/10 (carrier-grade NAT, and Tailscale's
    tailnet addresses) and IPv4-mapped forms of private addresses. The
    explicit flags stay because is_global alone admits some multicast.
    """
    return (not ip.is_global
            or ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or getattr(ip, "is_site_local", False)
            or any(ip in net for net in _EXTRA_INTERNAL))


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative: rejects any URL that could
    plausibly target an internal resource.

    Checks, in order:
      1. URL is a string and parses cleanly.
      2. Scheme is http or https (no file://, gopher://, javascript:, etc.)
      3. Hostname is non-empty.
      4. Hostname is not a known loopback alias.
      5. Hostname does not end in a disallowed local TLD.
      6. If the hostname is an IP literal, it is a public unicast address
         (see _ip_is_internal).
      7. Otherwise resolve the hostname via socket.getaddrinfo and reject
         if ANY resolved IP is internal. A resolution failure (including a
         name the resolver cannot even encode) is treated as unsafe -- we
         can't prove the target is public, so we refuse.

    On its own this is a point-in-time verdict about a name: a later,
    independent lookup can answer differently (DNS rebinding). The fetchers
    in this module close that by connecting to the very address this check
    approved (see _pinned_get); a caller that only calls is_safe_url and then
    fetches by itself does not get that protection.
    """
    ok, reason, _ip = _check(url)
    return ok, reason


def _check(url: str) -> tuple[bool, str, str | None]:
    """is_safe_url plus the address to connect to when it passes.

    The address returned is one the checks above approved, so a fetcher that
    connects to it cannot be walked to a different answer by a second lookup.
    Never raises: anything it cannot evaluate is a refusal.
    """
    if not isinstance(url, str):
        return False, f"URL is not a string: {type(url).__name__}", None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False, "unparseable URL", None
    if parsed.scheme not in ("http", "https"):
        return False, f"disallowed scheme: {parsed.scheme!r}", None
    if not host:
        return False, "empty hostname", None
    if host in LOOPBACK_NAMES:
        return False, f"loopback hostname: {host}", None
    for tld in DISALLOWED_TLDS:
        if host.endswith(tld):
            return False, f"disallowed local TLD: {host}", None
    # IP literal path: accept public IPs, reject internal IPs.
    try:
        ip = ipaddress.ip_address(host)
        if _ip_is_internal(ip):
            return False, f"disallowed IP literal: {ip}", None
        return True, "", str(ip)
    except ValueError:
        pass  # Not an IP literal — fall through to DNS resolution.
    # Hostname path: resolve and reject if ANY answer is internal.
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError) as e:
        # gaierror is an OSError. UnicodeError: a label over 63 characters
        # fails IDNA encoding before any lookup -- a hostile redirect
        # Location can carry one, and it used to escape as an exception.
        return False, (f"DNS resolution failed for {host!r}: "
                       f"{type(e).__name__}"), None
    if not infos:
        return False, f"DNS resolution returned no records for {host!r}", None
    pinned: str | None = None
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(ip_str)
        except ValueError:
            # Unparseable sockaddr — treat as suspicious and refuse.
            return False, (f"DNS returned unparseable address {ip_str!r} "
                           f"for {host!r}"), None
        if _ip_is_internal(resolved):
            return False, (f"{host} resolves to internal address "
                           f"{ip_str}"), None
        if pinned is None:
            pinned = str(resolved)
    return True, "", pinned


def _pinned_classes(ip: str, opened: list | None = None):
    """Connection-pool classes whose TCP connect goes to `ip`.

    Only the socket's destination changes. urllib3 reads the connection's
    `host` for the Host header, TLS SNI and certificate hostname checks, so
    the name is swapped in for the duration of the connect alone and those
    keep the name the URL carried.

    Every socket is appended to `opened` so a deadline can shut it: the TCP
    socket when it connects, and the TLS socket once the handshake is done.
    Sockets, not connections: http.client drops conn.sock as soon as a
    response is marked will-close (any HTTP/1.0 reply), while the response
    keeps reading from the socket itself.
    """
    def _new_conn(self):
        name = self._dns_host
        self._dns_host = ip
        try:
            sock = self._base_new_conn()
        finally:
            self._dns_host = name
        if opened is not None:
            opened.append(sock)
        return sock

    def connect(self):
        self._base_connect()
        if opened is not None and self.sock is not None:
            opened.append(self.sock)

    def conn_cls(base):
        return type(f"Pinned{base.__name__}", (base,),
                    {"_new_conn": _new_conn, "_base_new_conn": base._new_conn,
                     "connect": connect, "_base_connect": base.connect})

    http_pool = type("PinnedHTTPConnectionPool", (HTTPConnectionPool,),
                     {"ConnectionCls": conn_cls(HTTPConnection)})
    https_pool = type("PinnedHTTPSConnectionPool", (HTTPSConnectionPool,),
                      {"ConnectionCls": conn_cls(HTTPSConnection)})
    return http_pool, https_pool


class _PinnedAdapter(HTTPAdapter):
    """A requests adapter that connects every request to one validated IP."""

    def __init__(self, ip: str, opened: list | None = None) -> None:
        self._pinned_ip = ip
        self._opened = opened
        super().__init__()

    def init_poolmanager(self, *args, **kwargs) -> None:
        super().init_poolmanager(*args, **kwargs)
        http_pool, https_pool = _pinned_classes(self._pinned_ip, self._opened)
        self.poolmanager.pool_classes_by_scheme = {
            "http": http_pool, "https": https_pool}


def _pinned_get(url: str, ip: str, *, timeout: int,
                opened: list | None = None) -> requests.Response:
    """GET one hop, connecting to `ip`, never following a redirect.

    trust_env is OFF. With it on, requests takes HTTP(S)_PROXY / ALL_PROXY
    (and macOS's system proxy settings) from the environment and sends the
    request to the proxy, which resolves the name again itself: pinning off,
    silently. A configured proxy is therefore ignored here, and on a network
    that only allows egress through one these fetches fail closed rather
    than fetch unpinned. The one environment setting kept is a CA bundle
    (REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE), which only adds trust roots for
    TLS inspection and cannot redirect a connection.
    """
    session = requests.Session()
    session.trust_env = False
    session.verify = (os.environ.get("REQUESTS_CA_BUNDLE")
                      or os.environ.get("CURL_CA_BUNDLE") or True)
    adapter = _PinnedAdapter(ip, opened)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        return session.get(
            url,
            allow_redirects=False,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            proxies={},
        )
    except BaseException:
        session.close()
        raise


class _Deadline:
    """Wall-clock budget for one hop: when it runs out, shut its sockets.

    The requests timeout is per socket operation, so a server that sends one
    byte just inside it -- a chunked drip, or a slow header -- keeps a read
    alive indefinitely. Checking the clock between chunks does not help
    either: one iter_content chunk blocks until 64 KB arrive. Shutting the
    socket from a timer is what actually interrupts a blocked read. The
    shutdown is repeated until cancelled, so a connection opened just after
    the first one (or still in its TLS handshake) is caught too.
    """

    def __init__(self, opened: list, seconds: float) -> None:
        self.expired = False
        self._opened = opened
        self._seconds = max(seconds, 0.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if self._stop.wait(self._seconds):
            return
        self.expired = True
        while True:
            for sock in list(self._opened):
                try:
                    # The base-class method: SSLSocket.shutdown would also
                    # tear down its SSL object under the reading thread. A
                    # TCP socket already detached into a TLS one raises
                    # here, and its TLS socket is in the list too.
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except (OSError, TypeError, ValueError):
                    pass
            if self._stop.wait(0.25):
                return

    def cancel(self) -> None:
        self._stop.set()


# What a request or a body read can raise once the peer misbehaves. Any of
# them is a failed fetch, never an exception out of this module.
_TRANSPORT_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError,
                     OSError, ValueError)


class _ReadFailed(Exception):
    pass


def _chunks(resp):
    """iter_content, with every transport failure turned into _ReadFailed so
    a caller can tell it apart from its own (e.g. disk write) errors."""
    it = resp.iter_content(chunk_size=SAFE_FETCH_CHUNK_SIZE)
    while True:
        try:
            chunk = next(it)
        except StopIteration:
            return
        except _TRANSPORT_ERRORS as e:
            raise _ReadFailed(type(e).__name__) from None
        if chunk:
            yield chunk


def _walk(url: str, *, log: Logger, timeout: int, end: float):
    """Yield validated responses along a redirect chain.

    Each hop is re-validated by _check BEFORE its request issues and the
    request connects to the address that check approved; allow_redirects=False
    is set so requests can never follow one for us. `end` is a
    time.monotonic() deadline for the whole chain, body included.
    Yields (response, current_url, deadline) for the first non-redirect hop,
    or nothing if the chain is refused, errors, runs too long or runs out of
    time. The deadline stays armed until the caller has finished reading.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ok, reason, ip = _check(current)
        if not ok or ip is None:
            log(f"refusing {redact_url(current)}: {reason}")
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            log(f"deadline exceeded before {redact_url(current)}")
            return
        opened: list = []
        deadline = _Deadline(opened, remaining)
        try:
            # Connect to the address _check approved, not a fresh lookup.
            resp = _pinned_get(current, ip, timeout=timeout, opened=opened)
        except _TRANSPORT_ERRORS as e:
            deadline.cancel()
            # The exception text is not logged: requests puts the full URL,
            # query string and all, into it ("Max retries exceeded with url:
            # /p?sig=..."), which undid the redaction on the line's own prefix.
            what = "deadline exceeded" if deadline.expired else "transport error"
            log(f"{what} on {redact_url(current)}: {type(e).__name__}")
            return
        status = resp.status_code
        if status in (301, 302, 303, 307, 308):
            deadline.cancel()
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                log(f"redirect with no Location header: {redact_url(current)}")
                return
            # urljoin handles both absolute and relative redirects.
            try:
                current = urljoin(current, location)
            except ValueError:
                log(f"unparseable redirect from {redact_url(current)}")
                return
            continue
        if not (200 <= status < 300):
            deadline.cancel()
            log(f"non-2xx ({status}) for {redact_url(current)}")
            resp.close()
            return
        try:
            yield resp, current, deadline
        finally:
            deadline.cancel()
        return
    log(f"redirect chain longer than {MAX_REDIRECTS} hops from {redact_url(url)}")


def safe_fetch(url: str, *, log: Logger | None = None,
               max_bytes: int = MAX_BODY_BYTES,
               deadline_seconds: float | None = None) -> bytes | None:
    """Fetch a URL body into memory with SSRF guards, a size cap and a
    wall-clock deadline (SAFE_FETCH_DEADLINE_SECONDS unless given).

    Returns the raw body on a clean 2xx, or None on any refusal, transport
    error, non-2xx, malformed redirect, over-long chain, oversize body, or
    deadline. Never raises for anything the remote end can do.
    """
    log = log or _noop
    if deadline_seconds is None:
        deadline_seconds = SAFE_FETCH_DEADLINE_SECONDS
    end = time.monotonic() + deadline_seconds
    for resp, current, deadline in _walk(url, log=log,
                                         timeout=SAFE_FETCH_TIMEOUT_SECONDS,
                                         end=end):
        try:
            buf = bytearray()
            for chunk in _chunks(resp):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    log(f"body exceeded {max_bytes} bytes on {redact_url(current)}")
                    return None
                if deadline.expired:
                    break
            if deadline.expired:
                # A read-until-close body ends "cleanly" when its socket is
                # shut, so a short body here is not a complete one.
                log(f"deadline exceeded reading {redact_url(current)}")
                return None
            return bytes(buf)
        except _ReadFailed as e:
            what = "deadline exceeded" if deadline.expired else "read error"
            log(f"{what} on {redact_url(current)}: {e}")
            return None
        finally:
            resp.close()
    return None


def safe_download(url: str, dest: Path, *, log: Logger | None = None,
                  max_bytes: int = MAX_DOWNLOAD_BYTES,
                  deadline_seconds: float | None = None) -> bool:
    """Stream a URL to a file with SSRF guards, a size cap and a wall-clock
    deadline (SAFE_DOWNLOAD_DEADLINE_SECONDS unless given).

    Writes to a .part sibling and renames on success, so a partial download
    is never mistaken for a complete file by a later cache check. Returns
    True on success.
    """
    log = log or _noop
    if deadline_seconds is None:
        deadline_seconds = SAFE_DOWNLOAD_DEADLINE_SECONDS
    end = time.monotonic() + deadline_seconds
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for resp, current, deadline in _walk(url, log=log,
                                         timeout=SAFE_DOWNLOAD_TIMEOUT_SECONDS,
                                         end=end):
        try:
            total = 0
            with open(tmp, "wb") as out:
                for chunk in _chunks(resp):
                    total += len(chunk)
                    if total > max_bytes:
                        log(f"download exceeded {max_bytes} bytes on {redact_url(current)}")
                        out.close()
                        tmp.unlink(missing_ok=True)
                        return False
                    out.write(chunk)
                    if deadline.expired:
                        break
            if deadline.expired:
                log(f"deadline exceeded downloading {redact_url(current)}")
                tmp.unlink(missing_ok=True)
                return False
            tmp.replace(dest)
            log(f"downloaded {total:,} bytes -> {dest}")
            return True
        except _ReadFailed as e:
            what = "deadline exceeded" if deadline.expired else "read error"
            log(f"{what} on {redact_url(current)}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        except OSError as e:
            log(f"write failed for {dest}: {e}")
            tmp.unlink(missing_ok=True)
            return False
        finally:
            resp.close()
    tmp.unlink(missing_ok=True)
    return False
