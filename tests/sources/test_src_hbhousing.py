"""住商轉接器：請求只用公開範圍、列表解析、__NUXT_DATA__（devalue）解碼、錨點、被擋。"""

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from house_watch.errors import ErrorCode
from house_watch.models import BType, DateRange, Elevator, ListingFacts, MainKind, Parking
from house_watch.sources.base import (
    AreaQuery,
    Blocked,
    PageInvalid,
    built_from_decimal_years,
    to_decimal,
)
from house_watch.sources.hbhousing import (
    HOST,
    LIST_API,
    PAGE_ROWS,
    HbhousingAdapter,
    devalue_decode,
    find_house,
)

REF = dt.date(2026, 11, 21)
SITE = "hbhousing"


@pytest.fixture
def adapter():
    return HbhousingAdapter()


@pytest.fixture
def page1(adapter, fx, query):
    return adapter.parse_list(fx.resp(SITE, "list_p1.json"), query)


def items(page):
    return {facts.source_id: facts for facts in page.items}


# ---------------------------------------------------------------- 請求


def test_list_request_uses_only_city_zip_and_public_price_cap(adapter, query, public):
    spec = adapter.list_request(query, 3, "full")
    assert spec.method == "POST" and spec.url == LIST_API
    body = spec.json_payload()
    assert body["page"] == 3 and body["pageRows"] == PAGE_ROWS == 50
    assert body["cityNo"] == 5
    assert body["zipCode"] == [d.zip for d in query.districts]
    assert body["priceFinish"] == public.scope.listing.max_price_wan
    assert body["sort"] == 8
    assert body["style"] == [] and body["type"] == [] and body["tag"] == [] and body["theme"] == []
    used = {"page", "pageRows", "cityNo", "zipCode", "priceFinish", "sort", "vrType", "areaType"}
    assert all(value in (None, []) for key, value in body.items() if key not in used), (
        "型態、坪數、屋齡、樓層、車位都不在伺服器端篩（R1-11）"
    )
    headers = spec.header_dict()
    assert headers["Origin"] == f"https://{HOST}"
    assert "User-Agent" not in headers


def test_probe_and_full_use_listed_newest_first(adapter, query):
    assert adapter.list_request(query, 1, "probe").json_payload() == adapter.list_request(
        query, 1, "full"
    ).json_payload()


def test_unknown_city_is_an_error(adapter):
    with pytest.raises(ValueError):
        adapter.list_request(AreaQuery(area="x", districts=(), max_price_wan=1, city="某市"), 1, "full")


# ---------------------------------------------------------------- 列表


def test_page_counts(page1):
    assert page1.reported_total == 120
    assert len(page1.items) == 7 and page1.skipped == 1
    assert page1.has_next is False, "這頁不滿 50 筆，就是最後一頁"


def test_full_page_has_next(adapter, fx, query):
    doc = fx.json(SITE, "list_p1.json")
    row = doc["data"]["buyHouseListDatas"][0]
    doc["data"]["buyHouseListDatas"] = [dict(row, sn=f"CS{100200 + n}") for n in range(PAGE_ROWS)]
    page = adapter.parse_list(fx.body_resp(json.dumps(doc).encode()), query)
    assert len(page.items) == PAGE_ROWS and page.has_next is True


def test_own_listing(page1, query):
    a = items(page1)["CS100123"]
    assert (a.source, a.area) == (SITE, query.area)
    assert (a.price_yuan, a.site_original_price_yuan) == (12_880_000, 13_500_000)
    assert (a.main_cp, a.main_kind, a.title_area_cp) == (2650, MainKind.MAIN, 4010)
    assert (a.floor_lo, a.floor_hi, a.floors_total) == (9, 9, 17)
    assert (a.btype, a.elevator) == (BType.BUILDING, Elevator.INFERRED)
    assert a.built == built_from_decimal_years(to_decimal(8.4), REF)
    assert (a.parking, a.parking_public) == (Parking.UNKNOWN, False), "列表只有權屬，型式要看詳細頁"
    assert a.price_excl_parking is None
    assert a.district_hints == [("zip", "330"), ("address", "桃園區")]
    assert a.brand_origin_id is None
    assert a.layout == "3房(室)2廳2衛"


def test_cthouse_origin_listing(page1):
    b = items(page1)["c1234567"]
    assert b.brand_origin_id == "cthouse:1234567"
    assert (b.floor_lo, b.floor_hi, b.floors_total) == (-1, 1, 7)
    assert b.btype is BType.MANSION
    assert b.parking is Parking.UNKNOWN, "權屬寫無、標題卻提到車位"
    assert b.built == built_from_decimal_years(Decimal("31"), REF, decimals=1), "31 其實是 31.0"


def test_ownership_words(page1):
    found = items(page1)
    assert (found["s1234AB"].parking_public, found["s1234AB"].floors_total) == (True, None)
    assert (found["s1234AB"].floor_lo, found["s1234AB"].floor_hi) == (1, 2)
    assert found["pS1000001"].parking_public is True, "抽籤當公有處理"
    assert (found["pS1000001"].btype, found["pS1000001"].elevator) == (BType.WALKUP, Elevator.NO)
    assert found["AS100002"].parking_public is None


