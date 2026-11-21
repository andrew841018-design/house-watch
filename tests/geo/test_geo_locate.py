"""定位：區的決策表（R3-01）、精度降級（R1-14、R3-07）、座標合理性、社區名。"""

import dataclasses
import datetime as dt
import json

import pytest
import shapely
from geo_city import B1, FIXTURES, P1, TAOYUAN, YW, Z1, Z4, ZHONGLI, ZL

from house_watch.geo.community import CommunityLookup, OsmNames, SiteObservation, parse_overpass
from house_watch.geo.locate import (
    LocEvidence,
    area_metrics,
    decide,
    locate,
)
from house_watch.geo.proj import tm2_to_wgs84
from house_watch.models import ListingFacts, Precision, TxFacts


def site(point: tuple[float, float]) -> dict[str, float]:
    lon, lat = tm2_to_wgs84(*point)
    return {"site_lon": lon, "site_lat": lat}


def hints(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(pairs)


# ---------------------------------------------------------------- R3-01 區的決策表


def test_zip_says_zhongli_but_address_and_coords_say_bade(idx, ctx):
    """住商 AS143361 的寫法：zipCode 320，門牌欄寫八德區中華路，座標在中壢區外。"""
    ev = LocEvidence(
        source="hbhousing",
        district_hints=hints(("zip", "320"), ("address", "八德區中華路")),
        address_text="八德區中華路",
        **site(B1),
    )
    decision = decide(ev, idx, ctx)
    assert decision.method == "conflict"
    assert decision.location.conflict and decision.location.district is None
    kinds = {(e.kind, e.district) for e in decision.evidence}
    assert ("zip", "中壢區") in kinds and ("address", "八德區") in kinds
    assert ("coords", "八德區") in kinds and ("polygon", "not:中壢區") in kinds
    for area in ctx.area_ids:
        assert area_metrics(decision.location, idx, ctx, area) == {}


def test_two_district_names_in_the_address(idx, ctx):
    """住商 pS2973822 的寫法：zipCode 320、座標在中壢區內，門牌欄寫「中壢區桃園區莊敬路」。"""
    ev = LocEvidence(
        district_hints=hints(("zip", "320")), address_text="中壢區桃園區莊敬路", **site(Z1)
    )
    decision = decide(ev, idx, ctx)
    assert decision.method == "conflict" and decision.location.conflict
    assert decision.location.precision == Precision.SITE  # 座標還在，地圖可以畫，但不套方案


def test_two_district_names_without_other_evidence(idx, ctx):
    loc = locate(LocEvidence(address_text="中壢區八德區中華路3號"), idx, ctx)
    assert loc.conflict and loc.district is None and loc.precision == Precision.NONE


def test_same_name_roads_use_the_decided_district(idx, ctx):
    zl = decide(
        LocEvidence(district_hints=hints(("district", "中壢區")), address_text="中正路10號"),
        idx,
        ctx,
    )
    ty = decide(
        LocEvidence(district_hints=hints(("zip", "330")), address_text="中正路1188號"), idx, ctx
    )
    assert zl.location.district == "中壢區" and zl.match.district == ZHONGLI
    assert ty.location.district == "桃園區" and ty.match.district == TAOYUAN
    assert zl.location.precision == ty.location.precision == Precision.DOOR
    # 民族路二段在中壢、平鎮都有：用證據決定的區，不是隨便挑一個
    ev = LocEvidence(district_hints=hints(("zip", "320")), address_text="民族路二段100號")
    assert decide(ev, idx, ctx).match.road_key == f"{ZHONGLI}|民族路二段||"


def test_no_district_evidence_is_not_guessed(idx, ctx):
    """卡片上沒有區名：同名的路在兩個區都有，就不定位，也不從查詢的區推。"""
    decision = decide(LocEvidence(address_text="中正路1188號"), idx, ctx)
    assert decision.location.district is None
    assert decision.location.precision == Precision.NONE
    assert "road_in_several_districts" in decision.flags
    assert "area" not in {f.name for f in dataclasses.fields(LocEvidence)}


def test_unknown_district_gets_no_metrics(idx, ctx):
    loc = locate(LocEvidence(address_text="中正路1188號", **site(YW)), idx, ctx)
    assert loc.district == "桃園區"  # 座標附近只有桃園區的門牌
    bare = loc.model_copy(update={"district": None})
    assert area_metrics(bare, idx, ctx, "taoyuan_core") == {}
    assert area_metrics(loc, idx, ctx, "taoyuan_core")["d_yiwen_m"] == 0


def test_single_section_road_keeps_door_precision(idx, ctx):
    ev = LocEvidence(district_hints=hints(("zip", "330")), address_text="國際路3號")
    assert locate(ev, idx, ctx).precision == Precision.DOOR


def test_road_found_in_only_one_district_decides_it(idx, ctx):
    decision = decide(LocEvidence(address_text="中華路3號"), idx, ctx)
    assert decision.location.district == "八德區"
    assert [e.kind for e in decision.evidence] == ["door"]
    for area in ctx.area_ids:  # 八德區不在任何區域裡
        assert area_metrics(decision.location, idx, ctx, area) == {}


def test_road_only_in_another_district_is_a_conflict(idx, ctx):
    """地址說中壢，但這條路的門牌只在平鎮、桃園找得到。"""
    ev = LocEvidence(district_hints=hints(("zip", "320")), address_text="南平路5號")
    decision = decide(ev, idx, ctx)
    assert decision.method == "conflict"
    assert {e.district for e in decision.evidence if e.kind == "door"} == {"平鎮區", "桃園區"}


def test_unknown_zip_still_counts_as_evidence(idx, ctx):
    ev = LocEvidence(district_hints=hints(("zip", "999"), ("district", "中壢區")))
    assert decide(ev, idx, ctx).location.conflict
    alone = decide(LocEvidence(district_hints=hints(("zip", "999"))), idx, ctx)
    assert alone.location.district is None and not alone.location.conflict


def test_authoritative_tx_district(idx, ctx):
    tx = TxFacts(
        serial="RPTEST00001",
        release_id="test",
        trade_date=dt.date(2026, 1, 2),
        district="中壢區",
        address_text="桃園市中壢區新生路4號五樓",
        floor_lo=5,
        floor_hi=5,
        total_yuan=1,
    )
    ev = LocEvidence.from_tx(tx)
    decision = decide(ev, idx, ctx)
    loc = decision.location
    assert loc.precision == Precision.DOOR and loc.district == "中壢區"
    assert loc.door_id == f"{ZHONGLI}|新生路|||4|0"
    smallest = min(ctx.area("zhongli_ic").options.drive_min_options)
    assert area_metrics(loc, idx, ctx, "zhongli_ic") == {"in_district": True, "drive_min": smallest}
    assert area_metrics(loc, idx, ctx, "taoyuan_core") == {}


# ---------------------------------------------------------------- R3-07、R1-14 精度降級


@pytest.mark.parametrize(
    ("zip_code", "text", "key", "doors"),
    [
        ("320", "中壢區民族路", f"{ZHONGLI}|民族路*||", 9),
        ("320", "民族路123號", f"{ZHONGLI}|民族路*||", 9),
        ("320", "民族路3號", f"{ZHONGLI}|民族路*||", 9),
        ("320", "中園路", f"{ZHONGLI}|中園路*||", 4),
        ("320", "中豐北路", f"{ZHONGLI}|中豐北路*||", 3),
        ("330", "桃園區莊敬路", f"{TAOYUAN}|莊敬路*||", 3),
    ],
)
def test_road_without_section_uses_every_section(idx, ctx, zip_code, text, key, doors):
    decision = decide(
        LocEvidence(district_hints=hints(("zip", zip_code)), address_text=text), idx, ctx
    )
    loc = decision.location
    assert loc.precision == Precision.ROAD and loc.road_key == key
    assert "section_unknown" in decision.flags
    assert len(idx.road_doors_by_key(key, None)) == doors


def test_number_found_in_one_section_only_keeps_door_precision(idx, ctx):
    ev = LocEvidence(district_hints=hints(("zip", "320")), address_text="民族路100號")
    decision = decide(ev, idx, ctx)
    assert decision.location.precision == Precision.DOOR
    assert "section_inferred" in decision.flags and "section_unknown" not in decision.flags


def test_official_address_without_section_is_the_bare_road(idx, ctx):
    """實價登錄的地址是正式門牌：沒寫段就是不帶段的那條路，不必降級。"""
    tx = LocEvidence(district="中壢區", address_text="中壢區民族路3號", exact_address=True)
    decision = decide(tx, idx, ctx)
    assert decision.location.precision == Precision.DOOR
    assert decision.location.door_id == f"{ZHONGLI}|民族路|||3|0"
    assert "section_bare" in decision.flags


def test_road_fraction_uses_only_tall_enough_doors(idx, ctx):
    ev = LocEvidence(district_hints=hints(("zip", "330")), address_text="國際路一段", floor_hi=9)
    loc = locate(ev, idx, ctx)
    assert loc.precision == Precision.ROAD
    every = area_metrics(loc, idx, ctx, "taoyuan_core")["road_frac"]
    tall = area_metrics(loc, idx, ctx, "taoyuan_core", min_floors=ev.floor_hi)["road_frac"]
    assert set(every.values()) == {0.5} and set(tall.values()) == {1.0}
    assert list(tall) == ctx.area("taoyuan_core").road_frac_keys()
    # 篩完沒有門牌時改用不篩的
    fallback = area_metrics(loc, idx, ctx, "taoyuan_core", min_floors=40)["road_frac"]
    assert fallback == every


def test_drive_area_road_fractions(idx, ctx):
    loc = locate(LocEvidence(district_hints=hints(("zip", "320")), address_text="民族路"), idx, ctx)
    frac = area_metrics(loc, idx, ctx, "zhongli_ic")["road_frac"]
    assert list(frac) == ctx.area("zhongli_ic").road_frac_keys()
    assert frac[list(frac)[-1]] == 1.0  # 三個點位都在最大一檔內


@pytest.mark.parametrize(
    ("text", "precision", "flag"),
    [
        ("中壢區新生路6號", Precision.DOOR, "nearby_number"),  # 上下鄰號都在、彼此很近
        ("中壢區新興路4號", Precision.ROAD, "number_not_found"),  # 鄰號相距好幾公里
        ("中壢區新生路4號附近", Precision.ROAD, "fuzzy"),
        ("中壢區新生路與中正路口", Precision.ROAD, "intersection"),
        ("中壢區新生路121巷", Precision.ROAD, None),
    ],
)
def test_low_confidence_matches_are_downgraded(idx, ctx, text, precision, flag):
    decision = decide(
        LocEvidence(district_hints=hints(("zip", "320")), address_text=text), idx, ctx
    )
    assert decision.location.precision == precision
    if flag:
        assert flag in decision.flags


def test_unknown_road_has_no_location(idx, ctx):
    decision = decide(
        LocEvidence(district_hints=hints(("zip", "320")), address_text="不存在路1號"), idx, ctx
    )
    assert decision.location.precision == Precision.NONE and decision.location.district == "中壢區"


# ---------------------------------------------------------------- 網站座標


def test_site_coordinates(idx, ctx):
    decision = decide(LocEvidence(district_hints=hints(("zip", "320")), **site(Z1)), idx, ctx)
    loc = decision.location
    assert loc.precision == Precision.SITE and loc.district == "中壢區"
    assert {e.kind for e in decision.evidence} == {"coords", "polygon", "zip"}
    assert area_metrics(loc, idx, ctx, "zhongli_ic")["in_district"] is True


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        ({"site_lat": 25.30, "site_lon": 121.60}, "site_outside_city"),
        ({**site(Z1), "site_shared_n": 3}, "site_shared"),
        ({**site(Z1), "site_precise": False}, "site_imprecise"),
    ],
)
def test_suspicious_site_coordinates_are_not_used(idx, ctx, extra, flag):
    ev = LocEvidence(district_hints=hints(("zip", "320")), address_text="新生路4號", **extra)
    decision = decide(ev, idx, ctx)
    assert flag in decision.flags
    assert decision.location.precision == Precision.DOOR  # 改用門牌


