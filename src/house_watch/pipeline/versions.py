"""刊登狀態表與 append-only 的版本表（r4 §3.8；R1-09、R1-22、R3-22）。

**狀態表 listings**：一筆刊登（來源, 編號）一列：首次出現、最後看到、狀態、基準、群組。
- 任何一種掃描看到都更新 last_seen（R1-10：下架計數以它為準）。
- 下架後同站同編號再出現：狀態改回 active、記 relisted_at、清掉 gone_at（R1-09）。
- area 是第一次看到它的區域，之後不變（同一筆被另一個區域的查詢看到也不改）。

**版本表 listing_versions**：事實一變就加一列，舊列寫上 valid_to；永遠不改舊列的內容。
- 雜湊只算 ListingFacts（facts_hash）；內容一樣就不加新列。
- 詳細頁（與東森的批次）補的欄位不在列表上：記版本前先把這些欄位從上一版帶過來
  （`carry_forward`），否則每次完整掃描都會寫一個少了詳細資料的新版本、詳細資料也會不見。
  規則依欄位分：列表有值就用列表的（例如較新的含車位旗標、原價）；列表只有推定值
  （電梯推定有、只知道平面）時用上一版的確定值；成對的欄位一起帶。
- 完工日（所有來源）：列表的屋齡從抓取日往回推，區間每天漂移一天。和上一版有交集就沿用
  上一版，沒有交集才換（`stable_built`），所以不會每天多一個版本，也不會越縮越窄。
- valid_from 對同一筆刊登嚴格遞增（同一秒內第二版往後推一秒），所以主鍵不會撞。

**價格**（R1-22、R3-22）：
- price_yuan 欄只放通過檢查的開價：整數元、在 PRICE_RANGE 內、和上一個記錄的價格差不到
  PRICE_JUMP_RATIO 倍（差太多多半是單位或解析錯誤）。沒通過的版本照樣保存事實，只是價格欄是空的。
- 價格歷史＝版本表的檢視（`price_points`）。差距小於 1 萬的變動不算一次變動；
  降價只在同一筆刊登內判斷；站方標的原價只顯示，不產生降價。

**作廢**：某一版解析器出錯時，`void_parser_version` 刪掉那一版寫的版本列，
把剩下最新的一列重新打開；下一次完整掃描會補回正確的版本（r4 §2.3）。
"""

import datetime as dt
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from house_watch.clock import from_iso, to_iso
from house_watch.models import DateRange, Elevator, ListingFacts, Parking

PRICE_RANGE = (1_000_000, 1_000_000_000)  # 元：住宅開價 100 萬到 10 億以外的值當成錯誤
PRICE_MIN_STEP_YUAN = 10_000  # 變動小於 1 萬不算（R3-22）
PRICE_JUMP_RATIO = 3  # 和上一個記錄的價格差 3 倍以上，當成單位或解析錯誤

# 成對的欄位：任一個在詳細欄位裡，就整組一起從上一版帶過來。
PAIRED_FIELDS: tuple[tuple[str, ...], ...] = (
    ("site_lat", "site_lon"),
    ("main_cp", "main_kind"),
    ("floor_lo", "floor_hi", "floors_total"),
)
_WEAK_ELEVATOR = frozenset({Elevator.UNKNOWN, Elevator.INFERRED})
_SURE_ELEVATOR = frozenset({Elevator.YES, Elevator.NO})
_WEAK_PARKING = frozenset({Parking.UNKNOWN, Parking.FLAT_UNKNOWN_ACCESS})


# ---------------------------------------------------------------- 讀取


@dataclass(frozen=True)
class Version:
    """版本表的一列。"""

    valid_from: dt.datetime
    facts: ListingFacts
    facts_hash: str
    price_yuan: int | None
    parser_version: str
    scan_id: int | None


@dataclass(frozen=True)
class ListingRow:
    """狀態表的一列。時間都是 UTC。"""

    source: str
    source_id: str
    area: str
    first_seen: dt.datetime
    last_seen: dt.datetime
    last_full_seen_scan: int | None
    status: str
    gone_at: dt.datetime | None
    relisted_at: dt.datetime | None
    baseline: bool
    group_id: int | None
    detail_fetched_at: dt.datetime | None


