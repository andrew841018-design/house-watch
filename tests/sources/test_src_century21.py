"""21世紀轉接器：請求只用公開範圍、列表（ul#product_list 錨點、分頁連結）、詳細頁（標籤／值）、
被擋。fixtures 是合成樣本，數字都是示意值。"""

import datetime as dt
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup

from house_watch.errors import ErrorCode
from house_watch.models import BType, Elevator, ListingFacts, MainKind, Parking
from house_watch.sources.base import (
    AreaQuery,
    Blocked,
    DistrictRef,
    PageInvalid,
    narrow_built,
    parse_age_text,
)
from house_watch.sources.century21 import (
    HOST,
    Century21Adapter,
    _pairs,
    detail_elevator,
    detail_parking,
    money_yuan,
)

REF = dt.date(2026, 11, 21)
SITE = "century21"
DETAIL = f"https://{HOST}/buypage/{{id}}"


@pytest.fixture
def adapter():
    return Century21Adapter()


@pytest.fixture
def page1(adapter, fx, query):
    return adapter.parse_list(fx.resp(SITE, "list_p1.html"), query)


def items(page):
    return {facts.source_id: facts for facts in page.items}


def detail_resp(fx, name: str, source_id: str):
    return fx.resp(SITE, name, url=DETAIL.format(id=source_id))


def changed_fields(before: ListingFacts, after: ListingFacts) -> set[str]:
    old, new = before.model_dump(), after.model_dump()
    return {name for name in new if new[name] != old[name]}


# ---------------------------------------------------------------- 請求


def test_list_request_uses_only_district_purpose_and_public_price_cap(adapter, query, public):
    spec = adapter.list_request(query, 1, "full")
    assert spec.method == "GET" and spec.kind == "list"
    parts = urlsplit(spec.url)
    zips = "_".join(d.zip for d in query.districts)
    assert (parts.scheme, parts.hostname) == ("https", HOST)
    assert parts.path == f"/buy/taoyuan-city/{zips}/residence"
    params = parse_qs(parts.query)
    cap = public.scope.listing.max_price_wan
    assert params == {"priceFilter": [f"below-{cap}"], "order": ["price-low-to-high"], "page": ["1"]}
    for key in ("houseAge", "areaSizeFilter", "houseShape", "unitPrice", "direction", "keyword"):
        assert key not in params, "屋齡、坪數、型態都不在伺服器端篩（R1-11）"
    assert spec.header_dict() == {"Accept": "text/html"}
    assert parse_qs(urlsplit(adapter.list_request(query, 4, "full").url).query)["page"] == ["4"]


def test_probe_uses_newest_listed_and_full_uses_price_ascending(adapter, query):
    assert (adapter.caps.stable_sort, adapter.caps.probe_sort) == ("price-low-to-high", "newly")
    assert parse_qs(urlsplit(adapter.list_request(query, 1, "probe").url).query)["order"] == ["newly"]


def test_two_districts_are_joined_with_an_underscore(adapter):
    two = AreaQuery(area="x", districts=(DistrictRef("桃園區", "330"), DistrictRef("中壢區", "320")), max_price_wan=2500)
    assert urlsplit(adapter.list_request(two, 1, "full").url).path == "/buy/taoyuan-city/330_320/residence"


def test_list_request_rejects_bad_input(adapter, query):
    with pytest.raises(ValueError):
        adapter.list_request(query, 0, "full")
    with pytest.raises(ValueError):
        adapter.list_request(AreaQuery(area="x", districts=query.districts, max_price_wan=1, city="某市"), 1, "full")
    with pytest.raises(ValueError):
        adapter.list_request(AreaQuery(area="x", districts=(DistrictRef("桃園區", "33/0"),), max_price_wan=1), 1, "full")


