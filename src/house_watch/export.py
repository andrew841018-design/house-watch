"""匯出與本機網站（WP7a，契約 C2）。

把資料庫裡的事實寫成一個靜態網站（r4 §3.11），只輸出不含門檻的事實：

    <資料夾>/export/
        site/                     網站本體；`serve` 只提供這裡，publish 只發布 manifest 列出的檔
            index.html …          web/ 的檔案（不含 test/、package.json、隱藏檔）
            .nojekyll
            data/meta.json
            data/listings_<區域>.json
            data/tx_<區域>.json
            geo/<區域>.geojson     （WP1 的 provider 存在時才有）
        manifest.json             site/ 每個檔的路徑、位元組數、sha256（不在網站裡）

規則（R1-28、R1-31、R2-02、R3-02、R3-17、R3-19）：
- 每個 JSON 都由 models.py 的匯出模型產生。provider 給的每一筆都重新驗證，
  欄位白名單（extra=forbid）擋掉模型沒有的欄位；鍵排序、allow_nan=False。
- 順序固定：groups 依 gid、tx 依 serial、members 依 (src, id)、衝突值依 (src, 值)、
  flags 排序並去重；檔案依路徑。兩個 PYTHONHASHSEED 跑出來逐位元組相同。
- 大小有上限（SIZE_LIMITS）；超過就整次失敗，舊的匯出不動。
- 先寫到暫存資料夾，全部成功才整個換上；site/ 與 manifest.json 一起換。
- 錯誤代碼只輸出 ErrorCode 的值；例外訊息、路徑這類文字一律不輸出。
- Python 不讀任何個人條件（本來就沒有）。本機設定只決定匯出哪些區域、哪些來源。
- build_id 是 meta.json 以外所有檔案內容的雜湊：資料沒變，網址的 `?v=` 就不變。

資料的分工：
- meta 由本模組直接讀資料庫（C8 的 scans、source_state、kv）。
- 物件群組、成交紀錄、區域的 GeoJSON 需要其他工作包的領域邏輯（合併、單價、指標、裁切），
  由 provider 提供（PROVIDERS）。provider 還不存在時，該資料輸出空清單、不列 GeoJSON，
  meta 標成 building，並回報在 problems；本模組不自己補值（例如不會把單價全標成 none）。
- provider 只能讀資料庫，不能寫。
"""

import datetime as dt
import hashlib
import importlib
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from house_watch.clock import Clock, from_iso
from house_watch.errors import ErrorCode, ExitCode, HouseWatchError
from house_watch.models import (
    SCHEMA_VERSION,
    AreaMeta,
    Code,
    DatasetsMeta,
    DoorindexMeta,
    ListingGroup,
    ListingsFile,
    LvrMeta,
    Meta,
    MetaStatus,
    NonNegInt,
    SourceMeta,
    TxFile,
    TxRecord,
    UtcDateTime,
    canonical_json,
    export_json,
)
from house_watch.paths import DataPaths
from house_watch.settings import FRESHNESS_DATASETS, AreaConfig, Settings

# ---------------------------------------------------------------- 位置與上限

EXPORT_DIRNAME = "export"
SITE_DIRNAME = "site"
MANIFEST_NAME = "manifest.json"
META_PATH = "data/meta.json"

# 資料庫 kv 表裡給 meta.datasets 用的鍵（由執行各工作的程式寫入；值是字串）。
KV_LVR_LAST_CHECK = "lvr.last_check"  # UTC ISO 時間
KV_LVR_AS_OF = "lvr.as_of"  # YYYY-MM-DD，資料截至的日期
KV_DOORINDEX_LAST_CHECK = "doorindex.last_check"  # UTC ISO 時間
KV_DOORINDEX_VERSION = "doorindex.version"  # 門牌資料的版本文字

# scans.status 當成「成功」的值：一般成功，以及擁有者用 --accept-scan 手動接受的。
OK_SCAN_STATUSES = ("ok", "accepted")


