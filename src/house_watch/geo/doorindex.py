"""桃園市門牌位置坐標資料：下載、檢查、建索引、比對（契約 C7；R3b、R1-14、R1-27、R3-07）。

**資料**：data.gov.tw 資料集 157689，桃園市政府民政局，政府資料開放授權條款第 1 版。
每月一個 CSV（全市約 110 萬列、95 MB），每個門牌的每個樓層各一列，坐標是 TWD97／TM2。

**更新**（`refresh`，每週由排程呼叫一次；R1-27）：
1. 用 data.gov.tw API v2 讀詮釋資料，從各期說明的「(115年10月)」挑最新一期。
2. 只從 https 的 opendata.tycg.gov.tw 下載；每一跳轉址都檢查主機白名單，串流寫檔並限制大小。
3. 檢查：表頭剛好 11 欄、每區的最少列數、坐標在桃園市的框內、能解析的比例。
4. 建新的索引檔，全部通過才原子換檔；舊索引留一份 `doorindex.prev.sqlite`。
   任何一步失敗，現有的索引都不會被動到。

**索引**（`doorindex.sqlite`，rollback journal，查詢時唯讀開啟）：
- 全市每個門牌一筆：各樓層的中位數坐標、離散程度、最高樓層（R1-14）。
- 每個區域查詢的行政區裡的門牌，另外存範圍指標（`door_metrics`，r4 §3.9），
  依 geo 版本重算；路段比例直接從這張表聚合。
- 以 250 m 格網加速「某點附近有哪些門牌」（判斷座標屬於哪個區，R3-01）。

**比對**照 R3b §3、§5 移植原型：正規化（NFKC、異體字、造字萬用字元）、去前綴、
最長路名、段號國字化、巷弄之號、號碼範圍、鄰號、路口。段落沒寫而該區有同名分段的路時，
用「不帶段＋所有段」的聯集並標 `section_unknown`（R3-07）。

`road_doors()` 回傳的門牌 id 只在同一個索引版本內有效，請直接交給 `road_frac()`；
長期保存請用 `DoorMatch.door_key` 或 `road_key`（以區碼、路名、巷弄、號組成，換版也穩定）。
"""

import csv
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import sqlite3
import ssl
import statistics
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from house_watch import __version__
from house_watch.clock import to_iso
from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.geo.areas import GeoContext
from house_watch.geo.proj import tm2_to_wgs84
from house_watch.paths import DataPaths

# ---------------------------------------------------------------- 常數

DATASET_ID = 157689
DATASET_API = f"https://data.gov.tw/api/v2/rest/dataset/{DATASET_ID}"
METADATA_HOSTS = frozenset({"data.gov.tw"})
DOWNLOAD_HOSTS = frozenset({"opendata.tycg.gov.tw"})
METADATA_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024  # 和 default.toml 的 http.download_max_bytes 相同
USER_AGENT = f"house-watch/{__version__}"
MIN_INTERVAL_S = 4.0  # 同一主機兩次請求的最小間隔（IMPL_BRIEF 禮貌規則）

HEADER = (
    "省市縣市代碼",
    "鄉鎮市區代碼",
    "村里",
    "鄰",
    "街路段",
    "地區",
    "巷",
    "弄",
    "號",
    "橫座標",
    "縱座標",
)

# 鄉鎮市區代碼（門牌檔的 7 碼，R3b §2.1）
DISTRICT_CODES: dict[str, str] = {
    "桃園區": "6800100",
    "中壢區": "6800200",
    "大溪區": "6800300",
    "楊梅區": "6800400",
    "蘆竹區": "6800500",
    "大園區": "6800600",
    "龜山區": "6800700",
    "八德區": "6800800",
    "龍潭區": "6800900",
    "平鎮區": "6801000",
    "新屋區": "6801100",
    "觀音區": "6801200",
    "復興區": "6801300",
}
DISTRICT_NAMES: dict[str, str] = {code: name for name, code in DISTRICT_CODES.items()}

# 每區至少要有的列數：約為 115 年 10 月版各區列數的七成。少於這個數字代表檔案不完整。
DEFAULT_MIN_ROWS: dict[str, int] = {
    "6800100": 158_000,
    "6800200": 153_000,
    "6800300": 27_000,
    "6800400": 57_000,
    "6800500": 55_000,
    "6800600": 36_000,
    "6800700": 77_000,
    "6800800": 64_000,
    "6800900": 38_000,
    "6801000": 63_000,
    "6801100": 14_000,
    "6801200": 27_000,
    "6801300": 2_400,
}

# 桃園市的 TM2 外框（全市門牌的極值外加約 2 km）
CITY_BOX_TM2 = (246_000.0, 2_722_000.0, 296_000.0, 2_781_000.0)
MAX_BAD_ROW_FRACTION = 0.001  # 欄數、區碼、坐標有問題的列
MAX_UNPARSED_FRACTION = 0.02  # 「號」欄解析不了的列

CELL_M = 250.0  # 空間格網大小
DECISIVE_M = 100.0  # 這個距離內只有一個區的門牌，坐標才算決定性證據（R3-01）
NEARBY_SEARCH_M = 300.0
NEARBY_NUMBER_MAX_GAP = 20  # 鄰號最多差幾號（R3b §3）
NEARBY_NUMBER_MAX_SPAN_M = 100.0  # 上下兩個鄰號相距超過這個值就不用鄰號（R3b §3）
RANGE_DOOR_MAX_P90_M = 100.0  # 號碼範圍的離散程度超過這個值就只當路段

SCHEMA_VERSION = "1"

# ---------------------------------------------------------------- 正規化（移植自 R3b 原型）

VARIANTS = str.maketrans({"臺": "台", "峯": "峰", "厦": "廈", "塩": "鹽", "双": "雙", "舘": "館"})
CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "貳": 2,
    "兩": 2,
    "三": 3,
    "叁": 3,
    "參": 3,
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
CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
NUM = r"[0-9零〇一壹二貳兩三叁參四肆五伍六陸七柒八捌九玖十拾百佰千仟]+"

