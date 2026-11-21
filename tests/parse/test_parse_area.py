"""parse/area.py：坪與 m² → 0.01 單位整數；換算用 400/121（R3-10、R3-11）。數字都是示意值。"""

from fractions import Fraction

import pytest

from house_watch.parse.area import (
    PING_M2,
    Area,
    cm2_to_cp,
    cm2_to_cp_exact,
    cp_to_cm2,
    cp_to_cm2_exact,
    find_area,
    find_areas,
    m2_to_cm2,
    parse_area,
    ping_to_cp,
)


def test_conversion_constant_is_exact_fraction():
    assert Fraction(400, 121) == PING_M2
    # R3-10 的反例：100.00 m² 總價 1,210 萬，用精確分數剛好是 40.000 萬／坪。
    unit_price_wan = Fraction(12_100_000, 10_000) / (Fraction(10_000, 100) / PING_M2)
    assert unit_price_wan == 40
    # 近似值 3.3058 會判反
    assert Fraction(12_100_000, 10_000) / (Fraction(100) / Fraction("3.3058")) > 40


def test_r3_10_rounding_counterexample():
    # 99.17 m² 換算 29.9989… 坪；先四捨五入到 0.01 坪就會變成 30.00。
    exact = cm2_to_cp_exact(9917)
    assert exact < 3000
    assert cm2_to_cp(9917) == 2999  # 無條件捨去，不會因為進位而過門檻
    assert 121 * 9917 < 400 * 3000  # 整數交叉相乘的寫法


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("88.45", 8845),
        ("45.2100000000001", 4521),  # 實價登錄的浮點雜訊
        ("45.20", 4520),
        ("0", 0),
        ("1,234.5", 123450),
        (12.34, 1234),
        ("0.005", 1),  # 四捨五入到 0.01
    ],
)
def test_m2_to_cm2(value, expected):
    assert m2_to_cm2(value) == expected


def test_bad_area_values():
    with pytest.raises(ValueError):
        m2_to_cm2("abc")
    with pytest.raises(ValueError):
        m2_to_cm2("-1")
    with pytest.raises(ValueError):
        ping_to_cp("inf")


def test_ping_and_round_trips():
    assert ping_to_cp("30.25") == 3025
    assert cm2_to_cp(10000) == 3025  # 100 m² 剛好是 30.25 坪
    assert cp_to_cm2(3025) == 10000
    assert cp_to_cm2(100) == 330  # 1 坪＝3.3057… m²，捨去
    assert cp_to_cm2_exact(121) == 400


@pytest.mark.parametrize(
    ("text", "hundredths", "unit"),
    [
        ("88.45㎡", 8845, "m2"),  # NFKC 之後是 88.45m2，不會多抓一個 2
        ("88.45m²", 8845, "m2"),
        ("88.45 平方公尺", 8845, "m2"),
        ("主建物 30.25 坪", 3025, "ping"),
        ("30.25坪（100.00㎡）", 10000, "m2"),  # 兩種單位都有時取 m²
    ],
)
def test_parse_area(text, hundredths, unit):
    area = parse_area(text)
    assert area is not None
    assert (area.hundredths, area.unit) == (hundredths, unit)


def test_parse_area_ambiguous_or_missing():
    assert parse_area("2坪 3坪") is None
    assert parse_area("約三十坪") is None
    assert parse_area("") is None
    assert parse_area("30.25", default_unit="ping") == Area(3025, "ping", 0, 5)
    assert parse_area("30.25") is None


def test_area_unit_conversions():
    assert Area(10000, "m2").cp == 3025
    assert Area(3025, "ping").cm2 == 10000
    assert Area(8845, "m2").cm2 == 8845


def test_find_area_by_label():
    text = "權狀 45.67坪 主建物：30.25坪 附屬 2.1坪"
    found = find_area(text, "主建物")
    assert found is not None and (found.hundredths, found.unit) == (3025, "ping")
    assert find_area(text, "陽台") is None
    assert len(find_areas(text)) == 3
    with pytest.raises(ValueError):
        find_area(text, " ")
