"""車位類型（WP4）：結構化欄位優先，其次用片語表，最長的片語優先（R1-12）。

片語表（由長到短比對，同一段字不重複使用）：

| 片語 | 類型 |
|---|---|
| 機械升降平面、升降平面 | lift_flat |
| 坡道平面 | ramp_flat |
| 一樓平面、1樓平面 | ground_flat |
| 坡道機械、升降機械、機械式、機械、平移、塔式 | mechanical |
| 平面式、平面 | flat_unknown_access（是平面，但不知道是坡道還是升降） |

- 「機械升降平面」比「機械」長，所以算升降平面，不會被判成機械（R1-12 反例 1）。
- 「坡道機械式平移大車位」：「平移」是機械，「平」不會被當成平面。
- 「坡道-平面」「(坡道)平面」這類拆開的寫法先合併再比對。
- 多個車位（`combine`）：類型都一樣 → 該類型；有任何平面 → mixed_with_flat；
  有看不懂的 → unknown；其餘 → mixed_no_flat。
- 「無車位」「無」→ none。看不懂 → unknown。
- 公有車位（抽籤、輪停）由 `is_public` 判斷，另外標示（R1-12 第 4 點）。
"""

import re

from house_watch.models import Parking
from house_watch.parse.text import cn_to_int, compact

FLAT_TYPES = frozenset(
    {Parking.RAMP_FLAT, Parking.GROUND_FLAT, Parking.LIFT_FLAT, Parking.FLAT_UNKNOWN_ACCESS}
)

PHRASES: tuple[tuple[str, Parking], ...] = (
    ("機械升降平面", Parking.LIFT_FLAT),
    ("升降平面", Parking.LIFT_FLAT),
    ("坡道平面", Parking.RAMP_FLAT),
    ("一樓平面", Parking.GROUND_FLAT),
    ("1樓平面", Parking.GROUND_FLAT),
    ("坡道機械", Parking.MECHANICAL),
    ("升降機械", Parking.MECHANICAL),
    ("機械式", Parking.MECHANICAL),
    ("機械", Parking.MECHANICAL),
    ("平移", Parking.MECHANICAL),
    ("塔式", Parking.MECHANICAL),
    ("平面式", Parking.FLAT_UNKNOWN_ACCESS),
    ("平面", Parking.FLAT_UNKNOWN_ACCESS),
)
_BY_PHRASE = dict(PHRASES)
_PHRASE_RE = re.compile(
    "|".join(re.escape(p) for p, _ in sorted(PHRASES, key=lambda item: -len(item[0])))
)
# 「坡道-平面」「(坡道)平面」「平面(坡道)」：出入方式與型式拆開寫的，先合併成「坡道平面」。
# 型式在前的寫法只認括號，否則「升降平面、坡道平面」這種清單會被拆錯。
_SPLIT_ACCESS = re.compile(r"(坡道|升降|一樓|1樓)[()\-:]*(平面|機械)")
_FORM_THEN_ACCESS = re.compile(r"(平面|機械)\((坡道|升降)\)")
# 不同車位之間的分隔；同一段裡的片語都在描述同一個車位（例如「坡道機械式平移」）。
_SEGMENT_SEP = re.compile(r"[+、,;及和&]|\s")
_COUNT = re.compile(r"(?:[x×*](?P<a>\d+)|(?P<b>\d+|[一二三四五六七八九十兩]+)(?:位|個|格))")
_NONE = re.compile(r"^(?:無|無車位|沒有車位|無停車位|不含車位)$")


def _normalize(text: str | None) -> str:
    s = _FORM_THEN_ACCESS.sub(r"\2\1", compact(text))
    return _SPLIT_ACCESS.sub(r"\1\2", s)


def parse_parking_spaces(text: str | None) -> list[Parking]:
    """文字裡的每一個車位的類型，依出現順序。

    文字先依分隔符號（+、、、逗號、及、和）切成段，一段代表一種車位；同一段裡的片語合成一個類型，
    段裡的「×2」「2位」會重複該類型。看不懂的段略過。
    """
    s = _normalize(text)
    if not s:
        return []
    if _NONE.match(s):
        return [Parking.NONE]
    spaces: list[Parking] = []
    for segment in _SEGMENT_SEP.split(s):
        types = [_BY_PHRASE[m.group(0)] for m in _PHRASE_RE.finditer(segment)]
        if not types:
            continue
        count = 1
        tail = _COUNT.search(segment)
        if tail is not None:
            count = cn_to_int(tail.group("a") or tail.group("b")) or 1
        spaces.extend([combine(types)] * count)
    return spaces


def combine(types: list[Parking]) -> Parking:
    """多個車位合成一個類型（見模組說明）。"""
    distinct = set(types)
    if not distinct:
        return Parking.UNKNOWN
    if len(distinct) == 1:
        return next(iter(distinct))
    if Parking.NONE in distinct:
        return Parking.UNKNOWN  # 同時寫了沒有車位與某種車位，互相矛盾
    if distinct & FLAT_TYPES:
        return Parking.MIXED_WITH_FLAT
    if Parking.UNKNOWN in distinct:
        return Parking.UNKNOWN
    return Parking.MIXED_NO_FLAT


def parse_parking(text: str | None) -> Parking:
    """片語表判斷車位類型。看不懂回 unknown。"""
    return combine(parse_parking_spaces(text))


def classify_access_form(access: str | None, form: str | None) -> Parking:
    """結構化欄位：出入方式（坡道、升降、一樓）× 型式（平面、機械）。

    型式是機械、塔式或平移 → mechanical；型式是平面 → 依出入方式分成 ramp_flat、
    lift_flat、ground_flat，出入方式不明就是 flat_unknown_access。兩個欄位都看不懂 → unknown。
    """
    a, f = compact(access), compact(form)
    if re.search(r"機械|塔|平移", f):
        return Parking.MECHANICAL
    if "平面" in f:
        where = a + f  # 出入方式有時直接寫在型式欄位裡，例如「坡道平面」
        if "坡道" in where:
            return Parking.RAMP_FLAT
        if re.search(r"升降|電梯|車梯", where):
            return Parking.LIFT_FLAT
        if re.search(r"一樓|1樓|地面|平地", where):
            return Parking.GROUND_FLAT
        return Parking.FLAT_UNKNOWN_ACCESS
    if not f and not a:
        return Parking.UNKNOWN
    return parse_parking(f"{a}{f}")


def is_public(text: str | None) -> bool | None:
    """車位權屬：公有（常是抽籤或輪停）→ True；私有、自有、產權 → False；不明 → None。"""
    s = compact(text)
    if "公有" in s or "公共車位" in s or "抽籤" in s or "輪停" in s:
        return True
    if re.search(r"私有|自有|產權|獨立權狀", s):
        return False
    return None
