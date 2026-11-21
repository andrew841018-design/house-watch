"""lvr/unitprice.py：扣車位單價的分子與分母區間、分層中位數（R1-05、R3-12、r4 §2.2 #4）。

中位數與成交都是合成的示意值，不是任何區的真實行情。
"""

import datetime as dt
from fractions import Fraction

import pytest
from lvr_testkit import main_row, park_row

from house_watch import db
from house_watch.lvr.parse import parse_row
from house_watch.lvr.unitprice import (
    CITY,
    FLAT_GROUP,
    MIN_SAMPLES,
    MedianGroup,
    MedianTable,
    Quartiles,
    compute_medians,
    load_medians,
    lookup_chain,
    nearest_rank,
    quartiles,
    store_medians,
    unit_price,
)
from house_watch.models import Parking, ParkingSpace, TxFacts, UpBasis

D = dt.date
P = Parking

RAMP_PRICE = Quartiles(n=40, p25=1_420_000, p50=1_560_000, p75=1_710_000)
RAMP_AREA = Quartiles(n=35, p25=2_650, p50=2_990, p75=3_320)
FLAT_PRICE = Quartiles(n=60, p25=1_300_000, p50=1_500_000, p75=1_650_000)
FLAT_AREA = Quartiles(n=50, p25=2_500, p50=2_900, p75=3_200)
CITY_RAMP_PRICE = Quartiles(n=900, p25=1_350_000, p50=1_480_000, p75=1_600_000)
CITY_RAMP_AREA = Quartiles(n=800, p25=2_700, p50=3_000, p75=3_300)
MECH_PRICE = Quartiles(n=30, p25=880_000, p50=1_010_000, p75=1_150_000)
MECH_AREA = Quartiles(n=25, p25=1_210, p50=1_330, p75=1_460)


def group(level, district, ptype, price, area):
    return (level, district, ptype), MedianGroup(level, district, ptype, price, area)


MEDIANS = MedianTable(
    version="v-test",
    groups=dict(
        [
            group("district", "甲區", "ramp_flat", RAMP_PRICE, RAMP_AREA),
            group(
                "district",
                "甲區",
                "lift_flat",
                Quartiles(n=3, p25=1, p50=2, p75=3),
                Quartiles(n=2, p25=1, p50=2, p75=3),
            ),  # 樣本不足 → 退到下一層
            group("district_flat", "甲區", FLAT_GROUP, FLAT_PRICE, FLAT_AREA),
            group("city", CITY, "ramp_flat", CITY_RAMP_PRICE, CITY_RAMP_AREA),
            group("city", CITY, "mechanical", MECH_PRICE, MECH_AREA),
        ]
    ),
)


def tx(spaces=(), **overrides) -> TxFacts:
    data = {
        "serial": "TESTSERIAL000001XYZ",
        "release_id": "b20261111",
        "trade_date": D(2026, 3, 5),
        "district": "甲區",
        "total_yuan": 15_600_000,
        "bldg_cm2": 12_050,
        "main_cm2": 7_020,
        "aux_cm2": 510,
        "balcony_cm2": 1_030,
        "parkings": list(spaces),
        "parking_n": len(spaces),
    }
    data.update(overrides)
    return TxFacts(**data)


def space(ptype=P.RAMP_FLAT, price=None, area=None):
    return ParkingSpace(ptype=ptype, price_yuan=price, area_cm2=area)


# ------------------------------------------------------------------ basis 與區間


def test_official_split_is_exact():
    facts = tx([space(price=1_500_000, area=3_010)])
    result = unit_price(facts, MEDIANS)
    up = result.up
    assert up.basis == UpBasis.OFFICIAL
    assert (up.q_lo, up.q_hi, up.k_lo, up.k_hi) == (14_100_000, 14_100_000, 9_040, 9_040)
    assert result.flags == ()
    # 等於官方公式二：(總價 − 車位價) ÷ (總面積 − 車位面積)，單位元／m²
    assert Fraction(up.q_lo * 100, up.k_lo) == Fraction(15_600_000 - 1_500_000) / (
        Fraction("120.50") - Fraction("30.10")
    )


