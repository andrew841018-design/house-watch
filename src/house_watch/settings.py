"""設定（契約 C9，只由 WP0 修改）。

只有兩層，兩層都沒有個人條件：
1. 公開預設 `default.toml`：包在套件裡、進 git。公開抓取範圍、區域、選項清單與執行參數。
2. 本機運作設定 `settings.toml`：platformdirs 的設定資料夾，不進 git（位置見 paths.py）。

個人條件（預算、屋齡、樓層、坪數、距離或車程門檻、單價、成交月數）只存在瀏覽器，
這個模組沒有、也不能有讀取它們的程式碼。兩個設定檔只要出現條件類的鍵就拒絕執行（R3-15）。
錯誤訊息只寫哪個鍵、哪裡錯，不印出設定值（R2-17）。
"""

import tomllib
from dataclasses import dataclass
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from house_watch import paths
from house_watch.errors import ConfigError
from house_watch.models import AreaKind, BType, Code, NonNegInt, PosInt

# 條件類的鍵：瀏覽器條件（契約 C3）的欄位與容器，加上常見的別名。
# 比對不分大小寫，`-` 視同 `_`。公開設定的鍵刻意取不同的名字，所以兩個檔都套用這份清單。
CRITERIA_KEYS = frozenset(
    {
        "profiles",
        "profile",
        "criteria",
        "price_max_wan",
        "built_years",
        "floor",
        "main_min_ping",
        "elevator",
        "parking_ok",
        "lift_flat_ok",
        "near_m",
        "rca_m",
        "drive_min",
        "unit_price_max_wan",
        "tx_months",
        "budget",
        "price_max",
        "unit_price_max",
    }
)

FRESHNESS_DATASETS = ("lvr", "doorindex")

Name = Annotated[str, StringConstraints(min_length=1, max_length=60)]
Hostname = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GeoRole = Literal["near", "avoid", "district", "drive", "marker"]


def find_criteria_keys(tree: Any, prefix: str = "") -> list[str]:
    """條件類鍵的路徑，例如 `sources.cthouse.price_max_wan`。只回傳鍵，不回傳值。"""
    hits: list[str] = []
    if isinstance(tree, dict):
        for key, value in tree.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).strip().lower().replace("-", "_") in CRITERIA_KEYS:
                hits.append(path)
            hits.extend(find_criteria_keys(value, path))
    elif isinstance(tree, list):
        for index, value in enumerate(tree):
            hits.extend(find_criteria_keys(value, f"{prefix}[{index}]"))
    return hits


def _ascending(values: list[int] | None, what: str) -> None:
    if values is not None and (not values or any(a >= b for a, b in pairwise(values))):
        raise ValueError(f"{what} 要是非空、遞增、不重複的清單")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- 公開預設（default.toml）


class ListingScope(_Strict):
    """開價物件的公開抓取範圍。"""

    max_price_wan: PosInt
    max_age_years: PosInt
    min_main_ping: NonNegInt
    floors: tuple[int, int]
    btypes: list[BType] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.floors[0] > self.floors[1]:
            raise ValueError("floors 的下界大於上界")
        return self


class TxScope(_Strict):
    """成交的公開範圍。"""

    months: PosInt
    min_main_ping: NonNegInt
    btypes: list[BType] = Field(min_length=1)


class Scope(_Strict):
    listing: ListingScope
    tx: TxScope


class GeoRef(_Strict):
    """區域用到的一個 geo 檔。feature 依屬性挑選（match），不依順序（R1-29）。

    role 的意思：
    - near：buffer 區域的目標範圍（算周邊距離）。
    - avoid：buffer 區域要避開的範圍（算避開距離）。
    - district：查詢的行政區界。drive 區域一定要有一個；buffer 區域可以有一個（選填）。
      有效範圍＝外推範圍 ∩ 這個多邊形（R1-15、R3-21）。buffer 區域沒寫時，
      由 geo/areas.py 的裁切資料扣掉相鄰區（過渡做法，等區界檔補進套件後拿掉）。
    - drive：drive 區域的車程檔；band_property 是分鐘數所在的屬性。
    - marker：只畫在地圖上的標記。

    契約 C7 補充（定義在 geo，不在這裡）：門牌下載用 geo/doorindex.py 的 `HttpFetcher`
    （不受 C6 頁面 5 MiB 上限的限制，上限是 `[http] download_max_bytes`），
    `IndexInfo` 是 `doorindex.refresh()` 的回傳值；定位證據是 geo/locate.py 的 `LocEvidence`。
    """

    file: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+\.geojson$")]
    role: GeoRole
    match: dict[str, str | int] = Field(default_factory=dict)
    band_property: str | None = None  # role 是 drive 時：分鐘數所在的屬性

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.role == "drive") != (self.band_property is not None):
            raise ValueError("只有 role 是 drive 的範圍要寫 band_property")
        return self


