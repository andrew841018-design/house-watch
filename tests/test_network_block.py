"""conftest 的網路封鎖真的有效。"""

import socket

import httpx
import pytest

BLOCKED = pytest.raises(RuntimeError, match="測試禁止網路連線")


def test_dns_lookup_is_blocked():
    with BLOCKED:
        socket.getaddrinfo("example.com", 443)


def test_tcp_connection_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with BLOCKED:
            sock.connect(("192.0.2.1", 80))
        with BLOCKED:
            sock.connect_ex(("192.0.2.1", 80))
    with BLOCKED:
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def test_httpx_request_is_blocked():
    with BLOCKED:
        httpx.get("https://example.com", timeout=1)


def test_mock_transport_still_works():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/").text == "ok"


def _listener() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def test_loopback_is_blocked_by_default():
    with _listener() as server, socket.socket() as client, BLOCKED:
        client.connect(server.getsockname())


@pytest.mark.allow_loopback
def test_loopback_allowed_with_marker():
    with _listener() as server, socket.socket() as client:
        client.connect(server.getsockname())
        with BLOCKED:
            socket.getaddrinfo("example.com", 443)