def test_caps_hosts_and_robots(adapter):
    caps = adapter.caps
    assert (caps.page_size, caps.has_total) == (20, False)
    assert adapter.hosts == frozenset({HOST}), "不連 new.century21.com.tw，也不跟隨外部連結"
    assert adapter.robots_urls == (f"https://{HOST}/robots.txt",)
    assert adapter.scope_version == "1"


# ---------------------------------------------------------------- 列表


def test_page_counts(page1):
    assert (page1.reported_total, page1.has_next) == (None, True), "沒有總數；下一頁看分頁連結"
    assert len(page1.items) == 6
    assert page1.skipped == 1, "有 /buypage/ 連結、但編號不合法的卡片算 skipped"


def test_elevator_building_with_parking_in_price(page1, query):
    a = items(page1)["9000001"]
    assert (a.source, a.area) == (SITE, query.area)
    assert a.price_yuan == 11_350_000
    assert a.price_excl_parking is False, "價格旁寫(含車位價)（R1-13）"
    assert a.site_original_price_yuan is None
    assert (a.main_cp, a.main_kind, a.title_area_cp) == (None, MainKind.MAIN, 4408), "主建物只在詳細頁"
    assert (a.floor_lo, a.floor_hi, a.floors_total) == (9, 9, 15)
    assert (a.btype, a.elevator) == (BType.BUILDING, Elevator.INFERRED)
    assert a.built == parse_age_text("6.7 年", REF)
    assert a.parking is Parking.UNKNOWN, "車位型式只在詳細頁"
    assert a.road_text == "示意路一段"
    assert a.district_hints == [("address", "桃園區")]
    assert a.layout == "3房(室)2廳2衛"
    assert (a.site_lat, a.site_lon) == (None, None)


def test_discounted_mansion(page1):
    b = items(page1)["9000002"]
    assert (b.price_yuan, b.site_original_price_yuan) == (9_250_000, 9_680_000), "原價只顯示（R1-22）"
    assert b.price_excl_parking is None, "沒寫含車位價：不知道"
    assert (b.btype, b.elevator) == (BType.MANSION, Elevator.INFERRED)
    assert (b.floor_lo, b.floors_total, b.title_area_cp) == (5, 7, 3190)
    assert b.built == parse_age_text("33.9 年", REF)


def test_townhouse_card_without_layout(page1):
    c = items(page1)["9000003"]
    assert c.btype is BType.TOWNHOUSE and c.elevator is Elevator.UNKNOWN
    assert c.layout is None, "透天的卡片少一個 span：依文字樣式判斷，不靠位置"
    assert (c.floor_lo, c.floor_hi, c.floors_total) == (1, 4, 4)
    assert c.title_area_cp == 5840
    assert c.built == parse_age_text("41.0 年", REF)


def test_shop_is_other(page1):
    e = items(page1)["9000005"]
    assert e.btype is BType.OTHER and e.elevator is Elevator.UNKNOWN
    assert e.price_yuan == 31_500_000


def test_card_with_missing_fields_and_a_relative_link(page1):
    f = items(page1)["9000007"]
    assert (f.btype, f.price_yuan) == (BType.BUILDING, 14_650_000)
    assert (f.title_area_cp, f.floor_lo, f.floors_total, f.layout) == (None, None, None, None)
    assert f.built.lo is None and f.built.hi is None


def test_malicious_strings_stay_plain_text(adapter, page1):
    d = items(page1)["9000004"]
    assert d.road_text == "javascript:alert(1)路"
    assert d.district_hints == [("address", "中壢區")], "地址開頭的區名照實記，不從查詢推定（R3-01）"
    assert d.price_excl_parking is True, "標題寫車位另售"
    assert adapter.detail_request(d.source_id).url == f"https://{HOST}/buypage/9000004"
    dumped = json.dumps([f.model_dump(mode="json") for f in page1.items], ensure_ascii=False)
    assert "onerror" not in dumped and "示意標題" not in dumped, "標題不保存"
    assert "<script>" not in dumped and "示意國小" not in dumped, ".community 不可靠，不當社區名用"
    assert all(f.community_name is None for f in page1.items)