_SHORT_DISTRICTS = "|".join(name[:-1] for name in DISTRICT_CODES)
PREFIX_RE = re.compile(
    r"^(?:台灣省?|中華民國)?(?:桃園市|桃園縣)?"
    r"(?:(?P<dist>" + _SHORT_DISTRICTS + r")(?:區|市|鎮|鄉))?"
)
VILLAGE_RE = re.compile(r"^[^0-9路街巷弄號]{1,4}?[里村](?:" + NUM + r"鄰)?")
FUZZY_SUFFIX = re.compile(r"(附近|旁邊|旁|對向|對面|隔壁|後方|前方|一帶|周邊|上)$")
SEP = r"[,，、．.及和]"
_PAREN = re.compile(r"[（(][^（()）]*[）)]")
_SECTION_DIGIT = re.compile(r"(?<=[路街道])([0-9]+)段")
_SECTION_TAIL = re.compile(r"^(.+?)([一二三四五六七八九十]+段)$")
_DOOR_NUMBER = re.compile(
    r"^(?:(?P<alley>" + NUM + r")衖)?"
    r"(?P<num>" + NUM + r")(?:之(?P<sub>" + NUM + r"))?號"
    r"(?:之(?P<sub2>" + NUM + r")(?=$|" + NUM + r"[樓層]|地下))?"
    r"(?P<tail>.*)$"
)
_FLOOR = re.compile(r"(" + NUM + r")[樓層]")
_ADDR_NUMBER = re.compile(
    r"^(?:(" + NUM + r"(?:之" + NUM + r")?)巷)?(?:(" + NUM + r"(?:之" + NUM + r")?)[弄衖])?"
    r"(?:(" + NUM + r")(?:[~～〜至](" + NUM + r"))?(?:之(" + NUM + r"))?"
    r"((?:" + SEP + NUM + r"(?:之" + NUM + r")?)*)號)?(.*)$"
)


def cn2int(text: str | None) -> int | None:
    """'二十三' → 23、'十' → 10、'105' → 105、'一0五' → 105。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if all(ch in CN_DIGIT or ch.isdigit() for ch in text):
        return int("".join(str(CN_DIGIT.get(ch, ch)) for ch in text))
    total, current = 0, 0
    for ch in text:
        if ch in CN_DIGIT:
            current = CN_DIGIT[ch]
        elif ch.isdigit():
            current = current * 10 + int(ch)
        elif ch in CN_UNIT:
            total += (current or 1) * CN_UNIT[ch]
            current = 0
    return total + current


def int2cn(n: int) -> str:
    """1 → 一、10 → 十、23 → 二十三（段號用，0–99）。"""
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n - 10] if n > 10 else "")
    return digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")


def _section_digits(m: re.Match[str]) -> str:
    """'2段' → '二段'；只轉 1–99，其他寫法照原樣留著（不會是段號）。"""
    n = int(m.group(1))
    return int2cn(n) + "段" if 1 <= n < 100 else m.group(0)


def is_wild(ch: str) -> bool:
    """Big5 造字會變成 Unicode 私用區，或在有損轉換後變成 '?'。"""
    o = ord(ch)
    return ch in "?？" or 0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0x10FFFF


def in_big5(ch: str) -> bool:
    try:
        ch.encode("cp950")
    except UnicodeEncodeError:
        return False
    return True


def norm(text: str | None) -> str:
    """NFKC（全形變半形、CJK 相容字變一般字）、刪空白、異體字歸一。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", "", text)
    return text.translate(VARIANTS)


def lane_key(text: str | None) -> str:
    """'１２１之１０巷' 或 '121之10' → '121之10'；沒有巷就是空字串；具名的巷照原樣。"""
    if not text:
        return ""
    text = norm(text).rstrip("巷弄衖")
    m = re.fullmatch(r"(" + NUM + r")(?:之(" + NUM + r"))?", text)
    if not m:
        return text
    main = str(cn2int(m.group(1)))
    return main + ("之" + str(cn2int(m.group(2))) if m.group(2) else "")


def split_section(road: str) -> tuple[str, str]:
    """'大興西路二段' → ('大興西路', '二段')；沒有段就是 (road, '')。"""
    m = _SECTION_TAIL.match(road)
    return (m.group(1), m.group(2)) if m else (road, "")


def district_code(district: str) -> str:
    """區名（中壢區、中壢）或 7 碼區碼 → 區碼。認不得就丟 KeyError。"""
    if district in DISTRICT_NAMES:
        return district
    name = norm(district)
    if name in DISTRICT_CODES:
        return DISTRICT_CODES[name]
    if name + "區" in DISTRICT_CODES:
        return DISTRICT_CODES[name + "區"]
    raise KeyError(district)


def split_prefix(text: str | None) -> tuple[list[str], str]:
    """去掉地址前面的縣市與區，回傳（出現過的區名，剩下的字串）。

    前綴可能重複（桃園縣桃園市桃園市…），每一個區名都記下來，不只取最後一個（R3-01）。
    第一個「桃園市／桃園縣」是縣市；之後再出現的「桃園市」是升格前的桃園市＝桃園區。
    """
    s = norm(text)
    found: list[str] = []
    while True:
        m = PREFIX_RE.match(s)
        if not m or m.end() == 0:
            break
        if m.group("dist"):
            name = m.group("dist") + "區"
            if name not in found:
                found.append(name)
        s = s[m.end() :]
    return found, s


# ---------------------------------------------------------------- 結果型別


