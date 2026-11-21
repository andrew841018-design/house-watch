"""GeoJSON 的讀取、挑選與幾何小工具。

- feature 一律依屬性挑選，不依順序（R1-29）。
- 幾何讀進來先 make_valid，再投影到 TM2（公尺）；距離、面積、聯集都在 TM2 算。
- 車程檔載入時強制一層包一層：第 k 檔＝第 1 到第 k 檔的聯集（R3-04）。
- 距離一律無條件進位成整數公尺：範圍內是 0，範圍外至少 1。
  對整數門檻 n 來說，「實際距離 ≤ n」和「進位後 ≤ n」完全等價，所以單點的距離和
  路段比例用同一套整數比較，不會互相矛盾（R1-24）。
"""

import json
import math
from collections.abc import Iterable, Mapping
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

from house_watch.errors import ExitCode, HouseWatchError
from house_watch.geo.proj import geom_to_tm2, geom_to_wgs84

COORD_DIGITS = 6  # 輸出給網頁的經緯度位數（約 0.1 m）


class GeoDataError(HouseWatchError):
    """套件內的 geo 檔缺少、格式不對，或和設定對不上。"""

    exit_code = ExitCode.CONFIG


def read_geojson(source: Path | Traversable) -> dict[str, Any]:
    """讀一個 GeoJSON FeatureCollection（UTF-8）。"""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GeoDataError(f"讀不到 geo 檔：{source.name}（{type(exc).__name__}）") from None
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise GeoDataError(f"{source.name} 不是 FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list):
        raise GeoDataError(f"{source.name} 沒有 features")
    return data


def select_features(
    collection: Mapping[str, Any], match: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """屬性完全符合 match 的 feature（match 是空的就全部）。保留檔案裡的順序。"""
    selected = []
    for feature in collection["features"]:
        props = feature.get("properties") or {}
        if all(key in props and props[key] == value for key, value in (match or {}).items()):
            selected.append(feature)
    return selected


def feature_geometry(feature: Mapping[str, Any]) -> BaseGeometry:
    """feature 的 WGS84 幾何（已 make_valid）。"""
    geom = shape(feature["geometry"])
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    return geom


def union_tm2(features: Iterable[Mapping[str, Any]]) -> BaseGeometry:
    """把 feature 的幾何投影到 TM2 後聯集。"""
    parts = [geom_to_tm2(feature_geometry(feature)) for feature in features]
    if not parts:
        raise GeoDataError("沒有可以聯集的幾何")
    return shapely.union_all(parts)


def nest_bands(bands: Mapping[int, BaseGeometry]) -> dict[int, BaseGeometry]:
    """依分鐘數由小到大累積聯集，保證較小一檔完全包在較大一檔裡（R3-04）。"""
    nested: dict[int, BaseGeometry] = {}
    acc: BaseGeometry | None = None
    for minutes in sorted(bands):
        acc = bands[minutes] if acc is None else shapely.union(acc, bands[minutes])
        nested[minutes] = acc
    return nested


def ceil_m(distance: float) -> int:
    """距離（公尺）無條件進位成整數：0 還是 0，0.2 變 1。"""
    if distance <= 0:
        return 0
    return math.ceil(distance)


def _round_coords(obj: Any, digits: int) -> Any:
    if isinstance(obj, float):
        return round(obj, digits)
    if isinstance(obj, list | tuple):
        return [_round_coords(item, digits) for item in obj]
    return obj


def to_feature(
    geom_tm2: BaseGeometry, properties: Mapping[str, Any], *, digits: int = COORD_DIGITS
) -> dict[str, Any]:
    """TM2 幾何 → 給網頁畫的 WGS84 GeoJSON feature（座標取到 digits 位）。"""
    geometry = mapping(geom_to_wgs84(geom_tm2))
    rounded = {
        "type": geometry["type"],
        "coordinates": _round_coords(geometry["coordinates"], digits),
    }
    return {"type": "Feature", "properties": dict(properties), "geometry": rounded}


def lines_only(geom: BaseGeometry) -> BaseGeometry:
    """只留線（交集結果可能混著點），沒有線就回傳空的 MultiLineString。"""
    parts = [
        part
        for part in shapely.get_parts(geom).tolist()
        if part.geom_type in ("LineString", "MultiLineString") and not part.is_empty
    ]
    if not parts:
        return shapely.MultiLineString()
    return shapely.line_merge(shapely.union_all(parts))