def test_ads_duplicates_and_broken_cards(page1):
    assert set(items(page1)) == {"9000001", "9000002", "9000003", "9000004", "9000005", "9000007"}
    assert "9000006" not in items(page1), "別的主機的 /buypage/ 連結不算"


def test_parse_list_is_pure(adapter, fx, query):
    assert adapter.parse_list(fx.resp(SITE, "list_p1.html"), query) == adapter.parse_list(
        fx.resp(SITE, "list_p1.html"), query
    )


def test_last_page(adapter, fx, query):
    page = adapter.parse_list(fx.resp(SITE, "list_last.html"), query)
    assert (page.reported_total, page.has_next, len(page.items)) == (None, False, 1), (
        "只看分頁列的下一頁連結，<head> 的 link rel=next 不算"
    )


def test_zero_results_is_a_valid_empty_page(adapter, fx, query):
    page = adapter.parse_list(fx.body_resp(b'<ul id="product_list"></ul>'), query)
    assert (page.items, page.reported_total, page.has_next) == ([], None, False)


@pytest.mark.parametrize("name", ["list_invalid.html", "detail.html"])
def test_invalid_pages(adapter, fx, query, name):
    with pytest.raises(PageInvalid) as caught:
        adapter.parse_list(fx.resp(SITE, name), query)
    assert caught.value.code is ErrorCode.PAGE_INVALID


def test_non_200_is_invalid(adapter, fx, query):
    with pytest.raises(PageInvalid):
        adapter.parse_list(fx.resp(SITE, "list_p1.html", status=500), query)


def test_money_needs_a_unit_and_a_sane_range():
    assert money_yuan("1,088 萬") == 10_880_000
    assert money_yuan("1,088") is None
    assert money_yuan("") is None


# ---------------------------------------------------------------- 詳細頁


def test_needs_detail(adapter, page1, public, fx):
    found = items(page1)
    assert adapter.needs_detail(found["9000001"]) is True, "主建物、車位型式、電梯都要看詳細頁"
    scoped = Century21Adapter(scope=public.scope.listing)
    assert scoped.needs_detail(found["9000003"]) is False, "透天不在公開範圍內"
    assert scoped.needs_detail(found["9000005"]) is False, "店面（其他）不在公開範圍內"
    assert scoped.needs_detail(found["9000002"]) is True
    detailed = adapter.parse_detail(detail_resp(fx, "detail.html", "9000001"), found["9000001"])
    assert adapter.needs_detail(detailed) is False


def test_detail_request(adapter):
    spec = adapter.detail_request("9000001")
    assert (spec.method, spec.url, spec.kind) == ("GET", f"https://{HOST}/buypage/9000001", "detail")
    for bad in ("../1", "abc", "0123"):
        with pytest.raises(ValueError):
            adapter.detail_request(bad)


def test_detail_fills_main_area_parking_and_elevator(adapter, page1, fx):
    base = items(page1)["9000001"]
    detailed = adapter.parse_detail(detail_resp(fx, "detail.html", "9000001"), base)
    assert (detailed.main_cp, detailed.main_kind) == (2961, MainKind.MAIN), "主建物，不含公設車位"
    assert detailed.parking is Parking.RAMP_FLAT, "「有 (坡道-平面)」"
    assert detailed.elevator is Elevator.YES, "2 部"
    assert detailed.built == narrow_built(base.built, parse_age_text("6.7 年", REF))
    assert detailed.price_yuan == base.price_yuan and detailed.title_area_cp == base.title_area_cp


def test_detail_without_parking_or_elevator(adapter, page1, fx):
    base = items(page1)["9000002"]
    detailed = adapter.parse_detail(detail_resp(fx, "detail_other.html", "9000002"), base)
    assert detailed.parking is Parking.NONE
    assert detailed.elevator is Elevator.NO, "標籤是「電梯部數」、0 部"
    assert detailed.main_cp == 2286