class FetchError(HouseWatchError):
    """下載或檢查失敗。code 是公開錯誤代碼（C10）。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class DoorIndexError(FetchError):
    """門牌資料的下載、檢查或建索引失敗。現有的索引不會被動到。"""


@dataclass(frozen=True)
class HouseNumber:
    """地址裡路名後面的部分：巷、弄、號（含之號、號碼範圍）。"""

    lane: str = ""
    alley: str = ""
    num: int | None = None
    num_to: int | None = None
    sub: int | None = None


@dataclass(frozen=True)
class AddressParts:
    """`parse_road()` 的結果。section 是 None 表示沒寫段而該區有分段的同名路（R3-07）。"""

    base: str | None = None
    section: str | None = None
    number: HouseNumber | None = None
    cross_road: str | None = None
    flags: tuple[str, ...] = ()
    failure: str | None = None  # road_not_found、land_parcel


@dataclass(frozen=True)
class DoorMatch:
    """`lookup()` 的結果。

    level：exact、main_number、nearby_number、number_range、lane、alley、road。
    x、y 是所用門牌點的中位數（TM2），lat、lon 是同一點。
    """

    level: str
    district: str  # 區碼
    road_key: str
    door_ids: tuple[int, ...]
    x: float
    y: float
    lat: float
    lon: float
    n_points: int
    spread_p90_m: float
    door_key: str | None = None  # 單一門牌時的穩定鍵
    nearest_num: int | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoordDistrict:
    """某個座標附近門牌的區。decisive：100 m 內的門牌全都屬於同一區。"""

    district: str | None  # 區碼
    decisive: bool
    nearest_m: float | None


@dataclass(frozen=True)
class IndexInfo:
    """索引的版本資訊。version 是民國年月，例如 '11510'。"""

    version: str
    source_url: str | None = None
    built_at: str | None = None
    rows: int = 0
    doors: int = 0
    sha256: str | None = None
    changed: bool = False
    district_rows: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LatestResource:
    version: str
    url: str
    description: str


# ---------------------------------------------------------------- 找最新一期

_YM = re.compile(r"(\d{2,3})\s*年\s*0*(\d{1,2})\s*月")


def pick_latest(metadata: Any) -> LatestResource:
    """從 data.gov.tw 的詮釋資料挑最新一期（說明寫法不一致，要容錯，R3b §2.2）。"""
    try:
        distribution = metadata["result"]["distribution"]
    except (KeyError, TypeError):
        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "詮釋資料沒有 distribution") from None
    best: tuple[int, int, LatestResource] | None = None
    for item in distribution if isinstance(distribution, list) else []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("resourceDescription") or "")
        url = str(item.get("resourceDownloadUrl") or "").strip()
        fmt = str(item.get("resourceFormat") or "CSV").strip().upper()
        m = _YM.search(unicodedata.normalize("NFKC", description))
        if not m or not url or fmt != "CSV":
            continue
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            continue
        candidate = LatestResource(f"{year:03d}{month:02d}", url, description.strip())
        if best is None or (year, month) > (best[0], best[1]):
            best = (year, month, candidate)
    if best is None:
        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "詮釋資料裡找不到任何一期 CSV")
    return best[2]


# ---------------------------------------------------------------- 下載


class DoorFetcher(Protocol):
    """門牌更新需要的兩種請求。正式用 `HttpFetcher`，測試用 httpx.MockTransport。"""

    def get_json(self, url: str, *, allowed_hosts: frozenset[str], max_bytes: int) -> Any: ...

    def download(
        self, url: str, dest: Path, *, allowed_hosts: frozenset[str], max_bytes: int
    ) -> str:
        """串流寫到 dest，回傳內容的 sha256。"""
        ...


def check_url(
    url: str,
    allowed_hosts: frozenset[str],
    *,
    error: type[FetchError] = DoorIndexError,
    invalid: ErrorCode = ErrorCode.DOORINDEX_INVALID,
) -> httpx.URL:
    """只接受 https、主機在白名單、沒有帳密、預設埠。"""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise error(invalid, "網址格式錯誤") from None
    host = (parsed.host or "").lower()
    if (
        parsed.scheme != "https"
        or host not in allowed_hosts
        or parsed.userinfo
        or parsed.port not in (None, 443)
    ):
        raise error(invalid, f"不允許的網址主機或協定：{host}")
    return parsed


def _is_tls_error(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLError) or "CERTIFICATE" in str(seen).upper():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class HttpFetcher:
    """用 httpx 實作 DoorFetcher。

    - 只走 https；每一跳轉址都重新檢查主機白名單；最多 3 跳。
    - 同一主機兩次請求至少間隔 4 秒；User-Agent 是 house-watch/<版本>。
    - 403、429、挑戰頁就停，不重試、不繞過；憑證驗證不能關。
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        connect_timeout_s: float = 10.0,
        read_timeout_s: float = 30.0,
        min_interval_s: float = MIN_INTERVAL_S,
        user_agent: str = USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_redirects: int = 3,
    ) -> None:
        self.client = client
        self.timeout = httpx.Timeout(
            connect=connect_timeout_s, read=read_timeout_s, write=read_timeout_s, pool=10.0
        )
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._sleep = sleep
        self._monotonic = monotonic
        self.max_redirects = max_redirects
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval_s - (self._monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._monotonic()

    @contextmanager
    def _response(self, url: str, allowed_hosts: frozenset[str]) -> Iterator[httpx.Response]:
        for _ in range(self.max_redirects + 1):
            parsed = check_url(url, allowed_hosts)
            self._wait(parsed.host)
            request = self.client.build_request(
                "GET",
                parsed,
                headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                timeout=self.timeout,
            )
            try:
                response = self.client.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException:
                raise DoorIndexError(ErrorCode.TIMEOUT, f"連線逾時：{parsed.host}") from None
            except httpx.TransportError as exc:
                code = ErrorCode.TLS_ERROR if _is_tls_error(exc) else ErrorCode.TIMEOUT
                raise DoorIndexError(code, f"連線失敗：{parsed.host}") from None
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "轉址沒有目的地")
                    url = str(parsed.join(location))
                    continue
                check_status(response)
                yield response
                return
            finally:
                response.close()
        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "轉址次數太多")

    def get_json(self, url: str, *, allowed_hosts: frozenset[str], max_bytes: int) -> Any:
        with self._response(url, allowed_hosts) as response:
            ctype = response.headers.get("content-type", "").lower()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "詮釋資料超過大小上限")
        if "html" in ctype:
            raise DoorIndexError(ErrorCode.CHALLENGE, "詮釋資料回的是網頁，可能被擋")
        try:
            return json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "詮釋資料不是 JSON") from None

    def download(
        self, url: str, dest: Path, *, allowed_hosts: frozenset[str], max_bytes: int
    ) -> str:
        digest = hashlib.sha256()
        total = 0
        with self._response(url, allowed_hosts) as response:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌檔超過大小上限")
            if "html" in response.headers.get("content-type", "").lower():
                raise DoorIndexError(ErrorCode.CHALLENGE, "下載回的是網頁，可能被擋")
            with open(dest, "wb") as fh:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌檔超過大小上限")
                    digest.update(chunk)
                    fh.write(chunk)
        return digest.hexdigest()


def check_status(
    response: httpx.Response,
    *,
    error: type[FetchError] = DoorIndexError,
    invalid: ErrorCode = ErrorCode.DOORINDEX_INVALID,
) -> None:
    """200 才放行；403、429、挑戰頁、5xx 各有代碼，呼叫端一律停下，不重試、不繞過。"""
    if response.headers.get("cf-mitigated"):
        raise error(ErrorCode.CHALLENGE, "被挑戰頁擋下")
    status = response.status_code
    if status == 200:
        return
    if status == 403:
        raise error(ErrorCode.BLOCKED, "伺服器拒絕（403）")
    if status == 429:
        raise error(ErrorCode.RATE_LIMITED, "請求太頻繁（429）")
    if status >= 500:
        raise error(ErrorCode.HTTP_5XX, f"伺服器錯誤（{status}）")
    raise error(invalid, f"非預期的狀態碼（{status}）")


