"""dedupe：R1-07 的比對口徑與 R1-08 的分群（complete linkage、群組編號穩定、衝突）。

全部是示意值；社區名、路名都是「示意…」。
"""

import datetime as dt

import pytest

from house_watch import db
from house_watch.db import transaction
from house_watch.models import (
    CONFLICT,
    DateRange,
    Elevator,
    GroupFacts,
    ListingFacts,
    MainKind,
    Parking,
)
from house_watch.pipeline import dedupe, versions

T0 = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)


def unit(source="cthouse", source_id="1", **overrides) -> ListingFacts:
    data = {
        "source": source,
        "source_id": source_id,
        "area": "taoyuan_core",
        "floor_lo": 3,
        "floor_hi": 3,
        "floors_total": 18,
        "main_cp": 2345,
        "main_kind": MainKind.MAIN,
        "title_area_cp": 4321,
        "road_text": "示意西路三段",
        "district_hints": [("district", "桃園區")],
        "price_yuan": 18_500_000,
    }
    data.update(overrides)
    return ListingFacts.model_validate(data)


def ok(a: ListingFacts, b: ListingFacts) -> bool:
    return dedupe.compatible(dedupe.key_of(a), dedupe.key_of(b))


# ---------------------------------------------------------------- R1-07：缺值、口徑、精度


def test_missing_values_never_match():
    """第一批的 key-0 bug：兩筆都缺總樓層、屋齡、主建物時不能被當成相同。"""
    a = unit(main_cp=None, main_kind=None, title_area_cp=None, floors_total=None)
    b = unit(
        source="hbhousing", main_cp=None, main_kind=None, title_area_cp=None, floors_total=None
    )
    assert not ok(a, b), "沒有任何一種面積兩筆都知道"
    assert not ok(unit(floor_lo=None, floor_hi=None), unit(source="hbhousing")), "樓層要兩筆都知道"
    assert not ok(unit(road_text=None), unit(source="hbhousing", road_text=None)), "要有位置證據"


def test_zero_floors_total_is_unknown_not_equal():
    """「3樓/共0樓」解析成總樓層未知：不會和另一筆的未知「相等」而通過，但也不阻擋。"""
    a = unit(floors_total=None)
    assert ok(a, unit(source="hbhousing", floors_total=None))
    assert ok(a, unit(source="hbhousing", floors_total=19)), "差 1 以內"
    assert not ok(unit(floors_total=12), unit(source="hbhousing", floors_total=15))


def test_only_same_kind_areas_are_compared():
    main = unit(main_cp=2345, main_kind=MainKind.MAIN, title_area_cp=None)
    plus = unit(source="etwarm", main_cp=2500, main_kind=MainKind.MAIN_PLUS_AUX, title_area_cp=None)
    assert not ok(main, plus), "主建物對主建物＋附屬不能比，又沒有別的面積"
    plus_title = plus.model_copy(update={"title_area_cp": 4323})
    main_title = main.model_copy(update={"title_area_cp": 4321})
    assert ok(main_title, plus_title), "權狀坪 ±0.05 坪"
    assert not ok(main_title, plus_title.model_copy(update={"title_area_cp": 4337}))
    assert not ok(unit(main_cp=2345), unit(source="hbhousing", main_cp=2412)), (
        "主建物差超過 0.30 坪"
    )


def test_low_precision_or_far_coordinates():
    a = unit(site_lat=25.0, site_lon=121.3, road_text=None)
    near = unit(source="hbhousing", site_lat=25.0003, site_lon=121.3, road_text=None)  # 約 33 m
    far = unit(source="hbhousing", site_lat=25.01, site_lon=121.3, road_text=None)  # 約 1.1 km
    assert ok(a, near)
    assert not ok(a, far)
    imprecise = unit(source="rakuya", site_lat=25.0003, site_lon=121.3, road_text=None)
    assert not ok(a, imprecise), "不是建物等級的座標不當位置證據"


def test_community_only_against_road_only_is_not_evidence():
    community = unit(road_text=None, community_name="示意大國")
    road = unit(source="rakuya", community_name=None)
    assert not ok(community, road)
    assert ok(community, unit(source="rakuya", road_text=None, community_name="示意大國社區"))
    assert not ok(
        unit(road_text=None, community_name="中路"),
        unit(source="rakuya", road_text=None, community_name="中路特區示意"),
    ), "不做子字串比對"


def test_road_normalization():
    assert dedupe.normalize_road("示意西路路3段") == dedupe.normalize_road("示意西路三段")
    assert ok(unit(road_text="示意西路路3段"), unit(source="hbhousing", road_text="示意西路三段"))


def test_district_contradiction():
    assert not ok(unit(), unit(source="hbhousing", district_hints=[("district", "中壢區")]))


# ---------------------------------------------------------------- R1-08：同型戶、串連、矛盾


