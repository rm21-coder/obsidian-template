"""test_url_safety_pinning.py -- the fetch connects to the address it checked.

M-DASH 212 (CWE-918): is_safe_url resolved a name and checked every answer,
then requests resolved it AGAIN at connect time. A hostile name with a zero TTL
answers a public address to the check and 127.0.0.1 to the connect, so the
"DNS-rebinding defense" covered only static DNS and a feed's enclosure URL
could reach the workstation's own services (Ollama, Open WebUI, a router).

These tests stand up a real HTTP server on 127.0.0.1 and a resolver that
rebinds: the first lookup of the name answers public, every later one answers
loopback. The fixed walker never performs the later lookup; it connects to the
first, checked answer. No test here leaves the machine: a connect to the
pinned public address is refused at the resolver stub before any socket opens.
"""
from __future__ import annotations

import http.server
import socket
import threading
import time

import pytest

import url_safety

PUBLIC = "93.184.216.34"
NAME = "rebind.test.example"


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        type(self).hits.append((self.path, self.headers.get("Host", "")))
        body = b"LOCAL-SERVICE-BODY"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def local_server(monkeypatch: pytest.MonkeyPatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    _Recorder.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_rebinding_name_cannot_reach_loopback(
        monkeypatch: pytest.MonkeyPatch, local_server: int) -> None:
    port = local_server
    real = socket.getaddrinfo
    lookups: list[str] = []
    connects_to_public: list[str] = []

    def rebinding(host, p, *args, **kwargs):
        name = str(host)
        if name == NAME:
            lookups.append(name)
            addr = PUBLIC if len(lookups) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, p or 0))]
        if name == PUBLIC:
            # The pinned connect. Refuse it here so nothing leaves the machine.
            connects_to_public.append(name)
            raise socket.gaierror("test: public address not reachable offline")
        return real(host, p, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    logged: list[str] = []
    body = url_safety.safe_fetch(f"http://{NAME}:{port}/api/tags",
                                 log=logged.append)

    assert _Recorder.hits == [], (
        f"the rebound name reached the loopback server: {_Recorder.hits}")
    assert body is None
    assert lookups == [NAME], f"the name was resolved again: {lookups}"
    assert connects_to_public == [PUBLIC], connects_to_public
    assert any("transport error" in m for m in logged), logged


def test_pinned_fetch_keeps_the_name_in_the_host_header(
        monkeypatch: pytest.MonkeyPatch, local_server: int) -> None:
    """Ordinary use: the request reaches the checked address and still says
    which site it wants. Loopback is let through the internal-address check
    for this test only, so the 'public' host can be the local server."""
    port = local_server
    name = "pinned.test.example"

    def resolve(host, p, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(url_safety, "_ip_is_internal", lambda ip: False)
    body = url_safety.safe_fetch(f"http://{name}:{port}/c.vtt")

    assert body == b"LOCAL-SERVICE-BODY"
    assert _Recorder.hits == [("/c.vtt", f"{name}:{port}")]


def test_tls_keeps_the_name_for_sni_and_certificate_checks(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The name is swapped out for the TCP connect only; afterwards urllib3
    reads conn.host for SNI and hostname verification, and it must be the
    URL's name, not the pinned address."""
    import urllib3.util.connection as u3conn

    seen: list[tuple[str, int]] = []

    def fake_create_connection(address, *args, **kwargs):
        seen.append(address)
        raise OSError("test: no socket")

    monkeypatch.setattr(u3conn, "create_connection", fake_create_connection)
    _http_pool, https_pool = url_safety._pinned_classes(PUBLIC)
    conn = https_pool.ConnectionCls("videos.test.example", 443)
    with pytest.raises(Exception):
        conn._new_conn()
    assert seen == [(PUBLIC, 443)]
    assert conn.host == "videos.test.example"


def test_check_returns_the_first_public_answer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host, p, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert url_safety._check("https://cdn.test.example/x") == (True, "", PUBLIC)


def test_check_refuses_when_any_answer_is_internal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host, p, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    ok, reason, ip = url_safety._check("https://mixed.test.example/x")
    assert (ok, ip) == (False, None)
    assert "resolves to internal address 127.0.0.1" in reason


# ---------------------------------------------------------------------------
# Review round 2 (2026-10-04): proxies, address ranges, deadlines, input
# that used to raise, and a URL two parsers read differently.
# ---------------------------------------------------------------------------

def _public_resolver(monkeypatch: pytest.MonkeyPatch, names: dict[str, str]):
    """Resolve `names`; refuse (and record) any connect to PUBLIC; pass
    loopback literals through so the local listener can be reached."""
    real = socket.getaddrinfo
    log: dict[str, list[str]] = {"lookups": [], "public": []}

    def resolve(host, p, *args, **kwargs):
        name = str(host)
        if name in names:
            log["lookups"].append(name)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[name], p or 0))]
        if name == PUBLIC:
            log["public"].append(name)
            raise socket.gaierror("test: public address not reachable offline")
        return real(host, p, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return log


@pytest.mark.parametrize("var", ["HTTP_PROXY", "http_proxy", "ALL_PROXY"])
def test_an_environment_proxy_is_not_used(
        monkeypatch: pytest.MonkeyPatch, local_server: int, var: str) -> None:
    """C1: with trust_env on, requests sent the request to the proxy, which
    resolves the name itself -- the pinned address was never used."""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv(var, f"http://127.0.0.1:{local_server}")
    log = _public_resolver(monkeypatch, {"proxied.test.example": PUBLIC})
    logged: list[str] = []
    body = url_safety.safe_fetch("http://proxied.test.example/x", log=logged.append)
    assert _Recorder.hits == [], f"the proxy received the request: {_Recorder.hits}"
    assert body is None
    assert log["public"] == [PUBLIC]
    assert any("transport error" in m for m in logged), logged


def test_a_url_two_parsers_disagree_on_still_connects_to_the_checked_ip(
        monkeypatch: pytest.MonkeyPatch, local_server: int) -> None:
    """urlparse reads the host after the '@' (pub.test); urllib3 reads
    inner.test and percent-encodes the rest into the path. The TCP connect
    must still go to the address _check approved for pub.test."""
    port = local_server
    log = _public_resolver(monkeypatch, {"pub.test": PUBLIC,
                                         "inner.test": "127.0.0.1"})
    url = f"http://inner.test:{port}\\@pub.test:{port}/x"
    assert url_safety._check(url) == (True, "", PUBLIC)
    assert url_safety.safe_fetch(url) is None
    assert _Recorder.hits == [], _Recorder.hits
    assert "inner.test" not in log["lookups"], log["lookups"]
    assert log["public"] == [PUBLIC]


@pytest.mark.parametrize("addr", [
    "100.64.0.1",            # carrier-grade NAT / Tailscale
    "100.127.255.254",
    "fec0::1",               # IPv6 site-local
    "192.88.99.1",           # 6to4 relay anycast
    "::ffff:127.0.0.1",      # IPv4-mapped loopback
    "::ffff:10.0.0.1",
    "2002:7f00:1::",         # 6to4 wrapping 127.0.0.1
    "64:ff9b::7f00:1",       # NAT64 of 127.0.0.1
    "198.18.0.1",            # benchmarking
    "240.0.0.1",
])
def test_non_public_ranges_are_internal(addr: str) -> None:
    import ipaddress
    assert url_safety._ip_is_internal(ipaddress.ip_address(addr))
    host = f"[{addr}]" if ":" in addr else addr
    ok, reason = url_safety.is_safe_url(f"http://{host}/")
    assert not ok and "disallowed IP literal" in reason


@pytest.mark.parametrize("addr", ["93.184.216.34", "142.250.80.110",
                                  "2606:2800:220:1::1"])
def test_public_addresses_stay_public(addr: str) -> None:
    import ipaddress
    assert not url_safety._ip_is_internal(ipaddress.ip_address(addr))


@pytest.mark.parametrize("value", [None, 42, b"http://x.test/", ["http://x.test/"]])
def test_a_non_string_url_is_refused_not_raised(value) -> None:
    ok, reason = url_safety.is_safe_url(value)
    assert not ok and reason.startswith("URL is not a string")
    assert url_safety.safe_fetch(value) is None
    assert url_safety.redact_url(value) == "<unparseable url>"


def test_an_unencodable_name_is_refused_not_raised(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A label over 63 characters fails IDNA encoding inside getaddrinfo
    with UnicodeError, which is not a gaierror."""
    def encoding_resolver(host, p, *a, **k):
        str(host).encode("idna")             # what socket.getaddrinfo does first
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", encoding_resolver)
    ok, reason = url_safety.is_safe_url(f"https://{'a' * 64}.test.example/")
    assert not ok
    assert "DNS resolution failed" in reason and "UnicodeEncodeError" in reason


class _Drip(http.server.BaseHTTPRequestHandler):
    """Promises 40 bytes and sends one every 0.2 s: each read is well inside
    any per-read timeout, the whole body takes 8 s."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "40")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def drip_server(monkeypatch: pytest.MonkeyPatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The drip server is the "public" host for these tests.
    monkeypatch.setattr(url_safety, "_ip_is_internal", lambda ip: False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM,
                                                6, "", ("127.0.0.1", p or 0))])
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_safe_fetch_has_a_wall_clock_deadline(
        monkeypatch: pytest.MonkeyPatch, drip_server: int) -> None:
    """C4: the requests timeout is per read, so a drip outlived it."""
    monkeypatch.setattr(url_safety, "SAFE_FETCH_DEADLINE_SECONDS", 1, raising=False)
    logged: list[str] = []
    start = time.monotonic()
    body = url_safety.safe_fetch(f"http://drip.test.example:{drip_server}/c",
                                 log=logged.append)
    elapsed = time.monotonic() - start
    assert body is None
    assert elapsed < 4, f"took {elapsed:.1f}s; the drip runs 8s"
    assert any("deadline exceeded" in m for m in logged), logged


