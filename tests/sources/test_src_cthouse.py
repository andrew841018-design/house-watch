"""中信轉接器：請求只用公開範圍、列表與詳細頁解析、錨點、被擋。fixtures 是合成樣本。"""

import datetime as dt
import json

import httpx
import pytest

from house_watch.errors import ErrorCode
from house_watch.models import BType, DateRange, Elevator, ListingFacts, MainKind, Parking
from house_watch.sources.base import (
    AreaQuery,
    Blocked,
    DistrictRef,
    PageInvalid,
    built_from_floor_years,
    built_from_years_months,
)
from house_watch.sources.cthouse import HOST, LIST_API, CthouseAdapter

REF = dt.date(2026, 11, 21)
SITE = "cthouse"


@pytest.fixture
def adapter():
    return CthouseAdapter()


@pytest.fixture
def page1(adapter, fx, query):
    return adapter.parse_list(fx.resp(SITE, "list_p1.json"), query)


def items(page):
    return {facts.source_id: facts for facts in page.items}


# ---------------------------------------------------------------- 請求


def test_list_request_uses_only_district_and_public_price_cap(adapter, query, public):
    spec = adapter.list_request(query, 1, "full")
    assert spec.method == "POST" and spec.url == LIST_API
    district = query.districts[0].name
    cap = public.scope.listing.max_price_wan
    assert spec.json_payload() == {"arg": f"桃園市-city/{district}-town/0-{cap}-price/", "page": "1"}
    headers = spec.header_dict()
    assert headers["Origin"] == f"https://{HOST}"
    assert headers["Referer"].startswith(f"https://{HOST}/area/")
    assert "User-Agent" not in headers
    assert adapter.list_request(query, 2, "full").json_payload()["page"] == "2"


def test_probe_uses_the_newest_first_default_sort(adapter, query):
    assert adapter.caps.probe_sort == adapter.caps.stable_sort == "0"
    assert adapter.list_request(query, 1, "probe").json_payload() == adapter.list_request(
        query, 1, "full"
    ).json_payload()


def test_list_request_rejects_bad_input(adapter, query):
    with pytest.raises(ValueError):
        adapter.list_request(query, 0, "full")
    two = AreaQuery(area="x", districts=(DistrictRef("桃園區", "330"), DistrictRef("中壢區", "320")), max_price_wan=1)
    with pytest.raises(ValueError):
        adapter.list_request(two, 1, "full")


def test_caps_and_hosts(adapter):
    caps = adapter.caps
    assert (caps.page_size, caps.has_total) == (20, True)
    assert caps.detail_fields == frozenset({"built"})
    assert adapter.hosts == frozenset({HOST})
    assert adapter.robots_urls == (f"https://{HOST}/robots.txt",)


# ---------------------------------------------------------------- 列表


def test_page_counts(page1):
    assert page1.reported_total == 45
    assert page1.has_next is True
    assert len(page1.items) == 7
    assert page1.skipped == 1, "編號不合法的卡片算 skipped，不默默消失"


def test_own_listing_with_ramp_flat_parking(page1, query):
    a = items(page1)["1234567"]
    assert (a.source, a.area) == (SITE, query.area)
    assert a.price_yuan == 12_880_000
    assert a.site_original_price_yuan == 13_500_000
    assert (a.main_cp, a.main_kind, a.title_area_cp) == (2735, MainKind.MAIN, 4106)
    assert (a.floor_lo, a.floor_hi, a.floors_total) == (7, 7, 16)
    assert (a.btype, a.elevator) == (BType.BUILDING, Elevator.YES)
    assert a.parking is Parking.RAMP_FLAT
    assert a.price_excl_parking is None and a.parking_price_yuan is None
    assert a.built == built_from_floor_years(9, REF)
    assert (a.site_lat, a.site_lon) == (24.9931, 121.3012)
    assert a.district_hints == [("zip", "330"), ("address", "桃園區")]
    assert a.brand_origin_id == "cthouse:1234567"
    assert a.layout == "3房2廳2衛"