def test_polygon_evidence_needs_distance_from_the_edge(ctx):
    """離區界不到 100 m 的座標不算多邊形證據（R3-01：站方座標本身有 25–50 m 誤差）。"""
    district = ctx.area("zhongli_ic").district
    edge = shapely.line_interpolate_point(shapely.boundary(district), 0.37, normalized=True)
    assert [s for s in ctx.district_sides(edge.x, edge.y) if s[0] == "中壢區"] == []
    inner = shapely.Point(Z1)
    # 桃園區的範圍也宣告了行政區界（taoyuan_district），所以兩區都會列出
    assert ctx.district_sides(inner.x, inner.y) == [("桃園區", False), ("中壢區", True)]
    assert ctx.district_sides(*P1) == [("桃園區", False), ("中壢區", False)]


def test_doors_of_one_district_decide_the_coordinates(idx, ctx):
    decision = decide(LocEvidence(**site((P1[0], P1[1] + 3))), idx, ctx)
    assert decision.location.district == "平鎮區"
    assert {(e.kind, e.district) for e in decision.evidence} == {
        ("coords", "平鎮區"),
        ("polygon", "not:中壢區"),
        ("polygon", "not:桃園區"),
    }


def test_listing_evidence_from_facts(idx, ctx):
    lon, lat = tm2_to_wgs84(*Z4)
    facts = ListingFacts(
        source="cthouse",
        source_id="T1",
        area="zhongli_ic",
        road_text="中壢區民族路二段",
        district_hints=[("zip", "320")],
        site_lat=lat,
        site_lon=lon,
        floor_lo=6,
        floor_hi=7,
    )
    ev = LocEvidence.from_listing(facts)
    assert ev.floor_hi == 7 and ev.address_text == "中壢區民族路二段"
    assert locate(ev, idx, ctx).precision == Precision.SITE