# ---------------------------------------------------------------- 建索引

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE roads (
    road_id  INTEGER PRIMARY KEY,
    district TEXT NOT NULL,
    name     TEXT NOT NULL,
    base     TEXT NOT NULL,
    section  TEXT NOT NULL,
    orig     TEXT NOT NULL,
    UNIQUE (district, name)
);
CREATE INDEX roads_by_base ON roads (district, base);
CREATE TABLE doors (
    door_id   INTEGER PRIMARY KEY,
    road_id   INTEGER NOT NULL REFERENCES roads (road_id),
    lane      TEXT NOT NULL,
    alley     TEXT NOT NULL,
    num       INTEGER NOT NULL,
    sub       INTEGER NOT NULL,
    x         REAL NOT NULL,
    y         REAL NOT NULL,
    n_rows    INTEGER NOT NULL,
    spread_m  REAL NOT NULL,
    max_floor INTEGER NOT NULL,
    cell      INTEGER NOT NULL,
    UNIQUE (road_id, lane, alley, num, sub)
);
CREATE INDEX doors_by_cell ON doors (cell);
CREATE TABLE door_metrics (
    area        TEXT NOT NULL,
    door_id     INTEGER NOT NULL,
    near_m      INTEGER,
    avoid_m     INTEGER,
    in_district INTEGER,
    drive_min   INTEGER,
    PRIMARY KEY (area, door_id)
) WITHOUT ROWID;
"""

_STAGE = """
CREATE TEMP TABLE raw (
    district TEXT, road TEXT, orig TEXT, lane TEXT, alley TEXT,
    num INTEGER, sub INTEGER, x REAL, y REAL, floor INTEGER
);
"""


def _cell(x: float, y: float) -> int:
    return int(x // CELL_M) * 100_000 + int(y // CELL_M)


@dataclass
class _Counts:
    rows: int = 0
    bad: int = 0
    unparsed: int = 0
    per_district: dict[str, int] = field(default_factory=dict)


def parse_number_column(text: str) -> tuple[str, int, int, int | None] | None:
    """門牌檔的「號」欄 → (衖, 號, 之號, 樓層)。樓層 None 是地下室；沒寫樓層就是 1 樓。

    例：'１４９號八樓' → ('', 149, 0, 8)；'５衖１２號' → ('5', 12, 0, 1)；
    '８號之１' → ('', 8, 1, 1)；'３７５號地下室貳層' → ('', 375, 0, None)。
    """
    s = _PAREN.sub("", norm(text))
    m = _DOOR_NUMBER.match(s)
    if not m:
        return None
    num = cn2int(m.group("num"))
    if num is None:
        return None
    sub = cn2int(m.group("sub") or m.group("sub2")) or 0
    alley = str(cn2int(m.group("alley"))) if m.group("alley") else ""
    tail = m.group("tail")
    floor: int | None
    if "地下" in tail:
        floor = None
    else:
        fm = _FLOOR.search(tail)
        floor = max(1, cn2int(fm.group(1)) or 1) if fm else 1
    return alley, num, sub, floor


def _stage_rows(conn: sqlite3.Connection, csv_path: Path, box: tuple[float, ...]) -> _Counts:
    counts = _Counts()
    batch: list[tuple[Any, ...]] = []
    minx, miny, maxx, maxy = box
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None or tuple(h.strip() for h in header) != HEADER:
            raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌檔的表頭不是預期的 11 欄")
        for row in reader:
            counts.rows += 1
            if len(row) != len(HEADER) or row[1] not in DISTRICT_NAMES:
                counts.bad += 1
                continue
            road_raw = row[4] or row[5]
            road = norm(road_raw)
            try:
                x, y = float(row[9]), float(row[10])
            except ValueError:
                counts.bad += 1
                continue
            if not road or not (math.isfinite(x) and math.isfinite(y)):
                counts.bad += 1
                continue
            if not (minx <= x <= maxx and miny <= y <= maxy):
                counts.bad += 1
                continue
            parsed = parse_number_column(row[8])
            if parsed is None:
                counts.unparsed += 1
                continue
            alley_in_num, num, sub, floor = parsed
            orig = re.sub(r"\s+", "", unicodedata.normalize("NFKC", road_raw))
            alley = lane_key(row[7]) or alley_in_num
            batch.append((row[1], road, orig, lane_key(row[6]), alley, num, sub, x, y, floor))
            counts.per_district[row[1]] = counts.per_district.get(row[1], 0) + 1
            if len(batch) >= 20_000:
                conn.executemany("INSERT INTO raw VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO raw VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    return counts


def _validate_counts(counts: _Counts, min_rows: Mapping[str, int]) -> None:
    if counts.rows == 0:
        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌檔沒有資料")
    if counts.bad > counts.rows * MAX_BAD_ROW_FRACTION:
        raise DoorIndexError(
            ErrorCode.DOORINDEX_INVALID, f"有 {counts.bad} 列欄數、區碼或坐標不對（超過上限）"
        )
    if counts.unparsed > counts.rows * MAX_UNPARSED_FRACTION:
        raise DoorIndexError(
            ErrorCode.DOORINDEX_INVALID, f"有 {counts.unparsed} 列的門牌號碼解析不了（超過上限）"
        )
    for code, minimum in sorted(min_rows.items()):
        have = counts.per_district.get(code, 0)
        if have < minimum:
            name = DISTRICT_NAMES.get(code, code)
            raise DoorIndexError(
                ErrorCode.DOORINDEX_INVALID, f"{name} 只有 {have} 列，少於下限 {minimum}"
            )


def _aggregate(conn: sqlite3.Connection) -> int:
    road_ids: dict[tuple[str, str], int] = {}
    doors: list[tuple[Any, ...]] = []
    n_doors = 0
    cursor = conn.execute(
        "SELECT district, road, orig, lane, alley, num, sub, x, y, floor FROM raw "
        "ORDER BY district, road, lane, alley, num, sub"
    )
    for key, group in itertools.groupby(cursor, key=lambda r: r[:2] + r[3:7]):
        rows = list(group)
        district, road, lane, alley, num, sub = key
        road_id = road_ids.get((district, road))
        if road_id is None:
            base, section = split_section(road)
            road_id = conn.execute(
                "INSERT INTO roads (district, name, base, section, orig) VALUES (?,?,?,?,?)",
                (district, road, base, section, rows[0][2]),
            ).lastrowid
            assert road_id is not None
            road_ids[(district, road)] = road_id
        mx = statistics.median(r[7] for r in rows)
        my = statistics.median(r[8] for r in rows)
        spread = max(math.hypot(r[7] - mx, r[8] - my) for r in rows)
        max_floor = max((r[9] for r in rows if r[9] is not None), default=1)
        doors.append(
            (
                road_id,
                lane,
                alley,
                num,
                sub,
                mx,
                my,
                len(rows),
                round(spread, 1),
                max_floor,
                _cell(mx, my),
            )
        )
        n_doors += 1
        if len(doors) >= 20_000:
            conn.executemany(
                "INSERT INTO doors (road_id, lane, alley, num, sub, x, y, n_rows, spread_m, "
                "max_floor, cell) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                doors,
            )
            doors.clear()
    if doors:
        conn.executemany(
            "INSERT INTO doors (road_id, lane, alley, num, sub, x, y, n_rows, spread_m, "
            "max_floor, cell) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            doors,
        )
    return n_doors


def build_index(
    csv_path: Path,
    out_path: Path,
    *,
    version: str,
    now: dt.datetime,
    source_url: str | None = None,
    sha256: str | None = None,
    min_rows: Mapping[str, int] | None = None,
    box: tuple[float, float, float, float] = CITY_BOX_TM2,
    ctx: GeoContext | None = None,
) -> IndexInfo:
    """把門牌 CSV 建成索引檔 out_path（覆寫）。檢查不過就丟 DoorIndexError，不留檔。"""
    out_path.unlink(missing_ok=True)
    min_rows = DEFAULT_MIN_ROWS if min_rows is None else min_rows
    try:
        with closing(sqlite3.connect(out_path, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.executescript(_SCHEMA + _STAGE)
            conn.execute("BEGIN")
            counts = _stage_rows(conn, csv_path, box)
            _validate_counts(counts, min_rows)
            n_doors = _aggregate(conn)
            conn.execute("DROP TABLE raw")
            built_at = to_iso(now)
            meta = {
                "schema": SCHEMA_VERSION,
                "version": version,
                "source_url": source_url or "",
                "built_at": built_at,
                "rows": str(sum(counts.per_district.values())),
                "doors": str(n_doors),
                "sha256": sha256 or "",
                "district_rows": json.dumps(dict(sorted(counts.per_district.items()))),
                "geo_version": "",
            }
            conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
            conn.execute("COMMIT")
            conn.execute("VACUUM")
            conn.execute("PRAGMA journal_mode = DELETE")
        if ctx is not None:
            _compute_metrics(out_path, ctx)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return IndexInfo(
        version=version,
        source_url=source_url,
        built_at=built_at,
        rows=int(meta["rows"]),
        doors=n_doors,
        sha256=sha256,
        changed=True,
        district_rows=dict(sorted(counts.per_district.items())),
    )


def _compute_metrics(path: Path, ctx: GeoContext) -> None:
    """依 GeoContext 重算 door_metrics（只算各區域查詢的行政區裡的門牌）。"""
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM door_metrics")
            for area_id in ctx.area_ids:
                codes = [
                    DISTRICT_CODES[n] for n in ctx.area(area_id).districts if n in DISTRICT_CODES
                ]
                if not codes:
                    continue
                marks = ",".join("?" * len(codes))
                rows = conn.execute(
                    "SELECT d.door_id, d.x, d.y FROM doors d JOIN roads r ON r.road_id = d.road_id "
                    f"WHERE r.district IN ({marks}) ORDER BY d.door_id",
                    codes,
                ).fetchall()
                raw = ctx.raw_metrics([r[1] for r in rows], [r[2] for r in rows], area_id)
                conn.executemany(
                    "INSERT INTO door_metrics VALUES (?,?,?,?,?,?)",
                    [
                        (area_id, row[0], m[0], m[1], None if m[2] is None else int(m[2]), m[3])
                        for row, m in zip(rows, raw, strict=True)
                    ],
                )
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('geo_version', ?)", (ctx.version,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- 換檔與更新


def prev_path(data_dir: Path) -> Path:
    return data_dir / "doorindex.prev.sqlite"


def install(new_index: Path, data_dir: Path) -> None:
    """把建好的索引換上。先把現有的索引留一份 prev，最後一步才原子改名，中間永遠有索引可用。"""
    current = DataPaths(data_dir).doorindex
    if current.exists():
        keep = prev_path(data_dir)
        staging = keep.with_name(keep.name + ".tmp")
        staging.unlink(missing_ok=True)
        try:
            os.link(current, staging)
        except OSError:
            shutil.copyfile(current, staging)
        os.replace(staging, keep)
    os.replace(new_index, current)


def _read_meta(path: Path) -> dict[str, str]:
    try:
        with closing(_connect_ro(path)) as conn:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌索引損壞或不是索引檔") from None


def read_info(path: Path) -> IndexInfo | None:
    """索引檔的版本資訊；檔案不存在或讀不了就是 None。"""
    if not path.is_file():
        return None
    try:
        meta = _read_meta(path)
    except DoorIndexError:
        return None
    if meta.get("schema") != SCHEMA_VERSION:
        return None
    return _info_from_meta(meta)


def _info_from_meta(meta: Mapping[str, str]) -> IndexInfo:
    return IndexInfo(
        version=meta.get("version", ""),
        source_url=meta.get("source_url") or None,
        built_at=meta.get("built_at") or None,
        rows=int(meta.get("rows") or 0),
        doors=int(meta.get("doors") or 0),
        sha256=meta.get("sha256") or None,
        district_rows=json.loads(meta.get("district_rows") or "{}"),
    )


def refresh(
    fetcher: DoorFetcher | httpx.Client,
    data_dir: Path,
    now: dt.datetime,
    *,
    ctx: GeoContext | None = None,
    force: bool = False,
    max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
    min_rows: Mapping[str, int] | None = None,
) -> IndexInfo:
    """查最新一期，有新版才下載、檢查、建索引並換檔（契約 C7 的 `doorindex.refresh`）。

    fetcher 可以是 DoorFetcher，或是 httpx.Client（自動包成 HttpFetcher）。
    呼叫前要先關掉所有開著的 DoorIndex。失敗時丟 DoorIndexError，現有索引不動。
    """
    if isinstance(fetcher, httpx.Client):
        fetcher = HttpFetcher(fetcher)
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata = fetcher.get_json(
        DATASET_API, allowed_hosts=METADATA_HOSTS, max_bytes=METADATA_MAX_BYTES
    )
    latest = pick_latest(metadata)
    check_url(latest.url, DOWNLOAD_HOSTS)
    current = read_info(DataPaths(data_dir).doorindex)
    if (
        not force
        and current is not None
        and current.version == latest.version
        and current.source_url == latest.url
    ):
        return IndexInfo(**{**current.__dict__, "changed": False})
    part = data_dir / "doorindex.download.part"
    building = data_dir / "doorindex.sqlite.build"
    try:
        digest = fetcher.download(
            latest.url, part, allowed_hosts=DOWNLOAD_HOSTS, max_bytes=max_bytes
        )
        info = build_index(
            part,
            building,
            version=latest.version,
            now=now,
            source_url=latest.url,
            sha256=digest,
            min_rows=min_rows,
            ctx=ctx,
        )
        install(building, data_dir)
    finally:
        part.unlink(missing_ok=True)
        building.unlink(missing_ok=True)
    return info


# ---------------------------------------------------------------- 查詢


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _median_point(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    mx = statistics.median(p[0] for p in points)
    my = statistics.median(p[1] for p in points)
    dists = sorted(math.hypot(x - mx, y - my) for x, y in points)
    p90 = dists[int(0.9 * (len(dists) - 1))] if dists else 0.0
    return mx, my, round(p90, 1)


@dataclass(frozen=True)
class _Road:
    road_id: int
    name: str
    base: str
    section: str
    orig: str


class DoorIndex:
    """開好的門牌索引（唯讀）。用 `with DoorIndex.open(path, ctx) as idx:`。

    給了 ctx 時，如果索引裡的範圍指標是舊 geo 版本算的，會先重算一次。
    """

    def __init__(self, path: Path, ctx: GeoContext | None = None) -> None:
        self.path = path
        if not path.is_file():
            raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "還沒有門牌索引")
        meta = _read_meta(path)
        if meta.get("schema") != SCHEMA_VERSION:
            raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "門牌索引的格式版本不對，請重建")
        if ctx is not None and meta.get("geo_version") != ctx.version:
            _compute_metrics(path, ctx)
        self.info = _info_from_meta(meta)
        self._ctx = ctx
        self._roads: dict[str, list[_Road]] = {}
        self._conn = _connect_ro(path)

    @classmethod
    def open(cls, path: Path, ctx: GeoContext | None = None) -> "DoorIndex":
        return cls(path, ctx)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DoorIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def version(self) -> str:
        return self.info.version

    # ------------------------------------------------------------ 路名

    def _road_list(self, code: str) -> list[_Road]:
        roads = self._roads.get(code)
        if roads is None:
            rows = self._conn.execute(
                "SELECT road_id, name, base, section, orig FROM roads WHERE district = ?", (code,)
            ).fetchall()
            roads = sorted((_Road(*row) for row in rows), key=lambda r: (-len(r.name), r.name))
            self._roads[code] = roads
        return roads

    def _match_road(self, code: str, s: str) -> tuple[_Road | None, str]:
        """字串開頭最長的已知路名；造字萬用字元只能代表 Big5 沒有的字，而且結果要唯一。"""
        roads = self._road_list(code)
        for road in roads:
            if s.startswith(road.name):
                return road, s[len(road.name) :]
        if any(is_wild(ch) for ch in s[:10]):
            candidates = []
            for road in roads:
                cand = s[: len(road.name)]
                if len(cand) != len(road.name) or not any(is_wild(ch) for ch in cand):
                    continue
                orig = road.orig
                if all(
                    a == b or (is_wild(a) and (len(orig) != len(road.name) or not in_big5(orig[i])))
                    for i, (a, b) in enumerate(zip(cand, road.name, strict=True))
                ):
                    candidates.append(road)
            if candidates:
                best = max(len(c.name) for c in candidates)
                top = [c for c in candidates if len(c.name) == best]
                if len(top) == 1:
                    return top[0], s[best:]
        return None, s

    def _sectioned_base(self, code: str, s: str) -> tuple[str | None, str]:
        """只有分段的路（例如只有莊敬路一段、二段）：比對路名本體（R3-07）。"""
        bases = sorted(
            {r.base for r in self._road_list(code) if r.section},
            key=lambda b: (-len(b), b),
        )
        for base in bases:
            if s.startswith(base):
                return base, s[len(base) :]
        return None, s

    def _variants(self, code: str, base: str) -> list[_Road]:
        return [r for r in self._road_list(code) if r.base == base]

    def _find_roads_anywhere(self, code: str, s: str) -> list[str]:
        found, i = [], 0
        roads = self._road_list(code)
        while i < len(s):
            for road in roads:
                if s.startswith(road.name, i):
                    found.append(road.name)
                    i += len(road.name)
                    break
            else:
                i += 1
        return found

    def road_districts(self, text: str) -> list[str]:
        """路名在哪些區找得到（區碼）。地址沒寫區時，用來判斷這條路是不是只在一個區。"""
        _, s = split_prefix(text)
        found = []
        for code in sorted(DISTRICT_NAMES):
            parts = self.parse_road(code, s)
            if parts.base is not None:
                found.append(code)
        return found

    def parse_road(self, district: str, text: str) -> AddressParts:
        """把地址（可以帶縣市區前綴）切成路名、段、巷弄號。區是必要參數（R3-01）。"""
        code = district_code(district)
        _, s = split_prefix(text)
        flags: list[str] = []
        if not self._match_road(code, s)[0]:
            s = VILLAGE_RE.sub("", s, count=1)
        s = _PAREN.sub("", s)
        s = re.sub(r"等(?=號)", "", s)
        if s.endswith("等"):
            flags.append("multi_etc")
            s = s[:-1]
        fm = FUZZY_SUFFIX.search(s)
        if fm:
            flags.append("fuzzy")
            s = s[: fm.start()]
        s = _SECTION_DIGIT.sub(_section_digits, s)
        road, rest = self._match_road(code, s)
        if road is None:
            m3 = re.match(r"^(.*?)([0-9]+)(街|路)(.*)$", s)
            if m3 and int(m3.group(2)) < 100:
                road, rest = self._match_road(
                    code, m3.group(1) + int2cn(int(m3.group(2))) + m3.group(3) + m3.group(4)
                )
        base: str | None
        section: str | None
        if road is not None:
            base = road.base
            if road.section:
                section = road.section
            elif len(self._variants(code, road.base)) > 1:
                section = None
                flags.append("section_unknown")
            else:
                section = ""
        else:
            base, rest = self._sectioned_base(code, s)
            if base is None:
                failure = "land_parcel" if "地號" in s else "road_not_found"
                return AddressParts(flags=tuple(flags), failure=failure)
            variants = self._variants(code, base)
            if len(variants) == 1:  # 只有一個分段：沒寫段也不會弄錯
                section = variants[0].section
            else:
                section = None
                flags.append("section_unknown")
        full = road.name if road is not None else base
        others = [r for r in self._find_roads_anywhere(code, rest) if r != full]
        if others and not re.search(NUM + r"號", rest.split(others[0])[0]):
            return AddressParts(
                base=base, section=section, cross_road=others[0], flags=(*flags, "intersection")
            )
        rest = re.sub(r"(?<=[0-9])-(?=[0-9]+號)", "之", rest)
        mm = _ADDR_NUMBER.match(rest)
        assert mm is not None  # 每一段都是選填，一定比對得到
        num, num_to, sub = cn2int(mm.group(3)), cn2int(mm.group(4)), cn2int(mm.group(5))
        if mm.group(6):
            flags.append("multi_number")
        tail = mm.group(7)
        if num is not None and sub is None:
            sub2 = re.match(r"^之(" + NUM + r")(?!樓)", tail)
            if sub2:
                sub = cn2int(sub2.group(1))
        number = HouseNumber(
            lane=lane_key(mm.group(1)), alley=lane_key(mm.group(2)), num=num, num_to=num_to, sub=sub
        )
        return AddressParts(base=base, section=section, number=number, flags=tuple(flags))

    # ------------------------------------------------------------ 門牌

    def _resolve_roads(self, code: str, road: str, section: str | None) -> list[_Road]:
        """section：None＝不知道（全部同名路段的聯集）、''＝明確沒有段、'二段'＝指定。"""
        base, sec_in_name = split_section(norm(road))
        if sec_in_name:
            if section not in (None, sec_in_name):
                return []
            section = sec_in_name
        variants = self._variants(code, base)
        if section is None:
            return variants
        return [r for r in variants if r.section == section]

    def _doors(
        self, roads: Sequence[_Road], where: str = "", params: Sequence[Any] = ()
    ) -> list[tuple[Any, ...]]:
        if not roads:
            return []
        marks = ",".join("?" * len(roads))
        sql = (
            "SELECT door_id, road_id, lane, alley, num, sub, x, y, max_floor FROM doors "
            f"WHERE road_id IN ({marks}){where} ORDER BY door_id"
        )
        return self._conn.execute(sql, [*(r.road_id for r in roads), *params]).fetchall()

    def _road_key(self, code: str, roads: Sequence[_Road], lane: str, alley: str) -> str:
        names = {r.name for r in roads}
        spec = next(iter(names)) if len(names) == 1 else roads[0].base + "*"
        return f"{code}|{spec}|{lane}|{alley}"

    def _match(
        self,
        level: str,
        code: str,
        roads: Sequence[_Road],
        rows: Sequence[tuple[Any, ...]],
        lane: str,
        alley: str,
        flags: Sequence[str],
        nearest_num: int | None = None,
    ) -> DoorMatch:
        mx, my, p90 = _median_point([(r[6], r[7]) for r in rows])
        lon, lat = tm2_to_wgs84(mx, my)
        door_key = None
        if len(rows) == 1:
            row = rows[0]
            name = next(r.name for r in roads if r.road_id == row[1])
            door_key = f"{code}|{name}|{row[2]}|{row[3]}|{row[4]}|{row[5]}"
        return DoorMatch(
            level=level,
            district=code,
            road_key=self._road_key(code, roads, lane, alley),
            door_ids=tuple(r[0] for r in rows),
            x=mx,
            y=my,
            lat=round(lat, 7),
            lon=round(lon, 7),
            n_points=len(rows),
            spread_p90_m=p90,
            door_key=door_key,
            nearest_num=nearest_num,
            flags=tuple(flags),
        )

    def lookup(
        self, district: str, road: str, section: str | None, number: HouseNumber | None
    ) -> DoorMatch | None:
        """依序：號碼範圍 → 完全相符 → 主號 → 鄰號 → 巷弄 → 路（R3b §3）。

        沒寫段而有多個同名路段時（R3-07）：這個號碼（含之號，找不到再看主號）只在其中一段
        存在，就用那一段比對並標 section_inferred；不只一段有、或都沒有，就用巷或路的
        聯集，標 section_unknown。找不到這條路就回傳 None。
        """
        code = district_code(district)
        roads = self._resolve_roads(code, road, section)
        if not roads:
            return None
        flags = ["section_unknown"] if len(roads) > 1 else []
        lane = number.lane if number else ""
        alley = number.alley if number else ""
        if number is not None and number.num is not None:
            if len(roads) > 1:
                owners = self._number_owners(roads, number, lane, alley)
                if len(owners) == 1:
                    found = self._number_match(
                        code, owners, number, lane, alley, ["section_inferred"]
                    )
                    if found is not None and found.level in ("exact", "main_number"):
                        return found
            else:
                found = self._number_match(code, roads, number, lane, alley, flags)
                if found is not None:
                    return found
            flags.append("number_not_found")
        if lane:
            rows = self._doors(roads, " AND lane = ? AND alley = ?", (lane, alley))
            if rows:
                level = "alley" if alley else "lane"
                return self._match(level, code, roads, rows, lane, alley, flags)
            rows = self._doors(roads, " AND lane = ?", (lane,))
            if rows:
                return self._match("lane", code, roads, rows, lane, "", flags)
        rows = self._doors(roads)
        if not rows:
            return None
        return self._match("road", code, roads, rows, "", "", flags)

    def _number_owners(
        self, roads: Sequence[_Road], number: HouseNumber, lane: str, alley: str
    ) -> list[_Road]:
        """有這個號碼的路段：先看號＋之號完全相同，沒有再看主號。"""
        where = " AND lane = ? AND alley = ? AND num = ?"
        params: list[Any] = [lane, alley, number.num]
        rows = self._doors(roads, where + " AND sub = ?", [*params, number.sub or 0])
        if not rows:
            rows = self._doors(roads, where, params)
        owner_ids = {row[1] for row in rows}
        return [r for r in roads if r.road_id in owner_ids]

    def _number_match(
        self,
        code: str,
        roads: Sequence[_Road],
        number: HouseNumber,
        lane: str,
        alley: str,
        flags: list[str],
    ) -> DoorMatch | None:
        num = number.num
        assert num is not None
        where = " AND lane = ? AND alley = ?"
        if number.num_to is not None:
            lo, hi = sorted((num, number.num_to))
            rows = self._doors(roads, where + " AND num BETWEEN ? AND ?", (lane, alley, lo, hi))
            if rows:
                found = self._match("number_range", code, roads, rows, lane, alley, flags)
                if found.spread_p90_m > RANGE_DOOR_MAX_P90_M:
                    return None
                return found
        sub = number.sub or 0
        rows = self._doors(roads, where + " AND num = ? AND sub = ?", (lane, alley, num, sub))
        if rows:
            return self._match("exact", code, roads, rows, lane, alley, flags)
        rows = self._doors(roads, where + " AND num = ?", (lane, alley, num))
        if rows:
            return self._match("main_number", code, roads, rows, lane, alley, flags)
        rows = self._doors(
            roads,
            where + " AND num BETWEEN ? AND ? AND num % 2 = ?",
            (lane, alley, num - NEARBY_NUMBER_MAX_GAP, num + NEARBY_NUMBER_MAX_GAP, num % 2),
        )
        below = [r for r in rows if r[4] < num]
        above = [r for r in rows if r[4] > num]
        if below and above:
            low = max(r[4] for r in below)
            high = min(r[4] for r in above)
            low_rows = [r for r in below if r[4] == low]
            high_rows = [r for r in above if r[4] == high]
            lx, ly, _ = _median_point([(r[6], r[7]) for r in low_rows])
            hx, hy, _ = _median_point([(r[6], r[7]) for r in high_rows])
            if math.hypot(hx - lx, hy - ly) <= NEARBY_NUMBER_MAX_SPAN_M:
                nearest = low if num - low <= high - num else high
                chosen = low_rows if nearest == low else high_rows
                return self._match(
                    "nearby_number", code, roads, chosen, lane, alley, flags, nearest_num=nearest
                )
        return None

    def road_doors(
        self,
        district: str,
        road: str,
        section: str | None,
        min_floors: int | None,
        *,
        lane: str = "",
        alley: str = "",
    ) -> list[int]:
        """一條路（section 是 None 時為所有同名路段）的門牌 id，只留最高樓層 ≥ min_floors 的（R1-14）。"""
        code = district_code(district)
        roads = self._resolve_roads(code, road, section)
        where, params = "", []
        if lane:
            where += " AND lane = ?"
            params.append(lane)
            if alley:
                where += " AND alley = ?"
                params.append(alley)
        if min_floors is not None:
            where += " AND max_floor >= ?"
            params.append(min_floors)
        return [row[0] for row in self._doors(roads, where, params)]

    def road_doors_by_key(self, road_key: str, min_floors: int | None) -> list[int]:
        """用 `DoorMatch.road_key`／`Location.road_key` 取回門牌 id。"""
        try:
            code, spec, lane, alley = road_key.split("|")
        except ValueError:
            raise ValueError(f"road_key 格式錯誤：{road_key}") from None
        if spec.endswith("*"):
            return self.road_doors(code, spec[:-1], None, min_floors, lane=lane, alley=alley)
        base, section = split_section(spec)
        return self.road_doors(code, base, section, min_floors, lane=lane, alley=alley)

    def door_points(self, door_ids: Sequence[int]) -> list[tuple[float, float]]:
        """門牌 id → TM2 坐標（順序照輸入）。"""
        if not door_ids:
            return []
        rows = self._conn.execute(
            "SELECT j.key, d.x, d.y FROM json_each(?) j JOIN doors d ON d.door_id = j.value "
            "ORDER BY j.key",
            (json.dumps(list(door_ids)),),
        ).fetchall()
        return [(row[1], row[2]) for row in rows]

    def road_frac(self, door_ids: Sequence[int], area: str) -> dict[str, float]:
        """一組門牌點在各選項組合下「在範圍內」的比例（鍵見 areas 模組說明）。

        要在開索引時給 ctx。沒有指標的門牌（不在區域查詢的行政區）算在範圍外。
        """
        if self._ctx is None:
            raise DoorIndexError(ErrorCode.DOORINDEX_INVALID, "開索引時沒有給 GeoContext")
        if not door_ids:
            return {}
        rows = self._conn.execute(
            "SELECT m.near_m, m.avoid_m, m.in_district, m.drive_min, m.door_id "
            "FROM json_each(?) j LEFT JOIN door_metrics m "
            "ON m.area = ? AND m.door_id = j.value",
            (json.dumps(list(door_ids)), area),
        ).fetchall()
        raw = [
            None
            if row[4] is None
            else (row[0], row[1], None if row[2] is None else bool(row[2]), row[3])
            for row in rows
        ]
        return self._ctx.road_frac_from_raw(raw, area)

    # ------------------------------------------------------------ 座標屬於哪一區

    def district_at(self, x: float, y: float) -> CoordDistrict:
        """座標（TM2）附近門牌的區。100 m 內只有一個區的門牌才算決定性（R3-01）。"""
        reach = math.ceil(NEARBY_SEARCH_M / CELL_M)
        ix, iy = int(x // CELL_M), int(y // CELL_M)
        cells = [
            (ix + dx) * 100_000 + (iy + dy)
            for dx in range(-reach, reach + 1)
            for dy in range(-reach, reach + 1)
        ]
        marks = ",".join("?" * len(cells))
        rows = self._conn.execute(
            "SELECT d.x, d.y, r.district FROM doors d JOIN roads r ON r.road_id = d.road_id "
            f"WHERE d.cell IN ({marks})",
            cells,
        ).fetchall()
        dists = sorted((math.hypot(rx - x, ry - y), code) for rx, ry, code in rows)
        dists = [item for item in dists if item[0] <= NEARBY_SEARCH_M]
        if not dists:
            return CoordDistrict(None, False, None)
        nearest_m, nearest_code = dists[0]
        close = {code for d, code in dists if d <= DECISIVE_M}
        decisive = len(close) == 1
        return CoordDistrict(nearest_code, decisive, round(nearest_m, 1))

    def geocode(self, district: str, text: str) -> DoorMatch | None:
        """地址字串在指定區的定位（不處理社區名、不判斷區）。路口回傳路的層級並標 intersection。"""
        parts = self.parse_road(district, text)
        if parts.base is None:
            return None
        found = self.lookup(district, parts.base, parts.section, parts.number)
        return merge_flags(found, parts.flags) if found is not None else None


def merge_flags(match: DoorMatch, parse_flags: Sequence[str]) -> DoorMatch:
    """把 parse_road 的旗標併進比對結果；比對已經推得出段時，拿掉 section_unknown。"""
    extra = [
        f for f in parse_flags if not (f == "section_unknown" and "section_inferred" in match.flags)
    ]
    merged = tuple(dict.fromkeys((*match.flags, *extra)))
    return match if merged == match.flags else DoorMatch(**{**match.__dict__, "flags": merged})


def remove_partial(data_dir: Path) -> None:
    """清掉中斷的下載或建檔留下的暫存檔。"""
    for name in ("doorindex.download.part", "doorindex.sqlite.build", "doorindex.prev.sqlite.tmp"):
        with suppress(FileNotFoundError):
            (data_dir / name).unlink()
