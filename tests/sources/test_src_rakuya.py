"""樂屋網轉接器：請求只用公開範圍、列表（window.sellSearch 錨點與條件回顯）、詳細頁
（window.itemInfo）、被擋。fixtures 是合成樣本，數字都是示意值。"""

import datetime as dt
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from house_watch.errors import ErrorCode
from house_watch.models import BType, DateRange, Elevator, ListingFacts, MainKind, Parking
from house_watch.sources.base import (
    AreaQuery,
    Blocked,
    DistrictRef,
    PageInvalid,
    built_presale,
    narrow_built,
    parse_age_text,
)
from house_watch.sources.rakuya import (
    COMMUNITY_HOST,
    LIST_URL,
    WWW_HOST,
    RakuyaAdapter,
    detail_parking,
    money_yuan,
    window_json,
)

REF = dt.date(2026, 11, 21)
SITE = "rakuya"
A, B, C, D, E = (f"0aaaaaa0000000{n}" for n in range(1, 6))
# robots.txt 擋的路徑（R2a §4.1）：轉接器組出的網址一律不能碰到。
ROBOTS_BLOCKED = ("/api/", "/ajax/", "/sell_item/api/", "/search/", "/doc/", "/item/map/")


@pytest.fixture
def adapter():
    return RakuyaAdapter()


@pytest.fixture
def page1(adapter, fx, query):
    return adapter.parse_list(fx.resp(SITE, "list_p1.html"), query)


def items(page):
    return {facts.source_id: facts for facts in page.items}


def html_resp(fx, html: str):
    return fx.body_resp(html.encode("utf-8"))


def item_page(fx, *, ehid: str, detail: dict, price: dict | None = None, history: dict | None = None):
    """合成一頁只有 window.itemInfo 的詳細頁。"""
    info = {"price": price or {}, "detail": {"ehid": ehid, **detail}, "priceHistorySummary": history or {}}
    return html_resp(fx, f"<html><body><script>window.itemInfo = {json.dumps(info)};</script></body></html>")


def changed_fields(before: ListingFacts, after: ListingFacts) -> set[str]:
    old, new = before.model_dump(), after.model_dump()
    return {name for name in new if new[name] != old[name]}


# ---------------------------------------------------------------- 請求