# ---------------------------------------------------------------- 社區名


@pytest.fixture(scope="module")
def osm_names():
    payload = json.loads((FIXTURES / "overpass_min.json").read_text(encoding="utf-8"))
    return OsmNames(parse_overpass(payload))


def test_community_name_locates_a_listing(idx, ctx, osm_names):
    ev = LocEvidence(
        source="rakuya",
        district_hints=hints(("zip", "330")),
        community_name="示範晴空",
        community_key="9",
    )
    decision = decide(ev, idx, ctx, communities=CommunityLookup(osm_names))
    assert decision.location.precision == Precision.COMMUNITY
    assert decision.community.method == "osm_name" and decision.community.n == 2
    metrics = area_metrics(decision.location, idx, ctx, "taoyuan_core")
    assert metrics["d_zhonglu_m"] == 0


def test_community_in_another_district_is_rejected(idx, ctx, osm_names):
    ev = LocEvidence(district_hints=hints(("zip", "320")), community_name="示範晴空")
    decision = decide(ev, idx, ctx, communities=CommunityLookup(osm_names))
    assert decision.location.precision == Precision.NONE
    assert "community_other_district" in decision.flags


def test_door_number_beats_community_name(idx, ctx, osm_names):
    ev = LocEvidence(
        district_hints=hints(("zip", "330")),
        address_text="國際路一段3號",
        community_name="示範晴空",
    )
    assert locate(ev, idx, ctx, communities=CommunityLookup(osm_names)).precision == Precision.DOOR


