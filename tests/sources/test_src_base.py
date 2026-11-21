"""C5 介面與解析小工具：R3-11 的解析陷阱、R1-12 的車位片語表、§3.10 的屋齡區間、R3-01 的區名。

數字都是示意值。
"""

import datetime as dt
import math
from decimal import Decimal

import pytest

from house_watch.errors import ErrorCode
from house_watch.models import BType, DateRange, ListingFacts, Parking
from house_watch.sources.base import (
    FETCHED_AT_HEADER,
    HINT_ADDRESS,
    HINT_ZIP,
    AreaQuery,
    PageInvalid,
    RequestSpec,
    Resp,
    btype_by_height,
    btype_from_word,
    built_from_decimal_years,
    built_from_floor_years,
    built_from_iso_date,
    built_from_years_months,
    classify_parking_text,
    clean_text,
    combine_parking,
    district_hints,
    load_json,
    narrow_built,
    parse_age_text,
    parse_floor_field,
    parse_floor_range,
    parse_total_floors,
    ping_text_to_cp,
    ping_to_cp,
    price_excl_from_text,
    response_date,
    site_point,
    split_address,
    to_decimal,
    updated,
    wan_to_yuan,
    zip3,
)

REF = dt.date(2026, 11, 21)


# ---------------------------------------------------------------- 金額與面積（R3-11、R1-22）


@pytest.mark.parametrize(
    ("value", "yuan"),
    [
        ("1288", 12_880_000),
        ("1286.1", 12_861_000),
        ("1,198", 11_980_000),
        ("898.0000", 8_980_000),
        (966, 9_660_000),
        ("１２８８", 12_880_000),
    ],
)
def test_wan_to_integer_yuan(value, yuan):
    assert wan_to_yuan(value) == yuan


@pytest.mark.parametrize(
    "value",
    ["1億2,000萬", "原價1,980萬 現價1,880萬", "↓100萬 1,880萬", "0", "-5", "123456789", math.nan, True, None, ""],
)
def test_money_never_takes_the_first_number(value):
    assert wan_to_yuan(value) is None


def test_area_in_hundredths_of_ping():
    assert ping_to_cp(27.35) == 2735
    assert ping_to_cp(28.026600000000002) == 2803
    assert ping_to_cp("41.06") == 4106
    assert ping_to_cp(26327.189000000002) is None, "東森的髒資料"
    assert ping_to_cp(0.0) is None
    assert ping_to_cp("66.12㎡") is None, "NFKC 會變成 66.12m2，不能多讀一個 2"
    assert ping_text_to_cp("約16.2坪") == 1620
    assert ping_text_to_cp("58.96 坪(含車位：13.70坪)") == 5896
    assert ping_text_to_cp("16.2") is None, "沒有單位不讀"


def test_to_decimal_requires_the_whole_value_to_be_a_number():
    assert to_decimal("27.35") == Decimal("27.35")
    assert to_decimal("約27") is None
    assert to_decimal(float("inf")) is None


def test_site_point():
    assert site_point(24.9931, 121.3012) == (24.9931, 121.3012)
    assert site_point(0.0, 0.0) == (None, None)
    assert site_point("25.0", None) == (None, None)
    assert site_point(48.85, 2.35) == (None, None)


# ---------------------------------------------------------------- 樓層（R3-11、R1-30）


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6樓/共15樓", (6, 6, 15)),
        ("1~2樓/共9樓", (1, 2, 9)),
        ("1～2樓/共9樓", (1, 2, 9)),
        ("10F / 17F", (10, 10, 17)),
        ("6樓/共0樓", (6, 6, None)),
        ("0樓", (None, None, None)),
        ("", (None, None, None)),
        ("－", (None, None, None)),
    ],
)
def test_floor_field(text, expected):
    assert parse_floor_field(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B1-1", (-1, 1)),
        ("1-2", (1, 2)),
        ("十五樓之1", (15, 15)),
        ("8樓之2", (8, 8)),
        ("９F", (9, 9)),
        ("頂樓", (None, None)),
        ("整棟", (None, None)),
    ],
)
def test_floor_range_only_with_explicit_range_marks(text, expected):
    assert parse_floor_range(text) == expected


def test_total_floors():
    assert parse_total_floors("共15樓") == 15
    assert parse_total_floors("17F") == 17
    assert parse_total_floors("0") is None
    assert parse_total_floors("B2") is None


# ---------------------------------------------------------------- 屋齡 → 完工日區間（r4 §3.10）


def test_floor_years_are_one_year_wide():
    assert built_from_floor_years(9, REF) == DateRange(lo=dt.date(2016, 11, 22), hi=dt.date(2017, 11, 21))
    assert built_from_floor_years(0, REF) == DateRange(lo=dt.date(2025, 11, 22), hi=REF)


