"""實價登錄匯入（WP3；R1 §3.1、R1-26、R1-27、r4 §3.8）。

- 存全市（不篩區）：區域是匯出時才篩，新增區域不用重新下載（R3-14）。
- 一個發布一個資料庫交易：先寫 lvr_releases，再寫它的成交；中途失敗就整批回復。
- 同一個發布（編號與內容雜湊都相同）不重複匯入；內容變了就重新匯入。
- 同編號以「新」的發布為準（新舊見 fetch.release_order：登記期間最後一天，同一天季檔較新）。
  沒有建物的列不存，所以「有建物的那份優先」自然成立（R1 §3.1）。
- 定位用注入的 `locate`（WP2 實作）；結果和門牌版本一起存在紀錄上，換版時用 `relocate` 重算。
- 車位中位數綁資料版本（`lvr_version`）：只有匯入了新的發布才重算，結果存在 parking_medians，
  版本記在 kv。匯出前呼叫 `refresh_medians` 取得和資料一致的中位數。
"""

import datetime as dt
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from house_watch.clock import taipei_date, to_iso
from house_watch.db import transaction
from house_watch.lvr.fetch import (
    MEMBER_MAX_BYTES,
    Getter,
    ReleaseFiles,
    download,
    list_available,
    plan,
    release_order,
    release_window,
)
from house_watch.lvr.parse import LvrError, parse_release
from house_watch.lvr.unitprice import (
    WINDOW_MONTHS,
    MedianTable,
    compute_medians,
    load_medians,
    store_medians,
    unit_price,
)
from house_watch.models import Location, Precision, TxFacts, canonical_json

Locate = Callable[[TxFacts], Location]

MEDIANS_VERSION_KEY = "lvr_medians_version"
# 一個發布裡讀不懂的列超過這個比例（且超過 MIN_BAD_ROWS 列），整批拒絕。
MAX_BAD_RATIO = 0.05
MIN_BAD_ROWS = 10


@dataclass
class ImportStats:
    """一個發布的匯入結果。"""

    release_id: str
    rows: int = 0
    inserted: int = 0
    replaced: int = 0
    kept: int = 0  # 資料庫裡已經有更新的版本
    not_stored: int = 0  # 沒有建物，不存
    skipped: dict[str, int] = field(default_factory=dict)
    already_imported: bool = False


def imported_releases(conn: sqlite3.Connection) -> dict[str, str]:
    """已匯入的發布：編號 → 內容雜湊。"""
    return {row[0]: row[1] for row in conn.execute("SELECT release_id, sha256 FROM lvr_releases")}


def _order(release_id: str) -> tuple[dt.date, int]:
    try:
        return release_order(release_id)
    except LvrError:
        return dt.date.min, -1  # 看不懂的編號當成最舊


def import_release(
    conn: sqlite3.Connection,
    files: ReleaseFiles,
    *,
    locate: Locate,
    doorindex_version: str | None,
    now: dt.datetime,
) -> ImportStats:
    """匯入一個發布（規則見模組說明）。讀不懂的列太多就丟 LvrError，資料庫不變。"""
    stats = ImportStats(release_id=files.release_id)
    known = conn.execute(
        "SELECT sha256 FROM lvr_releases WHERE release_id = ?", (files.release_id,)
    ).fetchone()
    if known is not None and known[0] == files.sha256:
        stats.already_imported = True
        return stats
    result = parse_release(files.main, files.build, files.park, release_id=files.release_id)
    stats.rows = result.rows
    stats.skipped = dict(sorted(result.skipped.items()))
    bad = result.skipped_total - result.skipped.get("duplicate_serial", 0)
    if result.rows == 0:
        raise LvrError("主檔沒有任何資料列")
    if bad > max(MIN_BAD_ROWS, MAX_BAD_RATIO * result.rows):
        raise LvrError("讀不懂的資料列太多，整批不匯入")

    new_order = _order(files.release_id)
    with transaction(conn):
        conn.execute(
            "INSERT INTO lvr_releases (release_id, kind, imported_at, sha256) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (release_id) DO UPDATE SET kind = excluded.kind,"
            " imported_at = excluded.imported_at, sha256 = excluded.sha256",
            (files.release_id, files.kind, to_iso(now), files.sha256),
        )
        current = dict(conn.execute("SELECT serial, release_id FROM transactions"))
        for facts in result.facts:
            if facts.exclusion == "no_building":
                stats.not_stored += 1
                continue
            old = current.get(facts.serial)
            if old is not None and _order(old) > new_order:
                stats.kept += 1
                continue
            _upsert(conn, facts, locate(facts), doorindex_version)
            if old is None:
                stats.inserted += 1
            else:
                stats.replaced += 1
    return stats


