"""事實模型（C1）與匯出模型（C2）。契約檔，只由 WP0 修改。

- C1：解析器、定位與流程之間傳遞的事實。只放實質事實；
  不放標題、描述、聯絡資料、網站的更新時間、瀏覽數。
- C2：寫進網站 `data/*.json` 的匯出模型，同時是欄位白名單。
  一律用 `export_json()` 輸出：鍵排序、`allow_nan=False`。
- 缺值一律是 null。NaN、±Infinity、空白字串在模型層就轉成 null（R3-02）。
- 時間一律帶時區並轉成 UTC；日期判斷用台北日期（見 clock.py）。
- 單位：金額是整數元；`*_cp` 是 0.01 坪的整數；`*_cm2` 是 0.01 m² 的整數；距離是整數公尺。

`python -m house_watch.models` 會印出匯出 JSON Schema，內容等於 `schemas/export.schema.json`。
"""

import datetime as dt
import hashlib
import json
import math
import sys
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import models_json_schema

from house_watch.errors import ErrorCode

SCHEMA_VERSION = 1
CONFLICT = "conflict"


# ---------------------------------------------------------------- 列舉值（C1）


class Precision(StrEnum):
    """位置精度。"""

    SITE = "site"  # 網站提供的座標
    DOOR = "door"  # 門牌點
    COMMUNITY = "community"  # 社區名定位
    ROAD = "road"  # 只知道路段
    NONE = "none"  # 定位不到


class Parking(StrEnum):
    """車位類型。"""

    RAMP_FLAT = "ramp_flat"  # 坡道平面
    GROUND_FLAT = "ground_flat"  # 一樓平面
    LIFT_FLAT = "lift_flat"  # 升降平面
    FLAT_UNKNOWN_ACCESS = "flat_unknown_access"  # 只寫平面，坡道或升降不明
    MECHANICAL = "mechanical"  # 機械、塔式
    MIXED_WITH_FLAT = "mixed_with_flat"  # 多個車位，其中有平面
    MIXED_NO_FLAT = "mixed_no_flat"  # 多個車位，都不是平面
    NONE = "none"  # 沒有車位
    UNKNOWN = "unknown"


class Elevator(StrEnum):
    """電梯。"""

    YES = "yes"
    NO = "no"
    INFERRED = "inferred"  # 依建物型態推定
    UNKNOWN = "unknown"


class BType(StrEnum):
    """建物型態。"""

    BUILDING = "大樓"
    MANSION = "華廈"
    WALKUP = "公寓"
    TOWNHOUSE = "透天"
    OTHER = "其他"


class MainKind(StrEnum):
    """`main_cp` 的口徑。"""

    MAIN = "main"  # 只有主建物
    MAIN_PLUS_AUX = "main_plus_aux"  # 主建物＋附屬建物的合併值


class RecordStatus(StrEnum):
    """刊登或群組的狀態。"""

    ACTIVE = "active"
    GONE = "gone"


class UpBasis(StrEnum):
    """扣車位單價的口徑。"""

    OFFICIAL = "official"  # 官方拆價
    ESTIMATED = "estimated"  # 用車位中位數估算
    NONE = "none"  # 算不出來


class AreaKind(StrEnum):
    """區域的種類，決定有哪些指標與門檻。"""

    BUFFER = "buffer"  # 目標範圍周邊距離＋避開範圍（taoyuan_core）
    DRIVE = "drive"  # 行政區內＋到交流道的車程檔（zhongli_ic）


class MetaStatus(StrEnum):
    """網站資料的狀態。"""

    BUILDING = "building"  # 資料建立中
    READY = "ready"


# 瀏覽器規則會用到的列舉值；web/rules.js 的 ENUMS 必須和這裡完全相同（r4 §3.3）。
RULE_ENUMS: dict[str, type[StrEnum]] = {
    "BType": BType,
    "Elevator": Elevator,
    "MainKind": MainKind,
    "Parking": Parking,
    "Precision": Precision,
    "RecordStatus": RecordStatus,
    "UpBasis": UpBasis,
}


