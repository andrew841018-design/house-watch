"""轉接器的共用介面（契約 C5）與解析小工具。

契約部分（C5）：
- `Caps`、`Adapter`、`ListPage`、`Resp`、`RequestSpec`、`AreaQuery`、`PageInvalid`、`Blocked`。
- `parse_list`、`parse_detail` 是純函式：輸出只由回應決定。需要「抓取日」的欄位
  （屋齡換成完工日區間）用回應標頭的抓取時間，見 `response_date()`。

C5 的補充成員（2026-11-21 由 WP0 併入契約）：
- `Adapter.hosts`：轉址白名單與限速用的主機（C6「轉址白名單」需要每站一份）。
- `Adapter.robots_urls`：每天檢查一次的 robots.txt（R2-09）；一站可以有好幾個主機的檔。
- `ListPage.skipped`：卡片在、但解析不出 ListingFacts 的張數（預設 0）。置頂、廣告卡與
  同一頁重複的卡片不算 skipped，解析器直接略過。
- `BatchAdapter`（選用）：有批次端點的站（東森）補列表缺的欄位，見下。
- `DetailByFacts.detail_request_for(base)`（選用）：詳細頁網址要用到列表其他欄位的站。
- `Adapter.scope_version`（選用，預設 "1"）：伺服器端條件或排序改了就改這個字串，
  讓流程重新計算基準與下架計數（範圍版本，R1-09、R1-10）。

解析小工具：薄的包裝，實際的解析都在 WP4 的 `parse/*.py`。包裝只做三件事：
接受網站給的任何型別（數字或字串）、看不懂一律回 None 或未知（不丟例外），
以及少數網站寫法的前處理（例如車位的異體字、型態文字裡的非住宅字樣）。
- 每個欄位都用「帶標籤或帶單位」的寫法解析，不取字串裡的第一個數字（R3-11）。
- 金額一律轉成整數元並檢查合理範圍（R1-22）；面積轉成 0.01 坪的整數。
- 屋齡一律轉成完工日區間 DateRange（r4 §3.10）。
- 車位：結構化欄位優先，沒有才用片語表，最長的片語優先（R1-12）。
"""

import datetime as dt
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

from house_watch.clock import from_iso, taipei_date
from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.models import BType, DateRange, ListingFacts, Parking
from house_watch.parse.age import IMPLAUSIBLE_AGE_YEARS, built_from_age, built_from_date
from house_watch.parse.area import find_areas
from house_watch.parse.area import ping_to_cp as area_ping_to_cp
from house_watch.parse.elevator import parse_btype
from house_watch.parse.floor import parse_floor
from house_watch.parse.floor import parse_total_floors as floor_total
from house_watch.parse.money import sane_price, to_yuan
from house_watch.parse.parking import combine as combine_spaces
from house_watch.parse.parking import parse_parking
from house_watch.parse.text import nfkc
from house_watch.parse.text import zip3 as text_zip3
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


class BatchAdapter(Adapter, Protocol):
    """選用（C5 補充）：有批次端點的站（東森），一次補好幾筆列表缺的欄位（例如樓層）。

    流程只對還缺這些欄位的刊登呼叫；parse_batch 回傳「編號 → 補好的事實」，沒補到的不放。
    """

    batch_size: int

    def batch_request(self, source_ids: Sequence[str]) -> RequestSpec: ...

    def parse_batch(
        self, resp: Resp, bases: Mapping[str, ListingFacts]
    ) -> dict[str, ListingFacts]: ...


class DetailByFacts(Protocol):
    """選用（C5 補充）：詳細頁網址要用到列表的其他欄位時（例如樂屋網的社區編號），
    轉接器另外提供 `detail_request_for(base)`；流程有這個方法就優先用它。"""

    def detail_request_for(self, base: ListingFacts) -> RequestSpec: ...


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
    """郵遞區號取前 3 碼（有的欄位是 3＋3 的 6 碼，R3-11）；數字也收。"""
    if value is None or isinstance(value, bool):
        return None
    return text_zip3(str(value))


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

# 開價的合理範圍（R1-22）：超出就當解析錯誤（例如單位從萬變成元），不寫進價格。
# 比 parse.money.PRICE_RANGE 窄：住宅開價超過 10 億元幾乎一定是單位錯。
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
        text = nfkc(value).replace(",", "")
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


def wan_to_yuan(value: Any) -> int | None:
    """萬元（數字或數字字串）→ 整數元（parse.money）；不合理就是 None。"""
    number = to_decimal(value)
    if number is None:
        return None
    return sane_price(to_yuan(number, "萬"), lo=PRICE_MIN_YUAN, hi=PRICE_MAX_YUAN)


def ping_to_cp(value: Any, *, max_ping: int = TITLE_AREA_MAX_PING) -> int | None:
    """坪 → 0.01 坪的整數（parse.area）；0、負數或大得不合理（東森有 26,327 坪的髒資料）是 None。"""
    number = to_decimal(value)
    if number is None or number <= 0 or number > max_ping:
        return None
    return area_ping_to_cp(number)