def test_list_request_uses_only_district_public_price_cap_and_type(adapter, query, public):
    spec = adapter.list_request(query, 1, "full")
    assert spec.method == "GET" and spec.kind == "list"
    parts = urlsplit(spec.url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", WWW_HOST, "/sell/result")
    params = parse_qs(parts.query)
    cap = public.scope.listing.max_price_wan
    assert params == {
        "zipcode": [",".join(d.zip for d in query.districts)],
        "price": [f"~{cap}"],
        "typecode": ["R2"],
        "sort": ["30"],
        "page": ["1"],
    }
    for key in ("other", "age", "mainsize", "floor", "size", "city", "parking"):
        assert key not in params, "車位、屋齡、坪數、樓層都不在伺服器端篩（R1-11）；不帶 city 省一次 301"
    headers = spec.header_dict()
    assert headers == {"Accept": "text/html"}
    assert "page=3" in adapter.list_request(query, 3, "full").url


def test_probe_uses_recently_updated_and_full_uses_price_ascending(adapter, query):
    assert (adapter.caps.stable_sort, adapter.caps.probe_sort) == ("30", "21")
    assert parse_qs(urlsplit(adapter.list_request(query, 1, "probe").url).query)["sort"] == ["21"]
    assert parse_qs(urlsplit(adapter.list_request(query, 1, "full").url).query)["sort"] == ["30"]


def test_two_districts_are_joined_with_a_comma(adapter):
    two = AreaQuery(area="x", districts=(DistrictRef("桃園區", "330"), DistrictRef("中壢區", "320")), max_price_wan=2500)
    assert parse_qs(urlsplit(adapter.list_request(two, 1, "full").url).query)["zipcode"] == ["330,320"]


def test_list_request_rejects_bad_input(adapter, query):
    with pytest.raises(ValueError):
        adapter.list_request(query, 0, "full")
    bad = AreaQuery(area="x", districts=(DistrictRef("桃園區", "33&x=1"),), max_price_wan=2500)
    with pytest.raises(ValueError):
        adapter.list_request(bad, 1, "full")
    with pytest.raises(ValueError):
        adapter.list_request(AreaQuery(area="x", districts=(), max_price_wan=2500), 1, "full")


def test_caps_hosts_and_robots(adapter):
    caps = adapter.caps
    assert (caps.page_size, caps.has_total) == (19, True)
    assert adapter.hosts == frozenset({WWW_HOST, COMMUNITY_HOST})
    assert adapter.robots_urls == (f"https://{WWW_HOST}/robots.txt", f"https://{COMMUNITY_HOST}/robots.txt")
    assert adapter.scope_version == "1"


def test_requests_never_touch_paths_blocked_by_robots(adapter, query):
    base = ListingFacts(source=SITE, source_id=A, area="a", community_key="90001")
    urls = [
        adapter.list_request(query, 1, "full").url,
        adapter.list_request(query, 2, "probe").url,
        adapter.detail_request(A).url,
        adapter.detail_request_for(base).url,
    ]
    for url in urls:
        parts = urlsplit(url)
        assert parts.hostname in adapter.hosts
        assert not parts.path.startswith(ROBOTS_BLOCKED), url
    assert LIST_URL.endswith("/sell/result")


# ---------------------------------------------------------------- 列表


def test_page_counts(page1):
    assert (page1.reported_total, page1.has_next) == (45, True)
    assert len(page1.items) == 5
    assert page1.skipped == 1, "有詳細頁連結、但編號不合法的卡片算 skipped"


def test_road_card(page1, query):
    a = items(page1)[A]
    assert (a.source, a.area) == (SITE, query.area)
    assert a.price_yuan == 11_350_000
    assert a.site_original_price_yuan is None and a.price_excl_parking is None
    assert (a.main_cp, a.main_kind, a.title_area_cp) == (2961, MainKind.MAIN, 4408)
    assert (a.floor_lo, a.floor_hi, a.floors_total) == (9, 9, 15)
    assert (a.btype, a.elevator) == (BType.BUILDING, Elevator.INFERRED), "電梯大廈 → 大樓、推定有電梯"
    assert a.built == parse_age_text("6.7年", REF)
    assert a.parking is Parking.FLAT_UNKNOWN_ACCESS, "標籤「平面車位」：平面，出入方式不明"
    assert (a.road_text, a.community_name, a.community_key) == ("示意路一段", None, "90001")
    assert a.district_hints == [("district", "桃園區")]
    assert a.layout == "3房2廳2衛"
    assert (a.site_lat, a.site_lon) == (None, None), "樂屋網沒有經緯度"


def test_community_only_card_without_age(page1):
    b = items(page1)[B]
    assert (b.community_name, b.community_key, b.road_text) == ("示意社區", "90002", None)
    assert b.built == DateRange(), "卡片沒有屋齡：未知"
    assert (b.floor_lo, b.floors_total) == (5, 7)
    assert (b.btype, b.elevator) == (BType.MANSION, Elevator.INFERRED)
    assert (b.price_yuan, b.site_original_price_yuan) == (9_250_000, 9_680_000), "原價只顯示（R1-22）"
    assert b.parking is Parking.UNKNOWN, "沒有車位標籤是未知，不是沒有"
    assert b.layout == "2房1廳1衛"


def test_card_without_community_or_spaces(page1):
    c = items(page1)[C]
    assert c.community_key is None, "連到 www 主機的物件不屬於社區"
    assert (c.main_cp, c.title_area_cp) == (None, None), "沒有面積：未知，物件照樣保留"
    assert c.main_kind is MainKind.MAIN
    assert c.built == parse_age_text("10個月", REF)
    assert c.layout == "1房1衛", "格局可以沒有「廳」"
    assert (c.floor_lo, c.floor_hi, c.floors_total) == (2, 2, 5)


def test_walkup_card(page1):
    e = items(page1)[E]
    assert (e.btype, e.elevator) == (BType.WALKUP, Elevator.NO)
    assert e.built == parse_age_text("41年", REF), "整數屋齡照顯示值處理，不當成 41.0"
    assert e.built != parse_age_text("41年", REF, decimals=1)


def test_malicious_strings_stay_plain_text(adapter, page1):
    d = items(page1)[D]
    assert d.community_name == "<script>alert(1)</script>示意社區"
    assert d.price_excl_parking is True, "標題寫車位另售"
    e = items(page1)[E]
    assert e.road_text == "javascript:alert(1)路"
    for facts in (d, e):
        url = adapter.detail_request_for(facts).url
        assert url.startswith(("https://www.rakuya.com.tw/", "https://community.rakuya.com.tw/"))
        assert "javascript" not in url and "script" not in url, "網址只用編號套樣板"
    dumped = json.dumps([f.model_dump(mode="json") for f in page1.items], ensure_ascii=False)
    assert "onerror" not in dumped and "示意標題" not in dumped, "標題不保存"


def test_ads_duplicates_and_broken_cards(page1):
    found = items(page1)
    assert set(found) == {A, B, C, D, E}
    assert "0aaaaaa00000006" not in found, "沒有本站詳細頁連結的卡片是廣告（javascript:、別的主機）"
    assert "0aaaaaa00000008" not in found
    assert found[A].price_yuan == 11_350_000, "置頂的重複卡略過，不算 skipped"


def test_parse_list_is_pure(adapter, fx, query):
    first = adapter.parse_list(fx.resp(SITE, "list_p1.html"), query)
    second = adapter.parse_list(fx.resp(SITE, "list_p1.html"), query)
    assert first == second


def test_last_page(adapter, fx, query):
    page = adapter.parse_list(fx.resp(SITE, "list_last.html"), query)
    assert (page.reported_total, page.has_next, len(page.items), page.skipped) == (45, False, 1, 0)


def test_zero_results_is_a_valid_empty_page(adapter, fx, query, public):
    cap = public.scope.listing.max_price_wan
    doc = {
        "condition": {"zipcode": ["330"], "price": f"~{cap}", "typecode": ["R2"]},
        "pagination": {"total": 0, "pageCount": 0, "pageSize": 19, "currentPage": 1},
    }
    page = adapter.parse_list(html_resp(fx, f"<script>window.sellSearch = {json.dumps(doc)};</script>"), query)
    assert (page.items, page.reported_total, page.has_next) == ([], 0, False)


def test_empty_page_after_the_first_is_invalid(adapter, fx, query):
    html = fx.bytes(SITE, "list_last.html").decode("utf-8")
    start, end = html.index("<section"), html.index("</section>") + len("</section>")
    with pytest.raises(PageInvalid):
        adapter.parse_list(html_resp(fx, html[:start] + html[end:]), query)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"zipcode":["330"]', '"zipcode":["320"]'),
        ('"price":"~2500"', '"price":"~999"'),
        ('"price":"~2500"', '"price":""'),
        ('"typecode":["R2"]', '"typecode":["R1"]'),
        ('"typecode":["R2"]', '"typecode":[]'),
    ],
)
def test_condition_echo_must_match_the_request(adapter, fx, query, old, new):
    html = fx.bytes(SITE, "list_p1.html").decode("utf-8")
    assert old in html
    with pytest.raises(PageInvalid) as caught:
        adapter.parse_list(html_resp(fx, html.replace(old, new)), query)
    assert caught.value.code is ErrorCode.PAGE_INVALID