@dataclass(frozen=True)
class SizeLimits:
    """匯出檔的大小上限（位元組）。超過任一項，整次匯出失敗。"""

    meta: int = 256 * 1024
    data_file: int = 24 * 1024 * 1024  # 每個 data/listings_*.json、data/tx_*.json
    geo_file: int = 4 * 1024 * 1024  # 每個 geo/*.geojson
    web_file: int = 4 * 1024 * 1024  # web/ 複製過來的每個檔
    site: int = 64 * 1024 * 1024  # 整個 site/


SIZE_LIMITS = SizeLimits()


def export_dir(data: DataPaths) -> Path:
    """匯出成果的資料夾。不是 DataPaths.site_repo_dir（那是網站 repo 的 clone，給 publish 用）。"""
    return data.root / EXPORT_DIRNAME


def site_dir(data: DataPaths) -> Path:
    """網站本體；`serve` 只提供這個資料夾。"""
    return export_dir(data) / SITE_DIRNAME


def manifest_path(data: DataPaths) -> Path:
    return export_dir(data) / MANIFEST_NAME


class ExportError(HouseWatchError):
    """匯出失敗。舊的匯出成果保持不動。"""

    exit_code = ExitCode.PARTIAL


# ---------------------------------------------------------------- 其他工作包的入口


class NotAvailable(Exception):
    """其他工作包的模組或函式還沒有完成。"""

    def __init__(self, spec: str, reason: str) -> None:
        super().__init__(f"{reason}（{spec}）")
        self.spec = spec
        self.reason = reason


def load_entry(spec: str) -> Any:
    """依 `套件.模組:名稱` 取得其他工作包的函式，執行時才 import。

    - 模組本身（或它的上層套件）還不存在 → NotAvailable("not yet available")。
    - 模組在，但沒有這個名稱 → NotAvailable("interface not available")。
    - 模組存在、但它自己 import 失敗 → 原樣丟出，這是真的錯誤，不能當成「還沒完成」。
    """
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and (module_name == missing or module_name.startswith(f"{missing}.")):
            raise NotAvailable(spec, "not yet available") from None
        raise
    try:
        return getattr(module, attr)
    except AttributeError:
        raise NotAvailable(spec, "interface not available") from None


ProviderFn = Callable[["ExportContext", AreaConfig], Any]


@dataclass(frozen=True)
class Providers:
    """提供匯出紀錄的函式：`入口字串`（執行時才 import）或可呼叫物件（測試用）。

    三個函式的形狀都是 `fn(ctx: ExportContext, area: AreaConfig)`：
    - listing_groups → Iterable[ListingGroup | dict]：這個區域的物件群組（WP6）。
      成員只能來自 ctx.publish_sources。
    - tx_records → Iterable[TxRecord | dict]：這個區域、公開範圍內的成交（WP3）。
    - area_geojson → dict | None：網頁畫這個區域要用的 GeoJSON FeatureCollection（WP1）。
    """

    listing_groups: str | ProviderFn | None
    tx_records: str | ProviderFn | None
    area_geojson: str | ProviderFn | None


GEO_LOAD = "house_watch.geo.areas:load"  # C7 的 areas.load


def geo_site_geojson(ctx: "ExportContext", area: AreaConfig) -> Any:
    """區域的 GeoJSON：C7 的 `areas.load()` 加上 WP1 的 `GeoContext.site_geojson(area)`。"""
    geo = ctx.cache.get("geo")
    if geo is None:
        load = load_entry(GEO_LOAD)
        geo = ctx.cache["geo"] = load(config=ctx.settings.public)
    method = getattr(geo, "site_geojson", None)
    if method is None:
        raise NotAvailable(f"{GEO_LOAD} → GeoContext.site_geojson", "interface not available")
    return method(area.id)


# listing_groups、tx_records 兩個入口是提給 WP0 的契約提案，名稱由整合時確認。
PROVIDERS = Providers(
    listing_groups="house_watch.pipeline.dedupe:export_groups",
    tx_records="house_watch.lvr.importer:export_tx",
    area_geojson=geo_site_geojson,
)


def _resolve(value: str | ProviderFn | None, what: str) -> ProviderFn:
    if value is None:
        raise NotAvailable(what, "not yet available")
    if isinstance(value, str):
        return load_entry(value)
    return value