def test_no_parking_is_official_total_over_area():
    up = unit_price(tx(), MEDIANS).up
    assert up.basis == UpBasis.OFFICIAL
    assert (up.q_lo, up.q_hi, up.k_lo, up.k_hi) == (15_600_000, 15_600_000, 12_050, 12_050)


def test_price_bundled_area_registered():
    # 車位價含在總價裡（車位價＝0）、車位面積已登記：q 是區間，k 精確
    result = unit_price(tx([space(area=3_010)]), MEDIANS)
    up = result.up
    assert up.basis == UpBasis.ESTIMATED
    assert (up.q_lo, up.q_hi) == (15_600_000 - 1_710_000, 15_600_000 - 1_420_000)
    assert (up.k_lo, up.k_hi) == (9_040, 9_040)
    assert result.flags == ("up_price_district",)


def test_price_split_area_not_registered():
    # 官方有車位價、但車位面積沒有單獨登記：官方公式會不扣面積（單價偏低），這裡要估扣面積
    result = unit_price(tx([space(price=1_500_000)]), MEDIANS)
    up = result.up
    assert up.basis == UpBasis.ESTIMATED
    assert (up.q_lo, up.q_hi) == (14_100_000, 14_100_000)
    assert (up.k_lo, up.k_hi) == (12_050 - 3_320, 12_050 - 2_650)
    assert result.flags == ("up_area_district",)


def test_both_estimated_and_crossed_bounds():
    result = unit_price(tx([space()]), MEDIANS)
    up = result.up
    assert up.basis == UpBasis.ESTIMATED
    assert (up.q_lo, up.q_hi) == (13_890_000, 14_180_000)
    assert (up.k_lo, up.k_hi) == (8_730, 9_400)
    assert result.flags == ("up_area_district", "up_price_district")
    # 交叉配對（瀏覽器做）：上界＝q_hi/k_lo、下界＝q_lo/k_hi；比同方向配對寬
    crossed = (Fraction(up.q_lo, up.k_hi), Fraction(up.q_hi, up.k_lo))
    same_dir = sorted([Fraction(up.q_lo, up.k_lo), Fraction(up.q_hi, up.k_hi)])
    assert crossed[0] < same_dir[0] and crossed[1] > same_dir[1]


def test_multiple_spaces_known_and_estimated():
    spaces = [space(price=1_600_000, area=3_000), space(P.MECHANICAL)]
    result = unit_price(tx(spaces, bldg_cm2=15_000), MEDIANS)
    up = result.up
    # 機械車位在甲區沒有自己的組 → 退到全市（不會用平面類）
    assert result.flags == ("up_area_city", "up_price_city")
    assert (up.q_lo, up.q_hi) == (
        15_600_000 - 1_600_000 - 1_150_000,
        15_600_000 - 1_600_000 - 880_000,
    )
    assert (up.k_lo, up.k_hi) == (15_000 - 3_000 - 1_460, 15_000 - 3_000 - 1_210)


def test_fallback_levels():
    # 升降平面在甲區只有 3 筆 → 同區所有平面類
    lift = unit_price(tx([space(P.LIFT_FLAT)]), MEDIANS)
    assert lift.flags == ("up_area_district_flat", "up_price_district_flat")
    assert lift.up.q_hi == 15_600_000 - FLAT_PRICE.p25
    # 乙區沒有任何組 → 全市同類型；絕不借用甲區的數字
    other = unit_price(tx([space()], district="乙區"), MEDIANS)
    assert other.flags == ("up_area_city", "up_price_city")
    assert other.up.q_hi == 15_600_000 - CITY_RAMP_PRICE.p25


def test_lookup_chain_never_crosses_districts():
    assert lookup_chain("甲區", P.RAMP_FLAT) == [
        ("district", "甲區", "ramp_flat"),
        ("district_flat", "甲區", FLAT_GROUP),
        ("city", CITY, "ramp_flat"),
    ]
    assert lookup_chain("甲區", P.MECHANICAL) == [
        ("district", "甲區", "mechanical"),
        ("city", CITY, "mechanical"),
    ]
    assert lookup_chain("甲區", P.UNKNOWN) == []