def test_mirror_units_with_different_parking_are_not_merged():
    ramp = unit(parking=Parking.RAMP_FLAT)
    mech = unit(source="hbhousing", source_id="c1", parking=Parking.MECHANICAL)
    lift = unit(source="rakuya", source_id="r1", parking=Parking.LIFT_FLAT)
    assert not ok(ramp, mech) and not ok(ramp, lift)
    assert ok(ramp, unit(source="rakuya", parking=Parking.FLAT_UNKNOWN_ACCESS)), "只知道平面不矛盾"
    assert ok(ramp, unit(source="rakuya", parking=Parking.UNKNOWN))


def test_other_auxiliary_contradictions():
    assert not ok(unit(layout="3房2廳2衛"), unit(source="hbhousing", layout="2房2廳1衛"))
    assert ok(unit(layout="3房2廳2衛"), unit(source="hbhousing", layout="3房2廳2衛1室"))
    assert not ok(unit(elevator=Elevator.YES), unit(source="hbhousing", elevator=Elevator.NO))
    assert ok(unit(elevator=Elevator.YES), unit(source="hbhousing", elevator=Elevator.INFERRED))
    old = DateRange(lo=dt.date(2001, 1, 1), hi=dt.date(2001, 12, 31))
    new = DateRange(lo=dt.date(2015, 1, 1), hi=dt.date(2015, 12, 31))
    assert not ok(unit(built=old), unit(source="hbhousing", built=new))
    near = DateRange(lo=dt.date(2002, 6, 1), hi=dt.date(2002, 6, 30))
    assert ok(unit(built=old), unit(source="hbhousing", built=near)), "一年以內"


def test_brand_origin_is_strong_evidence():
    original = unit(source="cthouse", source_id="9000001", brand_origin_id="cthouse:9000001")
    copy = unit(
        source="hbhousing",
        source_id="c9000001",
        brand_origin_id="cthouse:9000001",
        floor_lo=None,
        floor_hi=None,
        main_cp=None,
        main_kind=None,
        road_text=None,
    )
    assert ok(original, copy)


# ---------------------------------------------------------------- 分群（寫資料庫）


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "hw.sqlite")
    yield connection
    connection.close()


def add(conn, facts, minutes, *, active=True, baseline=False):
    when = T0 + dt.timedelta(minutes=minutes)
    with transaction(conn):
        versions.record_sighting(
            conn, facts, now=when, scan_id=None, full=False, baseline=baseline, parser_version="t1"
        )
        if not active:
            conn.execute(
                "UPDATE listings SET status = 'gone' WHERE source = ? AND source_id = ?",
                (facts.source, facts.source_id),
            )


def assign(conn):
    with transaction(conn):
        return dedupe.assign_groups(conn)


def group_ids(conn):
    rows = conn.execute("SELECT source, source_id, group_id FROM listings").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def test_complete_linkage_does_not_chain(conn):
    a = unit(source="cthouse", source_id="1", main_cp=2345)
    b = unit(source="hbhousing", source_id="H1", main_cp=2367)  # 和 a 差 0.22
    c = unit(source="rakuya", source_id="R1", main_cp=2392)  # 和 b 差 0.25，和 a 差 0.47
    for minutes, item in enumerate((a, b, c)):
        add(conn, item, minutes)
    report = assign(conn)
    gids = group_ids(conn)
    assert gids[("cthouse", "1")] == gids[("hbhousing", "H1")]
    assert gids[("rakuya", "R1")] != gids[("cthouse", "1")]
    assert report.joined == 1 and report.created == 2


def test_new_listings_join_incrementally_and_ids_are_stable(conn):
    add(conn, unit(source="cthouse", source_id="1"), 0, baseline=True)
    assign(conn)
    first = group_ids(conn)[("cthouse", "1")]
    add(conn, unit(source="hbhousing", source_id="H1"), 60)
    assign(conn)
    gids = group_ids(conn)
    assert gids[("hbhousing", "H1")] == first
    row = conn.execute('SELECT first_seen, baseline FROM "groups" WHERE group_id = ?', (first,))
    first_seen, baseline = row.fetchone()
    assert first_seen == "2026-11-21T01:00:00Z" and baseline == 1, "首次出現與基準取最早的成員"
    assert assign(conn).joined == 0, "再跑一次不變"
    assert group_ids(conn) == gids


def test_ambiguous_candidates_get_their_own_group(conn):
    add(conn, unit(source="cthouse", source_id="1", parking=Parking.RAMP_FLAT), 0)
    add(conn, unit(source="cthouse", source_id="2", parking=Parking.MECHANICAL), 1)
    assign(conn)
    add(conn, unit(source="rakuya", source_id="R1", parking=Parking.UNKNOWN), 2)
    report = assign(conn)
    gids = group_ids(conn)
    assert report.ambiguous == 1
    assert gids[("rakuya", "R1")] not in (gids[("cthouse", "1")], gids[("cthouse", "2")])


