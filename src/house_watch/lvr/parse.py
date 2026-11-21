"""實價登錄買賣 a 檔解析（WP3）：主檔＋build＋park 明細 → TxFacts。

檔案格式（R1 §2.4）：UTF-8 含 BOM、CRLF；第 1 列中文欄名，第 2 列機器翻譯的英文欄名要跳過；
主檔與明細用「編號」關聯，明細一個編號可以有多列。

只輸出事實與「排除原因／附註」，不做任何樓層、屋齡、面積、價格的篩選；那些是瀏覽器的事。

**排除（exclusion，取表上第一個符合的）**

| 順序 | 規則 | 代碼 |
|---|---|---|
| 1 | 交易標的不含建物，或建物筆棟數＝0 | no_building |
| 2 | 交易筆棟數的建物 ≥ 2（一次買好幾棟） | multi_building |
| 3 | 交易日早於完工日（預售屋的訂約紀錄） | presale |
| 4 | 備註決策表（`classify_remark`，見下） | 各列代碼 |
| 5 | 移轉層次含夾層、屋頂突出物（附帶加大空間） | mezzanine、rooftop_structure |

**備註決策表**（R1-04；片語常常黏在一起，例如「鐵鋁窗增建其他增建；」，所以不用「；」切）
1. 先把「保留類片語」拿掉並記下附註：陽台外推、陽台增建 → balcony_enclosed；
   陽台加窗、鐵鋁窗（增建）、加窗 → window_added；含裝潢 → incl_renovation；
   含傢俱設備 → incl_furniture；露台（不含「露台增建」）→ terrace。
2. 剩下的字串依序比對排除類樣式（`REMARK_EXCLUSIONS`）。其他增建、頂樓加蓋、夾層、未登記建物，
   以及拿掉保留片語後還剩「增建／違建／加蓋」字樣的，都排除。
3. 「預售屋、或土地及建物分件登記案件」而交易日不早於完工日的，排除為 presale_split_reg
   （照 R1 的預設；這些多半是預售屋交屋或分件登記）。

**附註（flags，排序、不重複）**：備註的保留類、built_unknown（完工日不明 → 待確認）、
built_from_detail、built_mismatch、elevator_conflict、floor_unparsed、floor_whole，
以及車位相關的 parking_detail_missing、parking_price_split、parking_area_split、
parking_count_mismatch、parking_price_mismatch、parking_area_mismatch、parking_without_count。

**車位**：多個車位時逐位看 park 明細（主檔的車位類別只顯示一種）。
- 明細的價格都是 0 但主檔車位總價 > 0：把總價平均分給各車位（餘數給前面的），
  超過一個車位時加註 parking_price_split；面積同理（parking_area_split）。
- 有車位數卻沒有明細：用主檔的類別與總價、總面積補出 n 個車位，加註 parking_detail_missing。
- 類別非空但車位數＝0：不扣車位（R1-05 第 2 點），加註 parking_without_count。
- 價格或面積是 0 或空白 → None（沒有單獨申報）。
"""

import csv
import datetime as dt
import io
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.models import DateRange, Parking, ParkingSpace, TxFacts
from house_watch.parse.age import built_from_date, date_range
from house_watch.parse.area import m2_to_cm2
from house_watch.parse.elevator import decide_elevator, parse_btype
from house_watch.parse.parking import parse_parking
from house_watch.parse.text import CN_NUMERAL_CHARS, cn_to_int, nfkc, normalize_address

ROC_OFFSET = 1911
SERIAL_RE = re.compile(r"^[A-Z0-9]{8,40}$")
ADDRESS_MAX = 200

MAIN_COLUMNS = (
    "鄉鎮市區",
    "交易標的",
    "土地位置建物門牌",
    "交易年月日",
    "交易筆棟數",
    "移轉層次",
    "總樓層數",
    "建物型態",
    "建築完成年月",
    "建物移轉總面積平方公尺",
    "總價元",
    "車位類別",
    "車位移轉總面積平方公尺",
    "車位總價元",
    "備註",
    "編號",
    "主建物面積",
    "附屬建物面積",
    "陽台面積",
    "電梯",
)
BUILD_COLUMNS = ("編號", "建築完成日期", "建物分層")
PARK_COLUMNS = ("編號", "車位類別", "車位價格", "車位面積平方公尺")


class LvrError(HouseWatchError):
    """實價登錄的檔案或內容不合法。公開代碼是 lvr_invalid。"""

    code = ErrorCode.LVR_INVALID


# ---------------------------------------------------------------- 讀檔


