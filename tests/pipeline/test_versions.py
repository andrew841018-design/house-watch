"""versions：append-only 版本表、詳細欄位帶到下一版、價格歷史（R1-22、R3-22）。數字是示意值。"""

import datetime as dt
from decimal import Decimal

import pytest

from house_watch import db
from house_watch.clock import taipei_date
from house_watch.db import transaction
from house_watch.models import DateRange, Elevator, ListingFacts, MainKind, Parking
from house_watch.pipeline import versions
from house_watch.sources.base import (
    built_from_decimal_years,
    built_from_floor_years,
    built_from_years_months,
)

T0 = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "hw.sqlite")
    yield connection
    connection.close()


def facts(**overrides) -> ListingFacts:
    data = {
        "source": "cthouse",
        "source_id": "X1",
        "area": "taoyuan_core",
        "price_yuan": 12_340_000,
    }
    data.update(overrides)
    return ListingFacts.model_validate(data)


def sight(conn, f, when, *, detail=("parking", "elevator", "built", "main_cp", "floor_lo")):
    with transaction(conn):
        return versions.record_sighting(
            conn,
            f,
            now=when,
            scan_id=None,
            full=False,
            baseline=False,
            parser_version="t1",
            detail_fields=detail,
        )


def count(conn):
    return conn.execute("SELECT count(*) FROM listing_versions").fetchone()[0]


def test_same_facts_do_not_add_versions(conn):
    assert sight(conn, facts(), T0).new
    assert not sight(conn, facts(), T0 + dt.timedelta(hours=1)).changed
    assert count(conn) == 1
    assert sight(conn, facts(price_yuan=12_000_000), T0 + dt.timedelta(hours=2)).changed
    rows = conn.execute(
        "SELECT valid_from, valid_to FROM listing_versions ORDER BY valid_from"
    ).fetchall()
    assert rows[0][1] == rows[1][0] and rows[1][1] is None


def test_versions_in_the_same_second_do_not_collide(conn):
    sight(conn, facts(), T0)
    sight(conn, facts(price_yuan=12_000_000), T0)
    starts = [r[0] for r in conn.execute("SELECT valid_from FROM listing_versions")]
    assert len(set(starts)) == 2


def test_carry_forward_keeps_detail_values_and_lets_fresh_list_values_win():
    prior = facts(
        parking=Parking.RAMP_FLAT,
        elevator=Elevator.YES,
        main_cp=2500,
        main_kind=MainKind.MAIN,
        floor_lo=5,
        floor_hi=5,
        floors_total=12,
        built=DateRange(lo=dt.date(2015, 3, 1), hi=dt.date(2015, 3, 31)),
        price_excl_parking=False,
        site_original_price_yuan=13_000_000,
    )
    listed = facts(
        parking=Parking.FLAT_UNKNOWN_ACCESS,  # 列表只知道是平面
        elevator=Elevator.INFERRED,  # 列表只有推定
        built=DateRange(lo=dt.date(2014, 11, 22), hi=dt.date(2015, 11, 21)),  # 一整年
        floor_lo=5,
        floor_hi=5,
        price_excl_parking=True,  # 列表比較新的旗標
    )
    fields = {
        "parking",
        "elevator",
        "built",
        "main_cp",
        "main_kind",
        "floor_lo",
        "floor_hi",
        "floors_total",
        "price_excl_parking",
        "site_original_price_yuan",
    }
    merged = versions.carry_forward(listed, prior, fields)
    assert merged.parking == Parking.RAMP_FLAT and merged.elevator == Elevator.YES
    assert (merged.main_cp, merged.main_kind) == (2500, MainKind.MAIN)
    assert merged.floors_total == 12
    assert merged.built == prior.built, "完工日取交集"
    assert merged.price_excl_parking is True, "列表有值就用列表的"
    assert merged.site_original_price_yuan == 13_000_000, "列表沒有才帶上一版的"
    plain = versions.carry_forward(listed, prior, ())
    assert plain.parking == listed.parking and plain.elevator == listed.elevator
    assert plain.built == prior.built, "完工日一律沿用有交集的上一版"


def test_detail_values_survive_list_scans(conn):
    sight(conn, facts(parking=Parking.UNKNOWN), T0)
    with transaction(conn):
        versions.append(
            conn,
            facts(parking=Parking.RAMP_FLAT),
            now=T0 + dt.timedelta(minutes=5),
            parser_version="t1",
            scan_id=None,
        )
    for hours in (1, 2, 3):
        result = sight(conn, facts(parking=Parking.UNKNOWN), T0 + dt.timedelta(hours=hours))
        assert not result.changed and result.facts.parking == Parking.RAMP_FLAT
    assert count(conn) == 2


