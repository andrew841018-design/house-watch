"""文字正規化（WP4）。

- `nfkc`：NFKC 正規化再去頭尾空白。全形數字、英文、空白（含 U+3000）都會變半形；
  CJK 相容字變一般字。注意「㎡」「m²」經過 NFKC 會變成「m2」，抓數字時要把它當單位（R3-11）。
- `compact`：NFKC 後刪掉所有空白。
- `unify_variants`：異體字兩邊一起歸一（臺→台、峯→峰、厦→廈、塩→鹽、双→雙、舘→館），
  以及實價登錄已確認的造字（R3b §5 第 3、4 點）。
- `cn_to_int`、`int_to_cn`：中文數字與整數互轉，含大寫數字（壹貳參…）。
- `zip3`：郵遞區號取前 3 碼。有的欄位是 5 或 6 碼（3＋2、3＋3），整個拿去比 `320` 會失敗（R3-11）。
- `normalize_address`：地址的標準寫法。段與路名中的數字用國字（`3段`→`三段`、
  `中埔6街`→`中埔六街`）；巷、弄、號、樓、層與「之」後面的數字用阿拉伯數字；
  `24-4號` 看成 `24之4號`（R3b §5 第 7–9 點）。
"""

import re
import unicodedata

CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "貳": 2,
    "兩": 2,
    "三": 3,
    "參": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陸": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
CN_UNITS: dict[str, int] = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
CN_NUMERAL_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS)

# 異體字與已確認的造字（私用區）。推測但未確認的造字不放，以免誤配。
_VARIANTS = str.maketrans(
    {
        "臺": "台",
        "峯": "峰",
        "厦": "廈",
        "塩": "鹽",
        "双": "雙",
        "舘": "館",
        "": "峰",  # 造字：峯
        "": "廈",  # 造字：厦
    }
)

_WS = re.compile(r"\s+")
_CN_RUN = f"[{CN_NUMERAL_CHARS}]+"


def nfkc(text: str | None) -> str:
    """NFKC 正規化並去掉頭尾空白；None 當成空字串。"""
    if text is None:
        return ""
    return unicodedata.normalize("NFKC", text).strip()


def compact(text: str | None) -> str:
    """NFKC 正規化並刪掉所有空白。"""
    return _WS.sub("", nfkc(text))


def unify_variants(text: str) -> str:
    """異體字與已確認的造字歸一。"""
    return text.translate(_VARIANTS)


def cn_to_int(text: str | None) -> int | None:
    """中文數字或阿拉伯數字轉整數；看不懂就回 None。

    支援：`十`→10、`十五`→15、`二十三`→23、`一百零五`→105、`一百一十`→110、
    `兩千`→2000、大寫數字（`壹`、`拾`…），以及只有數字字元的位值寫法（`一〇五`→105）。
    """
    s = nfkc(text)
    if not s:
        return None
    if s.isascii() and s.isdigit():
        return int(s)
    if any(ch not in CN_DIGITS and ch not in CN_UNITS for ch in s):
        return None
    if all(ch in CN_DIGITS for ch in s):
        if len(s) == 1:
            return CN_DIGITS[s]
        return int("".join(str(CN_DIGITS[ch]) for ch in s))
    total = 0
    pending: int | None = None  # 還沒乘上單位的數字
    last_unit: int | None = None
    for ch in s:
        if ch in CN_DIGITS:
            if pending:
                return None  # 兩個非零數字中間沒有單位，例如「二三十」
            pending = CN_DIGITS[ch]
            continue
        unit = CN_UNITS[ch]
        if last_unit is not None and unit >= last_unit:
            return None  # 單位要由大到小，例如不能寫「十百」
        if not pending:
            if unit != 10:
                return None  # 「百」「千」前面一定要有數字
            pending = 1  # 「十五」「一百十」「一千零十」的「十」代表一十
        total += pending * unit
        pending = None
        last_unit = unit
    if pending:
        total += pending
    return total


def int_to_cn(value: int) -> str:
    """1–99 的整數轉國字，給段號與路名用（`3`→`三`、`11`→`十一`、`23`→`二十三`）。"""
    if not 1 <= value <= 99:
        raise ValueError("只支援 1–99")
    digits = "零一二三四五六七八九"
    tens, ones = divmod(value, 10)
    if tens == 0:
        return digits[ones]
    head = "" if tens == 1 else digits[tens]
    return f"{head}十{digits[ones] if ones else ''}"


def _cn_to_arabic(match: re.Match[str]) -> str:
    """把 num 群組的中文數字換成阿拉伯數字，前後的群組照原樣接回。"""
    value = cn_to_int(match.group("num"))
    if value is None:
        return match.group(0)
    return f"{match.group('pre')}{value}{match.group('post')}"


def _arabic_to_cn(match: re.Match[str]) -> str:
    value = int(match.group(1))
    if not 1 <= value <= 99:
        return match.group(0)
    return f"{int_to_cn(value)}{match.group(2)}"


_CN_BEFORE_UNIT = re.compile(rf"(?P<pre>)(?P<num>{_CN_RUN})(?P<post>巷|弄|號|樓|層|鄰)")
_CN_AFTER_ZHI = re.compile(rf"(?P<pre>之)(?P<num>{_CN_RUN})(?P<post>)")
_DIGITS_BEFORE_ROAD = re.compile(r"(?<![\d之-])(\d{1,2})(段|街|路)")
_DASH_NUMBER = re.compile(r"(\d+)-(\d+)號")


def normalize_address(text: str | None) -> str:
    """地址的標準寫法（見模組說明）。不切前綴，也不丟樓層，那些由定位端處理。"""
    s = unify_variants(compact(text))
    s = _CN_BEFORE_UNIT.sub(_cn_to_arabic, s)
    s = _CN_AFTER_ZHI.sub(_cn_to_arabic, s)
    s = _DIGITS_BEFORE_ROAD.sub(_arabic_to_cn, s)
    s = _DASH_NUMBER.sub(r"\1之\2號", s)
    return s


_ZIP = re.compile(r"(\d{3})(?:-?\d{2,3})?")


def zip3(text: str | None) -> str | None:
    """郵遞區號的前 3 碼：`330`、`33012`、`330012`、`330-12`、全形數字都可以；其他回 None。"""
    m = _ZIP.fullmatch(compact(text))
    return m.group(1) if m else None
