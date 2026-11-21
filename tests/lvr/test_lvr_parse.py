"""lvr/parse.py：實價登錄 a 檔 → TxFacts。資料都是合成的示意值（見 lvr_testkit）。"""

import datetime as dt

import pytest
from lvr_testkit import (
    BUILD_HEADER,
    MAIN_HEADER,
    PARK_HEADER,
    build_row,
    csv_bytes,
    main_row,
    park_row,
    release_files,
    serial,
)

from house_watch.lvr.parse import (
    MAIN_COLUMNS,
    LvrError,
    RowSkipped,
    classify_remark,
    parse_counts,
    parse_release,
    parse_row,
    parse_total_floors,
    parse_transfer_floors,
    read_table,
    roc_built,
    roc_date,
    split_evenly,
)
from house_watch.models import BType, DateRange, Elevator, Parking, ParkingSpace

D = dt.date


def one(main=None, parks=(), builds=(), **overrides):
    row = main if main is not None else main_row(**overrides)
    return parse_row(row, builds, parks, release_id="b20261111")


# ------------------------------------------------------------------ 讀檔


def test_read_table_skips_bom_and_english_header():
    data = csv_bytes(PARK_HEADER, [park_row(1), park_row(2)])
    assert data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data
    table = read_table(data, ("編號", "車位類別"), what="park")
    assert [r["編號"] for r in table.rows] == [serial(1), serial(2)]
    assert table.bad_rows == 0


def test_read_table_without_english_header_keeps_first_row():
    data = csv_bytes(PARK_HEADER, [park_row(1)], english=False, bom=False)
    assert len(read_table(data, ("編號",), what="park").rows) == 1


def test_read_table_errors():
    with pytest.raises(LvrError, match="缺少欄位"):
        read_table(csv_bytes(("編號",), []), ("編號", "車位價格"), what="park")
    with pytest.raises(LvrError, match="UTF-8"):
        read_table("編號\n".encode("big5"), ("編號",), what="park")
    with pytest.raises(LvrError, match="空的"):
        read_table(b"", ("編號",), what="park")
    assert LvrError.code == "lvr_invalid"


def test_read_table_counts_rows_with_wrong_width():
    data = csv_bytes(PARK_HEADER, [park_row(1)]) + b"X,Y\r\n"
    table = read_table(data, ("編號",), what="park")
    assert (len(table.rows), table.bad_rows) == (1, 1)


def test_main_columns_are_in_testkit_header():
    assert set(MAIN_COLUMNS) <= set(MAIN_HEADER)
    assert {"編號", "建築完成日期", "建物分層"} <= set(BUILD_HEADER)


# ------------------------------------------------------------------ 欄位


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1150903", D(2026, 9, 3)),
        ("941202", D(2005, 12, 2)),
        ("0990101", D(2010, 1, 1)),
        ("1150900", None),
        ("1151301", None),
        ("0000101", None),
        ("", None),
        ("abc", None),
    ],
)
def test_roc_date(text, expected):
    # 交易日是訂約日，要完整；不能用字串比較，先轉成日期
    assert roc_date(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1141208", DateRange(lo=D(2025, 12, 8), hi=D(2025, 12, 8))),
        ("0840419", DateRange(lo=D(1995, 4, 19), hi=D(1995, 4, 19))),
        ("0630400", DateRange(lo=D(1974, 4, 1), hi=D(1974, 4, 30))),  # 日為 00 → 整個月
        ("0150000", DateRange(lo=D(1926, 1, 1), hi=D(1926, 12, 31))),  # 月為 00 → 整年
        ("0000000", DateRange()),
        ("-250701", DateRange()),
        ("012", DateRange()),
        ("", DateRange()),
    ],
)
def test_roc_built(text, expected):
    assert roc_built(text) == expected


def test_parse_counts():
    assert parse_counts("土地1建物1車位2") == (1, 1, 2)
    assert parse_counts("土地2建物0車位0") == (2, 0, 0)
    assert parse_counts("") is None


@pytest.mark.parametrize(
    ("text", "total", "lo", "hi", "whole", "extra", "unknown"),
    [
        ("四層", None, 4, 4, False, (), ()),
        ("二十三層", None, 23, 23, False, (), ()),
        ("一層，二層", None, 1, 2, False, (), ()),
        ("地下二層", None, -2, -2, False, (), ()),
        ("地下層", None, -1, -1, False, (), ()),
        ("地下一層，一層", None, -1, 1, False, (), ()),
        ("全", 3, 1, 3, True, (), ()),
        ("全", None, None, None, True, (), ()),
        ("五層，陽台", None, 5, 5, False, (), ()),
        ("一層，見其他登記事項", None, 1, 1, False, (), ()),
        ("一層，騎樓", None, 1, 1, False, (), ()),
        ("七層，平台", None, 7, 7, False, (), ()),
        ("五層，夾層", None, 5, 5, False, ("mezzanine",), ()),
        ("九層，屋頂突出物", None, 9, 9, False, ("rooftop_structure",), ()),
        ("，", None, None, None, False, (), ()),  # 孤立的逗號
        ("", None, None, None, False, (), ()),
        ("五樓", None, None, None, False, (), ("五樓",)),
    ],
)
def test_parse_transfer_floors(text, total, lo, hi, whole, extra, unknown):
    floors = parse_transfer_floors(text, total)
    assert (floors.lo, floors.hi, floors.whole, floors.extra, floors.unknown) == (
        lo,
        hi,
        whole,
        extra,
        unknown,
    )