def test_malicious_strings_stay_plain_data(page1):
    a = items(page1)["1234567"]
    assert a.community_name == "<img src=x onerror=alert(1)>示意社區"
    assert a.community_key == "20190101000000001"
    assert a.road_text == "中正路</script>"
    g = items(page1)["1234003"]
    assert g.road_text == "javascript:alert(1)路"


def test_parking_price_means_total_includes_parking(page1):
    b = items(page1)["1234560"]
    assert b.parking_price_yuan == 1_600_000
    assert b.price_excl_parking is False
    assert b.parking is Parking.GROUND_FLAT
    assert (b.floor_lo, b.floor_hi, b.floors_total) == (1, 2, 9)
    assert b.btype is BType.MANSION
    assert b.built == DateRange(), "0 年當成未知（r4 §3.10），詳細頁有「0年N個月」"
    assert b.site_original_price_yuan is None


def test_other_brand_listing(page1):
    c = items(page1)["50123456"]
    assert (c.btype, c.elevator) == (BType.WALKUP, Elevator.NO)
    assert (c.floor_lo, c.floor_hi, c.floors_total) == (6, 6, None), "「共0樓」是未知"
    assert c.built == DateRange()
    assert c.parking is Parking.UNKNOWN, "他牌物件沒有車位欄位：未知，不是沒有"
    assert c.brand_origin_id is None, "8 位數是他牌物件，不知道原品牌編號"
    assert c.road_text == "中山路"
    assert c.district_hints == [("zip", "330")]


def test_presale_without_coordinates(page1):
    d = items(page1)["80123456"]
    assert d.built == DateRange(lo=REF, hi=None)
    assert (d.site_lat, d.site_lon) == (None, None)
    assert d.btype is BType.BUILDING
    assert d.road_text is None and d.district_hints == [("zip", "330"), ("address", "桃園區")]


def test_elevator_residence_height_without_total_floors(adapter, fx, query):
    doc = fx.json(SITE, "list_p1.json")
    house = dict(doc["houses"][0], floor_val="15樓/共0樓")
    low = dict(doc["houses"][0], id=1234568, floor_val="6樓/共0樓")
    doc["houses"] = [house, low]
    page = adapter.parse_list(fx.body_resp(json.dumps(doc).encode()), query)
    high_facts, low_facts = page.items
    assert (high_facts.floors_total, high_facts.btype) == (None, BType.BUILDING), "物件在 11 樓以上"
    assert (low_facts.floors_total, low_facts.btype) == (None, None)
    assert low_facts.elevator is Elevator.YES


def test_land_listing(page1):
    e = items(page1)["1234001"]
    assert e.btype is BType.OTHER
    assert (e.main_cp, e.title_area_cp) == (None, None)
    assert (e.floor_lo, e.floors_total) == (None, None)
    assert e.built == DateRange()


def test_mixed_parking_exclusion_phrase_and_two_districts(page1):
    f = items(page1)["1234002"]
    assert f.price_yuan == 12_861_000
    assert f.parking is Parking.MIXED_WITH_FLAT
    assert f.price_excl_parking is True, "標題寫車位另售、又沒有車位價"
    assert f.road_text == "莊敬路"
    assert f.district_hints == [("zip", "320"), ("address", "中壢區"), ("address", "桃園區")]


def test_no_parking_contradicted_by_title_is_unknown(page1):
    assert items(page1)["1234003"].parking is Parking.UNKNOWN
    assert items(page1)["1234001"].parking is Parking.NONE


def test_titles_are_not_stored(page1):
    dumped = json.dumps([f.model_dump(mode="json") for f in page1.items], ensure_ascii=False)
    assert "示意標題" not in dumped


def test_parse_list_is_pure(adapter, fx, query):
    first = adapter.parse_list(fx.resp(SITE, "list_p1.json"), query)
    second = adapter.parse_list(fx.resp(SITE, "list_p1.json"), query)
    assert first == second