def test_community_name_beats_road(idx, ctx, osm_names):
    ev = LocEvidence(
        district_hints=hints(("zip", "330")), address_text="國際路一段", community_name="示範晴空"
    )
    assert (
        locate(ev, idx, ctx, communities=CommunityLookup(osm_names)).precision
        == Precision.COMMUNITY
    )


def test_cross_site_community_point(idx, ctx):
    lon, lat = tm2_to_wgs84(*YW)
    lookup = CommunityLookup(
        observations=[
            SiteObservation("cthouse", "示範藝文大廈", lat, lon),
            SiteObservation("hbhousing", "示範藝文 大廈", lat + 0.0001, lon),
        ]
    )
    ev = LocEvidence(
        source="rakuya", district_hints=hints(("zip", "330")), community_name="示範藝文大廈"
    )
    decision = decide(ev, idx, ctx, communities=lookup)
    assert decision.location.precision == Precision.COMMUNITY
    assert decision.community.method == "cross_site" and decision.community.n == 2
    # 自己這一站的座標不算「他站」
    own = LocEvidence(
        source="cthouse", district_hints=hints(("zip", "330")), community_name="示範藝文大廈"
    )
    assert decide(own, idx, ctx, communities=lookup).community.n == 1


def test_locate_returns_the_decision_location(idx, ctx):
    ev = LocEvidence(district_hints=hints(("zip", "330")), address_text="國際路一段3號")
    assert locate(ev, idx, ctx) == decide(ev, idx, ctx).location
    lon, lat = tm2_to_wgs84(*ZL)
    assert locate(ev, idx, ctx).lat == pytest.approx(lat, abs=0.001)
    assert locate(ev, idx, ctx).lon == pytest.approx(lon, abs=0.001)