def rule_enum_values() -> dict[str, list[str]]:
    """RULE_ENUMS 的值，格式和 rules.js 的 ENUMS 一樣。"""
    return {name: [member.value for member in enum] for name, enum in RULE_ENUMS.items()}


# ---------------------------------------------------------------- 基本型別


def _blank_to_none(value: Any) -> Any:
    """NaN、±Infinity、空白字串都當成缺值。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("時間必須帶時區")
    return value.astimezone(dt.UTC)


_Missing = BeforeValidator(_blank_to_none)

Code = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
ItemId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
UtcDateTime = Annotated[dt.datetime, AfterValidator(_to_utc)]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]
Fraction = Annotated[float, Field(ge=0, le=1)]

OptText = Annotated[Text | None, _Missing]
OptCode = Annotated[Code | None, _Missing]
OptInt = Annotated[int | None, _Missing]
OptNonNegInt = Annotated[NonNegInt | None, _Missing]
OptPosInt = Annotated[PosInt | None, _Missing]
OptBool = Annotated[bool | None, _Missing]
OptDate = Annotated[dt.date | None, _Missing]
OptUtcDateTime = Annotated[UtcDateTime | None, _Missing]
OptLat = Annotated[Lat | None, _Missing]
OptLon = Annotated[Lon | None, _Missing]


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_serialization_defaults_required=True,
    )


def _check_range(lo: Any, hi: Any, what: str) -> None:
    if lo is not None and hi is not None and lo != CONFLICT and hi != CONFLICT and lo > hi:
        raise ValueError(f"{what} 的下界大於上界")


def _check_point(lat: float | None, lon: float | None, precision: Precision) -> None:
    if (lat is None) != (lon is None):
        raise ValueError("lat 與 lon 要同時有值或同時是 null")
    if precision == Precision.NONE and lat is not None:
        raise ValueError("precision 是 none 時不能有座標")


def canonical_json(data: Any) -> str:
    """固定格式的 JSON：鍵排序、不轉義中文、不允許 NaN。"""
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


# ---------------------------------------------------------------- C1 事實模型


class DateRange(_Model):
    """完工日區間 [lo, hi]，兩端都含；任一端可以是 null（r4 §3.10）。"""

    lo: OptDate = None
    hi: OptDate = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_range(self.lo, self.hi, "完工日區間")
        return self


class ListingFacts(_Model):
    """一筆刊登的實質事實。版本表的雜湊只算這些欄位（facts_hash）。"""

    # 識別
    source: Code
    source_id: ItemId
    area: Code
    # 位置線索
    community_name: OptText = None
    community_key: OptText = None  # 站內的社區 id
    road_text: OptText = None
    district_hints: list[tuple[Code, Text]] = Field(default_factory=list)  # (kind, value)
    site_lat: OptLat = None
    site_lon: OptLon = None
    # 價格（元）
    price_yuan: OptNonNegInt = None
    price_excl_parking: OptBool = None  # true：總價不含車位
    parking_price_yuan: OptNonNegInt = None
    site_original_price_yuan: OptNonNegInt = None  # 站方標示的原價，只顯示
    # 面積（0.01 坪）
    main_cp: OptNonNegInt = None
    main_kind: MainKind | None = None
    title_area_cp: OptNonNegInt = None
    # 樓層
    floor_lo: OptInt = None
    floor_hi: OptInt = None
    floors_total: OptPosInt = None
    # 其他
    built: DateRange = Field(default_factory=DateRange)
    btype: BType | None = None
    elevator: Elevator = Elevator.UNKNOWN
    parking: Parking = Parking.UNKNOWN
    parking_public: OptBool = None
    layout: OptText = None
    brand_origin_id: OptText = None  # 跨品牌的原品牌編號

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_range(self.floor_lo, self.floor_hi, "樓層")
        if (self.site_lat is None) != (self.site_lon is None):
            raise ValueError("site_lat 與 site_lon 要同時有值或同時是 null")
        return self

    def facts_hash(self) -> str:
        """版本表用的雜湊（sha256 hex），只算本模型的欄位。"""
        payload = canonical_json(self.model_dump(mode="json")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ParkingSpace(_Model):
    """成交裡的一個車位。面積是 0.01 m²。"""

    ptype: Parking
    price_yuan: OptNonNegInt = None
    area_cm2: OptNonNegInt = None


class TxFacts(_Model):
    """一筆實價登錄成交的事實。面積是 0.01 m² 的整數。"""

    serial: ItemId
    release_id: Text
    trade_date: dt.date
    district: Text
    address_text: OptText = None
    btype: BType | None = None
    floor_lo: OptInt = None
    floor_hi: OptInt = None
    floors_total: OptPosInt = None
    main_cm2: OptNonNegInt = None
    bldg_cm2: OptNonNegInt = None
    aux_cm2: OptNonNegInt = None
    balcony_cm2: OptNonNegInt = None
    built: DateRange = Field(default_factory=DateRange)
    elevator: Elevator = Elevator.UNKNOWN
    parkings: list[ParkingSpace] = Field(default_factory=list)
    parking_n: OptNonNegInt = None
    total_yuan: NonNegInt
    flags: list[Code] = Field(default_factory=list)
    exclusion: OptCode = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_range(self.floor_lo, self.floor_hi, "樓層")
        return self


class Location(_Model):
    """定位結果。"""

    lat: OptLat = None
    lon: OptLon = None
    precision: Precision
    district: OptText = None
    conflict: bool = False
    door_id: OptText = None
    road_key: OptText = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_point(self.lat, self.lon, self.precision)
        return self


# ---------------------------------------------------------------- C2 匯出模型

Conflict = Literal["conflict"]
GText = Annotated[Text | Conflict | None, _Missing]
GBool = Annotated[bool | Conflict | None, _Missing]
GInt = Annotated[int | Conflict | None, _Missing]
GNonNegInt = Annotated[NonNegInt | Conflict | None, _Missing]
GPosInt = Annotated[PosInt | Conflict | None, _Missing]
GDate = Annotated[dt.date | Conflict | None, _Missing]
GMainKind = Annotated[MainKind | Conflict | None, _Missing]
GBType = Annotated[BType | Conflict | None, _Missing]
GElevator = Annotated[Elevator | Conflict | None, _Missing]
GParking = Annotated[Parking | Conflict | None, _Missing]

RoadFracKey = Annotated[str, StringConstraints(pattern=r"^(?:n\d{1,5}_r\d{1,5}|\d{1,3})$")]
ThresholdKey = Literal["near_m", "rca_m", "drive_min"]
SitePath = Annotated[str, StringConstraints(pattern=r"^(?:data|geo)/[a-z0-9_]+\.(?:json|geojson)$")]


class ExportLoc(_Model):
    """匯出的位置。"""

    lat: OptLat = None
    lon: OptLon = None
    precision: Precision
    district: OptText = None
    conflict: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_point(self.lat, self.lon, self.precision)
        return self


class Metrics(_Model):
    """範圍指標，不含任何門檻。

    - buffer 區域：d_zhonglu_m、d_yiwen_m、d_rca_m（整數公尺，在範圍內是 0），
      只給門牌或網站座標精度；路段精度改給 road_frac，鍵是 "n<周邊>_r<RCA>"。
    - drive 區域：in_district、drive_min（包含這點的最小車程檔，沒有就是 null）；
      路段精度改給 road_frac，鍵是 "<分鐘>"。
    - road_frac 的值是 0..1 的比例。
    """

    d_zhonglu_m: OptNonNegInt = None
    d_yiwen_m: OptNonNegInt = None
    d_rca_m: OptNonNegInt = None
    in_district: OptBool = None
    drive_min: OptPosInt = None
    road_frac: dict[RoadFracKey, Fraction] | None = None


class GroupFacts(_Model):
    """群組合併後的事實。每個欄位是值、null 或 "conflict"；built_lo/built_hi 是完工日區間。"""

    community_name: GText = None
    road_text: GText = None
    price_excl_parking: GBool = None
    parking_price_yuan: GNonNegInt = None
    main_cp: GNonNegInt = None
    main_kind: GMainKind = None
    title_area_cp: GNonNegInt = None
    floor_lo: GInt = None
    floor_hi: GInt = None
    floors_total: GPosInt = None
    built_lo: GDate = None
    built_hi: GDate = None
    btype: GBType = None
    elevator: GElevator = None
    parking: GParking = None
    parking_public: GBool = None
    layout: GText = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_range(self.floor_lo, self.floor_hi, "樓層")
        _check_range(self.built_lo, self.built_hi, "完工日區間")
        return self


class ConflictValue(_Model):
    """某個來源對衝突欄位給的值。"""

    src: Code
    value: bool | int | Text | None


class ListingMember(_Model):
    """群組裡的一筆刊登。價格是元。"""

    src: Code
    id: ItemId
    price: OptNonNegInt = None
    price_first: OptNonNegInt = None
    price_drop_at: OptUtcDateTime = None
    site_original_price: OptNonNegInt = None
    first_seen: UtcDateTime
    status: RecordStatus


class ListingGroup(_Model):
    """一戶（跨站、跨仲介合併後的群組）。price_min、price_max 是在架成員的價格範圍（元）。"""

    gid: PosInt
    first_seen: UtcDateTime
    baseline: bool
    status: RecordStatus
    gone_at: OptUtcDateTime = None
    relisted_at: OptUtcDateTime = None
    price_min: OptNonNegInt = None
    price_max: OptNonNegInt = None
    facts: GroupFacts
    conflicts: dict[str, list[ConflictValue]] = Field(default_factory=dict)
    loc: ExportLoc
    metrics: Metrics
    members: list[ListingMember] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_range(self.price_min, self.price_max, "價格")
        conflicted = {
            name for name in GroupFacts.model_fields if getattr(self.facts, name) == CONFLICT
        }
        if conflicted != set(self.conflicts):
            raise ValueError("facts 裡標 conflict 的欄位要和 conflicts 的鍵一致")
        if any(len(values) < 2 for values in self.conflicts.values()):
            raise ValueError("每個衝突欄位至少要列出兩個值")
        return self


class ListingsFile(_Model):
    """`data/listings_<area>.json`。groups 依 gid 遞增排序。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    area: Code
    groups: list[ListingGroup]

    @model_validator(mode="after")
    def _check(self) -> Self:
        gids = [group.gid for group in self.groups]
        if any(a >= b for a, b in pairwise(gids)):
            raise ValueError("groups 必須依 gid 遞增排序且不重複")
        return self