def test_area_deduction_is_capped_at_common_parts():
    # R1-05 第 3 點：估扣面積不能讓分母小於主建物＋附屬＋陽台
    facts = tx(
        [space(price=1_000_000)], bldg_cm2=9_000, main_cm2=7_000, aux_cm2=500, balcony_cm2=1_000
    )
    result = unit_price(facts, MEDIANS)
    assert (result.up.k_lo, result.up.k_hi) == (8_500, 8_500)
    assert "up_area_capped" in result.flags


def test_cap_only_on_the_high_end():
    facts = tx(
        [space(price=1_000_000)], bldg_cm2=11_000, main_cm2=7_000, aux_cm2=500, balcony_cm2=500
    )
    result = unit_price(facts, MEDIANS)
    # 共有部分剩 30.00 m²：p25 2,650 扣得下，p75 3,320 只能扣 3,000
    assert (result.up.k_lo, result.up.k_hi) == (8_000, 11_000 - 2_650)
    assert "up_area_capped" in result.flags


def test_missing_main_area_means_no_cap():
    result = unit_price(
        tx([space(price=1_000_000)], main_cm2=None, aux_cm2=None, balcony_cm2=None), MEDIANS
    )
    assert result.up.basis == UpBasis.ESTIMATED
    assert "up_no_main" in result.flags


@pytest.mark.parametrize(
    ("facts", "medians", "flag"),
    [
        (tx([space(P.UNKNOWN)]), MEDIANS, "up_unknown_ptype"),  # 實價登錄的「其他」
        (tx([space()]), None, "up_no_median"),
        (tx([space(P.GROUND_FLAT)], district="乙區"), MEDIANS, "up_no_median"),
        (tx(bldg_cm2=None), MEDIANS, "up_no_base"),
        (tx(total_yuan=0), MEDIANS, "up_no_base"),
        (tx([space(price=20_000_000, area=1_000)]), MEDIANS, "up_nonpositive"),
    ],
)
def test_none_cases(facts, medians, flag):
    result = unit_price(facts, medians)
    assert result.up.basis == UpBasis.NONE
    assert (result.up.q_lo, result.up.q_hi, result.up.k_lo, result.up.k_hi) == (None,) * 4
    assert flag in result.flags


def test_unknown_space_with_known_values_is_still_official():
    # 類型不明但價格、面積都有申報：不用估算，照官方值扣
    up = unit_price(tx([space(P.UNKNOWN, price=900_000, area=1_500)]), MEDIANS).up
    assert up.basis == UpBasis.OFFICIAL


def test_official_price_never_overridden_and_remarks_never_read():
    # 備註寫了車位價：官方價 > 0 時照官方；官方價 = 0 時用中位數估，不看備註（r4 F4 延後）
    remark = {"備註": "車位價格250萬"}
    official = parse_row(main_row(**remark), (), [park_row(1)], release_id="b20261111")
    assert unit_price(official, MEDIANS).up.q_lo == 15_600_000 - 1_500_000
    bundled = parse_row(
        main_row(**remark, **{"車位總價元": "0"}),
        (),
        [park_row(1, price="0")],
        release_id="b20261111",
    )
    up = unit_price(bundled, MEDIANS).up
    assert up.basis == UpBasis.ESTIMATED
    assert (up.q_lo, up.q_hi) == (15_600_000 - 1_710_000, 15_600_000 - 1_420_000)


# ------------------------------------------------------------------ 中位數


def test_nearest_rank_quartiles():
    assert quartiles([40, 10, 30, 20]) == Quartiles(n=4, p25=10, p50=20, p75=30)
    assert quartiles([5, 1, 4, 2, 3]) == Quartiles(n=5, p25=2, p50=3, p75=4)
    assert quartiles([7]) == Quartiles(n=1, p25=7, p50=7, p75=7)
    assert quartiles([]) == Quartiles(n=0)
    assert nearest_rank([3, 1, 2], 100) == 3


SAMPLE_DAY = D(2026, 3, 1)