_LISTING_COLUMNS = (
    "source, source_id, area, first_seen, last_seen, last_full_seen_scan, status, gone_at,"
    " relisted_at, baseline, group_id, detail_fetched_at"
)


def _time(value: str | None) -> dt.datetime | None:
    return from_iso(value) if value else None


def _listing_row(row: sqlite3.Row | tuple) -> ListingRow:
    return ListingRow(
        source=row[0],
        source_id=row[1],
        area=row[2],
        first_seen=from_iso(row[3]),
        last_seen=from_iso(row[4]),
        last_full_seen_scan=row[5],
        status=row[6],
        gone_at=_time(row[7]),
        relisted_at=_time(row[8]),
        baseline=bool(row[9]),
        group_id=row[10],
        detail_fetched_at=_time(row[11]),
    )


def listing(conn: sqlite3.Connection, source: str, source_id: str) -> ListingRow | None:
    row = conn.execute(
        f"SELECT {_LISTING_COLUMNS} FROM listings WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    return None if row is None else _listing_row(row)


def listings(conn: sqlite3.Connection, where: str = "", params: Iterable = ()) -> list[ListingRow]:
    """狀態表的多列，依（來源, 編號）排序。where 是程式內寫死的條件，不放外來字串。"""
    sql = f"SELECT {_LISTING_COLUMNS} FROM listings"
    if where:
        sql += f" WHERE {where}"
    rows = conn.execute(sql + " ORDER BY source, source_id", tuple(params)).fetchall()
    return [_listing_row(row) for row in rows]


def _version(row: sqlite3.Row | tuple) -> Version:
    return Version(
        valid_from=from_iso(row[0]),
        facts=ListingFacts.model_validate_json(row[1]),
        facts_hash=row[2],
        price_yuan=row[3],
        parser_version=row[4],
        scan_id=row[5],
    )


def latest(conn: sqlite3.Connection, source: str, source_id: str) -> Version | None:
    """目前的版本（valid_to 是空的那一列）。"""
    row = conn.execute(
        "SELECT valid_from, facts_json, facts_hash, price_yuan, parser_version, scan_id"
        " FROM listing_versions WHERE source = ? AND source_id = ? AND valid_to IS NULL",
        (source, source_id),
    ).fetchone()
    return None if row is None else _version(row)


def latest_all(conn: sqlite3.Connection) -> dict[tuple[str, str], Version]:
    """所有刊登目前的版本（匯出與合併一次讀完）。"""
    rows = conn.execute(
        "SELECT valid_from, facts_json, facts_hash, price_yuan, parser_version, scan_id,"
        " source, source_id FROM listing_versions WHERE valid_to IS NULL"
    ).fetchall()
    return {(row[6], row[7]): _version(row) for row in rows}


# ---------------------------------------------------------------- 詳細欄位帶到下一版


def stable_built(listed: DateRange, prior: DateRange) -> DateRange:
    """完工日區間不隨抓取日漂移（r4 §3.10：區間是絕對日期）。

    列表的屋齡是「抓取日往回推」，所以同一筆刊登每天算出來的區間都差一天；
    直接比雜湊會每天寫一個新版本，取交集又會一天天縮到錯的地方。所以：
    上一版的區間和這次的有交集（或這次不知道）就沿用上一版；沒有交集（網站改了屋齡）
    或上一版不知道，才用這次的。詳細頁的縮小（parse_detail 裡的 narrow_built）不受影響。
    """
    if prior.lo is None and prior.hi is None:
        return listed
    lo = max((d for d in (listed.lo, prior.lo) if d is not None), default=None)
    hi = min((d for d in (listed.hi, prior.hi) if d is not None), default=None)
    if lo is None or hi is None or lo <= hi:
        return prior
    return listed


def carry_forward(
    listed: ListingFacts, prior: ListingFacts | None, detail_fields: Iterable[str]
) -> ListingFacts:
    """列表的事實加上上一版的詳細欄位（規則見模組說明）。

    完工日一律照 stable_built（所有來源）；其他不在 detail_fields 的欄位一律用列表的。
    """
    if prior is None:
        return listed
    fields = frozenset(detail_fields) - {"built"}
    data = listed.model_dump()
    old = prior.model_dump()
    data["built"] = stable_built(listed.built, prior.built).model_dump()
    handled: set[str] = set()
    for group in PAIRED_FIELDS:
        if not fields & set(group):
            continue
        handled.update(group)
        lead = group[0]
        if data[lead] is None and old[lead] is not None:
            for name in group:
                data[name] = old[name]
        elif (
            len(group) == 3
            and data["floors_total"] is None
            and old["floors_total"] is not None
            and (data["floor_lo"], data["floor_hi"]) == (old["floor_lo"], old["floor_hi"])
        ):
            data["floors_total"] = old["floors_total"]
    for name in sorted(fields - handled):
        if name not in data:
            continue
        new, previous = data[name], old[name]
        if name == "elevator":
            if new in _WEAK_ELEVATOR and previous in _SURE_ELEVATOR:
                data[name] = previous
        elif name == "parking":
            if new in _WEAK_PARKING and previous not in (None, Parking.UNKNOWN):
                data[name] = previous
        elif new is None and previous is not None:
            data[name] = previous
    return ListingFacts.model_validate(data)


# ---------------------------------------------------------------- 寫入（呼叫端負責交易）


def sane_price(yuan: int | None, previous: int | None) -> int | None:
    """價格欄要不要記這個值：範圍內，而且和上一個記錄的價格差不到 PRICE_JUMP_RATIO 倍。"""
    if yuan is None or not PRICE_RANGE[0] <= yuan <= PRICE_RANGE[1]:
        return None
    if previous and max(yuan, previous) >= PRICE_JUMP_RATIO * min(yuan, previous):
        return None
    return yuan


def _last_price(conn: sqlite3.Connection, source: str, source_id: str) -> int | None:
    row = conn.execute(
        "SELECT price_yuan FROM listing_versions WHERE source = ? AND source_id = ?"
        " AND price_yuan IS NOT NULL ORDER BY valid_from DESC LIMIT 1",
        (source, source_id),
    ).fetchone()
    return None if row is None else int(row[0])


def append(
    conn: sqlite3.Connection,
    facts: ListingFacts,
    *,
    now: dt.datetime,
    parser_version: str,
    scan_id: int | None,
) -> bool:
    """事實和目前的版本不同就加一列（舊列寫上 valid_to）。回傳有沒有加。"""
    prior = latest(conn, facts.source, facts.source_id)
    digest = facts.facts_hash()
    if prior is not None and prior.facts_hash == digest:
        return False
    start = now
    if prior is not None and start <= prior.valid_from:
        start = prior.valid_from + dt.timedelta(seconds=1)
    price = sane_price(facts.price_yuan, _last_price(conn, facts.source, facts.source_id))
    if prior is not None:
        conn.execute(
            "UPDATE listing_versions SET valid_to = ? WHERE source = ? AND source_id = ?"
            " AND valid_to IS NULL",
            (to_iso(start), facts.source, facts.source_id),
        )
    conn.execute(
        "INSERT INTO listing_versions (source, source_id, valid_from, valid_to, facts_hash,"
        " facts_json, price_yuan, parser_version, scan_id) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            facts.source,
            facts.source_id,
            to_iso(start),
            digest,
            facts.model_dump_json(),
            price,
            parser_version,
            scan_id,
        ),
    )
    return True


