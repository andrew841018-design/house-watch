"""parse/elevator.py：建物型態與電梯決策表（DESIGN 4-6、r4 對 R1-16 的裁決）。"""

import pytest

from house_watch.models import BType, Elevator
from house_watch.parse.elevator import (
    decide_elevator,
    elevator_from_field,
    elevator_from_type_text,
    parse_btype,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("住宅大樓(11層含以上有電梯)", BType.BUILDING),
        ("華廈(10層含以下有電梯)", BType.MANSION),
        ("公寓(5樓含以下無電梯)", BType.WALKUP),
        ("透天厝", BType.TOWNHOUSE),
        ("電梯大樓", BType.BUILDING),
        ("其他", BType.OTHER),
        ("廠辦", BType.OTHER),
        ("", None),
        (None, None),
    ],
)
def test_parse_btype(text, expected):
    assert parse_btype(text) == expected


def test_field_values():
    assert elevator_from_field("有") == Elevator.YES
    assert elevator_from_field("無") == Elevator.NO
    assert elevator_from_field(True) == Elevator.YES
    assert elevator_from_field(False) == Elevator.NO
    assert elevator_from_field("") is None
    assert elevator_from_field("不詳") is None
    assert elevator_from_type_text("公寓(5樓含以下無電梯)") == Elevator.NO
    assert elevator_from_type_text("電梯大樓") is None  # 沒有明寫有無


def test_priority_1_explicit_field_wins():
    assert decide_elevator(field="有", site_btype=BType.WALKUP) == (Elevator.YES, ())
    assert decide_elevator(field="無", site_btype=BType.BUILDING) == (Elevator.NO, ())


def test_priority_2_lvr_type_text():
    assert decide_elevator(type_text="住宅大樓(11層含以上有電梯)") == (Elevator.YES, ())
    assert decide_elevator(type_text="公寓(5樓含以下無電梯)") == (Elevator.NO, ())


def test_priority_3_site_type_is_inferred():
    assert decide_elevator(site_btype=BType.BUILDING) == (Elevator.INFERRED, ())
    assert decide_elevator(site_btype=BType.MANSION) == (Elevator.INFERRED, ())


def test_priority_4_unknown():
    # 公寓、透天沒有明確欄位時是未知；總樓層的推定不在 Python 做（r4：網頁上的切換）
    assert decide_elevator(site_btype=BType.WALKUP) == (Elevator.UNKNOWN, ())
    assert decide_elevator(site_btype=BType.TOWNHOUSE) == (Elevator.UNKNOWN, ())
    assert decide_elevator() == (Elevator.UNKNOWN, ())


def test_conflict_keeps_field_and_flags():
    # 實價登錄：住宅大樓但電梯=無（全市 2 筆）→ 以欄位為準並標示矛盾
    assert decide_elevator(field="無", type_text="住宅大樓(11層含以上有電梯)") == (
        Elevator.NO,
        ("elevator_conflict",),
    )
    assert decide_elevator(field="有", type_text="公寓(5樓含以下無電梯)") == (
        Elevator.YES,
        ("elevator_conflict",),
    )
