"""parse/money.py：億／萬／元 → 整數元；不取「第一個數字」（R3-11）。金額都是示意值。"""

import pytest

from house_watch.parse.money import (
    PRICE_RANGE,
    find_amounts,
    find_money,
    parse_money,
    sane_price,
    to_yuan,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2,350萬", 23_500_000),
        ("2350萬", 23_500_000),
        ("總價：2,345 萬元", 23_450_000),
        ("１，２３４萬", 12_340_000),  # 全形數字與全形逗號
        ("1億2,000萬", 120_000_000),  # R3-11：不是 1 萬
        ("1.2億", 120_000_000),
        ("2,345.5萬", 23_455_000),
        ("12,340,000元", 12_340_000),
        ("NT$12,340,000", 12_340_000),
        ("新台幣 987萬元整", 9_870_000),
        ("總價 2,345 萬（單價 45.6萬/坪）", 23_450_000),  # 單價不算總價
    ],
)
def test_parse_money_single_amount(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "原價2,480萬 現價2,350萬",  # 兩個金額要靠標籤區分（R3-11）
        "↓100萬 2,350萬",  # 降幅與現價
        "12,34萬",  # 壞的千分位，不能拆出「34萬」
        "45.6萬/坪",  # 只有單價
        "洽詢",
        "",
        None,
    ],
)
def test_parse_money_none_when_ambiguous_or_missing(text):
    assert parse_money(text) is None


def test_bare_number_needs_default_unit():
    assert parse_money("2345") is None
    assert parse_money("2345", default_unit="萬") == 23_450_000
    assert parse_money("2,345", default_unit="萬") == 23_450_000


def test_find_money_by_label():
    text = "原價2,480萬 現價2,350萬"
    assert find_money(text, "現價") == 23_500_000
    assert find_money(text, "原價") == 24_800_000
    assert find_money("原價：NT$ 24,800,000", "原價") == 24_800_000
    assert find_money(text, "車位價") is None
    # 標籤和金額之間有其他字，就不算
    assert find_money("現價請洽 2,350萬", "現價") is None
    with pytest.raises(ValueError):
        find_money(text, "")


def test_find_amounts_positions():
    amounts = find_amounts("原價2,480萬 現價2,350萬")
    assert [a.yuan for a in amounts] == [24_800_000, 23_500_000]
    assert amounts[0].start < amounts[1].start


def test_to_yuan_uses_decimal():
    assert to_yuan("2345.5") == 23_455_000
    assert to_yuan(1.1) == 11_000  # 不是 10999.999…
    assert to_yuan(3, "億") == 300_000_000
    assert to_yuan("1,234", "元") == 1_234
    with pytest.raises(ValueError):
        to_yuan("abc")
    with pytest.raises(ValueError):
        to_yuan("nan")


def test_sane_price_range():
    lo, hi = PRICE_RANGE
    assert sane_price(23_500_000) == 23_500_000
    assert sane_price(lo) == lo and sane_price(hi) == hi
    assert sane_price(2_350) is None  # 單位寫錯（萬當成元）
    assert sane_price(hi + 1) is None
    assert sane_price(None) is None
    assert sane_price(5_000, lo=1_000, hi=10_000) == 5_000
