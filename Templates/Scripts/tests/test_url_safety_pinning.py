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
