"""門牌更新：找最新一期、下載、檢查、換檔（doorindex.refresh）。用 httpx.MockTransport，不連網。"""

import json

import httpx
import pytest
from geo_city import FIXTURES, MIN_ROWS, NOW, city_doors, csv_text

from house_watch import __version__
from house_watch.errors import ErrorCode
from house_watch.geo.doorindex import (
    DATASET_API,
    DOWNLOAD_HOSTS,
    DoorIndex,
    DoorIndexError,
    HttpFetcher,
    pick_latest,
    prev_path,
    read_info,
    refresh,
)
from house_watch.paths import DataPaths

METADATA = json.loads((FIXTURES / "datagov_157689_min.json").read_text(encoding="utf-8"))
LATEST_URL = METADATA["result"]["distribution"][2]["resourceDownloadUrl"]
GOOD_CSV = csv_text(city_doors()).encode("utf-8")


class Server:
    """假的 data.gov.tw 與 opendata.tycg.gov.tw。"""

    def __init__(self, metadata=None, csv=GOOD_CSV, routes=None):
        self.metadata = METADATA if metadata is None else metadata
        self.csv = csv
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.routes:
            return self.routes[url](request)
        if url == DATASET_API:
            return httpx.Response(200, json=self.metadata)
        if url == LATEST_URL:
            return httpx.Response(200, content=self.csv, headers={"content-type": "text/csv"})
        return httpx.Response(404)

    def fetcher(self, **kwargs) -> HttpFetcher:
        client = httpx.Client(transport=httpx.MockTransport(self))
        kwargs.setdefault("sleep", lambda seconds: None)
        return HttpFetcher(client, **kwargs)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def _refresh(server, data_dir, **kwargs):
    fetcher = server.fetcher()
    try:
        return refresh(fetcher, data_dir, NOW, min_rows=MIN_ROWS, **kwargs)
    finally:
        fetcher.client.close()


def test_pick_latest_tolerates_description_formats():
    latest = pick_latest(METADATA)
    assert latest.version == "11510"  # 不是 JSON 格式的 11 月、也不是年份較早的 12 月
    assert latest.url == LATEST_URL


def test_pick_latest_rejects_empty_metadata():
    with pytest.raises(DoorIndexError):
        pick_latest({"result": {"distribution": []}})
    with pytest.raises(DoorIndexError):
        pick_latest({"oops": 1})


def test_refresh_installs_index_and_reuses_it(data_dir, ctx):
    server = Server()
    info = _refresh(server, data_dir, ctx=ctx)
    assert info.changed and info.version == "11510"
    current = DataPaths(data_dir).doorindex
    assert current.is_file() and not prev_path(data_dir).exists()
    assert sorted(p.name for p in data_dir.iterdir()) == ["doorindex.sqlite"]
    for request in server.requests:
        assert request.headers["user-agent"] == f"house-watch/{__version__}"
        assert request.url.scheme == "https"
    with DoorIndex.open(current, ctx) as idx:
        assert idx.version == "11510"
        ids = idx.road_doors("桃園區", "國際路一段", None, None)
        assert idx.road_frac(ids, "taoyuan_core")

    again = _refresh(server, data_dir)
    assert not again.changed and again.version == "11510"
    assert [str(r.url) for r in server.requests[-1:]] == [DATASET_API]  # 沒有重新下載


def _newer_metadata():
    metadata = json.loads(json.dumps(METADATA))
    metadata["result"]["distribution"].append(
        {
            "resourceDescription": "桃園市門牌位置坐標資料(115年12月)",
            "resourceFormat": "CSV",
            "resourceDownloadUrl": LATEST_URL.replace("000000000010", "000000000099"),
        }
    )
    return metadata


def test_new_version_keeps_the_previous_index(data_dir):
    _refresh(Server(), data_dir)
    newer = _newer_metadata()
    url = newer["result"]["distribution"][-1]["resourceDownloadUrl"]
    server = Server(newer, routes={url: lambda r: httpx.Response(200, content=GOOD_CSV)})
    info = _refresh(server, data_dir)
    assert info.changed and info.version == "11512"
    assert read_info(DataPaths(data_dir).doorindex).version == "11512"
    assert read_info(prev_path(data_dir)).version == "11510"


