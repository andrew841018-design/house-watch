"""東森轉接器：CSRF 準備、請求只用公開範圍、列表、批次樓層、詳細頁（物件地圖座標）、被擋。"""

import datetime as dt
import json

import httpx
import pytest

from house_watch.errors import ErrorCode
from house_watch.models import BType, DateRange, Elevator, ListingFacts, MainKind, Parking
from house_watch.sources.base import (
    Blocked,
    PageInvalid,
    RequestSpec,
    built_from_decimal_years,
    to_decimal,
)
from house_watch.sources.etwarm import (
    ACTION_API,
    HOST,
    LIST_API,
    LIST_PAGE,
    EtwarmAdapter,
    csrf_token,
)

REF = dt.date(2026, 11, 21)
SITE = "etwarm"
TOKEN = "PlaceholderCsrfToken000000000000000000000"
DETAIL_URL = f"https://{HOST}/houses/buy/700105"


@pytest.fixture
def adapter(fx):
    ready = EtwarmAdapter()
    ready.prepare(lambda spec: fx.resp(SITE, "list_page.html", url=spec.url))
    return ready


@pytest.fixture
def page1(adapter, fx, query):
    return adapter.parse_list(fx.resp(SITE, "list_p1.json"), query)


def items(page):
    return {facts.source_id: facts for facts in page.items}


# ---------------------------------------------------------------- CSRF 與請求


def test_prepare_reads_the_csrf_token(fx):
    seen: list[RequestSpec] = []
    ready = EtwarmAdapter()

    def fetch(spec):
        seen.append(spec)
        return fx.resp(SITE, "list_page.html", url=spec.url)

    ready.prepare(fetch)
    assert [(s.method, s.url, s.kind) for s in seen] == [("GET", LIST_PAGE, "prepare")]
    assert csrf_token(fx.bytes(SITE, "list_page.html").decode()) == TOKEN


def test_prepare_without_token_is_invalid(fx):
    with pytest.raises(PageInvalid):
        EtwarmAdapter().prepare(lambda spec: fx.resp(SITE, "list_page_no_token.html"))


def test_requests_need_prepare_first(query):
    with pytest.raises(RuntimeError):
        EtwarmAdapter().list_request(query, 1, "full")
    with pytest.raises(RuntimeError):
        EtwarmAdapter().batch_request(["700105"])


def test_list_request_uses_only_district_and_public_price_cap(adapter, query, public):
    spec = adapter.list_request(query, 2, "full")
    assert spec.method == "POST" and spec.url == LIST_API
    body = spec.json_payload()
    assert body["area"] == "-".join(f"桃園市{d.name}" for d in query.districts)
    assert (body["price1"], body["price2"]) == (0, public.scope.listing.max_price_wan)
    assert (body["sort"], body["page"]) == ("NEW", 2)
    assert body["type"] is None, "不在伺服器端篩型態"
    for key in ("ping1", "ping2", "old1", "old2", "room", "feature", "pos", "keyword"):
        assert body[key] is None
    headers = spec.header_dict()
    assert headers["X-CSRF-TOKEN"] == TOKEN
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Referer"] == LIST_PAGE
    assert adapter.list_request(query, 2, "probe").json_payload() == body


# ---------------------------------------------------------------- 列表


def test_page_counts(page1):
    assert (page1.reported_total, page1.has_next) == (12, True)
    assert len(page1.items) == 5 and page1.skipped == 1


def test_listing_fields(page1, query):
    a = items(page1)["700105"]
    assert (a.source, a.area) == (SITE, query.area)
    assert (a.price_yuan, a.site_original_price_yuan) == (12_880_000, 13_500_000)
    assert a.price_excl_parking is False, "備註寫含車位價格"
    assert (a.main_cp, a.main_kind) == (3146, MainKind.MAIN_PLUS_AUX)
    assert a.title_area_cp == 4512
    assert a.built == built_from_decimal_years(to_decimal("8.4"), REF)
    assert (a.btype, a.elevator, a.parking) == (BType.BUILDING, Elevator.INFERRED, Parking.RAMP_FLAT)
    assert a.layout == "3房2廳2衛"
    assert a.road_text == "中正路"
    assert a.district_hints == [("district", "桃園區"), ("address", "桃園區")]
    assert (a.floor_lo, a.site_lat) == (None, None), "樓層、座標只在詳細頁或批次 API"