def test_detail_changes_only_detail_fields(adapter, page1, fx):
    found = items(page1)
    for source_id, name in (("9000001", "detail.html"), ("9000002", "detail_other.html")):
        base = found[source_id]
        changed = changed_fields(base, adapter.parse_detail(detail_resp(fx, name, source_id), base))
        assert changed and changed <= adapter.caps.detail_fields, changed - adapter.caps.detail_fields


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("detail.html", DETAIL.format(id="9000002")),  # 分享連結是別的編號
        ("detail_other.html", DETAIL.format(id="9000001")),  # 回應網址是別的編號
        ("detail_other.html", f"https://{HOST}/buy/taoyuan-city"),  # 被轉到列表
        ("list_invalid.html", DETAIL.format(id="9000002")),  # 沒有分享連結（錨點不在）
    ],
)
def test_detail_of_another_listing_is_invalid(adapter, page1, fx, name, url):
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, name, url=url), items(page1)["9000002"])


def test_detail_non_200_is_invalid(adapter, page1, fx):
    resp = fx.resp(SITE, "detail_other.html", url=DETAIL.format(id="9000002"), status=404)
    with pytest.raises(PageInvalid):
        adapter.parse_detail(resp, items(page1)["9000002"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("有 (坡道-平面)", Parking.RAMP_FLAT),
        ("有 (坡道-機械)", Parking.MECHANICAL),
        ("有 (平面)", Parking.FLAT_UNKNOWN_ACCESS),
        ("有（升降-平面）", Parking.LIFT_FLAT),
        ("有", Parking.UNKNOWN),
        ("無", Parking.NONE),
        ("", Parking.UNKNOWN),
        (None, Parking.UNKNOWN),
    ],
)
def test_detail_parking_text(text, expected):
    assert detail_parking(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1 部", Elevator.YES), ("3部", Elevator.YES), ("0 部", Elevator.NO), ("有", Elevator.YES),
     ("無", Elevator.NO), ("－", None), ("示意", None)],
)
def test_detail_elevator_text(text, expected):
    assert detail_elevator(text) is expected


def test_only_wanted_labels_are_read():
    html = (
        '<div class="w-half"><h6><span>經紀人</span></h6><div class="val">示意姓名</div></div>'
        '<div class="w-half"><h6><span>屋齡</span></h6><div class="val">6.7 年</div></div>'
        '<div class="w-half"><h6><span>屋齡</span></h6><div class="val">99 年</div></div>'
    )
    assert _pairs(BeautifulSoup(html, "html.parser")) == {"屋齡": "6.7 年"}


# ---------------------------------------------------------------- 透過抓取層


def _robots(request):
    return httpx.Response(200, text="User-agent: *\nDisallow:\n")


def test_full_scan_pages_through_the_fetcher(adapter, fx, query, make_fetcher, policy_for):
    bodies = {"1": fx.bytes(SITE, "list_p1.html"), "3": fx.bytes(SITE, "list_last.html")}
    sent = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return _robots(request)
        sent.append(request.url.params["page"])
        return httpx.Response(200, content=bodies[request.url.params["page"]])

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    pages = [
        adapter.parse_list(fetcher.fetch(adapter.list_request(query, n, "full"), source=SITE, deadline=1e9), query)
        for n in (1, 3)
    ]
    assert [p.has_next for p in pages] == [True, False]
    assert sent == ["1", "3"]


def test_blocked_response_pauses_the_source(adapter, fx, query, make_fetcher, policy_for):
    def handler(request):
        if request.url.path == "/robots.txt":
            return _robots(request)
        return fx.envelope(SITE, "blocked.json")

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert caught.value.code is ErrorCode.BLOCKED
    with pytest.raises(Blocked) as again:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert again.value.code is ErrorCode.PAUSED