def test_parse_total_floors():
    assert parse_total_floors("十五層") == 15
    assert parse_total_floors("四十二層") == 42
    assert parse_total_floors("") is None
    assert parse_total_floors("地下二層") is None


# ------------------------------------------------------------------ 備註決策表（R1-04）

R1_TOP_REMARKS = [
    # R1 §4.1 桃園區前十名的備註字串，每一條一個測試
    ("預售屋、或土地及建物分件登記案件；", "presale_split_reg", ()),
    ("其他增建；", "other_addition", ()),
    ("親友、員工、共有人或其他特殊關係間之交易；", "special_relation", ()),
    ("陽台外推；", None, ("balcony_enclosed",)),
    ("包含公共設施保留地用地；", "public_facility_land", ()),
    ("陽台外推；其他增建；", "other_addition", ("balcony_enclosed",)),
    ("包含公共設施保留地用地；協議價購；", "public_facility_land", ()),
    ("僅車位交易；", "parking_only", ()),
    (
        "親友、員工、共有人或其他特殊關係間之交易；預售屋、或土地及建物分件登記案件；",
        "special_relation",
        (),
    ),
    ("頂樓加蓋；其他增建；", "other_addition", ()),
]


@pytest.mark.parametrize(("remark", "exclusion", "flags"), R1_TOP_REMARKS)
def test_r1_top_remarks(remark, exclusion, flags):
    assert classify_remark(remark) == (exclusion, flags)


@pytest.mark.parametrize(
    ("remark", "exclusion", "flags"),
    [
        # 片語黏在一起：先拿掉保留類，再比對排除類（R1-04 第 4 點）
        ("鐵鋁窗增建其他增建；", "other_addition", ("window_added",)),
        ("鐵鋁窗增建；", None, ("window_added",)),
        ("陽台加窗；", None, ("window_added",)),
        ("後陽台加窗陽台外推", None, ("balcony_enclosed", "window_added")),
        ("陽台外推加窗增建其他增建", "other_addition", ("balcony_enclosed", "window_added")),
        ("陽臺外推", None, ("balcony_enclosed",)),
        # 陽台增建：和陽台外推同一類的小增建，保留並標示
        ("陽台增建", None, ("balcony_enclosed",)),
        # 大增建：排除
        ("頂樓加蓋；", "rooftop_addition", ()),
        ("夾層；", "mezzanine", ()),
        ("夾層增建陽台外推", "mezzanine", ("balcony_enclosed",)),
        ("未登記建物；", "unregistered_building", ()),
        ("防火巷增建", "addition", ()),
        ("露台增建陽台外推", "addition", ("balcony_enclosed",)),
        ("增建部分:陽台", "addition", ()),
        # 露台本身不是增建
        ("本戶有露台", None, ("terrace",)),
        # 含裝潢、傢俱：保留並標示（會把總價拉高）
        ("含裝潢費；", None, ("incl_renovation",)),
        ("含傢俱設備費；", None, ("incl_furniture",)),
        ("內附傢俱設備", None, ("incl_furniture",)),
        # 其他排除
        ("受民情風俗因素影響", "defect", ()),
        ("急買急賣", "urgent_sale", ()),
        ("含租約", "lease", ()),
        ("毛胚屋", "non_standard", ()),
        ("市場攤位", "non_standard", ()),
        ("債務抵償", "debt", ()),
        ("法拍屋", "auction", ()),
        ("畸零地或有合併使用效益", "odd_lot", ()),
        ("政府機關標讓售", "government", ()),
        # 不相干的字不誤判
        ("含土地增值稅或其他稅費", None, ()),
        ("B1機車停車位壹位", None, ()),
        ("", None, ()),
        (None, None, ()),
    ],
)
def test_remark_decision_table(remark, exclusion, flags):
    assert classify_remark(remark) == (exclusion, flags)


# ------------------------------------------------------------------ 一列 → TxFacts


