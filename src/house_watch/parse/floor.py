"""樓層解析（WP4）：售屋網站的樓層寫法 → 樓層區間與總樓層（R1-30、R3-11）。

規則：
- 地下樓層是負數：B1、地下1樓、地下一層 → -1。只寫「地下室」→ -1，加註 basement_unspecified。
- 只有明確的範圍符號（~、～、〜、-、至、、）才算跨層：「1~2樓」→ [1, 2]、「B1-1」→ [-1, 1]。
- 「之N」與「樓/F 後面的 -N」是戶號，不是樓層：「8樓之2」→ 8、「十五樓之1」→ 15、「16F-3」→ 16。
- 中文數字先轉成阿拉伯數字。
- 總樓層 0 代表未知（「3樓/共0樓」→ 總樓層 None），不是 0 樓。
- 「整棟」「全」→ whole，樓層 [1, 總樓層]；「頂樓」→ top，樓層取總樓層。
- 「頂樓加蓋」「頂加」→ 樓層未知，加註 rooftop_addition。
- 樓層高於總樓層時兩個值都保留，加註 floor_above_total，交給規則判斷。
"""

import re
from dataclasses import dataclass

from house_watch.parse.text import CN_NUMERAL_CHARS, cn_to_int, compact

_NUM = rf"\d+|[{CN_NUMERAL_CHARS}]+"
_SINGLE = re.compile(rf"^(?:(?P<b>B|地下)(?P<bn>{_NUM})?|(?P<n>{_NUM}))(?:樓|F|層)?$")
_UNIT_NO = re.compile(rf"(樓|F|層)(?:之|-)(?:{_NUM})(?![樓F層\d])")
_TRAILING_UNIT_NO = re.compile(rf"之(?:{_NUM})$")
_TOTAL = re.compile(rf"(?:共|地上|總)(?P<n>{_NUM})(?:樓|F|層)?")
_RANGE_SEP = re.compile(r"[~～〜\-至、]")


@dataclass(frozen=True)
class FloorInfo:
    """樓層區間 [lo, hi]（地下是負數）、總樓層（地上層數），以及附註代碼。"""

    lo: int | None = None
    hi: int | None = None
    total: int | None = None
    whole: bool = False
    top: bool = False
    notes: tuple[str, ...] = ()


def _number(text: str) -> int | None:
    return cn_to_int(text)


def parse_single_floor(token: str) -> int | None:
    """單一樓層：`3`、`3樓`、`3F`、`三樓`、`B1`、`地下2樓`。看不懂回 None。"""
    m = _SINGLE.match(compact(token).upper())
    if m is None:
        return None
    if m.group("b"):
        depth = _number(m.group("bn")) if m.group("bn") else 1
        return None if depth is None or depth == 0 else -depth
    value = _number(m.group("n"))
    return None if value is None or value == 0 else value


def parse_total_floors(text: str | None) -> int | None:
    """總樓層：`共12樓`、`12F`、`地上12層`、`十五層`、`12`。0 或看不懂都回 None。"""
    s = compact(text).upper()
    if not s:
        return None
    m = _TOTAL.search(s)
    if m is not None:
        value = _number(m.group("n"))
    else:
        single = re.fullmatch(rf"(?P<n>{_NUM})(?:樓|F|層)?", s)
        value = _number(single.group("n")) if single else None
    return value if value else None


def parse_floor(text: str | None, total_text: str | None = None) -> FloorInfo:
    """樓層欄位，例如 `1~2樓/共2樓`、`3樓/共0樓`、`B1`、`頂樓/15樓`、`整棟`。

    total_text 是另外一欄的總樓層；樓層欄位本身也寫了總樓層（`/` 後面或「共N樓」）時，
    以欄位裡的為準。
    """
    s = compact(text).upper()
    total = parse_total_floors(total_text) if total_text is not None else None
    notes: list[str] = []
    if "/" in s:
        s, total_part = s.split("/", 1)
        total = parse_total_floors(total_part) if total_part else total
    else:
        m = _TOTAL.search(s)
        if m is not None:
            total = parse_total_floors(m.group(0))
            s = (s[: m.start()] + s[m.end() :]).strip()
    if s in ("", "-", "--", "—"):
        return FloorInfo(total=total)
    if "頂加" in s or "頂樓加蓋" in s:
        return FloorInfo(total=total, notes=("rooftop_addition",))
    if s in ("整棟", "全棟", "全", "整幢"):
        return FloorInfo(lo=1 if total else None, hi=total, total=total, whole=True)
    if s in ("頂樓", "頂層", "RF", "R"):
        return FloorInfo(lo=total, hi=total, total=total, top=True)
    if s == "地下室":
        return FloorInfo(lo=-1, hi=-1, total=total, notes=("basement_unspecified",))
    s = _UNIT_NO.sub(r"\1", s)
    s = _TRAILING_UNIT_NO.sub("", s)
    floors = [parse_single_floor(part) for part in _RANGE_SEP.split(s)]
    if any(f is None for f in floors):
        lo = hi = None
    else:
        values = [f for f in floors if f is not None]
        lo, hi = min(values), max(values)
    if lo is None:
        return FloorInfo(total=total, notes=("floor_unparsed",))
    if total is not None and hi is not None and hi > total:
        notes.append("floor_above_total")
    return FloorInfo(lo=lo, hi=hi, total=total, notes=tuple(notes))