def test_last_page(adapter, fx, query):
    page = adapter.parse_list(fx.resp(SITE, "list_p3_last.json"), query)
    assert page.has_next is False and page.reported_total == 45
    assert page.items[0].parking is Parking.UNKNOWN, "有車位但型式沒填"


def test_zero_results_is_a_valid_empty_page(adapter, fx, query):
    page = adapter.parse_list(fx.resp(SITE, "list_zero.json"), query)
    assert (page.items, page.reported_total, page.has_next) == ([], 0, False)


@pytest.mark.parametrize(
    "name", ["list_session_lost.json", "list_error.json", "maintenance.html"]
)
def test_invalid_pages(adapter, fx, query, name):
    with pytest.raises(PageInvalid) as caught:
        adapter.parse_list(fx.resp(SITE, name), query)
    assert caught.value.code is ErrorCode.PAGE_INVALID


def test_non_200_is_invalid(adapter, fx, query):
    with pytest.raises(PageInvalid):
        adapter.parse_list(fx.resp(SITE, "list_p1.json", status=404), query)


# ---------------------------------------------------------------- 詳細頁


def test_needs_detail_only_for_year_wide_ages(adapter, page1, public):
    found = items(page1)
    assert adapter.needs_detail(found["1234567"]) is True
    assert adapter.needs_detail(found["1234560"]) is True, "0 年要看詳細頁的「0年N個月」"
    assert adapter.needs_detail(found["50123456"]) is False, "信義原品牌不抓詳細頁"
    assert adapter.needs_detail(found["80123456"]) is False, "預售屋沒有區間可縮"
    assert adapter.needs_detail(found["1234001"]) is False, "土地不抓"
    scoped = CthouseAdapter(scope=public.scope.listing)
    assert scoped.needs_detail(found["1234001"]) is False, "土地不在公開範圍內"


def test_detail_request(adapter):
    spec = adapter.detail_request("1234567")
    assert (spec.method, spec.url, spec.kind) == ("GET", f"https://{HOST}/house/1234567.html", "detail")
    with pytest.raises(ValueError):
        adapter.detail_request("../1")


@pytest.mark.parametrize("name", ["detail.html", "detail_li_only.html"])
def test_detail_narrows_age_to_the_month(adapter, page1, fx, name):
    base = items(page1)["1234567"]
    detailed = adapter.parse_detail(fx.resp(SITE, name), base)
    assert detailed.built == built_from_years_months(9, 4, REF)
    assert detailed.model_dump(exclude={"built"}) == base.model_dump(exclude={"built"})
    assert adapter.needs_detail(detailed) is False


def test_detail_of_another_listing_or_blank_page_is_invalid(adapter, fx):
    other = ListingFacts(source=SITE, source_id="1234560", area="a")
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail.html"), other)
    mine = ListingFacts(source=SITE, source_id="1234567", area="a")
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail_blank.html"), mine)


# ---------------------------------------------------------------- 透過抓取層


def test_full_scan_pages_through_the_fetcher(adapter, fx, query, make_fetcher, policy_for):
    bodies = {1: fx.bytes(SITE, "list_p1.json"), 3: fx.bytes(SITE, "list_p3_last.json")}
    sent = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(200, content=bodies[int(payload["page"])])

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    pages = [
        adapter.parse_list(fetcher.fetch(adapter.list_request(query, n, "full"), source=SITE, deadline=1e9), query)
        for n in (1, 3)
    ]
    assert [p.has_next for p in pages] == [True, False]
    assert [p["page"] for p in sent] == ["1", "3"]


def test_blocked_response_pauses_the_source(adapter, fx, query, make_fetcher, policy_for):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return fx.envelope(SITE, "blocked.json")

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert caught.value.code is ErrorCode.BLOCKED
    with pytest.raises(Blocked) as again:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert again.value.code is ErrorCode.PAUSED