def test_dirty_areas_become_unknown(page1):
    b = items(page1)["700104"]
    assert (b.main_cp, b.title_area_cp) == (None, None)
    assert b.built == DateRange()
    assert b.parking is Parking.UNKNOWN, "「有」不知道型式"
    assert b.road_text == "大有路路", "網站的錯字原樣保留，交給定位正規化"


def test_conflicting_price_notes_are_unknown(page1):
    c = items(page1)["700103"]
    assert c.price_excl_parking is None, "備註含車位、案名卻寫車位另售"
    assert c.parking is Parking.MECHANICAL
    assert c.district_hints == [("district", "中壢區"), ("address", "中壢區")]


def test_walkup_land_and_malicious_road(page1):
    d = items(page1)["700102"]
    assert (d.btype, d.elevator, d.parking) == (BType.WALKUP, Elevator.NO, Parking.UNKNOWN)
    assert d.road_text == "javascript:alert(1)路"
    e = items(page1)["700101"]
    assert e.btype is BType.OTHER
    assert e.layout is None


def test_titles_are_not_stored(page1):
    dumped = json.dumps([f.model_dump(mode="json") for f in page1.items], ensure_ascii=False)
    assert "示意案名" not in dumped


def test_last_and_invalid_pages(adapter, fx, query):
    last = adapter.parse_list(fx.resp(SITE, "list_p2_last.json"), query)
    assert last.has_next is False and len(last.items) == 1
    for name in ("list_p2_empty.json", "list_invalid.html", "list_page.html"):
        with pytest.raises(PageInvalid):
            adapter.parse_list(fx.resp(SITE, name), query)


# ---------------------------------------------------------------- 批次 API（樓層）


def test_batch_request(adapter):
    spec = adapter.batch_request(["700105", "700104"])
    assert (spec.method, spec.url, spec.kind) == ("POST", ACTION_API, "batch")
    assert spec.json_payload() == {"ids": ["700105", "700104"]}
    assert spec.header_dict()["X-CSRF-TOKEN"] == TOKEN
    for bad in ([], [str(700000 + n) for n in range(11)], ["7001x"]):
        with pytest.raises(ValueError):
            adapter.batch_request(bad)


def test_batch_fills_floors(adapter, page1, fx):
    bases = {k: v for k, v in items(page1).items() if k in ("700105", "700104", "700103")}
    filled = adapter.parse_batch(fx.resp(SITE, "batch.json"), bases)
    assert set(filled) == {"700105", "700104", "700103"}, "不在 bases 的編號不處理"
    assert (filled["700105"].floor_lo, filled["700105"].floors_total) == (7, 16)
    assert (filled["700104"].floor_lo, filled["700104"].floor_hi) == (18, 18)
    assert (filled["700103"].floor_lo, filled["700103"].floors_total) == (9, None), "樓層大於總樓層"
    assert filled["700105"].price_yuan == bases["700105"].price_yuan


def test_batch_must_be_a_list(adapter, fx):
    with pytest.raises(PageInvalid):
        adapter.parse_batch(fx.resp(SITE, "list_p1.json"), {})


# ---------------------------------------------------------------- 詳細頁


def test_needs_detail(adapter, page1, public):
    found = items(page1)
    assert adapter.needs_detail(found["700105"]) is True
    scoped = EtwarmAdapter(scope=public.scope.listing)
    assert scoped.needs_detail(found["700101"]) is False