def test_floor_years_on_february_29():
    leap = dt.date(2028, 2, 29)
    assert built_from_floor_years(1, leap) == DateRange(lo=dt.date(2026, 3, 1), hi=dt.date(2027, 2, 28))


def test_one_decimal_age_covers_rounding_or_truncation():
    """顯示值 x：屋齡 ∈ [x − 0.05, x + 0.1)，同時涵蓋四捨五入與截斷（同 WP4 的 display 模式）。"""
    built = built_from_decimal_years(Decimal("8.4"), REF)
    assert (REF - built.hi).days == math.floor(Decimal("8.35") * Decimal("365.2425"))
    assert (REF - built.lo).days == math.ceil(Decimal("8.5") * Decimal("365.2425"))
    assert built_from_decimal_years(Decimal("31"), REF, decimals=1) == built_from_decimal_years(
        Decimal("31.0"), REF
    ), "網站顯示到小數一位時，整數其實是 x.0"


def test_years_and_months():
    assert built_from_years_months(9, 4, REF) == DateRange(
        lo=dt.date(2017, 6, 22), hi=dt.date(2017, 7, 21)
    )
    assert parse_age_text("9年4個月", REF) == built_from_years_months(9, 4, REF)
    assert parse_age_text("11個月", REF) == built_from_years_months(0, 11, REF)


def test_age_text_variants():
    assert parse_age_text("9年", REF, floor_years=True) == built_from_floor_years(9, REF)
    assert parse_age_text("16.3年", REF) == built_from_decimal_years(Decimal("16.3"), REF)
    approx = parse_age_text("約16", REF)
    exact = built_from_decimal_years(Decimal("16"), REF)
    assert approx.lo < exact.lo and approx.hi > exact.hi, "「約」要放寬"
    assert approx.lo == dt.date(exact.lo.year - 1, exact.lo.month, exact.lo.day), "放寬 1 年"
    at_least = parse_age_text("30年以上", REF)
    assert at_least.lo is None and at_least.hi is not None
    at_most = parse_age_text("5年以下", REF)
    assert at_most.hi == REF and at_most.lo is not None
    assert parse_age_text("預售屋", REF) == DateRange(lo=REF, hi=None)
    assert parse_age_text("新成屋", REF) == built_from_floor_years(0, REF)


@pytest.mark.parametrize("text", ["", "--", "－", "0", "0年", "103", "65年", "新"])
def test_unknown_or_implausible_ages(text):
    assert parse_age_text(text, REF) == DateRange()


def test_age_without_fetch_date_is_unknown():
    assert parse_age_text("9年", None) == DateRange()
    assert built_from_floor_years(9, None) == DateRange()


def test_completion_date():
    assert built_from_iso_date("1995-05-19") == DateRange(lo=dt.date(1995, 5, 19), hi=dt.date(1995, 5, 19))
    assert built_from_iso_date("not a date") is None
    assert built_from_iso_date("2031-01-01", REF) is None


def test_narrow_built_takes_the_intersection():
    year = built_from_floor_years(9, REF)
    month = built_from_years_months(9, 4, REF)
    assert narrow_built(year, month) == month
    elsewhere = DateRange(lo=dt.date(1990, 1, 1), hi=dt.date(1990, 2, 1))
    assert narrow_built(year, elsewhere) == elsewhere, "沒有交集就採用新的"
    assert narrow_built(year, DateRange()) == year


# ---------------------------------------------------------------- 車位（R1-12）、車位另售（R1-13）


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("機械升降平面車位", Parking.LIFT_FLAT),
        ("升降平面", Parking.LIFT_FLAT),
        ("坡道平面 1個", Parking.RAMP_FLAT),
        ("坡道平面車位", Parking.RAMP_FLAT),
        ("一樓平面", Parking.GROUND_FLAT),
        ("平面式", Parking.FLAT_UNKNOWN_ACCESS),
        ("坡道機械式平移大車位", Parking.MECHANICAL),
        ("機械 1個", Parking.MECHANICAL),
        ("塔式", Parking.MECHANICAL),
        ("平面式+機械式", Parking.MIXED_WITH_FLAT),
        ("機械式+塔式", Parking.MECHANICAL),
        ("無", Parking.NONE),
        ("有", Parking.UNKNOWN),
        ("有車位", Parking.UNKNOWN),
        ("", Parking.UNKNOWN),
    ],
)
def test_parking_phrase_table(text, kind):
    assert classify_parking_text(text) is kind


