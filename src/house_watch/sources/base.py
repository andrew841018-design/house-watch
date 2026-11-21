"""轉接器的共用介面（契約 C5）與解析小工具。

契約部分（C5）：
- `Caps`、`Adapter`、`ListPage`、`Resp`、`RequestSpec`、`AreaQuery`、`PageInvalid`、`Blocked`。
- `parse_list`、`parse_detail` 是純函式：輸出只由回應決定。需要「抓取日」的欄位
  （屋齡換成完工日區間）用回應標頭的抓取時間，見 `response_date()`。

C5 之外另加的成員（回報時列為契約補充）：
- `Adapter.hosts`：轉址白名單與限速用的主機（C6「轉址白名單」需要每站一份）。
- `Adapter.robots_urls`：每天檢查一次的 robots.txt（R2-09）。
- `ListPage.skipped`：卡片在、但編號不合法而無法變成 ListingFacts 的張數（預設 0）。

解析小工具：WP4（`parse/*.py`）完成之前的本地替身，換成 WP4 時只要換 import。
- 每個欄位都用「帶標籤或帶單位」的寫法解析，不取字串裡的第一個數字（R3-11）。
- 金額一律轉成整數元並檢查合理範圍（R1-22）；面積轉成 0.01 坪的整數。
- 屋齡一律轉成完工日區間 DateRange（r4 §3.10）。
- 車位：結構化欄位優先，沒有才用片語表，最長的片語優先（R1-12）。
"""

import calendar
import datetime as dt
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

from house_watch.clock import from_iso, taipei_date
from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.models import BType, DateRange, ListingFacts, Parking
from house_watch.settings import AreaConfig, ListingScope

Mode = Literal["full", "probe"]
Method = Literal["GET", "POST"]

CITY = "桃園市"

# 抓取層在每個回應加上抓取時間（UTC ISO）；解析器用它當「抓取日」。
FETCHED_AT_HEADER = "x-house-watch-fetched-at"

# 桃園市的行政區。地址字串開頭的區名只認這張表，避免把「工業區」這類路名當成區。
TAOYUAN_DISTRICTS = (
    "桃園區",
    "中壢區",
    "平鎮區",
    "八德區",
    "楊梅區",
    "蘆竹區",
    "大溪區",
    "龍潭區",
    "龜山區",
    "大園區",
    "觀音區",
    "新屋區",
    "復興區",
)

# district_hints 的種類（ListingFacts.district_hints 的 kind）。
HINT_ZIP = "zip"  # 網站的結構化郵遞區號欄位（取前 3 碼）
HINT_DISTRICT = "district"  # 網站的結構化區名欄位
HINT_ADDRESS = "address"  # 地址字串開頭的區名；出現兩個不同區名時兩個都列（R3-01）


# ---------------------------------------------------------------- 例外（C5）


