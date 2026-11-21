"""抓取層測試共用：假時鐘、暫存資料庫、用 MockTransport 的 Fetcher。全部離線。"""

import datetime as dt

import httpx
import pytest

from house_watch import db
from house_watch.clock import FakeClock
from house_watch.http import Fetcher, SourcePolicy
from house_watch.settings import load_public

START = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)  # 台北 09:00
HOST = "a.example"
SOURCE = "x"


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
    """make_fetcher(handler, policies=..., **kwargs)：sleep 只推進假時鐘。"""
    created: list[Fetcher] = []
    http = load_public().http

    def factory(handler, *, policies=None, **kwargs):
        fetcher = Fetcher(
            conn,
            http=http,
            policies=policies or {SOURCE: SourcePolicy(frozenset({HOST}))},
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
