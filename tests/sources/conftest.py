"""轉接器測試共用：讀 fixtures、組 Resp、用 MockTransport 的 Fetcher。全部離線。

fixtures 是依真實回應的結構手寫的合成樣本：只留解析器會讀的欄位，數值都是示意值。
"""

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from house_watch import db
from house_watch.clock import FakeClock
from house_watch.http import Fetcher, SourcePolicy
from house_watch.settings import load_public
from house_watch.sources.base import FETCHED_AT_HEADER, AreaQuery, Resp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sources"
FETCHED_AT = "2026-11-21T02:00:00Z"  # 台北 2026-11-21
START = dt.datetime(2026, 11, 21, 2, 0, tzinfo=dt.UTC)


class Fixtures:
    root = FIXTURES

    def path(self, site: str, name: str) -> Path:
        return FIXTURES / site / name

    def bytes(self, site: str, name: str) -> bytes:
        return self.path(site, name).read_bytes()

    def json(self, site: str, name: str):
        return json.loads(self.path(site, name).read_text(encoding="utf-8"))

    def resp(self, site: str, name: str, *, url: str = "https://example.invalid/", status: int = 200) -> Resp:
        return Resp(
            status=status,
            headers={"content-type": "application/json", FETCHED_AT_HEADER: FETCHED_AT},
            body=self.bytes(site, name),
            url=url,
        )

    def body_resp(self, body: bytes, *, url: str = "https://example.invalid/", status: int = 200) -> Resp:
        return Resp(status=status, headers={FETCHED_AT_HEADER: FETCHED_AT}, body=body, url=url)

    def envelope(self, site: str, name: str) -> httpx.Response:
        """被擋的回應：{status, headers, body} → httpx.Response。"""
        data = self.json(site, name)
        return httpx.Response(
            data["status"], headers=data["headers"], content=data["body"].encode("utf-8")
        )


@pytest.fixture
def fx() -> Fixtures:
    return Fixtures()


@pytest.fixture
def public():
    return load_public()


@pytest.fixture
def query(public) -> AreaQuery:
    """公開預設的第一個區域（只有公開範圍，沒有個人條件）。"""
    return AreaQuery.from_config(public.areas[0], public.scope.listing)


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "hw.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def make_fetcher(conn, clock):
    created: list[Fetcher] = []
    http = load_public().http

    def factory(handler, *, policies, **kwargs):
        fetcher = Fetcher(
            conn,
            http=http,
            policies=policies,
            clock=clock,
            transport=httpx.MockTransport(handler),
            sleep=clock.advance,
            **kwargs,
        )
        created.append(fetcher)
        return fetcher

    yield factory
    for fetcher in created:
        fetcher.close()


def adapter_policy(adapter) -> dict[str, SourcePolicy]:
    return {
        adapter.source: SourcePolicy(frozenset(adapter.hosts), robots_urls=tuple(adapter.robots_urls))
    }


@pytest.fixture
def policy_for():
    return adapter_policy
