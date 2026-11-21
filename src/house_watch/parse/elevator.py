"""建物型態與電梯（WP4）。

電梯照有優先順序的決策表判斷（DESIGN 4-6；r4 對 R1-16 的裁決）：

| 順序 | 依據 | 結果 |
|---|---|---|
| 1 | 明確的電梯欄位（有／無） | yes／no |
| 2 | 實價登錄的型態文字，例如「住宅大樓(11層含以上有電梯)」「公寓(5樓含以下無電梯)」 | yes／no |
| 3 | 網站寫的型態是大樓或華廈 | inferred（推定有，算通過，網頁加徽章） |
| 4 | 以上都沒有 | unknown |

- 欄位和型態文字矛盾時以欄位為準，加註 elevator_conflict。
- 「總樓層 ≥ 6 推定有電梯」不在 Python 做：r4 把它改成網頁上的一個切換。
"""

import re

from house_watch.models import BType, Elevator
from house_watch.parse.text import compact

_BTYPE_PATTERNS: tuple[tuple[str, BType], ...] = (
    (r"華廈", BType.MANSION),
    (r"大樓|電梯大廈|住宅大廈|摩天", BType.BUILDING),
    (r"公寓", BType.WALKUP),
    (r"透天|別墅|樓中樓透天", BType.TOWNHOUSE),
)


def parse_btype(text: str | None) -> BType | None:
    """型態文字 → BType。空白回 None；看得到文字但不是四種住宅型態的，歸「其他」。

    例：「住宅大樓(11層含以上有電梯)」→ 大樓、「華廈(10層含以下有電梯)」→ 華廈、
    「透天厝」→ 透天、「廠辦」→ 其他。
    """
    s = compact(text)
    if not s:
        return None
    for pattern, btype in _BTYPE_PATTERNS:
        if re.search(pattern, s):
            return btype
    return BType.OTHER


def elevator_from_field(value: str | bool | None) -> Elevator | None:
    """明確的電梯欄位：有／是／Y／True → yes；無／否／N／False → no；其他 → None。"""
    if isinstance(value, bool):
        return Elevator.YES if value else Elevator.NO
    s = compact(value).lower()
    if s in ("有", "是", "y", "yes", "true", "1", "有電梯"):
        return Elevator.YES
    if s in ("無", "否", "沒有", "n", "no", "false", "0", "無電梯"):
        return Elevator.NO
    return None


def elevator_from_type_text(text: str | None) -> Elevator | None:
    """型態文字明寫「有電梯」「無電梯」時回傳 yes／no，否則 None。"""
    s = compact(text)
    if "無電梯" in s:
        return Elevator.NO
    if "有電梯" in s:
        return Elevator.YES
    return None


def decide_elevator(
    *,
    field: str | bool | None = None,
    type_text: str | None = None,
    site_btype: BType | None = None,
) -> tuple[Elevator, tuple[str, ...]]:
    """依決策表判斷電梯，回傳（結果, 附註代碼）。"""
    explicit = elevator_from_field(field)
    by_type = elevator_from_type_text(type_text)
    flags: tuple[str, ...] = ()
    if explicit is not None and by_type is not None and explicit != by_type:
        flags = ("elevator_conflict",)
    if explicit is not None:
        return explicit, flags
    if by_type is not None:
        return by_type, flags
    if site_btype in (BType.BUILDING, BType.MANSION):
        return Elevator.INFERRED, flags
    return Elevator.UNKNOWN, flags