_APPROX = re.compile(r"^約\s*")


def ping_text_to_cp(text: Any, *, max_ping: int = TITLE_AREA_MAX_PING) -> int | None:
    """「約16.2坪」「58.96 坪(含車位：13.70坪)」：開頭第一個帶「坪」的面積（parse.area）。

    數字後面一定要接「坪」；開頭不是面積（例如只有數字、或先寫 m²）就是 None。
    """
    cleaned = clean_text(text)
    if cleaned is None:
        return None
    core = _APPROX.sub("", nfkc(cleaned))
    found = find_areas(core)
    if not found or found[0].start != 0 or found[0].unit != "ping":
        return None
    cp = found[0].hundredths
    return cp if 0 < cp <= max_ping * 100 else None


def site_point(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    """網站座標：0、缺值或明顯不在台灣的一律當成沒有。"""
    a, b = to_float(lat), to_float(lon)
    if a is None or b is None or not (21.5 <= a <= 26.5 and 118.0 <= b <= 122.5):
        return None, None
    return round(a, 7), round(b, 7)


# ---------------------------------------------------------------- 樓層（parse.floor）


def parse_floor_range(value: Any) -> tuple[int | None, int | None]:
    """所在樓層。只有明確的範圍符號才算跨層；「之N」是戶號不是樓層（R3-11、R1-30）。"""
    text = clean_text(value)
    if text is None:
        return None, None
    info = parse_floor(text)
    return info.lo, info.hi


def parse_total_floors(value: Any) -> int | None:
    """總樓層：「共15樓」「13F」「13」。0 或「共0樓」代表未知（R1-30）。"""
    text = clean_text(value)
    return None if text is None else floor_total(text)


def parse_floor_field(value: Any) -> tuple[int | None, int | None, int | None]:
    """「6樓/共15樓」「1~5樓/共5樓」「10F / 13F」→（下界, 上界, 總樓層）。"""
    text = clean_text(value)
    if text is None:
        return None, None, None
    info = parse_floor(text)
    return info.lo, info.hi, info.total


# ---------------------------------------------------------------- 屋齡 → 完工日區間（r4 §3.10）

# 屋齡大到這裡，比較可能是民國完工年而不是屋齡，當成未知（R3-11）。
MAX_PLAUSIBLE_AGE = IMPLAUSIBLE_AGE_YEARS

UNKNOWN_BUILT = DateRange()


def _plausible(years: int | Decimal, ref: dt.date | None) -> bool:
    return ref is not None and 0 <= years < MAX_PLAUSIBLE_AGE


def built_from_floor_years(years: int, ref: dt.date | None) -> DateRange:
    """無條件捨去的整數屋齡 n（中信）：完工日在 (R − (n+1) 年, R − n 年]（parse.age）。"""
    if ref is None or not _plausible(years, ref):
        return UNKNOWN_BUILT
    # 屋齡 0 年在這裡是「未滿 1 年」的已知值（呼叫端已確認），所以當成新成屋的區間。
    return built_from_age(years, as_of=ref, integer_mode="floor", new_house=years == 0).built


def built_from_decimal_years(
    years: Decimal, ref: dt.date | None, *, decimals: int | None = None
) -> DateRange:
    """顯示值 x（不知道是四捨五入還是截斷）：屋齡 ∈ [x − h, x + 2h)，h 是最後一位的一半。

    同 parse.age 的 display 模式（R1-06 第 3 點：兩種進位都可能）。decimals 是網站顯示的
    小數位數；不給就看 x 本身（住商的 33 其實是 33.0，呼叫端傳 decimals=1）。
    """
    if ref is None or not _plausible(years, ref):
        return UNKNOWN_BUILT
    if decimals is not None:
        years = years.quantize(Decimal(1).scaleb(-decimals))
    return built_from_age(str(years), as_of=ref, integer_mode="display").built


def built_from_years_months(years: int, months: int, ref: dt.date | None) -> DateRange:
    """「X年Y個月」（捨去到月）：完工日在 (R − (X年Y+1個月), R − X年Y個月]（parse.age）。"""
    if ref is None or not _plausible(years, ref) or not 0 <= months < 12:
        return UNKNOWN_BUILT
    return built_from_age(f"{years}年{months}個月", as_of=ref).built


def built_presale(ref: dt.date | None) -> DateRange:
    """預售屋：完工日不早於抓取日，晚多久不知道。"""
    return UNKNOWN_BUILT if ref is None else DateRange(lo=ref, hi=None)


_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def built_from_iso_date(value: Any, ref: dt.date | None = None) -> DateRange | None:
    """完工日「1995-05-19」（ISO，可以帶時間）。不是日期、早於 1850 年或晚於抓取日一年以上是 None。

    只收西元的 ISO 寫法：parse.age.built_from_date 會把小於 1911 的年份當民國年，
    「0001-01-01」這種空值的日期會變成 1912 年，所以先在這裡擋掉。
    """
    text = clean_text(value, 40)
    match = _ISO_DAY.match(text) if text else None
    if match is None or int(match.group(0)[:4]) < 1850:
        return None
    built = built_from_date(match.group(0))
    if built.lo is None or built.lo != built.hi:
        return None
    if ref is not None and built.lo > ref + dt.timedelta(days=366):
        return None
    return built


_INTEGER_AGE = re.compile(r"^(約|未滿)?(\d+)(年)?(以上|以下|以內|內)?$")


def parse_age_text(
    value: Any,
    ref: dt.date | None,
    *,
    floor_years: bool = False,
    decimals: int | None = None,
) -> DateRange:
    """屋齡文字 → 完工日區間（parse.age.built_from_age，加上兩個網站寫法）。

    - 「預售屋」→ 完工日不早於抓取日，晚多久不知道（parse.age 不處理預售）。
    - decimals：網站顯示的小數位數；整數的寫法（「33年」）其實是 33.0，先補上小數再換算。
    - 其他寫法照 parse.age：「15年6個月」、「11個月」、「約10」、「30年以上」、「5年以下」、
      0 年或空白是未知、「新成屋」是未滿 1 年、大得像民國年的是未知（r4 §3.10、R3-11）。
    """
    text = clean_text(value)
    if text is None or ref is None:
        return UNKNOWN_BUILT
    core = nfkc(text).replace(" ", "")
    if "預售" in core:
        return built_presale(ref)
    if decimals and not floor_years:
        match = _INTEGER_AGE.match(core)
        if match is not None:
            pre, number, unit, post = (match.group(k) or "" for k in (1, 2, 3, 4))
            core = f"{pre}{number}.{'0' * decimals}{unit}{post}"
    mode = "floor" if floor_years else "display"
    return built_from_age(core, as_of=ref, integer_mode=mode).built


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

# 網站型態文字裡出現這些字就不是住宅（parse.elevator.parse_btype 只看大樓、華廈等字，
# 「辦公商業大樓」會被當成大樓）。
_NON_RESIDENTIAL = re.compile(r"店面|店鋪|辦公|商辦|廠辦|廠房|商業|工業|土地|車位|倉庫")


def btype_from_word(value: Any) -> BType | None:
    """網站的型態文字 → BType（parse.elevator）。認不得的文字（店面、土地…）是「其他」，空白是未知。"""
    text = clean_text(value, 40)
    if text is None:
        return None
    if _NON_RESIDENTIAL.search(text):
        return BType.OTHER
    return parse_btype(text)


def btype_by_height(floors_total: int | None) -> BType | None:
    """有電梯的集合住宅：11 樓以上是大樓，10 樓以下是華廈（實價登錄的定義）。"""
    if floors_total is None:
        return None
    return BType.BUILDING if floors_total >= 11 else BType.MANSION


def outside_scope_btype(f: ListingFacts, scope: ListingScope | None) -> bool:
    """型態已知、而且不在公開抓取範圍內（用來省詳細頁請求；缺值一律不算在範圍外）。"""
    return scope is not None and f.btype is not None and f.btype not in scope.btypes


# ---------------------------------------------------------------- 車位（R1-12）

# 網站寫法先換成 parse.parking 片語表認得的寫法（異體字、多一個字的寫法）。
_PARKING_ALIASES: tuple[tuple[str, str], ...] = (
    ("昇", "升"),
    ("升降機平面", "升降平面"),
    ("升降式平面", "升降平面"),
    ("地面平面", "一樓平面"),
)
# 明確寫沒有車位的寫法（parse.parking 只認「無」「無車位」這類完整片語）。
_NO_PARKING = re.compile(r"(?:無|無車位|沒有|沒有車位|否|0個?|NO)", re.IGNORECASE)


def combine_parking(kinds: Iterable[Parking]) -> Parking:
    """多個車位合成一個值（parse.parking.combine）：全部相同就是那個；有任一個平面
    （含已經是「混合含平面」的值）是「混合含平面」；有未知的就整體未知（未知的那個可能是平面）；
    其餘是「混合無平面」。

    和 parse.parking.combine 不同的一點：「沒有車位」不算一個車位（一個都沒有就是未知）。
    """
    return combine_spaces([kind for kind in kinds if kind is not Parking.NONE])


def classify_parking_text(value: Any) -> Parking:
    """車位文字 → Parking（parse.parking 片語表，最長的優先）。

    空白、「有」、「有車位」都是未知；明確寫沒有才是 none。只寫「升降」不知道是升降平面
    還是升降機械，是未知。
    """
    text = clean_text(value, placeholders=False)
    if text is None:
        return Parking.UNKNOWN
    text = re.sub(r"\s+", "", nfkc(text))
    if _NO_PARKING.fullmatch(text):
        return Parking.NONE
    for old, new in _PARKING_ALIASES:
        text = text.replace(old, new)
    return parse_parking(text)


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