class TxParking(_Model):
    """成交裡的一個車位。price 是元，area_cm2 是 0.01 m²。"""

    ptype: Parking
    price: OptNonNegInt = None
    area_cm2: OptNonNegInt = None


class UnitPrice(_Model):
    """扣車位單價的分子與分母區間。

    q 是扣車位後的金額（元），k 是扣車位後的面積（0.01 m²）。
    上下界的交叉配對與換算只在瀏覽器做（r4 §2.2 #4）。
    """

    basis: UpBasis
    q_lo: OptNonNegInt = None
    q_hi: OptNonNegInt = None
    k_lo: OptPosInt = None
    k_hi: OptPosInt = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        values = (self.q_lo, self.q_hi, self.k_lo, self.k_hi)
        if self.basis == UpBasis.NONE:
            if any(v is not None for v in values):
                raise ValueError("basis 是 none 時，q 與 k 都要是 null")
            return self
        if any(v is None for v in values):
            raise ValueError("basis 不是 none 時，q 與 k 都要有值")
        _check_range(self.q_lo, self.q_hi, "q")
        _check_range(self.k_lo, self.k_hi, "k")
        return self


class TxRecord(_Model):
    """一筆成交。total 是元；main_cm2 是 0.01 m²；built_lo/built_hi 是完工日區間。"""

    serial: ItemId
    date: dt.date
    district: Text
    addr: OptText = None
    lat: OptLat = None
    lon: OptLon = None
    precision: Precision
    btype: BType | None = None
    floor_lo: OptInt = None
    floor_hi: OptInt = None
    floors_total: OptPosInt = None
    main_cm2: OptNonNegInt = None
    built_lo: OptDate = None
    built_hi: OptDate = None
    elevator: Elevator
    parking: list[TxParking] = Field(default_factory=list)
    total: NonNegInt
    up: UnitPrice
    flags: list[Code] = Field(default_factory=list)
    metrics: Metrics

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_point(self.lat, self.lon, self.precision)
        _check_range(self.floor_lo, self.floor_hi, "樓層")
        _check_range(self.built_lo, self.built_hi, "完工日區間")
        return self