def test_fixture_echo_is_the_public_scope(fx, public):
    html = fx.bytes(SITE, "list_p1.html").decode("utf-8")
    assert window_json(html, "sellSearch")["condition"]["price"] == f"~{public.scope.listing.max_price_wan}"


@pytest.mark.parametrize(
    "body",
    [
        "list_invalid.html",
        "<script>window.sellSearch = {\"condition\": </script>",
        "<script>window.sellSearch = {\"condition\": NaN};</script>",
        "<script>window.sellSearch = {\"condition\": {}, \"pagination\": {}};</script>",
    ],
)
def test_invalid_pages(adapter, fx, query, body):
    resp = fx.resp(SITE, body) if body.endswith(".html") else html_resp(fx, body)
    with pytest.raises(PageInvalid):
        adapter.parse_list(resp, query)


def test_non_200_is_invalid(adapter, fx, query):
    with pytest.raises(PageInvalid):
        adapter.parse_list(fx.resp(SITE, "list_p1.html", status=404), query)


def test_window_json_reads_only_the_named_object():
    html = (
        '<script>window.itemInfoOld = {"wrong": 1};\n'
        '  window.itemInfo = {"a": "}\\"{", "b": [1, {"c": "]"}]};\n'
        '  window.itemContact = {"buyerInfo": {"name": "示意"}};</script>'
    )
    assert window_json(html, "itemInfo") == {"a": '}"{', "b": [1, {"c": "]"}]}
    with pytest.raises(PageInvalid):
        window_json(html, "sellSearch")
    with pytest.raises(PageInvalid):
        window_json('window.itemInfo = {"a": [1, 2}', "itemInfo")


