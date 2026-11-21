"""parse/floor.py：售屋網站的樓層寫法（R1-30、R3-11）。樓層數字都是示意值。"""

import pytest

from house_watch.parse.floor import FloorInfo, parse_floor, parse_single_floor, parse_total_floors


@pytest.mark.parametrize(
    ("text", "lo", "hi", "total"),
    [
        ("1~2樓/共2樓", 1, 2, 2),  # 中信的跨層寫法：不是 1 樓的單層物件
        ("5樓/共0樓", 5, 5, None),  # 共0樓是未知，不是 0
        ("B1-1", -1, 1, None),  # 住商：B1 到 1 樓（回歸測試）
        ("十五樓之1", 15, 15, None),  # 不是 1 樓
        ("8樓之2", 8, 8, None),  # 之2 是戶號，不是 2–8 樓
        ("16F-3", 16, 16, None),  # 樓層後面的 -3 是戶號
        ("1F-2F", 1, 2, None),
        ("4樓～6樓", 4, 6, None),
        ("4至6樓", 4, 6, None),
        ("4、5樓", 4, 5, None),
        ("B2", -2, -2, None),
        ("地下二樓", -2, -2, None),
        ("地下1樓~2樓", -1, 2, None),
        ("4樓共15樓", 4, 4, 15),
        ("7F/15F", 7, 7, 15),
        ("七樓/十五樓", 7, 7, 15),
        ("頂樓/15樓", 15, 15, 15),
        ("整棟/3樓", 1, 3, 3),
    ],
)
def test_parse_floor(text, lo, hi, total):
    info = parse_floor(text)
    assert (info.lo, info.hi, info.total) == (lo, hi, total)


def test_whole_top_and_unknown_markers():
    assert parse_floor("整棟").whole
    assert parse_floor("整棟") == FloorInfo(whole=True)  # 不知道總樓層時區間也未知
    assert parse_floor("頂樓/15樓").top
    assert parse_floor("頂加").notes == ("rooftop_addition",)
    assert parse_floor("地下室").notes == ("basement_unspecified",)
    assert parse_floor("--") == FloorInfo()
    assert parse_floor("") == FloorInfo()


def test_only_explicit_range_symbols_mean_multi_floor():
    # 逗號不是範圍符號（R3-11）：看不懂就是看不懂，不猜
    assert parse_floor("4,6樓").notes == ("floor_unparsed",)
    assert parse_floor("4,6樓").lo is None


def test_floor_above_total_is_kept_and_noted():
    info = parse_floor("15/10")
    assert (info.lo, info.total) == (15, 10)
    assert info.notes == ("floor_above_total",)


def test_total_from_separate_field():
    assert parse_floor("6樓", total_text="共9樓").total == 9
    assert parse_floor("6樓/共11樓", total_text="共9樓").total == 11  # 欄位裡寫的優先


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", 3),
        ("3樓", 3),
        ("3F", 3),
        ("三樓", 3),
        ("B1", -1),
        ("地下3層", -3),
        ("地下層", -1),
        ("0", None),
        ("abc", None),
        ("", None),
    ],
)
def test_parse_single_floor(text, expected):
    assert parse_single_floor(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("共15樓", 15),
        ("15F", 15),
        ("地上15層", 15),
        ("十五層", 15),
        ("15", 15),
        ("共0樓", None),
        ("", None),
        (None, None),
        ("地上15層地下3層", 15),
    ],
)
def test_parse_total_floors(text, expected):
    assert parse_total_floors(text) == expected