class District(_Strict):
    """要查詢的行政區。"""

    name: Name
    zip: Annotated[str, StringConstraints(pattern=r"^\d{3}$")]


class AreaScope(_Strict):
    """區域的公開抓取範圍（地理部分）。"""

    max_near_m: NonNegInt | None = None
    max_drive_min: PosInt | None = None


class AreaOptions(_Strict):
    """公開的門檻選項清單。只是清單，不代表任何人選了哪一檔（R2-05）。"""

    near_m_options: list[NonNegInt] | None = None
    rca_m_options: list[NonNegInt] | None = None
    drive_min_options: list[PosInt] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _ascending(self.near_m_options, "near_m_options")
        _ascending(self.rca_m_options, "rca_m_options")
        _ascending(self.drive_min_options, "drive_min_options")
        return self

    def as_export(self) -> dict[str, list[int]]:
        """meta.json 的 `areas[].options`：鍵是瀏覽器條件的門檻名稱。"""
        pairs = (
            ("near_m", self.near_m_options),
            ("rca_m", self.rca_m_options),
            ("drive_min", self.drive_min_options),
        )
        return {key: list(values) for key, values in pairs if values is not None}


class AreaConfig(_Strict):
    """一個區域：要查詢的行政區、多邊形引用、選項清單、公開抓取範圍。"""

    id: Code
    name: Name
    kind: AreaKind
    districts: list[District] = Field(min_length=1)
    scope: AreaScope
    options: AreaOptions
    geo: dict[Code, GeoRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        roles = [ref.role for ref in self.geo.values()]
        options, scope = self.options, self.scope
        if self.kind == AreaKind.BUFFER:
            allowed = {"near", "avoid", "marker", "district"}
            ok = (
                "near" in roles
                and roles.count("district") <= 1
                and options.near_m_options is not None
                and options.drive_min_options is None
                and ("avoid" in roles) == (options.rca_m_options is not None)
                and scope.max_near_m is not None
                and scope.max_drive_min is None
            )
            if ok and scope.max_near_m < max(options.near_m_options):
                raise ValueError("公開抓取範圍要涵蓋所有周邊距離選項")
        else:
            allowed = {"district", "drive", "marker"}
            ok = (
                roles.count("district") == 1
                and roles.count("drive") == 1
                and options.drive_min_options is not None
                and options.near_m_options is None
                and options.rca_m_options is None
                and scope.max_drive_min is not None
                and scope.max_near_m is None
            )
            if ok and scope.max_drive_min < max(options.drive_min_options):
                raise ValueError("公開抓取範圍要涵蓋所有車程選項")
        if not ok or set(roles) - allowed:
            raise ValueError(f"{self.kind.value} 區域的 geo、options、scope 組合不對")
        return self


TotalRatio = Annotated[float, Field(gt=0, le=1)]


class SourceConfig(_Strict):
    """一個來源的抓取參數（PLAN_v2 §4 第 1 項：每天每個（來源, 區域）一次完整掃描）。

    - min_interval_s：同一網站兩次請求的最小間隔；抓取層另外保證至少 4 秒。
    - max_requests_day：這個來源每天（台北日期）最多送幾個請求，含 robots.txt、轉址、
      探測、詳細頁；由抓取層執行（http.daily_cap_of）。
    - max_requests_full：一次完整掃描（一個（來源, 區域））最多用幾個請求；超過就判無效
      （budget_exhausted）。不能大於 max_requests_day。
    - probe_pages：探測讀前幾頁（r4 §3.4 的 K）。
    - detail_cap：每一輪最多抓幾個詳細頁。
    - min_total_ratio：有回報總數的站，完整掃描去重後的筆數至少要是總數的這個比例
      才算有效（r4 §3.4 第 3 條，預設九成，每站可調）。
    """

    enabled: bool
    publish: bool
    min_interval_s: float = Field(ge=3)  # 同一網站兩次請求至少間隔 3 秒（DESIGN 決策 1）
    max_requests_day: PosInt
    max_requests_full: PosInt
    probe_pages: PosInt
    detail_cap: NonNegInt
    min_total_ratio: TotalRatio = 0.9

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.max_requests_full > self.max_requests_day:
            raise ValueError("max_requests_full 不能大於 max_requests_day")
        return self


class SourceOverride(_Strict):
    """本機對某個來源的覆寫，沒寫的沿用公開預設。"""

    enabled: bool | None = None
    publish: bool | None = None
    min_interval_s: float | None = Field(default=None, ge=3)
    max_requests_day: PosInt | None = None
    max_requests_full: PosInt | None = None
    probe_pages: PosInt | None = None
    detail_cap: NonNegInt | None = None
    min_total_ratio: TotalRatio | None = None


class Schedule(_Strict):
    """排程。window_hours 是台北時間的整點，含兩端。"""

    timezone: str
    window_hours: tuple[int, int]
    probe_every_hours: PosInt
    lvr_every_hours: PosInt
    doorindex_every_days: PosInt
    run_deadline_minutes: PosInt
    backup_keep: PosInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone 不是有效的時區名稱") from None
        start, end = self.window_hours
        if not 0 <= start <= end <= 23:
            raise ValueError("window_hours 要在 0–23 之間，而且開始不晚於結束")
        return self


class HttpConfig(_Strict):
    """抓取的 timeout 與大小上限。

    User-Agent 不能設定：擁有者決定照實寫 `house-watch/<版本>`（PLAN_v2 §4 第 2 項），
    由抓取層固定。`user_agent` 這個鍵只為了相容保留，不使用，只能是空的。
    """

    connect_timeout_s: float = Field(gt=0)
    read_timeout_s: float = Field(gt=0)
    page_max_bytes: PosInt
    download_max_bytes: PosInt
    user_agent: None = None  # 不使用：UA 固定是 house-watch/<版本>


class TlsConfig(_Strict):
    """只清掉 X509 strict 旗標的主機；憑證驗證本身永遠不能關（R2-10）。"""

    relax_strict_hosts: list[Hostname] = Field(default_factory=list)


class RobotsConfig(_Strict):
    """已確認過的 robots.txt 內容雜湊；內容一變就停用該站，等人確認（R2-09）。"""

    accepted_hash: dict[Code, Sha256Hex] = Field(default_factory=dict)


class PublicConfig(_Strict):
    scope: Scope
    areas: list[AreaConfig] = Field(min_length=1)
    sources: dict[Code, SourceConfig] = Field(min_length=1)
    schedule: Schedule
    freshness_hours: dict[Code, PosInt]
    http: HttpConfig
    tls: TlsConfig = TlsConfig()
    robots: RobotsConfig = RobotsConfig()

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [area.id for area in self.areas]
        if len(ids) != len(set(ids)):
            raise ValueError("areas 的 id 重複")
        if set(self.sources) & set(FRESHNESS_DATASETS):
            raise ValueError("來源 id 不能用 lvr 或 doorindex")
        if set(self.freshness_hours) != set(self.sources) | set(FRESHNESS_DATASETS):
            raise ValueError("freshness_hours 要剛好列出每個來源，以及 lvr、doorindex")
        if not set(self.robots.accepted_hash) <= set(self.sources):
            raise ValueError("robots.accepted_hash 有不存在的來源")
        return self


# ---------------------------------------------------------------- 本機運作設定（settings.toml）


def _absolute(value: Path) -> Path:
    if not value.expanduser().is_absolute():
        raise ValueError("路徑要寫絕對路徑")
    return value


AbsPath = Annotated[Path, AfterValidator(_absolute)]


class PublishSettings(_Strict):
    site_repo_dir: AbsPath | None = None
    remote: str | None = None
    deploy_key: AbsPath | None = None
    known_hosts: AbsPath | None = None


class LocalHttp(_Strict):
    """不使用：UA 不放帳號或聯絡方式（PLAN_v2 §4 第 2 項），這個值不會送出到任何網站。
    只為了相容舊的設定檔保留；doctor 只顯示「已設定／未設定」。"""

    contact: str | None = None


class LocalSettings(_Strict):
    data_dir: AbsPath | None = None
    enabled_areas: Annotated[list[Code], Field(min_length=1)] | None = None
    publish: PublishSettings = PublishSettings()
    http: LocalHttp = LocalHttp()
    sources: dict[Code, SourceOverride] = Field(default_factory=dict)


# ---------------------------------------------------------------- 載入


@dataclass(frozen=True)
class Settings:
    """公開預設加上本機設定之後的實際設定。"""

    public: PublicConfig
    local: LocalSettings

    @property
    def sources(self) -> dict[str, SourceConfig]:
        merged: dict[str, SourceConfig] = {}
        for source_id, base in self.public.sources.items():
            override = self.local.sources.get(source_id)
            if override is not None:
                base = _merge_source(base, override)
            merged[source_id] = base
        return merged

    @property
    def enabled_areas(self) -> list[AreaConfig]:
        """本機有寫就用本機的；沒寫只啟用公開預設的第一個區域（R3-15）。"""
        ids = self.local.enabled_areas or [self.public.areas[0].id]
        by_id = {area.id: area for area in self.public.areas}
        return [by_id[area_id] for area_id in ids]

    @property
    def data_dir(self) -> Path:
        return paths.resolve_data_dir(self.local.data_dir)


def _parse_toml(text: str, origin: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{origin}：TOML 格式錯誤（{exc}）") from None


def _describe(exc: ValidationError) -> str:
    items = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]) or "（最上層）"
        items.append(f"{location}：{error['msg']}")
    return "；".join(items)