def test_detail_uses_the_listing_map_not_the_store_map(adapter, page1, fx):
    base = items(page1)["700105"]
    detailed = adapter.parse_detail(fx.resp(SITE, "detail.html", url=DETAIL_URL), base)
    assert (detailed.site_lat, detailed.site_lon) == (24.9931, 121.3012)
    assert (detailed.floor_lo, detailed.floor_hi, detailed.floors_total) == (7, 7, 16)
    assert detailed.elevator is Elevator.YES
    assert detailed.parking is Parking.RAMP_FLAT
    assert detailed.community_name == "<img src=x onerror=alert(1)>示意社區"
    assert detailed.price_excl_parking is False
    assert detailed.built == base.built
    assert detailed.main_cp == base.main_cp, "列表已有的主建物＋附屬不被詳細頁的「約」值覆蓋"
    assert adapter.needs_detail(detailed) is False


def test_detail_fills_main_area_when_list_had_none(adapter, fx):
    base = ListingFacts(source=SITE, source_id="700105", area="a")
    detailed = adapter.parse_detail(fx.resp(SITE, "detail.html", url=DETAIL_URL), base)
    assert (detailed.main_cp, detailed.main_kind) == (3150, MainKind.MAIN_PLUS_AUX)
    assert detailed.built == built_from_decimal_years(to_decimal("8.4"), REF)


def test_unfilled_detail_keeps_unknowns(adapter, page1, fx):
    base = items(page1)["700104"]
    detailed = adapter.parse_detail(
        fx.resp(SITE, "detail_unfilled.html", url=f"https://{HOST}/houses/buy/700104"), base
    )
    assert (detailed.floor_lo, detailed.site_lat, detailed.community_name) == (None, None, None)
    assert detailed.elevator is base.elevator and detailed.parking is base.parking


def test_detail_redirected_elsewhere_or_without_anchor_is_invalid(adapter, page1, fx):
    base = items(page1)["700105"]
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "detail.html", url=f"https://{HOST}/houses"), base)
    with pytest.raises(PageInvalid):
        adapter.parse_detail(fx.resp(SITE, "list_invalid.html", url=DETAIL_URL), base)


def test_detail_request(adapter):
    spec = adapter.detail_request("700105")
    assert (spec.method, spec.url) == ("GET", DETAIL_URL)
    with pytest.raises(ValueError):
        adapter.detail_request("700105/../x")


# ---------------------------------------------------------------- 透過抓取層


def test_prepare_and_list_share_one_session(fx, query, make_fetcher, policy_for):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /print.php\n")
        if request.url.path == "/houses":
            return httpx.Response(
                200,
                headers={"set-cookie": "laravel_session=placeholder; Path=/"},
                content=fx.bytes(SITE, "list_page.html"),
            )
        return httpx.Response(200, content=fx.bytes(SITE, "list_p1.json"))

    adapter = EtwarmAdapter()
    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    adapter.prepare(fetcher.bind(SITE, 1e9))
    page = adapter.parse_list(fetcher.fetch(adapter.list_request(query, 1, "full"), source=SITE, deadline=1e9), query)
    assert len(page.items) == 5
    api = sent[-1]
    assert api.headers["x-csrf-token"] == TOKEN
    assert api.headers["cookie"] == "laravel_session=placeholder"


def test_normal_page_with_challenge_words_is_not_blocked(fx, make_fetcher, policy_for):
    adapter = EtwarmAdapter()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        return httpx.Response(200, content=fx.bytes(SITE, "list_page.html"))

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    adapter.prepare(fetcher.bind(SITE, 1e9))


def test_cloudflare_challenge_is_detected_by_header(fx, make_fetcher, policy_for):
    adapter = EtwarmAdapter()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        return fx.envelope(SITE, "blocked.json")

    fetcher = make_fetcher(handler, policies=policy_for(adapter))
    with pytest.raises(Blocked) as caught:
        adapter.prepare(fetcher.bind(SITE, 1e9))
    assert caught.value.code is ErrorCode.CHALLENGE