def test_money_needs_a_unit_and_a_sane_range():
    assert money_yuan("1,135萬") == 11_350_000
    assert money_yuan("1135") is None, "沒有單位不猜"
    assert money_yuan("0萬") is None
    assert money_yuan("968萬 925萬") is None, "不只一個金額不取第一個"


# ---------------------------------------------------------------- 詳細頁


def test_needs_detail(adapter, page1, public, fx):
    found = items(page1)
    assert adapter.needs_detail(found[A]) is True, "電梯只是推定、車位出入方式不明"
    assert adapter.needs_detail(found[E]) is True
    scoped = RakuyaAdapter(scope=public.scope.listing)
    assert scoped.needs_detail(found[E]) is False, "公寓不在公開範圍內"
    assert scoped.needs_detail(found[A]) is True
    detailed = adapter.parse_detail(fx.resp(SITE, "detail.html"), found[A])
    assert adapter.needs_detail(detailed) is False


def test_detail_urls(adapter, page1):
    found = items(page1)
    assert adapter.detail_request(A).url == f"https://{WWW_HOST}/sell_item/info?ehid={A}"
    assert adapter.detail_request(A).kind == "detail"
    spec = adapter.detail_request_for(found[A])
    assert spec.url == f"https://{COMMUNITY_HOST}/90001/sell/info?ehid={A}", "有社區 id 就不經過 301"
    assert adapter.detail_request_for(found[C]).url == f"https://{WWW_HOST}/sell_item/info?ehid={C}"
    odd = ListingFacts(source=SITE, source_id=A, area="a", community_key="../9")
    assert adapter.detail_request_for(odd).url == f"https://{WWW_HOST}/sell_item/info?ehid={A}"
    for bad in ("../x", "0AAAAAA00000001", "0aaaaaa0000001"):
        with pytest.raises(ValueError):
            adapter.detail_request(bad)
    with pytest.raises(ValueError):
        adapter.detail_request_for(ListingFacts(source=SITE, source_id="x1", area="a"))