def test_typical_row():
    facts = one(parks=[park_row(1)], builds=[build_row(1)])
    assert facts.serial == serial(1)
    assert facts.release_id == "b20261111"
    assert facts.trade_date == D(2026, 3, 5)
    assert facts.district == "甲區"
    assert facts.address_text == "示意市甲區示意路1號5樓"
    assert facts.btype == BType.BUILDING
    assert (facts.floor_lo, facts.floor_hi, facts.floors_total) == (5, 5, 15)
    assert (facts.bldg_cm2, facts.main_cm2, facts.aux_cm2, facts.balcony_cm2) == (
        12050,
        7020,
        510,
        1030,
    )
    assert facts.built == DateRange(lo=D(2016, 8, 20), hi=D(2016, 8, 20))
    assert facts.elevator == Elevator.YES
    assert facts.parkings == [
        ParkingSpace(ptype=Parking.RAMP_FLAT, price_yuan=1_500_000, area_cm2=3010)
    ]
    assert facts.parking_n == 1
    assert facts.total_yuan == 15_600_000
    assert facts.flags == []
    assert facts.exclusion is None


def test_float_noise_in_areas():
    facts = one(**{"建物移轉總面積平方公尺": "120.50000000000001"})
    assert facts.bldg_cm2 == 12050


def test_zero_main_area_means_unknown_composition():
    facts = one(**{"主建物面積": "0.0", "附屬建物面積": "0.0", "陽台面積": "0.0"})
    assert (facts.main_cm2, facts.aux_cm2, facts.balcony_cm2) == (None, None, None)


@pytest.mark.parametrize(
    ("overrides", "exclusion"),
    [
        ({"交易標的": "土地", "交易筆棟數": "土地1建物0車位0"}, "no_building"),
        ({"交易標的": "車位", "交易筆棟數": "土地0建物0車位1"}, "no_building"),
        ({"交易筆棟數": "土地2建物2車位2"}, "multi_building"),  # 一次買兩戶
        ({"交易年月日": "1040305"}, "presale"),  # 交易日早於完工日
        ({"備註": "預售屋、或土地及建物分件登記案件；"}, "presale_split_reg"),
        ({"移轉層次": "五層，夾層"}, "mezzanine"),
        ({"移轉層次": "九層，屋頂突出物"}, "rooftop_structure"),
    ],
)
def test_structural_exclusions(overrides, exclusion):
    assert one(**overrides).exclusion == exclusion


def test_exclusion_order_first_match_wins():
    # 一次買好幾棟又是預售：回報表上較前面的那條
    facts = one(**{"交易筆棟數": "土地1建物2車位0", "交易年月日": "1040305"})
    assert facts.exclusion == "multi_building"
    presale = one(**{"交易年月日": "1040305", "備註": "其他增建；"})
    assert presale.exclusion == "presale"


def test_unknown_completion_is_flagged_not_excluded():
    # R1-04 第 7 點：完工日讀不出來 → 待確認，不排除
    facts = one(**{"建築完成年月": ""})
    assert facts.built == DateRange()
    assert "built_unknown" in facts.flags
    assert facts.exclusion is None


def test_completion_date_from_build_detail_and_mismatch():
    fallback = one(builds=[build_row(1, done="105年8月20日")], **{"建築完成年月": ""})
    assert fallback.built == DateRange(lo=D(2016, 8, 20), hi=D(2016, 8, 20))
    assert "built_from_detail" in fallback.flags
    mismatch = one(builds=[build_row(1, done="105年9月1日")])
    assert mismatch.built.lo == D(2016, 8, 20)  # 以主檔為準
    assert "built_mismatch" in mismatch.flags
    # 共有部分（建物分層空白）的完工日不拿來比對
    common = one(builds=[build_row(1, done="99年1月1日", layer=""), build_row(1)])
    assert "built_mismatch" not in common.flags


def test_elevator_conflict_flag():
    facts = one(**{"電梯": "無"})
    assert facts.elevator == Elevator.NO
    assert "elevator_conflict" in facts.flags


def test_whole_building_floor():
    facts = one(**{"移轉層次": "全", "總樓層數": "三層", "建物型態": "透天厝"})
    assert (facts.floor_lo, facts.floor_hi) == (1, 3)
    assert "floor_whole" in facts.flags


def test_kept_remark_flags_reach_facts():
    facts = one(parks=[park_row(1)], **{"備註": "陽台外推；含裝潢費；"})
    assert facts.exclusion is None
    assert facts.flags == ["balcony_enclosed", "incl_renovation"]


# ------------------------------------------------------------------ 車位（R1-05 第 1、2 點）