def _validate[M: BaseModel](model: type[M], data: dict[str, Any], origin: str) -> M:
    hits = find_criteria_keys(data)
    if hits:
        raise ConfigError(
            f"{origin}：設定檔不能放個人條件（條件只存在瀏覽器）。請刪掉這些鍵：{', '.join(hits)}"
        )
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # from None：不要把 pydantic 的原始錯誤（含輸入值）串在後面。
        raise ConfigError(f"{origin}：{_describe(exc)}") from None


def load_public(text: str | None = None) -> PublicConfig:
    """讀公開預設。text 給值時讀那段文字（測試用），否則讀套件裡的 default.toml。"""
    if text is None:
        text = files("house_watch").joinpath("default.toml").read_text(encoding="utf-8")
    return _validate(PublicConfig, _parse_toml(text, "default.toml"), "default.toml")


def load_local(path: Path | None = None) -> LocalSettings:
    """讀本機設定。預設位置的檔案不存在時用空設定；明確指定的檔案不存在就是錯誤。"""
    explicit = path is not None
    if path is None:
        path, explicit = paths.settings_file()
    if not path.is_file():
        if explicit:
            raise ConfigError(f"找不到本機設定檔：{path}")
        return LocalSettings()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}：讀不到設定檔（{type(exc).__name__}）") from None
    return _validate(LocalSettings, _parse_toml(text, str(path)), str(path))