def test_detail_fills_elevator_parking_and_prices(adapter, page1, fx):
    base = items(page1)[A]
    detailed = adapter.parse_detail(fx.resp(SITE, "detail.html"), base)
    assert detailed.elevator is Elevator.YES
    assert detailed.parking is Parking.RAMP_FLAT, "結構化寫平面，自由文字補出坡道"
    assert detailed.parking_public is False, "自由文字寫私有"
    assert detailed.price_excl_parking is False, "價格備註寫含車位（R1-13）"
    assert detailed.parking_price_yuan == 1_500_000
    assert detailed.site_original_price_yuan == 12_180_000, "首次開價和現價不同"
    assert detailed.price_yuan == base.price_yuan, "開價一律用列表的（R3-22）"
    assert (detailed.main_cp, detailed.floors_total) == (base.main_cp, base.floors_total)
    assert detailed.built == narrow_built(base.built, parse_age_text("約6.7年", REF)) == base.built
    assert detailed.community_name is None, "詳細頁的社區名、地址不讀"


def test_detail_of_a_listing_without_parking(adapter, page1, fx):
    base = items(page1)[B]
    detailed = adapter.parse_detail(fx.resp(SITE, "detail_other.html"), base)
    assert detailed.parking is Parking.NONE, "parking 寫無車位，其他欄位空白"
    assert detailed.elevator is Elevator.INFERRED, "lifts 空白就不改"
    assert detailed.built == parse_age_text("約33.9年", REF), "列表沒有屋齡，用詳細頁的"
    assert detailed.site_original_price_yuan == 9_680_000, "firstUpPrice 0 是沒有資料；列表的原價保留"
    assert detailed.price_excl_parking is None and detailed.parking_price_yuan is None


def test_detail_fills_what_the_list_did_not_have(adapter, page1, fx):
    base = items(page1)[C]
    resp = item_page(
        fx,
        ehid=C,
        detail={"lifts": "無", "mainSize": "15.62坪", "transFloors": "2", "surFloors": "5"},
        price={"priceParkingMemo": "不含車位"},
        history={"firstUpPrice": 615},
    )
    detailed = adapter.parse_detail(resp, base)
    assert (detailed.main_cp, detailed.main_kind) == (1562, MainKind.MAIN)
    assert detailed.elevator is Elevator.NO
    assert detailed.price_excl_parking is True
    assert detailed.site_original_price_yuan is None, "首次開價等於現價：沒有原價"
    unfloored = base.model_copy(update={"floor_lo": None, "floor_hi": None, "floors_total": None})
    filled = adapter.parse_detail(resp, unfloored)
    assert (filled.floor_lo, filled.floor_hi, filled.floors_total) == (2, 2, 5)


def test_detail_presale(adapter, fx):
    base = ListingFacts(source=SITE, source_id=A, area="a")
    resp = item_page(fx, ehid=A, detail={"ageValue": "", "ageUnit": "", "agetype": "預售屋"})
    assert adapter.parse_detail(resp, base).built == built_presale(REF)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"parking": "有車位", "parkingType": "坡道平面式"}, Parking.RAMP_FLAT),
        ({"parking": "有車位", "parkingType": "平面式車位"}, Parking.FLAT_UNKNOWN_ACCESS),
        ({"parkingType": "平面式車位", "parkingGarage": "示意 B2 坡道平面 1 格"}, Parking.RAMP_FLAT),
        ({"parkingType": "坡道機械式", "parkingGarage": "坡道平面"}, Parking.MECHANICAL),
        ({"parkingType": "平面式車位", "parkingGarage": "機械式"}, Parking.FLAT_UNKNOWN_ACCESS),
        ({"parkingType": "機械循環式"}, Parking.MECHANICAL),
        ({"parking": "有車位", "parkingType": "", "parkingGarage": "坡道-平面"}, Parking.RAMP_FLAT),
        ({"parking": "有車位", "parkingType": "", "parkingGarage": "示意說明"}, Parking.UNKNOWN),
        ({"parking": "有車位", "parkingType": "", "parkingGarage": "無"}, Parking.UNKNOWN),
        ({"parking": "-", "parkingType": "", "parkingGarage": "無"}, Parking.UNKNOWN),
        ({"parking": "無車位", "parkingType": "", "parkingGarage": ""}, Parking.NONE),
        ({"parking": "無車位", "parkingType": "坡道平面式"}, Parking.UNKNOWN),
        ({}, Parking.UNKNOWN),
    ],
)
def test_detail_parking_rules(detail, expected):
    assert detail_parking(detail) is expected


