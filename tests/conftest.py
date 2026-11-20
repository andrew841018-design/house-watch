"""測試共用設定。

1. 擋掉網路：AF_INET／AF_INET6 的 connect、connect_ex、sendto 與 DNS 查詢一律丟出
   NetworkBlockedError。要連本機 loopback 的測試（例如 serve）加
   `@pytest.mark.allow_loopback`；外部網路永遠擋。測試抓取請用 httpx.MockTransport。
2. 隔離資料夾：每個測試都把 HOUSE_WATCH_DATA_DIR、HOUSE_WATCH_CONFIG 指到暫存資料夾，
   不會讀寫使用者真正的設定與資料。
"""

import ipaddress
import socket

import pytest


class NetworkBlockedError(RuntimeError):
    """測試裡發生了網路連線。"""


_INET = (socket.AF_INET, socket.AF_INET6)
_policy = {"allow_loopback": False}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_getaddrinfo = socket.getaddrinfo


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check(host: object) -> None:
    if _policy["allow_loopback"] and _is_loopback(host):
        return
    raise NetworkBlockedError(f"測試禁止網路連線：{host!r}")


def _connect(self, address):
    if self.family in _INET:
        _check(address[0])
    return _real_connect(self, address)


def _connect_ex(self, address):
    if self.family in _INET:
        _check(address[0])
    return _real_connect_ex(self, address)


def _sendto(self, data, *args):
    if self.family in _INET and args:
        _check(args[-1][0])
    return _real_sendto(self, data, *args)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None:
        _check(host)
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
socket.getaddrinfo = _getaddrinfo


@pytest.fixture(autouse=True)
def _network_policy(request):
    _policy["allow_loopback"] = request.node.get_closest_marker("allow_loopback") is not None
    yield
    _policy["allow_loopback"] = False


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("env")
    config = base / "config" / "settings.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOUSE_WATCH_DATA_DIR", str(base / "data"))
    monkeypatch.setenv("HOUSE_WATCH_CONFIG", str(config))