@dataclass
class Table:
    """一個 CSV 檔：資料列（欄名 → 值）與欄數不對而略過的列數。"""

    rows: list[dict[str, str]]
    bad_rows: int = 0


def read_table(data: bytes, required: Sequence[str], *, what: str) -> Table:
    """讀實價登錄 CSV：utf-8-sig、跳過第 2 列英文欄名、檢查必要欄位與欄數。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LvrError(f"{what}：不是 UTF-8") from None
    reader = csv.reader(io.StringIO(text, newline=""))
    header = [cell.strip() for cell in next(reader, [])]
    if not header:
        raise LvrError(f"{what}：檔案是空的")
    missing = [column for column in required if column not in header]
    if missing:
        raise LvrError(f"{what}：缺少欄位 {', '.join(missing)}")
    serial_at = header.index("編號")
    table = Table(rows=[])
    for index, row in enumerate(reader):
        if index == 0 and _is_english_header(row, serial_at):
            continue
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            table.bad_rows += 1
            continue
        table.rows.append(dict(zip(header, row, strict=True)))
    return table


def _is_english_header(row: list[str], serial_at: int) -> bool:
    serial = row[serial_at].strip() if serial_at < len(row) else ""
    return not SERIAL_RE.match(serial) and all(cell.isascii() for cell in row)


# ---------------------------------------------------------------- 欄位


def roc_date(text: str | None) -> dt.date | None:
    """民國 yyyMMdd（7 碼）或 yyMMdd（6 碼）→ 日期。交易日要完整，日或月為 00 都回 None。"""
    s = (text or "").strip()
    if not re.fullmatch(r"\d{6,7}", s):
        return None
    roc = int(s[:-4])
    if roc == 0:
        return None
    try:
        return dt.date(roc + ROC_OFFSET, int(s[-4:-2]), int(s[-2:]))
    except ValueError:
        return None


def roc_built(text: str | None) -> DateRange:
    """主檔的「建築完成年月」（名稱寫年月，實際含日）→ 完工日區間。

    日為 00 → 整個月；月為 00 → 整年；負數、位數不對、年為 0 → 未知。
    """
    s = (text or "").strip()
    if not re.fullmatch(r"\d{6,7}", s):
        return DateRange()
    roc = int(s[:-4])
    if roc == 0:
        return DateRange()
    return date_range(roc + ROC_OFFSET, int(s[-4:-2]), int(s[-2:]))


def parse_counts(text: str | None) -> tuple[int, int, int] | None:
    """「土地1建物1車位2」→ (1, 1, 2)。"""
    m = re.fullmatch(r"土地(\d+)建物(\d+)車位(\d+)", (text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_yuan(text: str | None) -> int | None:
    """整數元；空白或看不懂回 None。"""
    s = (text or "").strip().replace(",", "")
    if not s:
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value)


def parse_cm2(text: str | None) -> int | None:
    """m² 字串 → 0.01 m² 整數；空白或看不懂回 None（0 照樣回 0）。"""
    s = (text or "").strip()
    if not s:
        return None
    try:
        return m2_to_cm2(s)
    except ValueError:
        return None


_FLOOR_TOKEN = re.compile(rf"^(地下)?([{CN_NUMERAL_CHARS}]+)?層$")
FLOOR_ANNOTATIONS = frozenset({"陽台", "見其他登記事項", "平台", "通道", "騎樓"})
FLOOR_EXTRA_SPACE = {"夾層": "mezzanine", "屋頂突出物": "rooftop_structure"}


@dataclass(frozen=True)
class TransferFloors:
    """「移轉層次」的解析結果。"""

    lo: int | None = None
    hi: int | None = None
    whole: bool = False
    extra: tuple[str, ...] = ()  # 夾層、屋頂突出物的排除代碼
    unknown: tuple[str, ...] = ()  # 看不懂的項目


def parse_total_floors(text: str | None) -> int | None:
    """「十五層」→ 15；空白、0 或看不懂回 None。"""
    m = _FLOOR_TOKEN.match(nfkc(text))
    if m is None or m.group(1) or m.group(2) is None:
        return None
    value = cn_to_int(m.group(2))
    return value or None


def parse_transfer_floors(text: str | None, floors_total: int | None = None) -> TransferFloors:
    """「四層」「一層，二層」「地下二層」「全」「五層，陽台」「五層，夾層」…（R1 §4）。

    - 地下樓層是負數；「地下層」→ -1。同時有地上樓層時，地下不算進樓層區間。
    - 「全」是整棟：樓層 [1, 總樓層]（總樓層未知時樓層也未知）。
    - 陽台、見其他登記事項、平台、通道、騎樓只是附註，忽略。
    - 夾層、屋頂突出物是附帶加大空間，回傳對應的排除代碼。
    """
    floors: list[int] = []
    whole = False
    extra: list[str] = []
    unknown: list[str] = []
    for raw in re.split(r"[，,、]", nfkc(text)):
        token = raw.strip()
        if not token:
            continue
        if token == "全":
            whole = True
            continue
        if token in FLOOR_ANNOTATIONS:
            continue
        if token in FLOOR_EXTRA_SPACE:
            extra.append(FLOOR_EXTRA_SPACE[token])
            continue
        m = _FLOOR_TOKEN.match(token)
        if m is None:
            unknown.append(token)
            continue
        if m.group(1):
            depth = cn_to_int(m.group(2)) if m.group(2) else 1
            if not depth:
                unknown.append(token)
                continue
            floors.append(-depth)
            continue
        value = cn_to_int(m.group(2)) if m.group(2) else None
        if not value:
            unknown.append(token)
            continue
        floors.append(value)
    if whole:
        lo, hi = (1, floors_total) if floors_total else (None, None)
        if floors:
            lo = min([*floors, *([lo] if lo is not None else [])])
            hi = max([*floors, *([hi] if hi is not None else [])])
    elif floors:
        # 同時有地上和地下時，地下（通常是另有建號的車位或儲藏）不算進樓層區間，
        # 否則「七層，地下二層」會變成 -2～7 樓，樓層條件判不符（事後審查 CE-4）。
        above = [f for f in floors if f > 0]
        counted = above or floors
        lo, hi = min(counted), max(counted)
    else:
        lo = hi = None
    return TransferFloors(lo=lo, hi=hi, whole=whole, extra=tuple(extra), unknown=tuple(unknown))


# ---------------------------------------------------------------- 備註決策表

# 保留類片語：(樣式, 附註)。依序拿掉；長的、特定的寫在前面。
REMARK_KEEP: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), flag)
    for pattern, flag in (
        (r"(?:前|後)?陽[台臺]外推(?:增建)?", "balcony_enclosed"),
        (r"(?:前|後)?陽[台臺]增建", "balcony_enclosed"),
        (r"(?:前|後)?陽[台臺]加窗(?:增建)?", "window_added"),
        (r"鐵鋁窗(?:增建)?|鐵窗增建|鋁窗|窗戶增建|加窗(?:增建)?", "window_added"),
        (r"含?裝潢費?|裝修費?", "incl_renovation"),
        (r"(?:含|內附)?(?:傢俱|家俱|家具|傢具|家電)(?:設備)?費?|設備費", "incl_furniture"),
        (r"露[台臺](?!增建)", "terrace"),
    )
)

# 排除類樣式：(代碼, 樣式)，依序比對，取第一個（R1 §7.2 的順序）。
REMARK_EXCLUSIONS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(pattern))
    for code, pattern in (
        ("special_relation", r"親友|員工|共有人|特殊關係|親屬|[一二三]親等"),
        ("presale_split_reg", r"預售屋|分件登記|分次登記"),
        ("other_addition", r"其他增建"),
        ("rooftop_addition", r"頂樓加蓋|頂加"),
        ("mezzanine", r"夾層"),
        ("unregistered_building", r"未登記建物|未保存登記"),
        ("addition", r"增建|違建|加蓋"),
        ("public_facility_land", r"公共設施保留地"),
        ("government", r"協議價購|標讓售|政府機關|徵收|標售|讓售"),
        ("defect", r"瑕疵|漏水|凶宅|兇宅|海砂|輻射|民情風俗|非自然|壁癌|傾斜"),
        ("urgent_sale", r"急買|急賣"),
        ("debt", r"債權|債務|抵償|抵債"),
        ("lease", r"租約|帶租|含租"),
        ("partial_share", r"持分"),
        ("auction", r"拍賣|法拍"),
        ("parking_only", r"僅車位"),
        ("odd_lot", r"畸零地|合併使用"),
        ("non_standard", r"地上權|使用權|毛胚|毛坯|攤位|店面|農作物|殯葬|納骨"),
    )
)


def classify_remark(remark: str | None) -> tuple[str | None, tuple[str, ...]]:
    """備註 → (排除代碼或 None, 保留類附註)。先拿掉保留類片語，再比對排除類。"""
    s = nfkc(remark)
    flags: set[str] = set()
    for pattern, flag in REMARK_KEEP:
        if pattern.search(s):
            flags.add(flag)
            s = pattern.sub("；", s)
    for code, pattern in REMARK_EXCLUSIONS:
        if pattern.search(s):
            return code, tuple(sorted(flags))
    return None, tuple(sorted(flags))


# ---------------------------------------------------------------- 車位


def split_evenly(total: int, parts: int) -> list[int]:
    """把整數平均分成 parts 份，餘數給前面的。"""
    base, rest = divmod(total, parts)
    return [base + (1 if i < rest else 0) for i in range(parts)]


def lvr_parking_type(text: str | None) -> Parking:
    """park 明細與主檔的車位類別：坡道平面、一樓平面、升降平面、坡道機械、升降機械、塔式車位、其他。"""
    ptype = parse_parking(text)
    return ptype if ptype != Parking.NONE else Parking.UNKNOWN


def build_parkings(
    main: dict[str, str], park_rows: Sequence[dict[str, str]], declared: int | None
) -> tuple[list[ParkingSpace], set[str]]:
    """逐位的車位（見模組說明）。declared 是交易筆棟數的車位數。"""
    flags: set[str] = set()
    total_price = parse_yuan(main.get("車位總價元")) or 0
    total_area = parse_cm2(main.get("車位移轉總面積平方公尺")) or 0
    if park_rows:
        if declared is not None and declared != len(park_rows):
            flags.add("parking_count_mismatch")
        types = [lvr_parking_type(row.get("車位類別")) for row in park_rows]
        prices: list[int | None] = [parse_yuan(row.get("車位價格")) or None for row in park_rows]
        areas: list[int | None] = [
            parse_cm2(row.get("車位面積平方公尺")) or None for row in park_rows
        ]
    elif declared:
        flags.add("parking_detail_missing")
        types = [lvr_parking_type(main.get("車位類別"))] * declared
        prices = [None] * declared
        areas = [None] * declared
    else:
        if (main.get("車位類別") or "").strip() or total_price or total_area:
            # TODO(deferred.md 2026-11-21 P-6)：車位數 0 但有車位總價時，政府單價有沒有扣車位待查證
            flags.add("parking_without_count")
        return [], flags

    n = len(types)
    known_price = sum(p for p in prices if p)
    if known_price == 0 and total_price > 0:
        prices = [p or None for p in split_evenly(total_price, n)]
        if n > 1:
            flags.add("parking_price_split")
    elif known_price != total_price and park_rows:
        flags.add("parking_price_mismatch")
    known_area = sum(a for a in areas if a)
    if known_area == 0 and total_area > 0:
        areas = [a or None for a in split_evenly(total_area, n)]
        if n > 1:
            flags.add("parking_area_split")
    elif abs(known_area - total_area) > n and park_rows:  # 每位容許 0.01 m² 的捨入
        flags.add("parking_area_mismatch")
    spaces = [
        ParkingSpace(ptype=ptype, price_yuan=price, area_cm2=area)
        for ptype, price, area in zip(types, prices, areas, strict=True)
    ]
    return spaces, flags


# ---------------------------------------------------------------- 一列 → TxFacts


class RowSkipped(Exception):
    """這一列不能變成 TxFacts；reason 是略過原因的代碼。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _built(
    main: dict[str, str], build_rows: Sequence[dict[str, str]]
) -> tuple[DateRange, set[str]]:
    """完工日：以主檔為準；主檔讀不出來時用 build 明細的專有部分。不一致時加註。"""
    flags: set[str] = set()
    built = roc_built(main.get("建築完成年月"))
    primary = [row for row in build_rows if (row.get("建物分層") or "").strip()] or list(build_rows)
    detail = [built_from_date(row.get("建築完成日期")) for row in primary]
    detail_days = {r.lo for r in detail if r.lo is not None and r.lo == r.hi}
    if built.lo is None:
        if len(detail_days) == 1:
            day = next(iter(detail_days))
            built = DateRange(lo=day, hi=day)
            flags.add("built_from_detail")
        else:
            flags.add("built_unknown")
    elif detail_days and built.lo == built.hi and built.lo not in detail_days:
        flags.add("built_mismatch")
    return built, flags