@dataclass(frozen=True)
class Sighting:
    """一次「看到」的結果。"""

    new: bool  # 第一次看到
    relisted: bool  # 下架後同站同編號再出現
    changed: bool  # 加了新版本
    facts: ListingFacts  # 寫進版本表的事實（帶過詳細欄位）


def record_sighting(
    conn: sqlite3.Connection,
    facts: ListingFacts,
    *,
    now: dt.datetime,
    scan_id: int | None,
    full: bool,
    baseline: bool,
    parser_version: str,
    detail_fields: Iterable[str] = (),
) -> Sighting:
    """在掃描裡看到一筆刊登：更新狀態表、需要時加一個版本。呼叫端要在寫入交易裡。

    baseline：這個（來源, 區域, 範圍版本）還沒有任何有效的完整掃描時是 True，
    第一次看到的刊登標成基準，不給新上架徽章（R1-09）。
    """
    row = listing(conn, facts.source, facts.source_id)
    stamp = to_iso(now)
    if row is None:
        conn.execute(
            "INSERT INTO listings (source, source_id, area, first_seen, last_seen,"
            " last_full_seen_scan, status, baseline) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                facts.source,
                facts.source_id,
                facts.area,
                stamp,
                stamp,
                scan_id if full else None,
                int(baseline),
            ),
        )
        relisted = False
    else:
        relisted = row.status == "gone"
        assignments = ["last_seen = ?", "status = 'active'"]
        params: list = [stamp]
        if full and scan_id is not None:
            assignments.append("last_full_seen_scan = ?")
            params.append(scan_id)
        if relisted:
            assignments += ["relisted_at = ?", "gone_at = NULL"]
            params.append(stamp)
        conn.execute(
            f"UPDATE listings SET {', '.join(assignments)} WHERE source = ? AND source_id = ?",
            (*params, facts.source, facts.source_id),
        )
        if facts.area != row.area:
            facts = facts.model_copy(update={"area": row.area})
    prior = latest(conn, facts.source, facts.source_id)
    merged = carry_forward(facts, prior.facts if prior else None, detail_fields)
    changed = append(conn, merged, now=now, parser_version=parser_version, scan_id=scan_id)
    return Sighting(new=row is None, relisted=relisted, changed=changed, facts=merged)


