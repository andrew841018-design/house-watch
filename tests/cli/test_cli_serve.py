"""serve：只綁 127.0.0.1、只提供網站資料夾（R2-17）。"""

import http.client
import inspect
import threading

import pytest
from hw_cli_support import isolate

from house_watch import cli


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)


def _get(port: int, path: str) -> tuple[int, http.client.HTTPMessage, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def test_server_factory_has_no_host_choice():
    assert cli.SERVE_HOST == "127.0.0.1"
    assert "host" not in inspect.signature(cli.make_server).parameters


@pytest.mark.allow_loopback
def test_server_binds_loopback_and_serves_only_site(tmp_path):
    root = tmp_path / "data_dir"
    site = root / "export" / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text("<p>示意</p>\n", encoding="utf-8")
    (site / "data" / "meta.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (root / "house_watch.sqlite").write_bytes(b"not for the web")

    server = cli.make_server(site, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        status, headers, body = _get(port, "/data/meta.json")
        assert status == 200 and body == b'{"schema_version":1}\n'
        assert headers["Content-Type"].startswith("application/json")
        assert headers["Cache-Control"] == "no-store"
        assert _get(port, "/")[0] == 200
        for path in (
            "/../../house_watch.sqlite",
            "/%2e%2e/%2e%2e/house_watch.sqlite",
            "/data/",  # 不列目錄
        ):
            status, _, body = _get(port, path)
            assert status == 404 and b"not for the web" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_without_site_exits_2(capsys):
    assert cli.main(["serve"]) == 2
    assert "house-watch export" in capsys.readouterr().err


@pytest.mark.allow_loopback
def test_serve_command_runs_until_interrupted(monkeypatch, capsys):
    assert cli.main(["export"]) == 0

    def interrupt(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.LocalServer, "serve_forever", interrupt)
    assert cli.main(["serve", "--port", "0"]) == 0
    assert "http://127.0.0.1:" in capsys.readouterr().out