def _upsert(
    conn: sqlite3.Connection, facts: TxFacts, loc: Location, doorindex_version: str | None
) -> None:
    conn.execute(
        "INSERT INTO transactions (serial, release_id, trade_date, district, facts_json, lat, lon,"
        " precision, doorindex_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (serial) DO UPDATE SET release_id = excluded.release_id,"
        " trade_date = excluded.trade_date, district = excluded.district,"
        " facts_json = excluded.facts_json, lat = excluded.lat, lon = excluded.lon,"
        " precision = excluded.precision, doorindex_version = excluded.doorindex_version",
        (
            facts.serial,
            facts.release_id,
            facts.trade_date.isoformat(),
            facts.district,
            canonical_json(facts.model_dump(mode="json")),
            loc.lat,
            loc.lon,
            loc.precision.value,
            doorindex_version,
        ),
    )


def iter_facts(conn: sqlite3.Connection) -> Iterator[TxFacts]:
    """資料庫裡所有成交的 TxFacts，依編號排序。"""
    for (facts_json,) in conn.execute("SELECT facts_json FROM transactions ORDER BY serial"):
        yield TxFacts.model_validate_json(facts_json)


def relocate(conn: sqlite3.Connection, locate: Locate, doorindex_version: str | None) -> int:
    """門牌版本不同的紀錄重新定位（R1-27），回傳更新的筆數。"""
    rows = conn.execute(
        "SELECT facts_json FROM transactions WHERE doorindex_version IS NOT ? ORDER BY serial",
        (doorindex_version,),
    ).fetchall()
    with transaction(conn):
        for (facts_json,) in rows:
            facts = TxFacts.model_validate_json(facts_json)
            loc = locate(facts)
            conn.execute(
                "UPDATE transactions SET lat = ?, lon = ?, precision = ?, doorindex_version = ?"
                " WHERE serial = ?",
                (loc.lat, loc.lon, loc.precision.value, doorindex_version, facts.serial),
            )
    return len(rows)


# ---------------------------------------------------------------- 資料版本與中位數


def lvr_version(conn: sqlite3.Connection) -> str | None:
    """已匯入的發布集合（編號＋內容雜湊）決定的版本字串；沒有任何發布時是 None。"""
    releases = sorted(imported_releases(conn).items())
    if not releases:
        return None
    digest = hashlib.sha256()
    for release_id, sha in releases:
        digest.update(f"{release_id}:{sha}\n".encode())
    return digest.hexdigest()[:16]


def lvr_as_of(conn: sqlite3.Connection) -> dt.date | None:
    """資料截至：已匯入的發布中，登記期間最後的一天。"""
    ends = []
    for release_id in imported_releases(conn):
        try:
            ends.append(release_window(release_id)[1])
        except LvrError:
            continue
    return max(ends, default=None)


def refresh_medians(
    conn: sqlite3.Connection, *, months: int = WINDOW_MONTHS, force: bool = False
) -> MedianTable | None:
    """取得和目前資料版本一致的車位中位數；版本變了才重算並存檔。沒有資料時回 None。"""
    version = lvr_version(conn)
    as_of = lvr_as_of(conn)
    if version is None or as_of is None:
        return None
    stored = conn.execute("SELECT value FROM kv WHERE key = ?", (MEDIANS_VERSION_KEY,)).fetchone()
    if not force and stored is not None and stored[0] == version:
        table = load_medians(conn, version)
        if table is not None:
            return table
    table = compute_medians(iter_facts(conn), version=version, as_of=as_of, months=months)
    with transaction(conn):
        store_medians(conn, table)
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (MEDIANS_VERSION_KEY, version),
        )
    return table


# ---------------------------------------------------------------- 每日更新


@dataclass
class UpdateReport:
    """一次更新的結果。"""

    available: list[str]
    planned: list[str]
    imports: list[ImportStats]
    version: str | None
    as_of: dt.date | None


