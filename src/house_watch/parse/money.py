"""金額解析（WP4）：億／萬／元 → 整數元（R1-22、R3-11）。

- 先 NFKC，千分位逗號只在「數字,三位數字」時才算，所以「2,480萬,現價」不會被黏成一個數。
- 支援複合寫法：「1億2,000萬」＝120,000,000 元。
- 一律用 Decimal 換算，最後四捨五入到元；不經過浮點數。
- 「45.6萬/坪」這種單價寫法不算總價，抓金額時跳過。
- 同一段文字有兩個金額（例如「原價…現價…」「↓100萬 2,350萬」）時，`parse_money` 回 None；
  要靠標籤區分時用 `find_money`。絕不「取第一個數字」。
- `sane_price` 做合理範圍檢查：超出範圍多半是單位錯（萬與元搞混）。
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from house_watch.parse.text import nfkc

Unit = Literal["億", "萬", "元"]
YUAN_PER_UNIT: dict[str, int] = {"億": 100_000_000, "萬": 10_000, "元": 1}
# 房屋總價的合理範圍（元）：十萬到一百億。只用來擋單位錯誤，不是任何人的條件。
PRICE_RANGE = (100_000, 10_000_000_000)

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# 數字前面不能緊接著數字、小數點或「數字,」：「12,34萬」這種壞格式不會被拆出一個「34萬」。
_TOKEN = re.compile(
    rf"(?P<cur>NT\$|\$|新台幣|新臺幣|台幣|臺幣)?\s*(?<!\d,)(?<![\d.])(?P<num>{_NUMBER})"
    r"\s*(?P<unit>億|萬|元)?"
)
_RATE_SUFFIX = re.compile(r"\s*(?:/|每)\s*(?:坪|m2|平方公尺|平方米|平米)")


@dataclass(frozen=True)
class Amount:
    """一個金額（元）與它在正規化後文字裡的位置。"""

    yuan: int
    start: int
    end: int


def to_yuan(value: int | float | str | Decimal, unit: Unit = "萬") -> int:
    """把某個單位的數值換成整數元，例如 `to_yuan("2345.5", "萬")`→23,455,000。"""
    if isinstance(value, float):
        value = repr(value)  # 用最短的十進位寫法，避免二進位誤差
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        raise ValueError("不是數字") from None
    if not number.is_finite():
        raise ValueError("不是有限的數字")
    return int((number * YUAN_PER_UNIT[unit]).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _decimal(num: str) -> Decimal:
    return Decimal(num.replace(",", ""))


def find_amounts(text: str | None, *, default_unit: Unit | None = None) -> list[Amount]:
    """文字裡所有的金額。沒寫單位的數字用 default_unit；沒有 default_unit 就略過。

    位置是相對於 `nfkc(text)` 的。
    """
    return _find_amounts(nfkc(text), default_unit)


def _find_amounts(s: str, default_unit: Unit | None) -> list[Amount]:
    amounts: list[Amount] = []
    pos = 0
    while True:
        match = _TOKEN.search(s, pos)
        if match is None:
            break
        pos = match.end()
        unit = match.group("unit")
        if _RATE_SUFFIX.match(s, match.end()):
            continue  # 單價，不是總價
        if unit is None:
            if match.group("cur"):
                unit = "元"
            elif default_unit is not None:
                unit = default_unit
            else:
                continue
        total = _decimal(match.group("num")) * YUAN_PER_UNIT[unit]
        start, end = match.start("num"), match.end()
        # 複合寫法：後面緊接著更小單位的金額，例如「1億2,000萬」。
        while unit != "元":
            nxt = _TOKEN.match(s, end)
            if nxt is None or nxt.group("unit") is None or nxt.group("cur"):
                break
            if YUAN_PER_UNIT[nxt.group("unit")] >= YUAN_PER_UNIT[unit]:
                break
            if _RATE_SUFFIX.match(s, nxt.end()):
                break
            unit = nxt.group("unit")
            total += _decimal(nxt.group("num")) * YUAN_PER_UNIT[unit]
            end = pos = nxt.end()
        yuan = int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
        amounts.append(Amount(yuan=yuan, start=start, end=end))
    return amounts


def parse_money(text: str | None, *, default_unit: Unit | None = None) -> int | None:
    """文字裡剛好只有一個金額時回傳它（元）；沒有或不只一個都回 None。"""
    amounts = find_amounts(text, default_unit=default_unit)
    return amounts[0].yuan if len(amounts) == 1 else None


_LABEL_GAP = re.compile(r"[\s:：]*(?:NT\$|\$|新台幣|新臺幣|台幣|臺幣)?\s*")


def find_money(text: str | None, label: str, *, default_unit: Unit | None = None) -> int | None:
    """標籤後面緊接的金額（中間只能有空白、冒號、幣別），例如 `find_money(s, "現價")`。"""
    s = nfkc(text)
    label = nfkc(label)
    if not label:
        raise ValueError("標籤不能是空的")
    for match in re.finditer(re.escape(label), s):
        tail = s[match.end() :]
        amounts = _find_amounts(tail, default_unit)
        if amounts and _LABEL_GAP.fullmatch(tail[: amounts[0].start]):
            return amounts[0].yuan
    return None


def sane_price(
    yuan: int | None, *, lo: int = PRICE_RANGE[0], hi: int = PRICE_RANGE[1]
) -> int | None:
    """在合理範圍內才回傳原值，否則回 None（多半是單位錯）。"""
    if yuan is None or not lo <= yuan <= hi:
        return None
    return yuan
