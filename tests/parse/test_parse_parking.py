"""parse/parking.py：結構化欄位優先、片語表最長優先、混合車位規則（R1-12）。"""

import pytest

from house_watch.models import Parking
from house_watch.parse.parking import (
    FLAT_TYPES,
    PHRASES,
    classify_access_form,
    combine,
    is_public,
    parse_parking,
    parse_parking_spaces,
)

P = Parking


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # R1-12 反例 1：「機械升降平面」是升降平面，不是機械
        ("機械升降平面車位", P.LIFT_FLAT),
        ("升降平面", P.LIFT_FLAT),
        # 住商：「平移」是機械式，不能因為有「平」字就算平面
        ("坡道機械式平移大車位", P.MECHANICAL),
        ("坡道平面", P.RAMP_FLAT),
        ("一樓平面", P.GROUND_FLAT),
        ("1樓平面", P.GROUND_FLAT),
        ("坡道機械", P.MECHANICAL),
        ("升降機械", P.MECHANICAL),
        ("塔式車位", P.MECHANICAL),
        ("機械車位", P.MECHANICAL),
        ("平面式", P.FLAT_UNKNOWN_ACCESS),  # 是平面，但出入方式不明
        ("平面車位", P.FLAT_UNKNOWN_ACCESS),
        ("(坡道-平面)", P.RAMP_FLAT),  # 21 世紀的結構化寫法
        ("平面(坡道)", P.RAMP_FLAT),
        ("無", P.NONE),
        ("無車位", P.NONE),
        ("其他", P.UNKNOWN),  # 實價登錄的「其他」
        ("", P.UNKNOWN),
        (None, P.UNKNOWN),
    ],
)
def test_parse_parking(text, expected):
    assert parse_parking(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("平面式+機械式", P.MIXED_WITH_FLAT),  # 591 的混合寫法
        ("升降平面、坡道平面", P.MIXED_WITH_FLAT),  # 兩個都是平面，但類型不同
        ("坡道平面及坡道機械", P.MIXED_WITH_FLAT),
        ("坡道機械、塔式", P.MECHANICAL),  # 類型相同就合成一種
        ("坡道平面×2", P.RAMP_FLAT),
    ],
)
def test_multiple_spaces(text, expected):
    assert parse_parking(text) == expected


def test_space_counts():
    assert parse_parking_spaces("坡道平面×2") == [P.RAMP_FLAT, P.RAMP_FLAT]
    assert parse_parking_spaces("坡道平面2位") == [P.RAMP_FLAT, P.RAMP_FLAT]
    assert parse_parking_spaces("機械車位x3") == [P.MECHANICAL] * 3
    assert parse_parking_spaces("升降平面、坡道平面") == [P.LIFT_FLAT, P.RAMP_FLAT]
    # 同一段裡的片語在描述同一個車位
    assert parse_parking_spaces("坡道機械式平移大車位") == [P.MECHANICAL]


def test_combine_rules():
    assert combine([]) == P.UNKNOWN
    assert combine([P.MECHANICAL, P.MECHANICAL]) == P.MECHANICAL
    assert combine([P.RAMP_FLAT, P.MECHANICAL]) == P.MIXED_WITH_FLAT
    assert combine([P.MECHANICAL, P.UNKNOWN]) == P.UNKNOWN  # 看不懂的那個可能是平面
    assert combine([P.RAMP_FLAT, P.UNKNOWN]) == P.MIXED_WITH_FLAT
    assert combine([P.NONE, P.RAMP_FLAT]) == P.UNKNOWN  # 互相矛盾


def test_combine_keeps_flat_from_an_already_mixed_value():
    # 已經是「混合含平面」的值（例如同一段寫了平面與機械）再和別的車位合併，仍然含平面
    assert combine([P.MIXED_WITH_FLAT, P.MECHANICAL]) == P.MIXED_WITH_FLAT
    assert combine([P.MIXED_WITH_FLAT, P.UNKNOWN]) == P.MIXED_WITH_FLAT
    assert combine([P.MIXED_NO_FLAT, P.RAMP_FLAT]) == P.MIXED_WITH_FLAT
    assert combine([P.MIXED_NO_FLAT, P.MECHANICAL]) == P.MIXED_NO_FLAT
    assert parse_parking("坡道平面機械+機械") == P.MIXED_WITH_FLAT


def test_phrase_table_is_longest_first_safe():
    # 每個片語單獨比對都得到自己的類型，不會被比它短的片語搶走
    for phrase, ptype in PHRASES:
        assert parse_parking(phrase) == ptype, phrase
    assert FLAT_TYPES == {P.RAMP_FLAT, P.GROUND_FLAT, P.LIFT_FLAT, P.FLAT_UNKNOWN_ACCESS}


@pytest.mark.parametrize(
    ("access", "form", "expected"),
    [
        ("坡道", "平面", P.RAMP_FLAT),
        ("升降", "平面", P.LIFT_FLAT),
        ("車梯", "平面式", P.LIFT_FLAT),
        ("一樓", "平面", P.GROUND_FLAT),
        ("", "平面", P.FLAT_UNKNOWN_ACCESS),
        ("坡道", "機械", P.MECHANICAL),
        ("坡道", "平移", P.MECHANICAL),
        (None, None, P.UNKNOWN),
        ("", "坡道平面", P.RAMP_FLAT),
    ],
)
def test_structured_fields_first(access, form, expected):
    assert classify_access_form(access, form) == expected


def test_public_parking():
    assert is_public("公有") is True
    assert is_public("抽籤車位") is True
    assert is_public("私有") is False
    assert is_public("") is None