def test_two_spaces_detail_price_zero_uses_main_total():
    # 實價登錄有 662 列：明細價格都是 0，但主檔的車位總價 > 0
    main = main_row(
        **{
            "交易筆棟數": "土地1建物1車位2",
            "車位總價元": "3000001",
            "車位移轉總面積平方公尺": "63.90",
        }
    )
    parks = [park_row(1, price="0", area="31.95"), park_row(1, price="0", area="31.95")]
    facts = one(main, parks=parks)
    assert [p.price_yuan for p in facts.parkings] == [1_500_001, 1_500_000]
    assert [p.area_cm2 for p in facts.parkings] == [3195, 3195]
    assert "parking_price_split" in facts.flags
    assert sum(p.price_yuan for p in facts.parkings) == 3_000_001


def test_spaces_without_detail_rows():
    main = main_row(
        **{
            "交易筆棟數": "土地1建物1車位2",
            "車位類別": "坡道機械",
            "車位總價元": "2000000",
            "車位移轉總面積平方公尺": "0",
        }
    )
    facts = one(main)
    assert facts.parking_n == 2
    assert [p.ptype for p in facts.parkings] == [Parking.MECHANICAL, Parking.MECHANICAL]
    assert [p.price_yuan for p in facts.parkings] == [1_000_000, 1_000_000]
    assert [p.area_cm2 for p in facts.parkings] == [None, None]
    assert {"parking_detail_missing", "parking_price_split"} <= set(facts.flags)


def test_type_without_count_is_not_deducted():
    main = main_row(
        **{
            "交易筆棟數": "土地1建物1車位0",
            "車位類別": "坡道平面",
            "車位總價元": "0",
            "車位移轉總面積平方公尺": "0",
        }
    )
    facts = one(main)
    assert facts.parkings == []
    assert facts.parking_n == 0
    assert "parking_without_count" in facts.flags


def test_per_space_mixed_values():
    main = main_row(
        **{
            "交易筆棟數": "土地1建物1車位2",
            "車位類別": "坡道平面",
            "車位總價元": "1200000",
            "車位移轉總面積平方公尺": "30.00",
        }
    )
    parks = [
        park_row(1, price="1200000", area="0"),
        park_row(1, ptype="升降機械", price="0", area="30.00"),
    ]
    facts = one(main, parks=parks)
    assert facts.parkings == [
        ParkingSpace(ptype=Parking.RAMP_FLAT, price_yuan=1_200_000, area_cm2=None),
        ParkingSpace(ptype=Parking.MECHANICAL, price_yuan=None, area_cm2=3000),
    ]
    assert "parking_price_split" not in facts.flags


def test_no_parking():
    main = main_row(
        **{
            "交易標的": "房地(土地+建物)",
            "交易筆棟數": "土地1建物1車位0",
            "車位類別": "",
            "車位總價元": "0",
            "車位移轉總面積平方公尺": "0.0",
        }
    )
    facts = one(main)
    assert facts.parkings == [] and facts.parking_n == 0
    assert facts.flags == []


def test_other_type_is_unknown():
    facts = one(
        parks=[park_row(1, ptype="其他", price="0", area="")],
        **{"車位類別": "其他", "車位總價元": "0", "車位移轉總面積平方公尺": "0"},
    )
    assert facts.parkings == [ParkingSpace(ptype=Parking.UNKNOWN)]


def test_split_evenly():
    assert split_evenly(1_000_001, 3) == [333_334, 333_334, 333_333]
    assert split_evenly(10, 1) == [10]


# ------------------------------------------------------------------ 略過與整個發布


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"編號": "bad serial"}, "bad_serial"),
        ({"交易年月日": "1150900"}, "bad_trade_date"),
        ({"鄉鎮市區": " "}, "bad_district"),
        ({"總價元": "abc"}, "bad_total"),
    ],
)
def test_row_skipped(overrides, reason):
    with pytest.raises(RowSkipped) as info:
        one(**overrides)
    assert info.value.reason == reason


def test_parse_release_dedupes_and_sorts():
    mains = [
        main_row(3),
        main_row(1, **{"總價元": "11000000"}),
        main_row(1, **{"總價元": "12000000"}),  # 同編號：取後面那筆
        main_row(2),
        main_row(2, **{"交易標的": "土地", "交易筆棟數": "土地1建物0車位0"}),  # 沒有建物的不取代
        main_row(4, **{"交易年月日": "abc"}),
    ]
    parks = [park_row(1), park_row(2), park_row(3)]
    result = parse_release(*release_files(mains, parks), release_id="s115S3")
    assert [f.serial for f in result.facts] == [serial(1), serial(2), serial(3)]
    by_serial = {f.serial: f for f in result.facts}
    assert by_serial[serial(1)].total_yuan == 12_000_000
    assert by_serial[serial(2)].exclusion is None
    assert result.rows == 6
    assert result.skipped == {"duplicate_serial": 2, "bad_trade_date": 1}
    assert result.skipped_total == 3
    assert all(f.release_id == "s115S3" for f in result.facts)