def load_settings(
    *, public: PublicConfig | None = None, local_path: Path | None = None
) -> Settings:
    """讀兩層設定並互相核對。"""
    public = public if public is not None else load_public()
    local = load_local(local_path)
    area_ids = {area.id for area in public.areas}
    unknown_areas = [a for a in local.enabled_areas or [] if a not in area_ids]
    if unknown_areas:
        raise ConfigError(f"enabled_areas 有不存在的區域：{', '.join(unknown_areas)}")
    if local.enabled_areas is not None and len(set(local.enabled_areas)) != len(
        local.enabled_areas
    ):
        raise ConfigError("enabled_areas 有重複的區域")
    unknown_sources = [s for s in local.sources if s not in public.sources]
    if unknown_sources:
        raise ConfigError(f"sources 有不存在的來源：{', '.join(unknown_sources)}")
    for source_id, override in local.sources.items():
        base = public.sources[source_id]
        day = override.max_requests_day
        if day is not None and day > base.max_requests_day:
            raise ConfigError(
                f"sources.{source_id}.max_requests_day 只能調低：每日上限是擁有者定的禮貌上限"
            )
        try:
            _merge_source(base, override)
        except ValidationError as exc:
            raise ConfigError(f"sources.{source_id}：{_describe(exc)}") from None
    return Settings(public=public, local=local)


def _merge_source(base: SourceConfig, override: SourceOverride) -> SourceConfig:
    """公開預設加上本機覆寫，合併後重新檢查（事後審查 SA-7：model_copy 不會重新驗證）。"""
    return SourceConfig.model_validate({**base.model_dump(), **override.model_dump(exclude_none=True)})