def mark_detail_fetched(conn: sqlite3.Connection, source: str, source_id: str, now: dt.datetime):
    conn.execute(
        "UPDATE listings SET detail_fetched_at = ? WHERE source = ? AND source_id = ?",
        (to_iso(now), source, source_id),
    )


def void_parser_version(conn: sqlite3.Connection, source: str, parser_version: str) -> int:
    """刪掉某來源某一版解析器寫的所有版本列，再把每筆刊登剩下最新的一列重新打開。

    呼叫端要在寫入交易裡。回傳刪掉的列數。
    """
    keys = conn.execute(
        "SELECT DISTINCT source_id FROM listing_versions WHERE source = ? AND parser_version = ?",
        (source, parser_version),
    ).fetchall()
    removed = conn.execute(
        "DELETE FROM listing_versions WHERE source = ? AND parser_version = ?",
        (source, parser_version),
    ).rowcount
    for (source_id,) in keys:
        conn.execute(
            "UPDATE listing_versions SET valid_to = NULL WHERE source = ? AND source_id = ?"
            " AND valid_from = (SELECT max(valid_from) FROM listing_versions"
            " WHERE source = ? AND source_id = ?)",
            (source, source_id, source, source_id),
        )
    return removed


# ---------------------------------------------------------------- 價格歷史


@dataclass(frozen=True)
class PriceSummary:
    """一筆刊登的價格：目前、第一次記錄、最後一次降價的時間（元、UTC）。"""

    price: int | None
    price_first: int | None
    price_drop_at: dt.datetime | None


def price_points(
    conn: sqlite3.Connection, source: str, source_id: str
) -> list[tuple[dt.datetime, int]]:
    """價格歷史：通過檢查的價格與它開始的時間，依時間排序。"""
    rows = conn.execute(
        "SELECT valid_from, price_yuan FROM listing_versions WHERE source = ? AND source_id = ?"
        " AND price_yuan IS NOT NULL ORDER BY valid_from",
        (source, source_id),
    ).fetchall()
    return [(from_iso(row[0]), int(row[1])) for row in rows]


def price_summary(points: Iterable[tuple[dt.datetime, int]]) -> PriceSummary:
    """目前價格是最後一個值；變動小於 1 萬的不算一次變動（R3-22）；降價時間是最後一次往下變的時間。"""
    series: list[tuple[dt.datetime, int]] = []
    current: int | None = None
    for when, price in points:
        current = price
        if series and abs(price - series[-1][1]) < PRICE_MIN_STEP_YUAN:
            continue
        series.append((when, price))
    if not series:
        return PriceSummary(None, None, None)
    drop_at = None
    for (_, before), (when, after) in zip(series, series[1:], strict=False):
        if after < before:
            drop_at = when
    return PriceSummary(current, series[0][1], drop_at)
