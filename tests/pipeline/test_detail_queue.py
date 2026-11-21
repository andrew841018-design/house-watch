"""detail_queue：依首次出現排隊、每輪上限、只用公開範圍判斷、失敗不重抓（R1-21）。"""

import datetime as dt

import httpx
import pytest
from pipeline_kit import FakeAdapter, FakeSite, Unit

from house_watch import db
from house_watch.clock import FakeClock
from house_watch.db import transaction
from house_watch.http import Fetcher, SourcePolicy
from house_watch.models import BType, DateRange, ListingFacts, MainKind, Parking
from house_watch.pipeline import detail_queue, versions
from house_watch.settings import load_public

T0 = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 11, 21)
FAR = 10**9


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "hw.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def scope():
    return load_public().scope.listing


def facts(source_id: str, **overrides) -> ListingFacts:
    data = {"source": "cthouse", "source_id": source_id, "area": "taoyuan_core"}
    data.update(overrides)
    return ListingFacts.model_validate(data)


def test_outside_public_scope_uses_only_known_values(scope):
    out = detail_queue.outside_public_scope
    assert not out(facts("1"), scope, TODAY), "全部缺值：照排"
    assert out(facts("1", price_yuan=scope.max_price_wan * 10_000 + 1), scope, TODAY)
    assert not out(facts("1", price_yuan=scope.max_price_wan * 10_000), scope, TODAY)
    assert out(facts("1", btype=BType.TOWNHOUSE), scope, TODAY)
    assert out(
        facts("1", main_cp=scope.min_main_ping * 100 - 1, main_kind=MainKind.MAIN), scope, TODAY
    )
    assert out(facts("1", floor_lo=scope.floors[1] + 1, floor_hi=scope.floors[1] + 1), scope, TODAY)
    old = DateRange(lo=None, hi=dt.date(TODAY.year - scope.max_age_years - 1, 1, 1))
    assert out(facts("1", built=old), scope, TODAY)
    unknown_end = DateRange(lo=None, hi=None)
    assert not out(facts("1", built=unknown_end), scope, TODAY)


def add(conn, f, minutes):
    with transaction(conn):
        versions.record_sighting(
            conn,
            f,
            now=T0 + dt.timedelta(minutes=minutes),
            scan_id=None,
            full=True,
            baseline=False,
            parser_version="t1",
        )


def fetcher(conn, site, clock):
    return Fetcher(
        conn,
        http=load_public().http,
        policies={"cthouse": SourcePolicy(frozenset({"fake.example"}))},
        clock=clock,
        transport=httpx.MockTransport(site.handler),
        sleep=clock.advance,
    )


def test_fifo_cap_and_failures(conn, scope, tmp_path):
    site = FakeSite()
    site.add(Unit(id="A"), Unit(id="B"), Unit(id="C", detail_parking="機械"))
    add(conn, facts("C", parking=Parking.UNKNOWN), 0)
    add(conn, facts("A", parking=Parking.UNKNOWN), 1)
    add(conn, facts("B", parking=Parking.RAMP_FLAT), 2)  # 不需要詳細頁
    add(conn, facts("Z", parking=Parking.UNKNOWN), 3)  # 網站上沒有 → 404 → 解析失敗
    add(conn, facts("Y", parking=Parking.UNKNOWN, btype=BType.WALKUP), 4)  # 公開範圍外
    clock = FakeClock(T0 + dt.timedelta(hours=1))
    with fetcher(conn, site, clock) as f:
        fetch = f.bind("cthouse", FAR)
        adapter = FakeAdapter()
        first = detail_queue.run(
            conn,
            fetch,
            adapter,
            scope=scope,
            now=clock.now,
            limit=1,
            failed_dir=tmp_path / "failed",
        )
        assert first.ids == ["C"], "最早出現的先抓"
        assert versions.latest(conn, "cthouse", "C").facts.parking == Parking.MECHANICAL
        second = detail_queue.run(
            conn,
            fetch,
            adapter,
            scope=scope,
            now=clock.now,
            limit=10,
            failed_dir=tmp_path / "failed",
        )
        assert second.ids == ["A", "Z"]
        assert second.invalid == 1 and second.updated == 1
        third = detail_queue.run(conn, fetch, adapter, scope=scope, now=clock.now, limit=10)
        assert third.ids == [], "抓過（含失敗）的不再排"
    assert list((tmp_path / "failed").glob("*.bin")), "解析失敗的回應存到資料夾外"
    assert all("/detail/Y" not in r for r in site.requests), "公開範圍外的不抓"
    assert any("/detail/Z" in r for r in site.requests)


def test_blocked_detail_stops_the_source_without_marking(conn, scope):
    site = FakeSite(mode="blocked")
    site.add(Unit(id="A"))
    add(conn, facts("A", parking=Parking.UNKNOWN), 0)
    clock = FakeClock(T0 + dt.timedelta(hours=1))
    with fetcher(conn, site, clock) as f:
        report = detail_queue.run(
            conn, f.bind("cthouse", FAR), FakeAdapter(), scope=scope, now=clock.now, limit=5
        )
    assert report.stop_source and report.error is not None and report.fetched == 0
    assert versions.listing(conn, "cthouse", "A").detail_fetched_at is None