def parse_row(
    main: dict[str, str],
    build_rows: Sequence[dict[str, str]],
    park_rows: Sequence[dict[str, str]],
    *,
    release_id: str,
) -> TxFacts:
    """主檔一列加上它的明細 → TxFacts。不能轉換時丟 RowSkipped。"""
    serial = (main.get("編號") or "").strip()
    if not SERIAL_RE.match(serial):
        raise RowSkipped("bad_serial")
    trade_date = roc_date(main.get("交易年月日"))
    if trade_date is None:
        raise RowSkipped("bad_trade_date")
    district = nfkc(main.get("鄉鎮市區"))
    if not district:
        raise RowSkipped("bad_district")
    total = parse_yuan(main.get("總價元"))
    if total is None:
        raise RowSkipped("bad_total")

    flags: set[str] = set()
    exclusions: list[str] = []

    counts = parse_counts(main.get("交易筆棟數"))
    if counts is None:
        flags.add("counts_unparsed")
    if "建物" not in (main.get("交易標的") or "") or (counts is not None and counts[1] == 0):
        exclusions.append("no_building")
    elif counts is not None and counts[1] >= 2:
        exclusions.append("multi_building")

    built, built_flags = _built(main, build_rows)
    flags |= built_flags
    if built.lo is not None and trade_date < built.lo:
        exclusions.append("presale")

    remark_exclusion, remark_flags = classify_remark(main.get("備註"))
    flags.update(remark_flags)
    if remark_exclusion is not None:
        exclusions.append(remark_exclusion)

    floors_total = parse_total_floors(main.get("總樓層數"))
    floors = parse_transfer_floors(main.get("移轉層次"), floors_total)
    exclusions.extend(floors.extra)
    if floors.unknown:
        flags.add("floor_unparsed")
    if floors.whole:
        flags.add("floor_whole")

    elevator, elevator_flags = decide_elevator(
        field=main.get("電梯"), type_text=main.get("建物型態")
    )
    flags.update(elevator_flags)

    parkings, parking_flags = build_parkings(main, park_rows, counts[2] if counts else None)
    flags |= parking_flags

    main_cm2 = parse_cm2(main.get("主建物面積")) or None
    aux_cm2 = parse_cm2(main.get("附屬建物面積"))
    balcony_cm2 = parse_cm2(main.get("陽台面積"))
    if main_cm2 is None:
        aux_cm2 = balcony_cm2 = None  # 主建物沒有拆出來時，附屬與陽台的 0 也不可信

    address = normalize_address(main.get("土地位置建物門牌"))[:ADDRESS_MAX] or None
    try:
        return TxFacts(
            serial=serial,
            release_id=release_id,
            trade_date=trade_date,
            district=district,
            address_text=address,
            btype=parse_btype(main.get("建物型態")),
            floor_lo=floors.lo,
            floor_hi=floors.hi,
            floors_total=floors_total,
            main_cm2=main_cm2,
            bldg_cm2=parse_cm2(main.get("建物移轉總面積平方公尺")) or None,
            aux_cm2=aux_cm2,
            balcony_cm2=balcony_cm2,
            built=built,
            elevator=elevator,
            parkings=parkings,
            parking_n=len(parkings),
            total_yuan=total,
            flags=sorted(flags),
            exclusion=exclusions[0] if exclusions else None,
        )
    except ValidationError:
        raise RowSkipped("invalid") from None