def test_strong_link_breaks_the_tie(conn):
    add(conn, unit(source="cthouse", source_id="1", brand_origin_id="cthouse:1"), 0)
    add(conn, unit(source="cthouse", source_id="2", parking=Parking.MECHANICAL), 1)
    assign(conn)
    add(conn, unit(source="hbhousing", source_id="c1", brand_origin_id="cthouse:1"), 2)
    assign(conn)
    gids = group_ids(conn)
    assert gids[("hbhousing", "c1")] == gids[("cthouse", "1")]


def test_changed_member_is_split_out(conn):
    add(conn, unit(source="cthouse", source_id="1"), 0)
    add(conn, unit(source="hbhousing", source_id="H1"), 1)
    assign(conn)
    gid = group_ids(conn)[("cthouse", "1")]
    add(conn, unit(source="hbhousing", source_id="H1", floor_lo=9, floor_hi=9), 60)
    report = assign(conn)
    gids = group_ids(conn)
    assert report.split == 1
    assert gids[("cthouse", "1")] == gid and gids[("hbhousing", "H1")] != gid


def test_new_listing_does_not_revive_an_all_gone_group(conn):
    add(conn, unit(source="cthouse", source_id="1"), 0, active=False)
    assign(conn)
    add(conn, unit(source="hbhousing", source_id="H1"), 60)
    assign(conn)
    gids = group_ids(conn)
    assert gids[("hbhousing", "H1")] != gids[("cthouse", "1")], "跨編號的重新上架延後（R1-09）"


# ---------------------------------------------------------------- 群組事實


def test_group_facts_conflicts_and_refinements():
    members = [
        (
            "cthouse",
            unit(
                parking=Parking.FLAT_UNKNOWN_ACCESS,
                elevator=Elevator.INFERRED,
                main_cp=2345,
                floors_total=18,
                built=DateRange(lo=dt.date(2015, 1, 1), hi=dt.date(2015, 12, 31)),
            ),
        ),
        (
            "hbhousing",
            unit(
                parking=Parking.RAMP_FLAT,
                elevator=Elevator.YES,
                main_cp=2352,
                floors_total=19,
                built=DateRange(lo=dt.date(2015, 6, 1), hi=dt.date(2016, 5, 31)),
                price_excl_parking=True,
            ),
        ),
        (
            "rakuya",
            unit(parking=Parking.UNKNOWN, price_excl_parking=False, community_name="示意苑"),
        ),
    ]
    values, conflicts = dedupe.group_facts(members)
    assert values["parking"] == Parking.RAMP_FLAT and values["elevator"] == Elevator.YES
    assert values["main_cp"] == 2345, "容差內取最小（保守）"
    assert values["floors_total"] == 19
    assert (values["built_lo"], values["built_hi"]) == (dt.date(2015, 6, 1), dt.date(2015, 12, 31))
    assert values["price_excl_parking"] == CONFLICT
    assert {item["src"] for item in conflicts["price_excl_parking"]} == {"hbhousing", "rakuya"}
    assert values["community_name"] == "示意苑"
    GroupFacts.model_validate(values)


def test_group_facts_contradiction_lists_every_value():
    members = [
        ("cthouse", unit(elevator=Elevator.YES, parking=Parking.RAMP_FLAT)),
        ("hbhousing", unit(elevator=Elevator.NO, parking=Parking.MECHANICAL)),
    ]
    values, conflicts = dedupe.group_facts(members)
    assert values["elevator"] == CONFLICT and values["parking"] == CONFLICT
    assert conflicts["parking"] == [
        {"src": "cthouse", "value": "ramp_flat"},
        {"src": "hbhousing", "value": "mechanical"},
    ]
    disjoint = [
        ("cthouse", unit(built=DateRange(lo=dt.date(2001, 1, 1), hi=None))),
        ("hbhousing", unit(built=DateRange(lo=None, hi=dt.date(1999, 1, 1)))),
    ]
    values, conflicts = dedupe.group_facts(disjoint)
    assert values["built_lo"] == CONFLICT and len(conflicts["built_lo"]) == 2


def test_price_excl_parking_must_be_known_for_every_member():
    """事後審查 P-3：網頁用群組的最低價，加上群組的「是否含車位」判斷總價。這兩個值要對同一筆
    刊登；有成員沒寫的話，群組當成未知（網頁判待確認），不能借別的成員的旗標。"""
    excl_false = unit(price_excl_parking=False)
    unknown = unit(price_excl_parking=None)
    values, _ = dedupe.group_facts([("cthouse", excl_false), ("hbhousing", unknown)])
    assert values["price_excl_parking"] is None
    with_parking = unit(price_excl_parking=True, parking_price_yuan=2_000_000)
    values, _ = dedupe.group_facts([("cthouse", with_parking), ("hbhousing", unknown)])
    assert values["price_excl_parking"] is None and values["parking_price_yuan"] is None
    values, _ = dedupe.group_facts([("cthouse", excl_false), ("hbhousing", unit(price_excl_parking=False))])
    assert values["price_excl_parking"] is False