def test_public_parking(adapter, fx):
    base = ListingFacts(source=SITE, source_id=A, area="a")
    resp = item_page(fx, ehid=A, detail={"parking": "有車位", "parkingGarage": "公有 抽籤"})
    assert adapter.parse_detail(resp, base).parking_public is True


def test_detail_changes_only_detail_fields(adapter, page1, fx):
    found = items(page1)
    cases = [
        (found[A], fx.resp(SITE, "detail.html")),
        (found[B], fx.resp(SITE, "detail_other.html")),
        (
            ListingFacts(source=SITE, source_id=C, area="a", price_yuan=6_150_000),
            item_page(
                fx,
                ehid=C,
                detail={"lifts": "有", "mainSize": "15.62坪", "transFloors": "2", "surFloors": "5",
                        "parking": "有車位", "parkingType": "坡道平面式", "parkingGarage": "公有",
                        "parkingPrice": "120萬", "ageValue": "約9", "ageUnit": "個月"},
                price={"priceParkingMemo": "含車位"},
                history={"firstUpPrice": 688},
            ),
        ),
    ]
    for base, resp in cases:
        changed = changed_fields(base, adapter.parse_detail(resp, base))
        assert changed and changed <= adapter.caps.detail_fields, changed - adapter.caps.detail_fields


def test_detail_of_another_listing_or_without_item_info_is_invalid(adapter, page1, fx):
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail.html"), items(page1)[B])
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "list_invalid.html"), items(page1)[A])
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail.html", status=404), items(page1)[A])


# ---------------------------------------------------------------- 透過抓取層


def _robots(request):
    return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")


def test_full_scan_reads_both_robots_files_then_pages(adapter, fx, query, make_fetcher, policy_for):
    bodies = {"1": fx.bytes(SITE, "list_p1.html"), "3": fx.bytes(SITE, "list_last.html")}
    sent = []

    def handler(request):
        sent.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            return _robots(request)
        return httpx.Response(200, content=bodies[request.url.params["page"]])

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    pages = [
        adapter.parse_list(fetcher.fetch(adapter.list_request(query, n, "full"), source=SITE, deadline=1e9), query)
        for n in (1, 3)
    ]
    assert [p.has_next for p in pages] == [True, False]
    assert sent[:2] == [(WWW_HOST, "/robots.txt"), (COMMUNITY_HOST, "/robots.txt")]
    assert sent[2:] == [(WWW_HOST, "/sell/result")] * 2


def test_www_detail_redirects_to_the_community_host(adapter, fx, make_fetcher, policy_for):
    def handler(request):
        if request.url.path == "/robots.txt":
            return _robots(request)
        if request.url.host == WWW_HOST:
            target = f"https://{COMMUNITY_HOST}/90001/sell/info?ehid={A}"
            return httpx.Response(301, headers={"Location": target})
        return httpx.Response(200, content=fx.bytes(SITE, "detail.html"))

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    resp = fetcher.fetch(adapter.detail_request(A), source=SITE, deadline=1e9)
    assert urlsplit(resp.url).hostname == COMMUNITY_HOST
    base = ListingFacts(source=SITE, source_id=A, area="a")
    assert adapter.parse_detail(resp, base).elevator is Elevator.YES


def test_challenge_pauses_the_source(adapter, fx, query, make_fetcher, policy_for):
    def handler(request):
        if request.url.path == "/robots.txt":
            return _robots(request)
        return fx.envelope(SITE, "blocked.json")

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert caught.value.code is ErrorCode.CHALLENGE
    with pytest.raises(Blocked) as again:
        fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9)
    assert again.value.code is ErrorCode.PAUSED