def update(
    conn: sqlite3.Connection,
    get: Getter,
    *,
    now: dt.datetime,
    locate: Locate,
    doorindex_version: str | None,
    months: int,
    max_bytes: int = MEMBER_MAX_BYTES,
) -> UpdateReport:
    """每日檢查：讀下載頁 → 決定要下載哪些發布（舊的在前）→ 下載並匯入 → 更新中位數。

    任何一個發布失敗就停在那裡：已匯入的保留，中位數照樣更新，再把例外丟給呼叫端
    （呼叫端記錄 lvr_invalid 等錯誤代碼）。
    """
    available = list_available(get, max_bytes=max_bytes)
    todo = plan(available, imported_releases(conn), today=taipei_date(now), months=months)
    imports: list[ImportStats] = []
    error: Exception | None = None
    for release in todo:
        try:
            files = download(get, release, max_bytes=max_bytes)
            imports.append(
                import_release(
                    conn, files, locate=locate, doorindex_version=doorindex_version, now=now
                )
            )
        except Exception as exc:  # 交給呼叫端；已匯入的發布保留
            error = exc
            break
    refresh_medians(conn, months=months)
    if error is not None:
        raise error
    return UpdateReport(
        available=[r.release_id for r in available],
        planned=[r.release_id for r in todo],
        imports=imports,
        version=lvr_version(conn),
        as_of=lvr_as_of(conn),
    )


# ---------------------------------------------------------------- update 的 lvr 步驟與匯出 provider（WP6 整合）
#
# cli 的入口 `house_watch.lvr.importer:run`（update 的 lvr 步驟）與 export 的 provider
# `house_watch.lvr.importer:export_tx`（契約 C2 的 tx_<區域>.json）。
# - 下載經過 C6 的 Fetcher：限速、timeout、User-Agent、轉址白名單；大小上限用
#   `[http] download_max_bytes`（頁面的 5 MiB 不夠放 zip）。政府開放資料沒有 robots 檢查。
# - 定位用 pipeline.locating（有門牌索引時是 WP2 的決策表，沒有就只靠「鄉鎮市區」欄）。
#   門牌換版時，舊版定位的紀錄整批重算（relocate）。
# - export_tx 只讀：中位數用資料庫裡同一版的結果，沒有就在記憶體裡算，不寫回。

LVR_SOURCE = "lvr"
_LEVEL_ORDER = ("district", "district_flat", "city")


@dataclass
class RunResult:
    """cli 的 StepOutcome 相容物件，加上 as_of（cli 寫進 kv 的 lvr.as_of）。"""

    ok: bool
    summary: str
    as_of: dt.date | None


def _make_fetcher(ctx):
    """實價登錄用的 Fetcher（測試可以換掉 FETCHER_FACTORY，注入 MockTransport 與假時鐘）。"""
    from house_watch.http import Fetcher, SourcePolicy
    from house_watch.lvr.fetch import ALLOWED_HOSTS

    public = ctx.settings.public
    policy = SourcePolicy(
        allowed_hosts=ALLOWED_HOSTS, max_bytes=public.http.download_max_bytes
    )
    return Fetcher(
        ctx.conn, http=public.http, tls=public.tls, policies={LVR_SOURCE: policy}, clock=ctx.clock
    )


FETCHER_FACTORY = _make_fetcher


def run(ctx) -> RunResult:
    """update 的 lvr 步驟：檢查下載頁、下載並匯入新的發布、更新中位數、門牌換版時重新定位。"""
    from house_watch.errors import ErrorCode, HouseWatchError
    from house_watch.http import FetchFailed
    from house_watch.pipeline.locating import Locator
    from house_watch.sources.base import Blocked, RequestSpec

    public = ctx.settings.public
    fetcher = FETCHER_FACTORY(ctx)

    def get(url: str) -> bytes:
        spec = RequestSpec.get(url, kind="download", max_bytes=public.http.download_max_bytes)
        resp = fetcher.fetch(spec, source=LVR_SOURCE, deadline=ctx.deadline)
        if resp.status != 200:
            raise LvrError(f"下載回應 HTTP {resp.status}")
        return resp.body

    error: str | None = None
    report: UpdateReport | None = None
    moved = 0
    try:
        with Locator(ctx.settings, ctx.data.root) as locator:
            version = locator.door_version
            try:
                report = update(
                    ctx.conn,
                    get,
                    now=ctx.clock.now(),
                    locate=locator.locate_tx,
                    doorindex_version=version,
                    months=public.scope.tx.months,
                )
            except (Blocked, FetchFailed) as exc:
                error = exc.code.value
            except (LvrError, HouseWatchError, ValueError, OSError):
                error = ErrorCode.LVR_INVALID.value
            moved = relocate(ctx.conn, locator.locate_tx, version)
    finally:
        fetcher.close()
    as_of = lvr_as_of(ctx.conn)
    parts = []
    if report is not None:
        imported = [s.release_id for s in report.imports if not s.already_imported]
        parts.append(f"匯入 {len(imported)} 個發布" if imported else "沒有新的發布")
    if moved:
        parts.append(f"重新定位 {moved} 筆")
    if error:
        parts.append(f"錯誤 {error}")
    return RunResult(ok=error is None, summary="；".join(parts), as_of=as_of)