def test_safe_download_has_a_wall_clock_deadline(
        monkeypatch: pytest.MonkeyPatch, drip_server: int, tmp_path) -> None:
    monkeypatch.setattr(url_safety, "SAFE_DOWNLOAD_DEADLINE_SECONDS", 1,
                        raising=False)
    logged: list[str] = []
    dest = tmp_path / "ep.mp3"
    start = time.monotonic()
    ok = url_safety.safe_download(f"http://drip.test.example:{drip_server}/e",
                                  dest, log=logged.append)
    assert not ok
    assert time.monotonic() - start < 4
    assert not dest.exists() and not (tmp_path / "ep.mp3.part").exists()
    assert any("deadline exceeded" in m for m in logged), logged


@pytest.fixture
def tls_drip_server(monkeypatch: pytest.MonkeyPatch, tmp_path,
                    allow_subprocess: None):
    """The drip over TLS, with a throwaway self-signed certificate for
    drip.test.example made by the local openssl (no network)."""
    import shutil
    import ssl
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=drip.test.example",
                    "-addext", "subjectAltName=DNS:drip.test.example"],
                   check=True, capture_output=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(url_safety, "_ip_is_internal", lambda ip: False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM,
                                                6, "", ("127.0.0.1", p or 0))])
    # Trusting the throwaway cert is also the test that the CA-bundle
    # environment setting survives trust_env=False.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_the_deadline_interrupts_a_tls_read(
        monkeypatch: pytest.MonkeyPatch, tls_drip_server: int) -> None:
    """Over TLS the TCP socket is detached into an SSL socket; the deadline
    must shut the one the response is actually reading from. The request
    also verifies the certificate against the URL's name while connected
    to the pinned address."""
    monkeypatch.setattr(url_safety, "SAFE_FETCH_DEADLINE_SECONDS", 1, raising=False)
    logged: list[str] = []
    start = time.monotonic()
    body = url_safety.safe_fetch(f"https://drip.test.example:{tls_drip_server}/c",
                                 log=logged.append)
    assert body is None
    assert time.monotonic() - start < 4
    assert any("deadline exceeded" in m for m in logged), logged


def test_a_body_inside_the_deadline_is_returned(
        monkeypatch: pytest.MonkeyPatch, local_server: int) -> None:
    monkeypatch.setattr(url_safety, "_ip_is_internal", lambda ip: False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM,
                                                6, "", ("127.0.0.1", p or 0))])
    body = url_safety.safe_fetch(f"http://quick.test.example:{local_server}/c",
                                 deadline_seconds=5)
    assert body == b"LOCAL-SERVICE-BODY"