@dataclass(frozen=True)
class ExportContext:
    """傳給 provider 的唯讀資訊。cache 只在這一次匯出裡有效（例如共用載入好的幾何）。"""

    conn: sqlite3.Connection
    settings: Settings
    now: dt.datetime
    publish_sources: frozenset[str]  # 已啟用而且可以發布的來源
    cache: dict[str, Any] = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------- manifest


SitePathAny = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:\.nojekyll|[A-Za-z0-9][A-Za-z0-9._-]*)(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
    ),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: SitePathAny  # 相對於 site/，一律用 /
    bytes: NonNegInt
    sha256: Sha256Hex


class Manifest(BaseModel):
    """`export/manifest.json`：site/ 底下每一個檔。publish 只發布這份清單上的檔（R2-02）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    build_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    generated_at: UtcDateTime
    status: MetaStatus
    files: list[ManifestFile]


def load_manifest(path: Path) -> Manifest:
    """讀 manifest.json（給 publish、doctor 用）。"""
    try:
        return Manifest.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ExportError(f"manifest.json 讀不到或格式不對（{type(exc).__name__}）") from None


@dataclass(frozen=True)
class ExportResult:
    site_dir: Path
    manifest: Manifest
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """所有資料都有 provider、也都成功。False 時網站仍然合法，只是有些資料是空的。"""
        return not self.problems


# ---------------------------------------------------------------- GeoJSON（區域圖層）


class _Geo(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Position = Annotated[list[float], Field(min_length=2, max_length=3)]


class _Point(_Geo):
    type: Literal["Point"]
    coordinates: Position


class _MultiPoint(_Geo):
    type: Literal["MultiPoint"]
    coordinates: list[Position]


class _LineString(_Geo):
    type: Literal["LineString"]
    coordinates: list[Position]


class _MultiLineString(_Geo):
    type: Literal["MultiLineString"]
    coordinates: list[list[Position]]


class _Polygon(_Geo):
    type: Literal["Polygon"]
    coordinates: list[list[Position]]


class _MultiPolygon(_Geo):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[Position]]]


Geometry = Annotated[
    _Point | _MultiPoint | _LineString | _MultiLineString | _Polygon | _MultiPolygon,
    Field(discriminator="type"),
]
GeoText = Annotated[str, StringConstraints(max_length=200)]
PropValue = bool | int | float | GeoText | None


class _Feature(_Geo):
    type: Literal["Feature"]
    geometry: Geometry
    properties: dict[Code, PropValue] = Field(default_factory=dict)


class GeoFile(_Geo):
    """`geo/<區域>.geojson` 的白名單：只有幾何與簡單的屬性值。"""

    type: Literal["FeatureCollection"]
    area: Code | None = None
    features: list[_Feature]


# ---------------------------------------------------------------- 讀資料庫（meta）


def _parse_time(text: Any) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return from_iso(text)
    except (ValueError, TypeError):
        return None


def _parse_date(text: Any) -> dt.date | None:
    if not isinstance(text, str):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def public_code(value: Any) -> ErrorCode | None:
    """只接受公開錯誤代碼；其他文字（例外訊息、路徑）一律當成沒有（R1-31）。"""
    if not isinstance(value, str):
        return None
    try:
        return ErrorCode(value)
    except ValueError:
        return None


def read_kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True)
class SourceTimes:
    last_ok: dt.datetime | None = None
    last_full_ok: dt.datetime | None = None


def scan_times(conn: sqlite3.Connection, area_ids: list[str]) -> dict[str, SourceTimes]:
    """各來源在這些區域最後一次成功的掃描、最後一次成功的完整掃描（finished_at）。"""
    marks = ", ".join("?" for _ in area_ids)
    oks = ", ".join("?" for _ in OK_SCAN_STATUSES)
    rows = conn.execute(
        f"SELECT source, mode, finished_at FROM scans "
        f"WHERE status IN ({oks}) AND finished_at IS NOT NULL AND area IN ({marks})",
        (*OK_SCAN_STATUSES, *area_ids),
    ).fetchall()
    last_ok: dict[str, dt.datetime] = {}
    last_full: dict[str, dt.datetime] = {}
    for source, mode, finished_at in rows:
        when = _parse_time(finished_at)
        if when is None:
            continue
        if source not in last_ok or when > last_ok[source]:
            last_ok[source] = when
        if mode == "full" and (source not in last_full or when > last_full[source]):
            last_full[source] = when
    return {
        source: SourceTimes(last_ok.get(source), last_full.get(source))
        for source in sorted(last_ok)
    }


@dataclass(frozen=True)
class LatestScan:
    scan_id: int
    status: str
    error_code: str | None
    finished_at: dt.datetime


def latest_scans(conn: sqlite3.Connection, area_ids: list[str]) -> dict[str, LatestScan]:
    """各來源在這些區域最後一次結束的掃描（不論成功與否）。"""
    marks = ", ".join("?" for _ in area_ids)
    rows = conn.execute(
        f"SELECT scan_id, source, status, error_code, finished_at FROM scans "
        f"WHERE finished_at IS NOT NULL AND area IN ({marks})",
        tuple(area_ids),
    ).fetchall()
    latest: dict[str, LatestScan] = {}
    for scan_id, source, status, error_code, finished_at in rows:
        when = _parse_time(finished_at)
        if when is None:
            continue
        current = latest.get(source)
        if current is None or (when, scan_id) > (current.finished_at, current.scan_id):
            latest[source] = LatestScan(scan_id, str(status), error_code, when)
    return latest


def latest_full_failures(conn: sqlite3.Connection, area_ids: list[str]) -> dict[str, LatestScan]:
    """各（來源, 區域）最後一次完整掃描沒成功的，每個來源取最近的一筆（事後審查 AT-2）。

    某一區的完整掃描失敗或無效，不能被別區或探測的成功蓋掉：那一區沒有有效完整掃描，
    就不會判下架，網頁卻會顯示「正常」。
    """
    marks = ", ".join("?" for _ in area_ids)
    rows = conn.execute(
        f"SELECT scan_id, source, area, status, error_code, finished_at FROM scans "
        f"WHERE mode = 'full' AND finished_at IS NOT NULL AND area IN ({marks})",
        tuple(area_ids),
    ).fetchall()
    last: dict[tuple[str, str], LatestScan] = {}
    for scan_id, source, area, status, error_code, finished_at in rows:
        when = _parse_time(finished_at)
        if when is None:
            continue
        current = last.get((source, area))
        if current is None or (when, scan_id) > (current.finished_at, current.scan_id):
            last[(source, area)] = LatestScan(scan_id, str(status), error_code, when)
    failures: dict[str, LatestScan] = {}
    for (source, _area), scan in last.items():
        if scan.status in OK_SCAN_STATUSES:
            continue
        current = failures.get(source)
        if current is None or (scan.finished_at, scan.scan_id) > (
            current.finished_at,
            current.scan_id,
        ):
            failures[source] = scan
    return failures


@dataclass(frozen=True)
class SourceStateRow:
    paused_until: dt.datetime | None
    backoff_level: int
    last_error: str | None
    has_robots_hash: bool


def source_states(conn: sqlite3.Connection) -> dict[str, SourceStateRow]:
    rows = conn.execute(
        "SELECT source, paused_until, backoff_level, last_error, robots_hash FROM source_state"
    ).fetchall()
    return {
        source: SourceStateRow(_parse_time(paused), int(level or 0), error, robots is not None)
        for source, paused, level, error, robots in rows
    }


def source_error(
    latest: LatestScan | None,
    state: SourceStateRow | None,
    now: dt.datetime,
    full_failure: LatestScan | None = None,
) -> ErrorCode | None:
    """來源目前的錯誤代碼：最後一次掃描失敗時用它的代碼；其次是某一區最後一次完整掃描
    沒成功的代碼（latest_full_failures）；再其次，暫停中時用暫停的原因。"""
    for scan in (latest, full_failure):
        if scan is not None and scan.status not in OK_SCAN_STATUSES:
            code = public_code(scan.error_code)
            if code is not None:
                return code
    if state is not None and state.paused_until is not None and state.paused_until > now:
        return public_code(state.last_error) or ErrorCode.PAUSED
    return None


def _sources_meta(
    conn: sqlite3.Connection, settings: Settings, area_ids: list[str], now: dt.datetime
) -> list[SourceMeta]:
    times = scan_times(conn, area_ids)
    latest = latest_scans(conn, area_ids)
    full_failures = latest_full_failures(conn, area_ids)
    states = source_states(conn)
    result = []
    for source_id, config in sorted(settings.sources.items()):
        if not config.enabled:
            continue
        state = states.get(source_id)
        paused = state.paused_until if state is not None else None
        stamp = times.get(source_id, SourceTimes())
        result.append(
            SourceMeta(
                id=source_id,
                publish=config.publish,
                last_ok=stamp.last_ok,
                last_full_ok=stamp.last_full_ok,
                paused_until=paused if paused is not None and paused > now else None,
                error_code=source_error(
                    latest.get(source_id), state, now, full_failures.get(source_id)
                ),
            )
        )
    return result


def _datasets_meta(conn: sqlite3.Connection, problems: list[str]) -> DatasetsMeta:
    def time_of(key: str) -> dt.datetime | None:
        raw = read_kv(conn, key)
        value = _parse_time(raw)
        if raw is not None and value is None:
            problems.append(f"kv {key} 的格式不對，當成沒有")
        return value

    raw_as_of = read_kv(conn, KV_LVR_AS_OF)
    as_of = _parse_date(raw_as_of)
    if raw_as_of is not None and as_of is None:
        problems.append(f"kv {KV_LVR_AS_OF} 的格式不對，當成沒有")
    version = read_kv(conn, KV_DOORINDEX_VERSION)
    door_check = time_of(KV_DOORINDEX_LAST_CHECK)
    try:
        doorindex = DoorindexMeta(last_check=door_check, version=version)
    except ValidationError:
        problems.append(f"kv {KV_DOORINDEX_VERSION} 的格式不對，當成沒有")
        doorindex = DoorindexMeta(last_check=door_check)
    lvr = LvrMeta(last_check=time_of(KV_LVR_LAST_CHECK), as_of=as_of)
    return DatasetsMeta(lvr=lvr, doorindex=doorindex)


# ---------------------------------------------------------------- 紀錄的驗證與排序


def _describe(exc: ValidationError) -> str:
    """驗證錯誤的位置與原因，不帶輸入值。"""
    parts = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False)[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "（最上層）"
        parts.append(f"{location}：{error['msg']}")
    return "；".join(parts)


def _as_data(item: Any, what: str) -> dict[str, Any]:
    """provider 給的一筆轉成 dict。模型實例也先倒出來，再用匯出模型本身重新驗證：
    子類別多出的欄位、繞過驗證建立的值（model_construct）都會在驗證時被擋下。"""
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if isinstance(item, Mapping):
        return dict(item)
    raise ExportError(f"{what}：provider 給的不是紀錄（{type(item).__name__}）")


def _value_key(value: Any) -> str:
    return canonical_json(value)


def _listings_file(area_id: str, items: Iterable[Any], publish: frozenset[str]) -> ListingsFile:
    what = f"listings_{area_id}"
    groups: list[ListingGroup] = []
    for item in items:
        try:
            group = ListingGroup.model_validate(_as_data(item, what))
        except ValidationError as exc:
            raise ExportError(f"{what}：欄位白名單或格式不符（{_describe(exc)}）") from None
        outside = sorted({member.src for member in group.members} - publish)
        if outside:
            raise ExportError(f"{what}：群組 {group.gid} 有不能發布的來源 {', '.join(outside)}")
        members = sorted(group.members, key=lambda m: (m.src, m.id))
        conflicts = {
            name: sorted(values, key=lambda v: (v.src, _value_key(v.value)))
            for name, values in sorted(group.conflicts.items())
        }
        groups.append(group.model_copy(update={"members": members, "conflicts": conflicts}))
    groups.sort(key=lambda g: g.gid)
    try:
        return ListingsFile(area=area_id, groups=groups)
    except ValidationError as exc:
        raise ExportError(f"{what}：{_describe(exc)}") from None


def _tx_file(area_id: str, as_of: dt.date | None, items: Iterable[Any]) -> TxFile:
    what = f"tx_{area_id}"
    records: list[TxRecord] = []
    for item in items:
        try:
            record = TxRecord.model_validate(_as_data(item, what))
        except ValidationError as exc:
            raise ExportError(f"{what}：欄位白名單或格式不符（{_describe(exc)}）") from None
        records.append(record.model_copy(update={"flags": sorted(set(record.flags))}))
    records.sort(key=lambda r: r.serial)
    try:
        return TxFile(area=area_id, as_of=as_of, tx=records)
    except ValidationError as exc:
        raise ExportError(f"{what}：{_describe(exc)}") from None


def _geo_file(area_id: str, data: Any) -> GeoFile:
    if not isinstance(data, Mapping):
        raise ExportError(f"geo/{area_id}.geojson：provider 給的不是 GeoJSON 物件")
    try:
        geo = GeoFile.model_validate(dict(data))
    except ValidationError as exc:
        raise ExportError(f"geo/{area_id}.geojson：格式不符（{_describe(exc)}）") from None
    if geo.area is not None and geo.area != area_id:
        raise ExportError(f"geo/{area_id}.geojson：內容是別的區域（{geo.area}）")
    return geo.model_copy(update={"area": area_id})


# ---------------------------------------------------------------- 網頁檔

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WEB_SKIP_TOP = frozenset({"test"})  # 網頁的測試只在本機與 CI 用，不發布
WEB_SKIP_NAMES = frozenset({"package.json", "package-lock.json", "node_modules"})
WEB_SKIP_SUFFIXES = ("~", ".swp", ".bak", ".orig", ".tmp", ".pyc")
WEB_RESERVED_TOP = frozenset({"data", "geo"})  # 這兩個資料夾由匯出產生


def default_web_root() -> Path:
    """網頁檔的位置：安裝好的 wheel 裡是 `house_watch/web`；開發時是 repo 的 `web/`。"""
    packaged = files("house_watch").joinpath("web")
    if isinstance(packaged, Path) and packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[2] / "web"
    if source.is_dir():
        return source
    raise ExportError("找不到網頁檔（house_watch/web 或 repo 的 web/）")


def web_files(web_root: Path) -> list[tuple[str, Path]]:
    """要複製進網站的網頁檔：(網站上的路徑, 來源)，依路徑排序。

    跳過 test/、package.json、node_modules、隱藏檔、編輯器暫存檔、符號連結。
    """
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(web_root):
        current = Path(dirpath)
        rel_dir = current.relative_to(web_root)
        top = rel_dir == Path(".")
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in WEB_SKIP_NAMES or (top and name in WEB_SKIP_TOP):
                continue
            if (current / name).is_symlink():
                continue
            if top and name in WEB_RESERVED_TOP:
                raise ExportError(f"web/{name}/ 和匯出的資料夾撞名")
            if not _NAME.match(name):
                raise ExportError(f"web/ 裡的資料夾名稱不合規則：{(rel_dir / name).as_posix()}")
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            source = current / name
            if name.startswith(".") or name in WEB_SKIP_NAMES or name.endswith(WEB_SKIP_SUFFIXES):
                continue
            if source.is_symlink() or not source.is_file():
                continue
            rel = (rel_dir / name).as_posix()
            if not _NAME.match(name):
                raise ExportError(f"web/ 裡的檔名不合規則：{rel}")
            found.append((rel, source))
    found.sort()
    return found


# ---------------------------------------------------------------- 寫檔


class _SiteWriter:
    """寫進暫存的 site 資料夾，每個檔都檢查大小上限。"""

    def __init__(self, root: Path, limits: SizeLimits) -> None:
        self.root = root
        self.limits = limits

    def _check(self, rel: str, size: int, limit: int) -> None:
        if size > limit:
            raise ExportError(f"{rel} 有 {size} 位元組，超過上限 {limit}")

    def write(self, rel: str, payload: bytes, limit: int) -> None:
        self._check(rel, len(payload), limit)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)  # 位元組寫入：換行一律是 \n（R3-19）

    def copy(self, rel: str, source: Path) -> None:
        self._check(rel, source.stat().st_size, self.limits.web_file)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_site(root: Path) -> list[ManifestFile]:
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            entries.append(ManifestFile(path=rel, bytes=path.stat().st_size, sha256=_sha256(path)))
    entries.sort(key=lambda e: e.path)
    return entries


def _build_id(entries: list[ManifestFile]) -> str:
    """meta.json 以外所有檔的內容雜湊（前 16 碼）。"""
    digest = hashlib.sha256()
    for entry in entries:
        if entry.path != META_PATH:
            digest.update(f"{entry.path}\0{entry.sha256}\n".encode())
    return digest.hexdigest()[:16]


def _remove_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _swap(new: Path, current: Path) -> None:
    """把 new 換成 current。中途失敗時放回舊的。"""
    old = current.with_name(f".{current.name}.old")
    _remove_tree(old)
    had_current = current.exists()
    if had_current:
        os.replace(current, old)
    try:
        os.replace(new, current)
    except OSError:
        if had_current:
            os.replace(old, current)
        raise
    _remove_tree(old)


# ---------------------------------------------------------------- 匯出


@dataclass
class _Collected:
    listings: dict[str, ListingsFile]
    tx: dict[str, TxFile]
    geo: dict[str, GeoFile]
    listings_available: bool
    tx_available: bool


def _collect(
    ctx: ExportContext,
    areas: list[AreaConfig],
    providers: Providers,
    lvr_as_of: dt.date | None,
    problems: list[str],
) -> _Collected:
    """向 provider 取紀錄。

    - provider 還不存在（NotAvailable）：該資料輸出空清單，記進 problems。
    - 物件、成交的 provider 出錯或給了不合格的紀錄：整次匯出失敗（ExportError），
      不能把「沒資料」發布出去。
    - 區域 GeoJSON 出錯：只略過那個檔（meta 不列它），記進 problems；資料照常匯出。
    """

    def provider(value: str | ProviderFn | None, what: str) -> ProviderFn | None:
        try:
            return _resolve(value, what)
        except NotAvailable as exc:
            problems.append(f"{what}：{exc}")
            return None

    def records(fn: ProviderFn, what: str, area: AreaConfig) -> list[Any]:
        try:
            return list(fn(ctx, area) or ())
        except (NotAvailable, HouseWatchError):
            raise
        except Exception as exc:  # provider 的錯誤：整次匯出失敗，舊的匯出不動
            raise ExportError(f"{what}：provider 失敗（{type(exc).__name__}: {exc}）") from exc

    groups_fn = provider(providers.listing_groups, "listings")
    tx_fn = provider(providers.tx_records, "tx")
    geo_fn = provider(providers.area_geojson, "geo")
    collected = _Collected({}, {}, {}, groups_fn is not None, tx_fn is not None)

    for area in areas:
        groups: list[Any] = []
        if groups_fn is not None:
            try:
                groups = records(groups_fn, f"listings_{area.id}", area)
            except NotAvailable as exc:
                problems.append(f"listings：{exc}")
                collected.listings_available = False
        collected.listings[area.id] = _listings_file(area.id, groups, ctx.publish_sources)

        txs: list[Any] = []
        if tx_fn is not None:
            try:
                txs = records(tx_fn, f"tx_{area.id}", area)
            except NotAvailable as exc:
                problems.append(f"tx：{exc}")
                collected.tx_available = False
        as_of = lvr_as_of if collected.tx_available else None
        collected.tx[area.id] = _tx_file(area.id, as_of, txs)

        if geo_fn is not None:
            what = f"geo/{area.id}.geojson"
            try:
                geojson = geo_fn(ctx, area)
                if geojson is not None:
                    collected.geo[area.id] = _geo_file(area.id, geojson)
            except NotAvailable as exc:
                problems.append(f"geo：{exc}")
            except Exception as exc:  # 地圖圖層壞了只略過這個檔，資料照常匯出
                problems.append(f"{what}：略過（{type(exc).__name__}: {exc}）")
    return collected


def _status(
    sources: list[SourceMeta], collected: _Collected, lvr_as_of: dt.date | None
) -> MetaStatus:
    """資料建立中（R3-15）：成交還沒匯入過，或可發布的來源都還沒有成功的完整掃描。"""
    published = [source for source in sources if source.publish]
    listings_ready = not published or (
        collected.listings_available and any(s.last_full_ok is not None for s in published)
    )
    tx_ready = collected.tx_available and lvr_as_of is not None
    return MetaStatus.READY if listings_ready and tx_ready else MetaStatus.BUILDING


def export_site(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    data: DataPaths,
    clock: Clock,
    providers: Providers | None = None,
    web_root: Path | None = None,
    limits: SizeLimits = SIZE_LIMITS,
) -> ExportResult:
    """產生 `<資料夾>/export/`（site/ 與 manifest.json）。失敗時丟 ExportError，舊的不動。

    呼叫前要持有執行鎖，資料庫也要已經存在（db.open_or_init）。
    providers、web_root 沒給時用 PROVIDERS 與 default_web_root()（執行時才查）。
    """
    providers = providers if providers is not None else PROVIDERS
    now = clock.now()
    problems: list[str] = []
    areas = settings.enabled_areas
    area_ids = [area.id for area in areas]
    sources = _sources_meta(conn, settings, area_ids, now)
    datasets = _datasets_meta(conn, problems)
    publish = frozenset(source.id for source in sources if source.publish)
    ctx = ExportContext(conn=conn, settings=settings, now=now, publish_sources=publish)
    collected = _collect(ctx, areas, providers, datasets.lvr.as_of, problems)

    root = web_root if web_root is not None else default_web_root()
    pages = web_files(root)
    if "index.html" not in {rel for rel, _ in pages}:
        problems.append("web：index.html not yet available")

    final = export_dir(data)
    building = final.with_name(f".{final.name}.tmp")
    _remove_tree(building)
    tmp_site = building / SITE_DIRNAME
    writer = _SiteWriter(tmp_site, limits)
    try:
        tmp_site.mkdir(parents=True)
        for rel, source in pages:
            writer.copy(rel, source)
        writer.write(".nojekyll", b"", limits.web_file)
        area_meta = []
        for area in areas:
            listings_rel = f"data/listings_{area.id}.json"
            tx_rel = f"data/tx_{area.id}.json"
            area_files = [listings_rel, tx_rel]
            listings_payload = export_json(collected.listings[area.id]).encode()
            writer.write(listings_rel, listings_payload, limits.data_file)
            writer.write(tx_rel, export_json(collected.tx[area.id]).encode(), limits.data_file)
            if area.id in collected.geo:
                geo_rel = f"geo/{area.id}.geojson"
                payload = canonical_json(collected.geo[area.id].model_dump(mode="json")) + "\n"
                writer.write(geo_rel, payload.encode(), limits.geo_file)
                area_files.append(geo_rel)
            area_meta.append(
                AreaMeta(
                    id=area.id,
                    name=area.name,
                    kind=area.kind,
                    options=area.options.as_export(),
                    files=sorted(area_files),
                )
            )
        build_id = _build_id(_list_site(tmp_site))
        status = _status(sources, collected, datasets.lvr.as_of)
        freshness = {
            key: hours
            for key, hours in sorted(settings.public.freshness_hours.items())
            if key in FRESHNESS_DATASETS or any(source.id == key for source in sources)
        }
        meta = Meta(
            build_id=build_id,
            generated_at=now,
            status=status,
            sources=sources,
            datasets=datasets,
            areas=area_meta,
            freshness_hours=freshness,
        )
        writer.write(META_PATH, export_json(meta).encode(), limits.meta)
        entries = _list_site(tmp_site)
        total = sum(entry.bytes for entry in entries)
        if total > limits.site:
            raise ExportError(f"整個網站有 {total} 位元組，超過上限 {limits.site}")
        manifest = Manifest(build_id=build_id, generated_at=now, status=status, files=entries)
        (building / MANIFEST_NAME).write_bytes(
            (canonical_json(manifest.model_dump(mode="json")) + "\n").encode()
        )
        _swap(building, final)
    except BaseException:
        _remove_tree(building)
        raise
    return ExportResult(site_dir=final / SITE_DIRNAME, manifest=manifest, problems=tuple(problems))