@pytest.mark.parametrize(
    "bad_csv",
    [
        "這不是門牌檔\n".encode(),  # 表頭不對
        csv_text(city_doors()[:3]).encode("utf-8"),  # 缺少某一區
        b"<html><body>challenge</body></html>",
    ],
)
def test_validation_failure_keeps_the_old_index(data_dir, bad_csv):
    _refresh(Server(), data_dir)
    current = DataPaths(data_dir).doorindex
    before = current.read_bytes()
    newer = _newer_metadata()
    url = newer["result"]["distribution"][-1]["resourceDownloadUrl"]
    server = Server(newer, routes={url: lambda r: httpx.Response(200, content=bad_csv)})
    with pytest.raises(DoorIndexError) as caught:
        _refresh(server, data_dir)
    assert caught.value.code == ErrorCode.DOORINDEX_INVALID
    assert current.read_bytes() == before
    assert sorted(p.name for p in data_dir.iterdir()) == ["doorindex.sqlite"]


def test_download_size_cap(data_dir):
    with pytest.raises(DoorIndexError, match="大小上限"):
        _refresh(Server(), data_dir, max_bytes=len(GOOD_CSV) - 1)
    assert not DataPaths(data_dir).doorindex.exists()
    assert list(data_dir.iterdir()) == []


def test_download_url_must_be_allow_listed(data_dir):
    metadata = json.loads(json.dumps(METADATA))
    for item in metadata["result"]["distribution"]:
        item["resourceDownloadUrl"] = item["resourceDownloadUrl"].replace(
            "opendata.tycg.gov.tw", "example.com"
        )
    server = Server(metadata)
    with pytest.raises(DoorIndexError, match="不允許"):
        _refresh(server, data_dir)
    assert [str(r.url) for r in server.requests] == [DATASET_API]


def test_plain_http_is_rejected(data_dir):
    metadata = json.loads(json.dumps(METADATA))
    for item in metadata["result"]["distribution"]:
        item["resourceDownloadUrl"] = item["resourceDownloadUrl"].replace("https://", "http://")
    with pytest.raises(DoorIndexError, match="不允許"):
        _refresh(Server(metadata), data_dir)


def test_redirect_to_another_host_is_rejected(data_dir):
    def bounce(request):
        return httpx.Response(302, headers={"location": "https://files.example.net/doors.csv"})

    server = Server(routes={LATEST_URL: bounce})
    with pytest.raises(DoorIndexError, match="不允許"):
        _refresh(server, data_dir)
    assert "files.example.net" not in {r.url.host for r in server.requests}


def test_redirect_on_same_host_waits_between_requests(data_dir):
    target = LATEST_URL.replace("/download", "/file.csv")
    waits: list[float] = []
    server = Server(
        routes={
            LATEST_URL: lambda r: httpx.Response(302, headers={"location": target}),
            target: lambda r: httpx.Response(200, content=GOOD_CSV),
        }
    )
    clock = iter([100.0, 100.0, 100.5, 104.5, 104.5, 104.5])
    fetcher = server.fetcher(sleep=waits.append, monotonic=lambda: next(clock))
    try:
        info = refresh(fetcher, data_dir, NOW, min_rows=MIN_ROWS)
    finally:
        fetcher.client.close()
    assert info.changed
    assert waits and all(w > 0 for w in waits)
    assert {r.url.host for r in server.requests} == {"data.gov.tw", *DOWNLOAD_HOSTS}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(403), ErrorCode.BLOCKED),
        (httpx.Response(429), ErrorCode.RATE_LIMITED),
        (httpx.Response(503), ErrorCode.HTTP_5XX),
        (httpx.Response(200, headers={"cf-mitigated": "challenge"}), ErrorCode.CHALLENGE),
        (
            httpx.Response(200, text="<html>請稍候</html>", headers={"content-type": "text/html"}),
            ErrorCode.CHALLENGE,
        ),
    ],
)
def test_blocked_metadata_stops(data_dir, response, code):
    server = Server(routes={DATASET_API: lambda r: response})
    with pytest.raises(DoorIndexError) as caught:
        _refresh(server, data_dir)
    assert caught.value.code == code
    assert len(server.requests) == 1  # 不重試


def test_timeout_is_reported(data_dir):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DoorIndexError) as caught:
        _refresh(Server(routes={DATASET_API: slow}), data_dir)
    assert caught.value.code == ErrorCode.TIMEOUT


def test_refresh_accepts_a_plain_httpx_client(data_dir):
    server = Server()  # 兩個請求在不同主機，不需要等
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        info = refresh(client, data_dir, NOW, min_rows=MIN_ROWS)
    assert info.version == "11510"