def _medians_for_export(conn: sqlite3.Connection) -> MedianTable | None:
    version, as_of = lvr_version(conn), lvr_as_of(conn)
    if version is None or as_of is None:
        return None
    stored = load_medians(conn, version)
    if stored is not None:
        return stored
    return compute_medians(iter_facts(conn), version=version, as_of=as_of)


def _levels(flags: tuple[str, ...], kind: str) -> str | None:
    found = [level for level in _LEVEL_ORDER if f"up_{kind}_{level}" in flags]
    return found[-1] if found else None  # 多個車位用到不同層時，標最粗的那一層


def export_tx(ctx, area) -> list[dict]:
    """export 的 tx_records provider：這個區域、公開範圍內的成交（C2 的 TxRecord 格式）。

    公開範圍（default.toml [scope.tx]）：成交日在資料截至日往回 months 個月內、主建物不小於
    min_main_ping（400/121 精確換算）、型態在 btypes 裡。缺值一律保留（R1-11）；
    被排除的特殊交易（exclusion）不輸出。
    """
    from fractions import Fraction

    from house_watch.clock import taipei_date
    from house_watch.parse.age import add_months
    from house_watch.parse.area import PING_M2
    from house_watch.pipeline.locating import Locator, read_only

    conn: sqlite3.Connection = ctx.conn
    read_only(conn)
    scope = ctx.settings.public.scope.tx
    names = sorted({d.name for d in area.districts})
    as_of = lvr_as_of(conn) or taipei_date(ctx.now)
    start = add_months(as_of, -scope.months)
    min_cm2 = Fraction(scope.min_main_ping * 100) * PING_M2
    medians = _medians_for_export(conn)
    marks = ", ".join("?" for _ in names)
    rows = conn.execute(
        "SELECT facts_json, lat, lon, precision FROM transactions"
        f" WHERE district IN ({marks}) AND trade_date > ? ORDER BY serial",
        (*names, start.isoformat()),
    ).fetchall()
    out: list[dict] = []
    with Locator(ctx.settings, ctx.settings.data_dir, geo=ctx.cache.get("geo")) as locator:
        ctx.cache.setdefault("geo", locator.geo)
        for facts_json, lat, lon, precision in rows:
            tx = TxFacts.model_validate_json(facts_json)
            if tx.exclusion is not None:
                continue
            if tx.btype is not None and tx.btype not in scope.btypes:
                continue
            if tx.main_cm2 is not None and tx.main_cm2 < min_cm2:
                continue
            loc = Location(
                lat=lat,
                lon=lon,
                precision=Precision(precision),
                district=tx.district,
            )
            if loc.precision == Precision.ROAD and locator.idx is not None:
                loc = locator.locate_tx(tx)  # 路段精度要 road_key 才算得出路段比例
            floor = tx.floor_hi if tx.floor_hi is not None else tx.floor_lo
            metrics = locator.metrics(loc, area, min_floors=floor)
            result = unit_price(tx, medians)
            up = result.up.model_dump(mode="json")
            if result.up.basis.value == "estimated":
                up["price_level"] = _levels(result.flags, "price")
                up["area_level"] = _levels(result.flags, "area")
            out.append(
                {
                    "serial": tx.serial,
                    "date": tx.trade_date,
                    "district": tx.district,
                    "addr": tx.address_text,
                    "lat": loc.lat,
                    "lon": loc.lon,
                    "precision": loc.precision,
                    "btype": tx.btype,
                    "floor_lo": tx.floor_lo,
                    "floor_hi": tx.floor_hi,
                    "floors_total": tx.floors_total,
                    "main_cm2": tx.main_cm2,
                    "built_lo": tx.built.lo,
                    "built_hi": tx.built.hi,
                    "elevator": tx.elevator,
                    "parking": [
                        {"ptype": p.ptype, "price": p.price_yuan, "area_cm2": p.area_cm2}
                        for p in tx.parkings
                    ],
                    "total": tx.total_yuan,
                    "up": up,
                    "flags": sorted(set(tx.flags) | set(result.flags)),
                    "metrics": metrics,
                }
            )
    return out