class PageInvalid(HouseWatchError):
    """回應不是預期的頁面：資料錨點不在、格式不對、內容自相矛盾。

    `code` 是公開錯誤代碼（通常是 page_invalid）。`detail` 只給 log 用，不放回應內容。
    """

    def __init__(self, code: ErrorCode = ErrorCode.PAGE_INVALID, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail


class Blocked(HouseWatchError):
    """網站擋了我們，或這個來源目前暫停中。整站停止，不繞過（R2-09）。

    - code：blocked（403）、rate_limited（429）、challenge（挑戰頁標頭）、
      paused（退避中）、robots_changed（robots.txt 內容變了，等人確認）。
    - retry_after_s：距離可以再試還有幾秒；不知道就是 None。
    """

    def __init__(self, code: ErrorCode, retry_after_s: float | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------- 資料結構（C5）


@dataclass(frozen=True)
class RequestSpec:
    """一個要送出的請求。網址一律由轉接器用樣板組出，不跟隨頁面上的連結（R2-10）。

    - kind：list、detail、batch、prepare、robots、download，只用來記錄與統計。
    - max_bytes：回應大小上限；None 表示用設定的 page_max_bytes。
    - headers 裡可能有 CSRF token：不要寫進 log。
    """

    method: Method
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    kind: str = "list"
    max_bytes: int | None = None

    @classmethod
    def get(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        kind: str = "list",
        max_bytes: int | None = None,
    ) -> "RequestSpec":
        return cls("GET", url, tuple((headers or {}).items()), None, kind, max_bytes)

    @classmethod
    def post_json(
        cls,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        kind: str = "list",
    ) -> "RequestSpec":
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        merged = {"Content-Type": "application/json;charset=UTF-8", **(headers or {})}
        return cls("POST", url, tuple(merged.items()), body, kind, None)

    def header_dict(self) -> dict[str, str]:
        return dict(self.headers)

    def json_payload(self) -> Any:
        """POST 內容（測試與除錯用）。"""
        return None if self.body is None else json.loads(self.body)


@dataclass(frozen=True)
class Resp:
    """一個回應。headers 的鍵一律小寫；body 已解壓縮（gzip 等），但沒有解碼成文字。"""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class DistrictRef:
    """要查詢的一個行政區。"""

    name: str
    zip: str


@dataclass(frozen=True)
class AreaQuery:
    """一個（來源, 區域）查詢用到的公開範圍。只放公開設定，沒有任何個人條件。"""

    area: str
    districts: tuple[DistrictRef, ...]
    max_price_wan: int
    city: str = CITY

    @classmethod
    def from_config(
        cls, area: AreaConfig, scope: ListingScope, *, city: str = CITY
    ) -> "AreaQuery":
        districts = tuple(DistrictRef(d.name, d.zip) for d in area.districts)
        return cls(area=area.id, districts=districts, max_price_wan=scope.max_price_wan, city=city)


@dataclass(frozen=True)
class Caps:
    """轉接器的能力常數（C5）。

    - stable_sort：完整掃描用的排序代碼（該站最穩定的排序）。
    - probe_sort：探測用的「最新刊登」排序代碼；排序語意還沒確認的站是 None，
      只做每日完整掃描（r4 §3.4 例外）。
    - max_pages：一次掃描最多讀幾頁（安全上限）。
    - detail_fields：詳細頁補得到的 ListingFacts 欄位。
    """

    page_size: int
    has_total: bool
    stable_sort: str
    probe_sort: str | None
    max_pages: int
    detail_fields: frozenset[str]


@dataclass(frozen=True)
class ListPage:
    """一頁列表的解析結果。skipped 是編號不合法、無法變成 ListingFacts 的卡片數。"""

    items: list[ListingFacts]
    reported_total: int | None
    has_next: bool
    skipped: int = 0


Fetch = Callable[[RequestSpec], Resp]


class Adapter(Protocol):
    """一個來源的轉接器（C5）。parse_* 是純函式；prepare 可以有狀態（例如 CSRF token）。"""

    source: str
    parser_version: str
    caps: Caps
    hosts: frozenset[str]
    robots_urls: tuple[str, ...]

    def prepare(self, fetch: Fetch) -> None: ...

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec: ...

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage: ...

    def needs_detail(self, f: ListingFacts) -> bool: ...

    def detail_request(self, source_id: str) -> RequestSpec: ...

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts: ...


# ---------------------------------------------------------------- 回應

_JSON_CONSTANTS = frozenset({"NaN", "Infinity", "-Infinity"})


def _reject_constant(name: str) -> Any:
    raise ValueError(f"不接受的 JSON 常數 {name}")


def load_json(resp: Resp) -> Any:
    """把回應當 JSON 讀。不是 JSON（例如回了一頁 HTML）就是 PageInvalid。"""
    try:
        text = resp.body.decode("utf-8-sig")
        return json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PageInvalid(ErrorCode.PAGE_INVALID, "not json") from None


def response_date(resp: Resp) -> dt.date | None:
    """抓取日（台北日期）：先看抓取層加的抓取時間，再看 HTTP Date 標頭。"""
    stamp = resp.header(FETCHED_AT_HEADER)
    if stamp:
        try:
            return taipei_date(from_iso(stamp))
        except ValueError:
            pass
    date_header = resp.header("date")
    if date_header:
        try:
            when = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is not None:
            return taipei_date(when)
    return None


def require_ok(resp: Resp) -> None:
    if resp.status != 200:
        raise PageInvalid(ErrorCode.PAGE_INVALID, f"HTTP {resp.status}")


# ---------------------------------------------------------------- 文字

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏  ‪-‮﻿]")
_PLACEHOLDERS = frozenset({"－", "-", "--", "—", "─", "無", "null", "None", "NULL", "未提供"})


def clean_text(value: Any, max_len: int = 200, *, placeholders: bool = True) -> str | None:
    """外來文字：去掉控制字元、收合空白、截到 max_len。只當資料，不解讀其中的 HTML。

    placeholders=True 時，「－」「無」這類佔位字也當成缺值；車位文字要保留「無」，傳 False。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        value = str(value)
    text = " ".join(_CONTROL.sub("", value).split())
    if not text or (placeholders and text in _PLACEHOLDERS):
        return None
    return text[:max_len]


def nfkc(text: str) -> str:
    """只給解析用：全形數字、符號轉半形（例如「１２」「～」），不用在要保存的文字上。"""
    return unicodedata.normalize("NFKC", text)


_CITY_PREFIX = re.compile(r"^(?:桃園市|桃園縣|臺灣省|台灣省)")


def split_address(value: Any) -> tuple[str | None, list[str]]:
    """把「桃園市中壢區莊敬路」拆成路段文字與開頭的區名。

    區名只認 TAOYUAN_DISTRICTS；開頭有好幾個區名時全部列出，交給定位判斷矛盾（R3-01），
    不取最後一個，也不從查詢的區推定。
    """
    text = clean_text(value)
    if text is None:
        return None, []
    rest = text
    districts: list[str] = []
    while True:
        rest = rest.lstrip()
        city = _CITY_PREFIX.match(rest)
        if city:
            rest = rest[city.end() :]
            continue
        hit = next((d for d in TAOYUAN_DISTRICTS if rest.startswith(d)), None)
        if hit is None:
            break
        if hit not in districts:
            districts.append(hit)
        rest = rest[len(hit) :]
    return (rest.strip() or None), districts


def zip3(value: Any) -> str | None:
    """郵遞區號取前 3 碼（有的欄位是 3＋3 的 6 碼，R3-11）。"""
    if value is None or isinstance(value, bool):
        return None
    text = nfkc(str(value)).strip()
    return text[:3] if re.fullmatch(r"\d{3}(?:\d{2,3})?", text) else None


def district_hints(
    *, zip_code: Any = None, district: Any = None, address_districts: Iterable[str] = ()
) -> list[tuple[str, str]]:
    hints: list[tuple[str, str]] = []
    z = zip3(zip_code)
    if z:
        hints.append((HINT_ZIP, z))
    d = clean_text(district, 20)
    if d:
        hints.append((HINT_DISTRICT, d))
    hints.extend((HINT_ADDRESS, name) for name in address_districts)
    return hints


# ---------------------------------------------------------------- 數字、金額、面積

_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")

# 合理範圍（R1-22）：超出就當解析錯誤（例如單位從萬變成元），不寫進價格。
PRICE_MIN_YUAN = 100_000
PRICE_MAX_YUAN = 1_000_000_000
TITLE_AREA_MAX_PING = 1_000
MAIN_AREA_MAX_PING = 500


def to_decimal(value: Any) -> Decimal | None:
    """整個值就是一個數字才接受（可以有千分位逗號）；「約12坪」這種要用帶單位的函式。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        text = nfkc(value).strip().replace(",", "")
        if not _NUMBER.fullmatch(text):
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def to_int(value: Any) -> int | None:
    number = to_decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def to_float(value: Any) -> float | None:
    number = to_decimal(value)
    return None if number is None else float(number)


def _round_int(number: Decimal) -> int:
    return int(number.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def wan_to_yuan(value: Any) -> int | None:
    """萬元（數字或數字字串）→ 整數元；不合理就是 None。"""
    number = to_decimal(value)
    if number is None:
        return None
    yuan = _round_int(number * 10_000)
    return yuan if PRICE_MIN_YUAN <= yuan <= PRICE_MAX_YUAN else None


def ping_to_cp(value: Any, *, max_ping: int = TITLE_AREA_MAX_PING) -> int | None:
    """坪 → 0.01 坪的整數；0、負數或大得不合理（東森有 26,327 坪的髒資料）就是 None。"""
    number = to_decimal(value)
    if number is None or number <= 0 or number > max_ping:
        return None
    return _round_int(number * 100)


_PING_TEXT = re.compile(r"^約?\s*([\d,]+(?:\.\d+)?)\s*坪")


def ping_text_to_cp(text: Any, *, max_ping: int = TITLE_AREA_MAX_PING) -> int | None:
    """「約16.2坪」「58.96 坪」：數字後面一定要接「坪」。"""
    cleaned = clean_text(text)
    if cleaned is None:
        return None
    match = _PING_TEXT.match(nfkc(cleaned))
    return ping_to_cp(match.group(1), max_ping=max_ping) if match else None


def site_point(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    """網站座標：0、缺值或明顯不在台灣的一律當成沒有。"""
    a, b = to_float(lat), to_float(lon)
    if a is None or b is None or not (21.5 <= a <= 26.5 and 118.0 <= b <= 122.5):
        return None, None
    return round(a, 7), round(b, 7)


# ---------------------------------------------------------------- 樓層

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5}
_CN_DIGITS |= {"六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(text: str) -> int | None:
    """一到九十九的中文數字（「十五」「二十」）。"""
    if not text:
        return None
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if "十" not in text or text.count("十") > 1:
        return None
    tens, _, ones = text.partition("十")
    if tens and tens not in _CN_DIGITS:
        return None
    if ones and ones not in _CN_DIGITS:
        return None
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


_TOKEN = r"(?:B\d{1,2}|\d{1,3}|[一二兩三四五六七八九十]{1,3})"
_LEVEL = r"(?:樓|F|層)?"
_FLOOR_RANGE = re.compile(rf"({_TOKEN}){_LEVEL}(?:[~\-至、]({_TOKEN}){_LEVEL})?")
_UNIT_SUFFIX = re.compile(r"之(?:\d+|[一二兩三四五六七八九十]+)")


def floor_token(token: str) -> int | None:
    """「B1」→ -1、「15」→ 15、「十五」→ 15；0 不是有效樓層。"""
    if token.startswith("B"):
        number = int(token[1:])
        return -number if number > 0 else None
    number = int(token) if token.isdigit() else cn_number(token)
    return number if number else None


def parse_floor_range(value: Any) -> tuple[int | None, int | None]:
    """所在樓層。只有明確的範圍符號才算跨層；「之N」是戶號不是樓層（R3-11、R1-30）。"""
    text = clean_text(value)
    if text is None:
        return None, None
    text = _UNIT_SUFFIX.sub("", nfkc(text).upper().replace(" ", ""))
    match = _FLOOR_RANGE.fullmatch(text)
    if not match:
        return None, None
    lo = floor_token(match.group(1))
    hi = floor_token(match.group(2)) if match.group(2) else lo
    if lo is None or hi is None:
        return None, None
    return (lo, hi) if lo <= hi else (hi, lo)


def parse_total_floors(value: Any) -> int | None:
    """總樓層：「共15樓」「13F」「13」。0 或「共0樓」代表未知（R1-30）。"""
    text = clean_text(value)
    if text is None:
        return None
    text = nfkc(text).upper().replace(" ", "")
    match = re.fullmatch(rf"共?({_TOKEN}){_LEVEL}", text)
    if not match or match.group(1).startswith("B"):
        return None
    total = floor_token(match.group(1))
    return total if total and total > 0 else None


def parse_floor_field(value: Any) -> tuple[int | None, int | None, int | None]:
    """「6樓/共15樓」「1~5樓/共5樓」「10F / 13F」→（下界, 上界, 總樓層）。"""
    text = clean_text(value)
    if text is None:
        return None, None, None
    unit, _, total = text.partition("/")
    lo, hi = parse_floor_range(unit)
    return lo, hi, parse_total_floors(total) if total else None


# ---------------------------------------------------------------- 屋齡 → 完工日區間（r4 §3.10）

DAYS_PER_YEAR = Decimal("365.2425")
# 屋齡大到這裡，比較可能是民國完工年而不是屋齡，當成未知（R3-11）。
MAX_PLAUSIBLE_AGE = 60

UNKNOWN_BUILT = DateRange()


def _minus_years(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 2 月 29 日 → 該年 2 月 28 日
        return day.replace(year=day.year - years, day=28)


def _minus_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def built_from_floor_years(years: int, ref: dt.date | None) -> DateRange:
    """無條件捨去的整數屋齡 n（中信）：完工日在 (R − (n+1) 年, R − n 年]。"""
    if ref is None or years < 0 or years >= MAX_PLAUSIBLE_AGE:
        return UNKNOWN_BUILT
    hi = _minus_years(ref, years)
    lo = _minus_years(ref, years + 1) + dt.timedelta(days=1)
    return DateRange(lo=lo, hi=hi)


def built_from_decimal_years(
    years: Decimal, ref: dt.date | None, *, decimals: int | None = None
) -> DateRange:
    """顯示值 x（不知道是四捨五入還是截斷）：屋齡 ∈ [x − h, x + 2h)，h 是最後一位的一半。

    同 WP4 `parse.age` 的 display 模式（R1-06 第 3 點：兩種進位都可能）。decimals 是網站
    顯示的小數位數；不給就看 x 本身（住商的 33 其實是 33.0，呼叫端傳 decimals=1）。
    以天數近似（每年 365.2425 天），往外取整，所以區間只會更寬。
    """
    if ref is None or years < 0 or years >= MAX_PLAUSIBLE_AGE:
        return UNKNOWN_BUILT
    if decimals is None:
        exponent = years.as_tuple().exponent
        decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    half = Decimal(5) * Decimal(10) ** -(decimals + 1)
    far = math.ceil((years + 2 * half) * DAYS_PER_YEAR)
    near = max(math.floor((years - half) * DAYS_PER_YEAR), 0)
    return DateRange(lo=ref - dt.timedelta(days=far), hi=ref - dt.timedelta(days=near))


def built_from_years_months(years: int, months: int, ref: dt.date | None) -> DateRange:
    """「X年Y個月」（捨去到月）：完工日在 (R − (X年Y+1個月), R − X年Y個月]。"""
    if ref is None or years < 0 or not 0 <= months < 12 or years >= MAX_PLAUSIBLE_AGE:
        return UNKNOWN_BUILT
    total = years * 12 + months
    hi = _minus_months(ref, total)
    lo = _minus_months(ref, total + 1) + dt.timedelta(days=1)
    return DateRange(lo=lo, hi=hi)


def built_presale(ref: dt.date | None) -> DateRange:
    """預售屋：完工日不早於抓取日，晚多久不知道。"""
    return UNKNOWN_BUILT if ref is None else DateRange(lo=ref, hi=None)


def built_from_iso_date(value: Any, ref: dt.date | None = None) -> DateRange | None:
    """完工日「1995-05-19」。不是日期、早於 1850 年或晚於抓取日一年以上就是 None。"""
    text = clean_text(value, 40)
    if text is None:
        return None
    try:
        day = dt.date.fromisoformat(text[:10])
    except ValueError:
        return None
    if day.year < 1850 or (ref is not None and day > ref + dt.timedelta(days=366)):
        return None
    return DateRange(lo=day, hi=day)


_AGE_YM = re.compile(r"(\d{1,3})年(\d{1,2})個?月")
_AGE_MONTHS = re.compile(r"(\d{1,2})個?月")
_AGE_YEARS = re.compile(r"(\d{1,3}(?:\.\d+)?)年?")
_NEW_HOUSE = re.compile(r"新成屋|全新|新屋")


def parse_age_text(
    value: Any,
    ref: dt.date | None,
    *,
    floor_years: bool = False,
    decimals: int | None = None,
) -> DateRange:
    """屋齡文字 → 完工日區間（和 WP4 `parse.age.built_from_age` 同一套約定）。

    - 「15年6個月」→ 到月的區間；「11個月」→ 0 年 11 個月。
    - 「33年」：floor_years=True（中信）用整數捨去；否則是顯示值，decimals 是網站的小數位數。
    - 「約10」→ 兩端各再放寬 1 年；「30年以上」→ 較早那端不設限；「5年以下」→ 較晚那端到抓取日。
    - 0 年、空白、「--」→ 未知（r4 §3.10），除非寫了「新成屋」（當成未滿 1 年）。
    - 「預售屋」→ 完工日不早於抓取日，晚多久不知道。
    """
    text = clean_text(value)
    if text is None or ref is None:
        return UNKNOWN_BUILT
    text = nfkc(text).replace(" ", "")
    if "預售" in text:
        return built_presale(ref)
    new_house = bool(_NEW_HOUSE.search(text))
    core = _NEW_HOUSE.sub("", text).strip("()（）")
    approx = core.startswith("約")
    core = core.removeprefix("約")
    open_far = core.endswith("以上")
    open_near = core.endswith(("以下", "以內", "內"))
    core = re.sub(r"(以上|以下|以內|內)$", "", core)
    ym = _AGE_YM.fullmatch(core)
    months_only = _AGE_MONTHS.fullmatch(core)
    years_only = _AGE_YEARS.fullmatch(core)
    if ym:
        result = built_from_years_months(int(ym.group(1)), int(ym.group(2)), ref)
    elif months_only:
        result = built_from_years_months(0, int(months_only.group(1)), ref)
    elif years_only:
        years = Decimal(years_only.group(1))
        if years == 0:
            return built_from_floor_years(0, ref) if new_house else UNKNOWN_BUILT
        if floor_years and "." not in years_only.group(1):
            result = built_from_floor_years(int(years), ref)
        else:
            result = built_from_decimal_years(years, ref, decimals=decimals)
    elif new_house and not core:
        return built_from_floor_years(0, ref)
    else:
        return UNKNOWN_BUILT
    if result == UNKNOWN_BUILT:
        return result
    lo, hi = result.lo, result.hi
    if approx:
        lo = _minus_years(lo, 1) if lo else lo
        hi = min(_minus_years(hi, -1), ref) if hi else hi
    if open_far:
        lo = None
    if open_near:
        hi = ref
    return DateRange(lo=lo, hi=hi)


def built_span_days(built: DateRange) -> int | None:
    """區間寬度（天）；任一端未知就是 None。"""
    if built.lo is None or built.hi is None:
        return None
    return (built.hi - built.lo).days


def narrow_built(base: DateRange, new: DateRange) -> DateRange:
    """兩個來源的完工日區間：有交集就取交集，沒有交集就採用新的（較新的抓取）。"""
    if new == UNKNOWN_BUILT:
        return base
    lo = max((d for d in (base.lo, new.lo) if d is not None), default=None)
    hi = min((d for d in (base.hi, new.hi) if d is not None), default=None)
    if lo is not None and hi is not None and lo > hi:
        return new
    return DateRange(lo=lo, hi=hi)


# ---------------------------------------------------------------- 建物型態

_BTYPE_WORDS = {
    "大樓": BType.BUILDING,
    "電梯大樓": BType.BUILDING,
    "華廈": BType.MANSION,
    "電梯華廈": BType.MANSION,
    "公寓": BType.WALKUP,
    "無電梯公寓": BType.WALKUP,
    "透天": BType.TOWNHOUSE,
    "透天厝": BType.TOWNHOUSE,
    "別墅": BType.TOWNHOUSE,
    "透天/別墅": BType.TOWNHOUSE,
}


def btype_from_word(value: Any) -> BType | None:
    """網站的型態文字 → BType。認不得的文字（店面、土地…）是「其他」，空白是未知。"""
    text = clean_text(value, 40)
    if text is None:
        return None
    text = re.sub(r"\[.*?\]|（.*?）|\(.*?\)", "", text).strip()
    return _BTYPE_WORDS.get(text, BType.OTHER)


def btype_by_height(floors_total: int | None) -> BType | None:
    """有電梯的集合住宅：11 樓以上是大樓，10 樓以下是華廈（實價登錄的定義）。"""
    if floors_total is None:
        return None
    return BType.BUILDING if floors_total >= 11 else BType.MANSION


def outside_scope_btype(f: ListingFacts, scope: ListingScope | None) -> bool:
    """型態已知、而且不在公開抓取範圍內（用來省詳細頁請求；缺值一律不算在範圍外）。"""
    return scope is not None and f.btype is not None and f.btype not in scope.btypes


# ---------------------------------------------------------------- 車位（R1-12）

_FLAT_KINDS = frozenset(
    {
        Parking.RAMP_FLAT,
        Parking.GROUND_FLAT,
        Parking.LIFT_FLAT,
        Parking.FLAT_UNKNOWN_ACCESS,
        Parking.MIXED_WITH_FLAT,
    }
)

# 片語表：最長的優先；比對到的片段會被消掉，不會再被短片語比對到。
_PARKING_PHRASES: tuple[tuple[str, Parking], ...] = (
    ("機械升降平面", Parking.LIFT_FLAT),
    ("升降機平面", Parking.LIFT_FLAT),
    ("升降式平面", Parking.LIFT_FLAT),
    ("昇降式平面", Parking.LIFT_FLAT),
    ("升降平面", Parking.LIFT_FLAT),
    ("昇降平面", Parking.LIFT_FLAT),
    ("坡道平面", Parking.RAMP_FLAT),
    ("一樓平面", Parking.GROUND_FLAT),
    ("地面平面", Parking.GROUND_FLAT),
    ("坡道機械", Parking.MECHANICAL),
    ("升降機械", Parking.MECHANICAL),
    ("昇降機械", Parking.MECHANICAL),
    ("平移", Parking.MECHANICAL),
    ("塔式", Parking.MECHANICAL),
    ("機械", Parking.MECHANICAL),
    ("升降", Parking.MECHANICAL),
    ("昇降", Parking.MECHANICAL),
    ("平面", Parking.FLAT_UNKNOWN_ACCESS),
)
_NO_PARKING = re.compile(r"(?:無|無車位|沒有|否|0個?|NO)", re.IGNORECASE)


def combine_parking(kinds: Iterable[Parking]) -> Parking:
    """多個車位合成一個值：全部相同就是那個；有任一個平面是「混合含平面」；
    有未知的就整體未知（未知的那個可能是平面）；其餘是「混合無平面」。"""
    found = {k for k in kinds if k is not Parking.NONE}
    if not found:
        return Parking.UNKNOWN
    if len(found) == 1:
        return next(iter(found))
    if found & _FLAT_KINDS:
        return Parking.MIXED_WITH_FLAT
    if Parking.UNKNOWN in found:
        return Parking.UNKNOWN
    return Parking.MIXED_NO_FLAT


def classify_parking_text(value: Any) -> Parking:
    """車位文字 → Parking。空白、「有」、「有車位」都是未知；明確寫沒有才是 none。"""
    text = clean_text(value, placeholders=False)
    if text is None:
        return Parking.UNKNOWN
    text = re.sub(r"\s+", "", nfkc(text))
    if _NO_PARKING.fullmatch(text):
        return Parking.NONE
    kinds: list[Parking] = []
    rest = text
    for phrase, kind in _PARKING_PHRASES:
        if phrase in rest:
            kinds.append(kind)
            rest = rest.replace(phrase, "|")
    return combine_parking(kinds) if kinds else Parking.UNKNOWN


# 標題或欄位寫「車位另售」之類：總價不含車位（R1-13）。標題只讀不存。
PRICE_EXCLUDES_PARKING = re.compile(
    r"(?:車位|車)(?:需|要)?另(?:購|售|計|議|外|價)|不含車位|車位不含|另(?:購|售|計)車位"
)
# 標題提到車位：列表寫「無車位」時改成未知（兩者矛盾）。
TITLE_MENTIONS_PARKING = re.compile(r"車位|平車|坡平|房車|房\+車|＋車|\+車|雙車|含車|附車")


def price_excl_from_text(*texts: Any) -> bool | None:
    """有「車位另售」這類片語就是 True；沒有就是 None（不知道）。"""
    for text in texts:
        cleaned = clean_text(text, 400)
        if cleaned and PRICE_EXCLUDES_PARKING.search(cleaned):
            return True
    return None


def none_unless_title_mentions_parking(title: Any) -> Parking:
    """列表說沒有車位，但標題提到車位 → 未知，否則 none。"""
    cleaned = clean_text(title, 400)
    if cleaned and TITLE_MENTIONS_PARKING.search(cleaned):
        return Parking.UNKNOWN
    return Parking.NONE


# ---------------------------------------------------------------- 其他


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def as_list(value: Any) -> Sequence[Any] | None:
    return value if isinstance(value, list) else None


@dataclass
class FactsBuilder:
    """組 ListingFacts 的暫存；build() 失敗（欄位不合法）就回傳 None，由呼叫端計入 skipped。"""

    values: dict[str, Any] = field(default_factory=dict)

    def set(self, **values: Any) -> "FactsBuilder":
        self.values.update(values)
        return self

    def build(self) -> ListingFacts | None:
        try:
            return ListingFacts(**self.values)
        except ValueError:  # pydantic.ValidationError 是 ValueError 的子類別
            return None


def updated(base: ListingFacts, **changes: Any) -> ListingFacts:
    """回傳 base 加上 changes 的新 ListingFacts（會重新驗證）。changes 裡的 None 不覆蓋原值。"""
    data = base.model_dump()
    data.update({key: value for key, value in changes.items() if value is not None})
    return ListingFacts.model_validate(data)