def sample_tx(n, district="甲區", trade=SAMPLE_DAY, spaces=(), flags=(), exclusion=None):
    return tx(
        spaces,
        serial=f"TESTSERIAL{n:06d}XYZ",
        district=district,
        trade_date=trade,
        flags=list(flags),
        exclusion=exclusion,
    )


def test_compute_medians_samples_and_levels():
    as_of = D(2026, 10, 31)
    txs = [
        sample_tx(1, spaces=[space(price=1_000_000, area=3_000)]),
        sample_tx(2, spaces=[space(price=2_000_000, area=2_000)]),
        sample_tx(3, spaces=[space(P.LIFT_FLAT, price=3_000_000)]),
        sample_tx(4, spaces=[space(P.MECHANICAL, price=800_000, area=1_200)]),
        sample_tx(5, district="乙區", spaces=[space(price=4_000_000, area=4_000)]),
        # 以下都不算樣本
        sample_tx(6, spaces=[space(price=9_000_000)], exclusion="special_relation"),
        sample_tx(7, trade=D(2024, 10, 31), spaces=[space(price=9_000_000)]),  # 窗外
        sample_tx(8, trade=D(2026, 11, 1), spaces=[space(price=9_000_000)]),  # as_of 之後
        sample_tx(
            9,
            spaces=[space(price=4_500_000), space(price=4_500_000)],
            flags=["parking_price_split"],
        ),  # 由總價平分出來的
        sample_tx(10, spaces=[space(price=10)]),  # 明顯錯誤的值
        sample_tx(11, spaces=[space(P.UNKNOWN, price=1_000_000)]),  # 類型不明
    ]
    table = compute_medians(txs, version="v1", as_of=as_of)
    g = table.groups
    assert g[("district", "甲區", "ramp_flat")].price == quartiles([1_000_000, 2_000_000])
    assert g[("district", "甲區", "ramp_flat")].area == quartiles([3_000, 2_000])
    assert g[("district_flat", "甲區", "flat")].price == quartiles(
        [1_000_000, 2_000_000, 3_000_000]
    )
    assert g[("district", "甲區", "mechanical")].price.n == 1
    assert ("district_flat", "甲區", "mechanical") not in g
    assert g[("city", "*", "ramp_flat")].price == quartiles([1_000_000, 2_000_000, 4_000_000])
    assert g[("district", "乙區", "ramp_flat")].price.n == 1
    assert all(key[2] != "unknown" for key in g)
    assert table.version == "v1"


def test_window_is_anchored_on_data_version_not_clock():
    txs = [sample_tx(1, trade=D(2025, 1, 1), spaces=[space(price=1_000_000)])]
    old = compute_medians(txs, version="v1", as_of=D(2026, 10, 31))
    assert old.groups[("district", "甲區", "ramp_flat")].price.n == 1
    later = compute_medians(txs, version="v2", as_of=D(2027, 2, 1))
    assert ("district", "甲區", "ramp_flat") not in later.groups


def test_min_samples_is_enforced():
    table = MedianTable(
        version="v",
        groups=dict(
            [
                group(
                    "district",
                    "甲區",
                    "ramp_flat",
                    Quartiles(n=MIN_SAMPLES - 1, p25=1, p50=2, p75=3),
                    Quartiles(n=MIN_SAMPLES, p25=1, p50=2, p75=3),
                )
            ]
        ),
    )
    assert table.lookup("甲區", P.RAMP_FLAT, "price") is None
    assert table.lookup("甲區", P.RAMP_FLAT, "area") == (
        Quartiles(n=MIN_SAMPLES, p25=1, p50=2, p75=3),
        "district",
    )
    assert table.lookup("甲區", P.RAMP_FLAT, "price", min_n=1) is not None


def test_store_and_load_medians(tmp_path):
    conn = db.init_db(tmp_path / "hw.sqlite")
    try:
        with db.transaction(conn):
            store_medians(conn, MEDIANS)
        loaded = load_medians(conn, "v-test")
        assert loaded == MEDIANS
        assert load_medians(conn, "other") is None
        replaced = MedianTable(version="v-next", groups={})
        with db.transaction(conn):
            store_medians(conn, replaced)
        assert load_medians(conn, "v-test") is None  # 舊版本一起刪掉
    finally:
        conn.close()