class TxFile(_Model):
    """`data/tx_<area>.json`。tx 依 serial 遞增排序；as_of 是資料截至的日期。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    area: Code
    as_of: OptDate = None
    tx: list[TxRecord]

    @model_validator(mode="after")
    def _check(self) -> Self:
        serials = [record.serial for record in self.tx]
        if any(a >= b for a, b in pairwise(serials)):
            raise ValueError("tx 必須依 serial 遞增排序且不重複")
        return self


class SourceMeta(_Model):
    """一個來源的狀態。error_code 只放公開代碼。"""

    id: Code
    publish: bool
    last_ok: OptUtcDateTime = None
    last_full_ok: OptUtcDateTime = None
    paused_until: OptUtcDateTime = None
    error_code: ErrorCode | None = None


class LvrMeta(_Model):
    """實價登錄：最後檢查時間與資料截至日。"""

    last_check: OptUtcDateTime = None
    as_of: OptDate = None


class DoorindexMeta(_Model):
    """門牌資料：最後檢查時間與版本。"""

    last_check: OptUtcDateTime = None
    version: OptText = None


class DatasetsMeta(_Model):
    lvr: LvrMeta
    doorindex: DoorindexMeta


class AreaMeta(_Model):
    """一個區域。options 是公開的門檻選項清單，鍵是條件的門檻名稱；files 是相對於網站根目錄的路徑。"""

    id: Code
    name: Text
    kind: AreaKind
    options: dict[ThresholdKey, list[NonNegInt]]
    files: list[SitePath]


class Meta(_Model):
    """`data/meta.json`。時間都是 UTC；freshness_hours 是各來源的過期門檻（小時）。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    build_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    generated_at: UtcDateTime
    status: MetaStatus
    sources: list[SourceMeta]
    datasets: DatasetsMeta
    areas: list[AreaMeta]
    freshness_hours: dict[Code, PosInt]


EXPORT_MODELS: tuple[type[BaseModel], ...] = (Meta, ListingsFile, TxFile)


def export_json(model: BaseModel) -> str:
    """匯出檔的唯一寫法：鍵排序、allow_nan=False、結尾換行。"""
    return canonical_json(model.model_dump(mode="json")) + "\n"


def export_json_schema() -> dict[str, Any]:
    """由匯出模型產生 JSON Schema（序列化模式）。"""
    _, defs = models_json_schema(
        [(model, "serialization") for model in EXPORT_MODELS],
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "house_watch 匯出 JSON",
        "description": "由 src/house_watch/models.py 產生，不要手改。",
        "$defs": defs["$defs"],
        "anyOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in EXPORT_MODELS],
    }


def export_json_schema_text() -> str:
    return json.dumps(export_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    sys.stdout.buffer.write(export_json_schema_text().encode("utf-8"))
