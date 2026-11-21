"""面積解析（WP4）：坪與 m² → 0.01 單位的整數（R3-10、R3-11）。

- 1 坪＝400/121 m²（精確分數，R3-10 與 r4 的裁決）。不用 3.305785 這類近似值：
  它在門檻上會讓剛好相等的情況判反。
- 一律用 Decimal 讀十進位字串。實價登錄的面積有時帶浮點雜訊（例如 15 位小數），
  讀成 0.01 m² 時四捨五入（ROUND_HALF_UP）；其他地方輸出前不再四捨五入。
- 「㎡」「m²」經過 NFKC 會變成「m2」，這裡把它當單位，不會多抓到一個「2」（R3-11）。
- 坪與 m² 互換時結果通常不是整數。`cm2_to_cp`、`cp_to_cm2` 一律無條件捨去：
  最小面積的比較不會因為進位而通過。需要精確值時用 `*_exact` 拿 Fraction。
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from typing import Literal

from house_watch.parse.text import nfkc

PING_M2 = Fraction(400, 121)  # 1 坪的平方公尺數
AreaUnit = Literal["m2", "ping"]

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_AREA = re.compile(
    rf"(?<![\d.])(?P<num>{_NUMBER})\s*(?P<unit>平方公尺|平方米|平米|m2|M2|坪)(?![A-Za-z])"
)


def _hundredths(value: int | float | str | Decimal) -> int:
    if isinstance(value, float):
        value = repr(value)
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError("不是數字") from None
    if not number.is_finite() or number < 0:
        raise ValueError("面積要是非負的有限數字")
    return int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def m2_to_cm2(value: int | float | str | Decimal) -> int:
    """m² 數值 → 0.01 m² 的整數，例如 `"66.12"`→6612、`"45.2100000000001"`→4521。"""
    return _hundredths(value)


def ping_to_cp(value: int | float | str | Decimal) -> int:
    """坪數 → 0.01 坪的整數，例如 `"30.25"`→3025。"""
    return _hundredths(value)


def cm2_to_cp_exact(cm2: int) -> Fraction:
    """0.01 m² → 0.01 坪的精確值。"""
    return Fraction(cm2) / PING_M2


def cp_to_cm2_exact(cp: int) -> Fraction:
    """0.01 坪 → 0.01 m² 的精確值。"""
    return Fraction(cp) * PING_M2


def cm2_to_cp(cm2: int) -> int:
    """0.01 m² → 0.01 坪，無條件捨去。"""
    return int(cm2_to_cp_exact(cm2) // 1)


def cp_to_cm2(cp: int) -> int:
    """0.01 坪 → 0.01 m²，無條件捨去。"""
    return int(cp_to_cm2_exact(cp) // 1)


@dataclass(frozen=True)
class Area:
    """解析出的面積：hundredths 是該單位的 0.01 倍整數。"""

    hundredths: int
    unit: AreaUnit
    start: int = 0
    end: int = 0

    @property
    def cm2(self) -> int:
        """0.01 m²（坪換算時無條件捨去）。"""
        return self.hundredths if self.unit == "m2" else cp_to_cm2(self.hundredths)

    @property
    def cp(self) -> int:
        """0.01 坪（m² 換算時無條件捨去）。"""
        return self.hundredths if self.unit == "ping" else cm2_to_cp(self.hundredths)


def find_areas(text: str | None, *, default_unit: AreaUnit | None = None) -> list[Area]:
    """文字裡所有帶單位的面積。整段文字只是一個數字、又給了 default_unit 時，用那個單位。"""
    s = nfkc(text)
    found = [
        Area(
            hundredths=_hundredths(m.group("num")),
            unit="ping" if m.group("unit") == "坪" else "m2",
            start=m.start(),
            end=m.end(),
        )
        for m in _AREA.finditer(s)
    ]
    if not found and default_unit is not None and re.fullmatch(_NUMBER, s):
        found = [Area(hundredths=_hundredths(s), unit=default_unit, start=0, end=len(s))]
    return found


def parse_area(text: str | None, *, default_unit: AreaUnit | None = None) -> Area | None:
    """剛好一個面積時回傳它。

    同時寫了坪與 m² 各一個（例如「30.25坪（100.00㎡）」）時，回傳 m² 那個，因為比較精確。
    其他「不只一個」的情況回 None，不取第一個數字。
    """
    found = find_areas(text, default_unit=default_unit)
    if len(found) == 1:
        return found[0]
    if len(found) == 2 and {a.unit for a in found} == {"m2", "ping"}:
        return next(a for a in found if a.unit == "m2")
    return None


def find_area(text: str | None, label: str) -> Area | None:
    """標籤後面緊接的面積（中間只能有空白、冒號），例如 `find_area(s, "主建物")`。"""
    s = nfkc(text)
    label = nfkc(label)
    if not label:
        raise ValueError("標籤不能是空的")
    for match in re.finditer(re.escape(label), s):
        tail = s[match.end() :]
        gap = re.match(r"[\s:：]*", tail)
        start = gap.end() if gap else 0
        area = _AREA.match(tail, start)
        if area is not None:
            return Area(
                hundredths=_hundredths(area.group("num")),
                unit="ping" if area.group("unit") == "坪" else "m2",
            )
    return None