def test_combine_parking():
    assert combine_parking([Parking.RAMP_FLAT, Parking.RAMP_FLAT]) is Parking.RAMP_FLAT
    assert combine_parking([Parking.RAMP_FLAT, Parking.UNKNOWN]) is Parking.MIXED_WITH_FLAT
    assert combine_parking([Parking.MECHANICAL, Parking.UNKNOWN]) is Parking.UNKNOWN
    assert combine_parking([Parking.MECHANICAL, Parking.MIXED_NO_FLAT]) is Parking.MIXED_NO_FLAT
    assert combine_parking([Parking.NONE]) is Parking.UNKNOWN
    assert combine_parking([]) is Parking.UNKNOWN


@pytest.mark.parametrize("title", ["三房 車位另售", "車位另購", "總價不含車位", "車另計"])
def test_price_excludes_parking_phrases(title):
    assert price_excl_from_text(title) is True


@pytest.mark.parametrize("title", ["三房含車位", "平車", "", None])
def test_no_exclusion_phrase_means_unknown(title):
    assert price_excl_from_text(title) is None


# ---------------------------------------------------------------- 區名（R3-01）、文字


def test_split_address_keeps_every_leading_district():
    assert split_address("桃園市中壢區桃園區莊敬路") == ("莊敬路", ["中壢區", "桃園區"])
    assert split_address("八德區中華路") == ("中華路", ["八德區"])
    assert split_address("工業區一路") == ("工業區一路", [])
    assert split_address("桃園市桃園區") == (None, ["桃園區"])
    assert split_address(None) == (None, [])


def test_district_hints():
    hints = district_hints(zip_code="330000", address_districts=["桃園區"])
    assert hints == [(HINT_ZIP, "330"), (HINT_ADDRESS, "桃園區")]
    assert zip3(330) == "330"
    assert zip3("33") is None


def test_clean_text_keeps_markup_as_plain_data():
    assert clean_text("<img src=x onerror=alert(1)>社區") == "<img src=x onerror=alert(1)>社區"
    assert clean_text("a\x00b​ c\n d") == "ab c d"
    assert clean_text("－") is None
    assert clean_text("無", placeholders=False) == "無"
    assert len(clean_text("字" * 500)) == 200


def test_btype_words_and_height():
    assert btype_from_word("電梯大樓 [住宅]") is BType.BUILDING
    assert btype_from_word("華廈") is BType.MANSION
    assert btype_from_word("透天/別墅") is BType.TOWNHOUSE
    assert btype_from_word("店面") is BType.OTHER
    assert btype_from_word("") is None
    assert btype_by_height(11) is BType.BUILDING
    assert btype_by_height(10) is BType.MANSION
    assert btype_by_height(None) is None


# ---------------------------------------------------------------- 回應與資料結構


def test_response_date_prefers_fetch_time():
    resp = Resp(200, {FETCHED_AT_HEADER: "2026-11-20T17:00:00Z", "date": "Mon, 01 Jan 2001 00:00:00 GMT"}, b"", "")
    assert response_date(resp) == dt.date(2026, 11, 21), "UTC 17:00 已經是台北隔天"
    assert response_date(Resp(200, {"date": "Fri, 20 Nov 2026 13:30:10 GMT"}, b"", "")) == dt.date(2026, 11, 20)
    assert response_date(Resp(200, {}, b"", "")) is None


def test_load_json_rejects_html_and_nan():
    with pytest.raises(PageInvalid) as caught:
        load_json(Resp(200, {}, b"<html>maintenance</html>", ""))
    assert caught.value.code is ErrorCode.PAGE_INVALID
    with pytest.raises(PageInvalid):
        load_json(Resp(200, {}, b'{"x": NaN}', ""))
    assert load_json(Resp(200, {}, '﻿{"x": 1}'.encode(), "")) == {"x": 1}


def test_area_query_only_has_public_scope(public):
    area = public.areas[0]
    query = AreaQuery.from_config(area, public.scope.listing)
    assert query.area == area.id
    assert [(d.name, d.zip) for d in query.districts] == [(d.name, d.zip) for d in area.districts]
    assert query.max_price_wan == public.scope.listing.max_price_wan
    assert query.city == "桃園市"


def test_request_spec_post_json():
    spec = RequestSpec.post_json("https://a.example/api", {"頁": 1}, headers={"X-Test": "1"})
    assert spec.method == "POST"
    assert spec.header_dict()["Content-Type"].startswith("application/json")
    assert spec.json_payload() == {"頁": 1}
    assert "頁".encode() in spec.body


def test_updated_does_not_erase_known_values():
    base = ListingFacts(source="x", source_id="1", area="a", floor_lo=4, floor_hi=4)
    assert updated(base, floor_lo=None, floor_hi=None).floor_lo == 4
    assert updated(base, floors_total=9).floors_total == 9