def test_district_evidence_is_kept_as_is(page1):
    """R3-01 的兩個真實寫法：郵遞區號和門牌欄矛盾、門牌欄有兩個區名。"""
    e = items(page1)["AS100002"]
    assert e.district_hints == [("zip", "320"), ("address", "八德區")]
    assert e.road_text == "中華路<img src=x onerror=alert(1)>"
    f = items(page1)["pS1000002"]
    assert f.district_hints == [("zip", "320"), ("address", "中壢區"), ("address", "桃園區")]
    assert f.road_text == "莊敬路"
    assert f.price_excl_parking is True
    assert f.built == DateRange()


def test_missing_price_is_kept_not_dropped(page1):
    h = items(page1)["CS100124"]
    assert h.price_yuan is None
    assert h.btype is BType.TOWNHOUSE
    assert (h.floor_lo, h.floor_hi, h.floors_total) == (1, 4, 4)


def test_titles_are_not_stored(page1):
    dumped = json.dumps([f.model_dump(mode="json") for f in page1.items], ensure_ascii=False)
    assert "示意標題" not in dumped


def test_last_page(adapter, fx, query):
    page = adapter.parse_list(fx.resp(SITE, "list_last.json"), query)
    assert (page.reported_total, page.has_next, len(page.items)) == (1, False, 1)


@pytest.mark.parametrize("name", ["list_error.json", "detail_no_nuxt.html"])
def test_invalid_list_pages(adapter, fx, query, name):
    with pytest.raises(PageInvalid):
        adapter.parse_list(fx.resp(SITE, name), query)


# ---------------------------------------------------------------- 詳細頁


def test_needs_detail(adapter, page1, public):
    found = items(page1)
    assert adapter.needs_detail(found["CS100123"]) is True
    assert adapter.needs_detail(found["s1234AB"]) is False, "信義原品牌不抓詳細頁"
    scoped = HbhousingAdapter(scope=public.scope.listing)
    assert scoped.needs_detail(found["CS100124"]) is False, "透天不在公開範圍內"


def test_detail_request(adapter):
    spec = adapter.detail_request("CS100123")
    assert (spec.method, spec.url) == ("GET", f"https://{HOST}/detail?sn=CS100123")
    with pytest.raises(ValueError):
        adapter.detail_request("../x")


def test_detail_fills_parking_elevator_completion_and_community(adapter, page1, fx):
    base = items(page1)["CS100123"]
    detailed = adapter.parse_detail(fx.resp(SITE, "detail.html"), base)
    assert detailed.parking is Parking.RAMP_FLAT
    assert detailed.elevator is Elevator.YES
    assert detailed.price_excl_parking is False, "詳細頁在總價旁寫「含車位價」"
    assert detailed.community_name == "</script><script>alert(1)</script>示意社區"
    assert detailed.community_key == "C000123"
    assert detailed.price_yuan == base.price_yuan
    # 完工日精確到日；和列表的一位小數區間沒有交集時採用詳細頁的
    assert detailed.built == DateRange(lo=dt.date(2017, 6, 15), hi=dt.date(2017, 6, 15))
    assert adapter.needs_detail(detailed) is False


def test_detail_without_the_listing_is_invalid(adapter, fx):
    other = ListingFacts(source=SITE, source_id="CS999999", area="a")
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail.html"), other)
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail_no_nuxt.html"), other)


# ---------------------------------------------------------------- devalue 解碼


def test_devalue_decodes_nuxt_wrappers_and_specials():
    values = [
        ["ShallowReactive", 1],
        {"a": 2, "b": -1, "c": 3, "d": 4, "e": 5, "f": 6},
        ["Reactive", 7],
        ["Set", 8, 8],
        ["Map", 8, 9],
        ["Date", "2026-01-02T00:00:00.000Z"],
        [9, -2, 9],
        {"x": 9},
        "k",
        -1,
    ]
    assert devalue_decode(values) == {
        "a": {"x": -1},
        "b": None,
        "c": ["k", "k"],
        "d": {"k": -1},
        "e": "2026-01-02T00:00:00.000Z",
        "f": [-1, None, -1],
    }


def test_devalue_handles_cycles_and_rejects_bad_input():
    cyclic = devalue_decode([{"self": 0, "n": 1}, 5])
    assert cyclic["self"] is cyclic and cyclic["n"] == 5
    for bad in ([], {"a": 1}, [{"a": 9}], [{"a": "1"}], [["Ref", 0]]):
        with pytest.raises(PageInvalid):
            devalue_decode(bad)


def test_devalue_depth_is_limited():
    deep = [[n + 1] for n in range(200)] + [1]
    with pytest.raises(PageInvalid):
        devalue_decode(deep)


def test_find_house_needs_matching_sn():
    doc = {"data": {"k1": 1, "k2": {"sn": "CS1", "parkingMethod": None}}, "other": {"sn": "CS1"}}
    assert find_house(doc, "CS1") == {"sn": "CS1", "parkingMethod": None}
    assert find_house(doc, "CS2") is None


# ---------------------------------------------------------------- 透過抓取層


def test_rate_limited_response_pauses_the_source(adapter, fx, query, make_fetcher, policy_for, conn):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return fx.envelope(SITE, "blocked.json")

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_s == 24 * 3600, "Retry-After 一小時比 24 小時短，照倍增的時間"