# ---------------------------------------------------------------- 整個發布


@dataclass
class ParseResult:
    """一個發布的解析結果。facts 依編號排序、編號不重複；skipped 是略過原因與列數。"""

    facts: list[TxFacts]
    rows: int
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


def _has_building(facts: TxFacts) -> bool:
    return facts.exclusion != "no_building"


def _group(rows: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("編號") or "").strip()].append(row)
    return grouped


def parse_release(main: bytes, build: bytes, park: bytes, *, release_id: str) -> ParseResult:
    """一個發布的三個檔 → TxFacts。

    同一個發布裡同編號出現兩次時，取有建物的那筆；都有（或都沒有）就取後面那筆（R1 §3.1）。
    """
    main_table = read_table(main, MAIN_COLUMNS, what="主檔")
    build_table = read_table(build, BUILD_COLUMNS, what="build 明細")
    park_table = read_table(park, PARK_COLUMNS, what="park 明細")
    builds = _group(build_table.rows)
    parks = _group(park_table.rows)
    result = ParseResult(facts=[], rows=len(main_table.rows) + main_table.bad_rows)
    if main_table.bad_rows:
        result.skipped["bad_width"] += main_table.bad_rows
    chosen: dict[str, TxFacts] = {}
    for row in main_table.rows:
        serial = (row.get("編號") or "").strip()
        try:
            facts = parse_row(
                row, builds.get(serial, ()), parks.get(serial, ()), release_id=release_id
            )
        except RowSkipped as skipped:
            result.skipped[skipped.reason] += 1
            continue
        previous = chosen.get(facts.serial)
        if previous is not None:
            result.skipped["duplicate_serial"] += 1
            if _has_building(previous) and not _has_building(facts):
                continue
        chosen[facts.serial] = facts
    result.facts = [chosen[serial] for serial in sorted(chosen)]
    return result