def test_price_sanity_and_jumps_stay_out_of_the_history(conn):
    sight(conn, facts(price_yuan=12_340_000), T0)
    sight(conn, facts(price_yuan=1_234), T0 + dt.timedelta(hours=1))  # 單位錯：範圍外
    sight(conn, facts(price_yuan=123_400_000), T0 + dt.timedelta(hours=2))  # 跳 10 倍
    sight(conn, facts(price_yuan=12_000_000), T0 + dt.timedelta(hours=3))
    points = versions.price_points(conn, "cthouse", "X1")
    assert [p for _, p in points] == [12_340_000, 12_000_000]
    assert count(conn) == 4, "事實照樣保存，只是價格不進歷史"


def test_price_summary_ignores_steps_under_ten_thousand():
    t = [T0 + dt.timedelta(days=d) for d in range(5)]
    summary = versions.price_summary(
        [(t[0], 15_000_000), (t[1], 14_995_000), (t[2], 14_500_000), (t[3], 14_800_000)]
    )
    assert summary.price == 14_800_000 and summary.price_first == 15_000_000
    assert summary.price_drop_at == t[2]
    tiny = versions.price_summary([(t[0], 15_000_000), (t[1], 14_995_000)])
    assert tiny.price == 14_995_000 and tiny.price_drop_at is None
    assert versions.price_summary([]) == versions.PriceSummary(None, None, None)


def test_relisting_same_id_reopens_the_listing(conn):
    sight(conn, facts(), T0)
    with transaction(conn):
        conn.execute("UPDATE listings SET status = 'gone', gone_at = ?", ("2026-11-22T00:00:00Z",))
    result = sight(conn, facts(), T0 + dt.timedelta(days=3))
    assert result.relisted
    row = versions.listing(conn, "cthouse", "X1")
    assert row.status == "active" and row.gone_at is None and row.relisted_at is not None


def test_listing_keeps_its_first_area(conn):
    sight(conn, facts(), T0)
    result = sight(conn, facts(area="zhongli_ic"), T0 + dt.timedelta(hours=1))
    assert not result.changed and result.facts.area == "taoyuan_core"
    assert versions.listing(conn, "cthouse", "X1").area == "taoyuan_core"


def test_void_parser_version_reopens_the_previous_version(conn):
    sight(conn, facts(), T0)
    with transaction(conn):
        versions.append(
            conn,
            facts(price_yuan=1_500_000),
            now=T0 + dt.timedelta(hours=1),
            parser_version="bad",
            scan_id=None,
        )
    with transaction(conn):
        assert versions.void_parser_version(conn, "cthouse", "bad") == 1
    current = versions.latest(conn, "cthouse", "X1")
    assert current.facts.price_yuan == 12_340_000 and current.parser_version == "t1"


def test_list_age_window_does_not_drift_into_new_versions(conn):
    """列表的屋齡從抓取日往回推：每天差一天的區間不能每天寫一個新版本，也不能越縮越窄。"""
    days = [T0 + dt.timedelta(days=n) for n in range(4)]
    for when in days:
        sight(conn, facts(built=built_from_floor_years(9, taipei_date(when))), when)
    assert count(conn) == 1
    first = versions.latest(conn, "cthouse", "X1").facts.built
    assert first == built_from_floor_years(9, taipei_date(days[0]))
    for when in days:
        decimal = built_from_decimal_years(Decimal("12.3"), taipei_date(when))
        sight(conn, facts(source_id="X2", built=decimal), when)
    assert count(conn) == 2
    # 網站把屋齡改了（區間沒有交集）：才換成新的
    sight(conn, facts(built=built_from_floor_years(30, taipei_date(days[-1]))), days[-1])
    assert count(conn) == 3


def test_detail_month_range_survives_year_window_scans(conn):
    sight(conn, facts(built=built_from_floor_years(9, taipei_date(T0))), T0)
    month = built_from_years_months(9, 4, taipei_date(T0))
    with transaction(conn):
        versions.append(
            conn,
            facts(built=month),
            now=T0 + dt.timedelta(minutes=5),
            parser_version="t1",
            scan_id=None,
        )
    for n in range(1, 4):
        later = T0 + dt.timedelta(days=n)
        result = sight(conn, facts(built=built_from_floor_years(9, taipei_date(later))), later)
        assert not result.changed and result.facts.built == month
