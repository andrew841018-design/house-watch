"""區域的幾何與範圍指標（契約 C7；指標的格式見 C2 的 metrics）。

`load()` 讀公開設定的 `[[areas]]` 與套件裡的 geo 檔，得到 `GeoContext`：

- buffer 區域（taoyuan_core）：
  - `metrics` 給每個 near／avoid 範圍的距離 `d_<範圍 id>_m`，也就是
    `d_zhonglu_m`、`d_yiwen_m`、`d_rca_m`。整數公尺、無條件進位，在範圍內是 0。
  - 路段比例的鍵是 `n<周邊>_r<RCA>`，每一種選項組合都給。一個門牌點算「在範圍內」：
    `min(d_zhonglu_m, d_yiwen_m) ≤ 周邊`，而且 `d_rca_m > RCA`（R1-14、R1-24：
    「以內」含本數；避開 RCA r 公尺＝距離 ≤ r 的排除）。
- drive 區域（zhongli_ic）：
  - `metrics` 給 `in_district`（在不在行政區多邊形內）與 `drive_min`
    （包含這點的最小車程檔，沒有就是 null；各檔已強制一層包一層）。
  - 路段比例的鍵是 `<分鐘>`：`in_district` 而且 `drive_min ≤ 分鐘`。
- 選項清單只照公開設定原樣提供，程式裡沒有、也不能有任何人選了哪一檔。

有效範圍＝外推範圍 ∩ 區域查詢的行政區（R1-15、R3-21、PLAN_v2 §4 第 4 項）：

- 每個區域都用設定裡 role 是 district 的多邊形裁切：drive 區域是中壢區界，buffer 區域是
  桃園區界（桃園區周邊 1 公里跨進蘆竹區的那一塊因此裁掉）。地圖上另外畫出裁切線。
  沒有 district 的區域載入時就失敗，不會悄悄用沒裁切的範圍。
- 點位判斷：一筆物件的區由 locate 的決策表決定（R3-01）；行政區多邊形只是其中一種證據
  （district_sides）。
- 網站的 `geo/<區域>.geojson`（site_geojson）照契約 GEO_FEATURE_ROLES 輸出 role、name、
  option、minutes。

`metrics(x, y, area)` 的 x、y 是 TM2（EPSG:3826）公尺；經緯度用 `metrics_lonlat()`。
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry.base import BaseGeometry

from house_watch.geo.proj import tm2_to_wgs84, wgs84_to_tm2
from house_watch.geo.shapes import (
    GeoDataError,
    ceil_m,
    lines_only,
    nest_bands,
    read_geojson,
    select_features,
    to_feature,
    union_tm2,
)
from house_watch.models import AreaKind
from house_watch.settings import AreaConfig, AreaOptions, PublicConfig, load_public

BUFFER_QUAD_SEGS = 16  # 畫外推範圍時，四分之一圓用幾段折線
FRACTION_DIGITS = 4  # 路段比例的小數位數

RawMetrics = tuple[int | None, int | None, bool | None, int | None]
"""一個點的原始指標：(到 near 的最短距離, 到 avoid 的最短距離, 在行政區內, 最小車程檔)。

buffer 區域只用前兩個，drive 區域只用後兩個，其餘是 None。門牌索引存的就是這四欄。
"""


def default_geo_dir() -> Traversable:
    """套件內附的 geo 資料夾（wheel 裡也一樣）。"""
    return files("house_watch").joinpath("data", "geo")


@dataclass(frozen=True)
class AreaGeo:
    """一個區域載入後的幾何（TM2）。"""

    id: str
    name: str
    kind: AreaKind
    districts: tuple[str, ...]  # 要查詢的行政區名稱
    options: AreaOptions
    near: dict[str, BaseGeometry] = field(default_factory=dict)  # 範圍 id → 幾何
    avoid: dict[str, BaseGeometry] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # 範圍 id → 顯示名稱
    district: BaseGeometry | None = None  # 查詢的行政區多邊形（範圍裁到這裡）
    bands: dict[int, BaseGeometry] = field(default_factory=dict)  # drive：分鐘 → 一層包一層的範圍
    markers: tuple[dict[str, Any], ...] = ()  # 地圖標記（原樣的 WGS84 feature）
    near_union: BaseGeometry | None = None
    avoid_union: BaseGeometry | None = None

    def road_frac_keys(self) -> list[str]:
        """路段比例的鍵，順序固定。"""
        if self.kind == AreaKind.BUFFER:
            near = self.options.near_m_options or []
            rca = self.options.rca_m_options or [0]
            return [f"n{n}_r{r}" for n in near for r in rca]
        return [str(m) for m in self.options.drive_min_options or []]


class GeoContext:
    """所有區域的幾何。由 `load()` 建立。"""

    def __init__(self, areas: Sequence[AreaGeo], version: str, zip_districts: dict[str, str]):
        self._areas = {area.id: area for area in areas}
        self._order = [area.id for area in areas]
        self.version = version
        self.zip_districts = dict(zip_districts)  # 公開設定裡的郵遞區號 → 行政區名稱
        for area in areas:
            for geom in (*area.near.values(), *area.avoid.values(), *area.bands.values()):
                shapely.prepare(geom)
            if area.district is not None:
                shapely.prepare(area.district)

    @property
    def area_ids(self) -> list[str]:
        return list(self._order)

    def area(self, area_id: str) -> AreaGeo:
        try:
            return self._areas[area_id]
        except KeyError:
            raise GeoDataError(f"沒有這個區域：{area_id}") from None

    @property
    def districts(self) -> list[str]:
        """所有區域查詢的行政區名稱（不重複，依設定順序）。"""
        seen: list[str] = []
        for area_id in self._order:
            for name in self._areas[area_id].districts:
                if name not in seen:
                    seen.append(name)
        return seen

    # ------------------------------------------------------------ 指標

    def metrics(self, x: float, y: float, area: str) -> dict[str, Any]:
        """一個點（TM2 公尺）的範圍指標，格式照 C2 的 metrics。門牌點與網站座標共用。"""
        geo = self.area(area)
        point = shapely.Point(x, y)
        if geo.kind == AreaKind.BUFFER:
            out: dict[str, Any] = {}
            for ref_id, geom in (*geo.near.items(), *geo.avoid.items()):
                out[f"d_{ref_id}_m"] = ceil_m(shapely.distance(geom, point))
            return out
        raw = self.raw_metrics([x], [y], area)[0]
        return {"in_district": raw[2], "drive_min": raw[3]}

    def metrics_lonlat(self, lon: float, lat: float, area: str) -> dict[str, Any]:
        """同 `metrics()`，輸入是經緯度。"""
        x, y = wgs84_to_tm2(lon, lat)
        return self.metrics(x, y, area)

    def raw_metrics(self, xs: Sequence[float], ys: Sequence[float], area: str) -> list[RawMetrics]:
        """多個點（TM2）的原始指標，一次向量化算完。門牌索引用它預先算好每個門牌點。"""
        geo = self.area(area)
        if not xs:
            return []
        points = shapely.points(list(xs), list(ys))
        n = len(xs)
        if geo.kind == AreaKind.BUFFER:
            near = _min_ceil([shapely.distance(g, points).tolist() for g in geo.near.values()], n)
            avoid = (
                _min_ceil([shapely.distance(g, points).tolist() for g in geo.avoid.values()], n)
                if geo.avoid
                else [None] * n
            )
            return [(near[i], avoid[i], None, None) for i in range(n)]
        if geo.district is None:
            raise GeoDataError(f"{area} 沒有行政區多邊形")
        inside = shapely.covers(geo.district, points).tolist()
        drive: list[int | None] = [None] * n
        for minutes in sorted(geo.bands, reverse=True):
            covered = shapely.covers(geo.bands[minutes], points).tolist()
            for i in range(n):
                if covered[i]:
                    drive[i] = minutes
        return [(None, None, bool(inside[i]), drive[i]) for i in range(n)]

    def road_frac_from_raw(self, rows: Sequence[RawMetrics | None], area: str) -> dict[str, float]:
        """一組門牌點的原始指標 → 各選項組合「在範圍內」的比例。None 的列算在範圍外。"""
        geo = self.area(area)
        total = len(rows)
        if total == 0:
            return {}
        out: dict[str, float] = {}
        if geo.kind == AreaKind.BUFFER:
            for n in geo.options.near_m_options or []:
                for r in geo.options.rca_m_options or [0]:
                    hits = sum(1 for row in rows if row is not None and _in_buffer(row, n, r, geo))
                    out[f"n{n}_r{r}"] = round(hits / total, FRACTION_DIGITS)
            return out
        for m in geo.options.drive_min_options or []:
            hits = sum(
                1
                for row in rows
                if row is not None and row[2] and row[3] is not None and row[3] <= m
            )
            out[str(m)] = round(hits / total, FRACTION_DIGITS)
        return out

    def road_frac_points(
        self, xs: Sequence[float], ys: Sequence[float], area: str
    ) -> dict[str, float]:
        """直接用點位算路段比例（不經門牌索引；測試與除錯用）。"""
        return self.road_frac_from_raw(self.raw_metrics(xs, ys, area), area)

    # ------------------------------------------------------------ 有效範圍與地圖

    def effective(self, area: str, option: int) -> BaseGeometry:
        """某個選項的有效範圍（TM2）：外推範圍 ∩ 查詢的行政區。

        buffer：option 是周邊公尺數；drive：option 是分鐘數。
        """
        geo = self.area(area)
        if geo.kind == AreaKind.BUFFER:
            if option not in (geo.options.near_m_options or []):
                raise GeoDataError(f"{area} 沒有周邊 {option} 的選項")
            return _clip(self._unclipped_buffer(geo, option), geo)
        if option not in geo.bands:
            raise GeoDataError(f"{area} 沒有 {option} 分鐘的車程檔")
        return _clip(geo.bands[option], geo)

    def clip_line(self, area: str) -> BaseGeometry:
        """最大選項的範圍被行政區切掉的那段線（TM2）。沒有被切到就是空的。"""
        geo = self.area(area)
        if geo.kind == AreaKind.BUFFER:
            widest = self._unclipped_buffer(geo, max(geo.options.near_m_options or [0]))
        else:
            widest = geo.bands[max(geo.bands)]
        assert geo.district is not None
        return lines_only(shapely.intersection(shapely.boundary(geo.district), widest))

    def district_sides(self, x: float, y: float, margin_m: float = 100.0) -> list[tuple[str, bool]]:
        """座標（TM2）和設定裡完整行政區多邊形（drive 區域的 district）的關係。

        回傳 (區名, 在區內?)，只列離區界至少 margin_m 的區；太近的不算決定性證據（R3-01）。
        """
        point = shapely.Point(x, y)
        out: list[tuple[str, bool]] = []
        for area_id in self._order:
            geo = self._areas[area_id]
            if geo.district is None or len(geo.districts) != 1:
                continue
            name = geo.districts[0]
            if any(name == seen for seen, _ in out):
                continue
            edge = shapely.distance(shapely.boundary(geo.district), point)
            if edge >= margin_m:
                out.append((name, bool(shapely.covers(geo.district, point))))
        return out

    def query_bbox(self, area: str, margin_m: float = 500.0) -> tuple[float, float, float, float]:
        """最大選項範圍外加 margin 的外框，回傳 (south, west, north, east) 經緯度。"""
        geo = self.area(area)
        if geo.kind == AreaKind.BUFFER:
            widest = self._unclipped_buffer(geo, max(geo.options.near_m_options or [0]))
        else:
            widest = geo.bands[max(geo.bands)]
        minx, miny, maxx, maxy = shapely.buffer(widest, margin_m).bounds
        corners = [tm2_to_wgs84(x, y) for x in (minx, maxx) for y in (miny, maxy)]
        lons = [lon for lon, _ in corners]
        lats = [lat for _, lat in corners]
        return (min(lats), min(lons), max(lats), max(lons))

    def site_geojson(self, area: str) -> dict[str, Any]:
        """網站 `geo/<area>.geojson` 的內容：範圍、各選項的有效範圍、裁切線、標記。

        每個選項都用一樣的屬性格式輸出，不標示任何人選了哪一檔（R2-05）。
        """
        geo = self.area(area)
        features: list[dict[str, Any]] = []
        if geo.kind == AreaKind.BUFFER:
            for role, refs in (("near", geo.near), ("avoid", geo.avoid)):
                for ref_id, geom in refs.items():
                    features.append(to_feature(geom, {"role": role, "name": geo.names[ref_id]}))
            for n in geo.options.near_m_options or []:
                props = {"role": "near_band", "option": n}
                features.append(to_feature(self.effective(area, n), props))
            avoid = geo.avoid_union
            if avoid is not None:
                for r in geo.options.rca_m_options or []:
                    shape_r = shapely.buffer(avoid, r, quad_segs=BUFFER_QUAD_SEGS) if r else avoid
                    features.append(to_feature(shape_r, {"role": "avoid_band", "option": r}))
        else:
            assert geo.district is not None
            props = {"role": "district", "name": "、".join(geo.districts)}
            features.append(to_feature(geo.district, props))
            for m in geo.options.drive_min_options or []:
                props = {"role": "drive", "minutes": m}
                features.append(to_feature(self.effective(area, m), props))
        line = self.clip_line(area)
        if not line.is_empty:
            note = f"範圍超出{'、'.join(geo.districts)}的部分已裁掉（只查詢這些行政區）"
            features.append(to_feature(line, {"role": "clip", "name": note}))
        for marker in geo.markers:
            name = (marker.get("properties") or {}).get("name")
            props = {"role": "marker", "name": str(name) if name else ""}
            features.append(
                {"type": "Feature", "properties": props, "geometry": marker["geometry"]}
            )
        return {"type": "FeatureCollection", "area": area, "features": features}

    def _unclipped_buffer(self, geo: AreaGeo, meters: int) -> BaseGeometry:
        near = geo.near_union
        assert near is not None
        if meters <= 0:
            return near
        return shapely.buffer(near, meters, quad_segs=BUFFER_QUAD_SEGS)


def _min_ceil(columns: list[list[float]], n: int) -> list[int | None]:
    out: list[int | None] = []
    for i in range(n):
        values = [column[i] for column in columns]
        out.append(ceil_m(min(values)) if values else None)
    return out


def _in_buffer(row: RawMetrics, near_m: int, rca_m: int, geo: AreaGeo) -> bool:
    near, avoid = row[0], row[1]
    if near is None or near > near_m:
        return False
    if geo.avoid and (avoid is None or avoid <= rca_m):
        return False
    return True


def _clip(geom: BaseGeometry, geo: AreaGeo) -> BaseGeometry:
    assert geo.district is not None  # 載入時已保證每個區域都有 district
    return shapely.intersection(geom, geo.district)


# ---------------------------------------------------------------- 載入


def _sha256(source: Path | Traversable) -> str:
    return hashlib.sha256(source.read_bytes()).hexdigest()


def _load_area(area: AreaConfig, geo_dir: Path | Traversable, used: dict[str, str]) -> AreaGeo:
    near: dict[str, BaseGeometry] = {}
    avoid: dict[str, BaseGeometry] = {}
    names: dict[str, str] = {}
    district: BaseGeometry | None = None
    bands: dict[int, BaseGeometry] = {}
    markers: list[dict[str, Any]] = []
    for ref_id, ref in area.geo.items():
        source = geo_dir.joinpath(ref.file)
        if not source.is_file():
            raise GeoDataError(f"{area.id}：找不到 geo 檔 {ref.file}")
        used[ref.file] = _sha256(source)
        selected = select_features(read_geojson(source), ref.match)
        if not selected:
            raise GeoDataError(f"{area.id}.{ref_id}：{ref.file} 裡沒有符合條件的 feature")
        if ref.role in ("near", "avoid"):
            target = near if ref.role == "near" else avoid
            target[ref_id] = union_tm2(selected)
            first = selected[0].get("properties") or {}
            names[ref_id] = str(first.get("name") or ref_id)
        elif ref.role == "district":
            district = union_tm2(selected)
        elif ref.role == "drive":
            assert ref.band_property is not None
            by_minutes: dict[int, list[dict[str, Any]]] = {}
            for feature in selected:
                value = (feature.get("properties") or {}).get(ref.band_property)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise GeoDataError(f"{area.id}.{ref_id}：{ref.band_property} 不是正整數")
                by_minutes.setdefault(value, []).append(feature)
            bands = nest_bands({m: union_tm2(fs) for m, fs in by_minutes.items()})
        elif ref.role == "marker":
            markers.extend(selected)
    if district is None:
        raise GeoDataError(
            f"{area.id}：沒有 role 是 district 的行政區界，無法把範圍裁到查詢的行政區"
        )
    if area.kind == AreaKind.DRIVE:
        missing = set(area.options.drive_min_options or []) - set(bands)
        if missing:
            raise GeoDataError(f"{area.id}：車程檔缺少 {sorted(missing)} 分鐘")
    return AreaGeo(
        id=area.id,
        name=area.name,
        kind=area.kind,
        districts=tuple(d.name for d in area.districts),
        options=area.options,
        near=near,
        avoid=avoid,
        names=names,
        district=district,
        bands=bands,
        markers=tuple(markers),
        near_union=shapely.union_all(list(near.values())) if near else None,
        avoid_union=shapely.union_all(list(avoid.values())) if avoid else None,
    )


def _version(areas: Iterable[AreaConfig], used: dict[str, str]) -> str:
    payload = {
        "areas": [area.model_dump(mode="json") for area in areas],
        "files": dict(sorted(used.items())),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load(
    geo_dir: Path | Traversable | None = None, *, config: PublicConfig | None = None
) -> GeoContext:
    """讀公開設定的區域與 geo 檔，回傳 GeoContext（契約 C7 的 `areas.load`）。

    geo_dir 沒給就用套件內附的資料夾；config 沒給就讀套件的 default.toml。
    """
    config = config if config is not None else load_public()
    geo_dir = geo_dir if geo_dir is not None else default_geo_dir()
    used: dict[str, str] = {}
    loaded = [_load_area(area, geo_dir, used) for area in config.areas]
    zips = {d.zip: d.name for area in config.areas for d in area.districts}
    return GeoContext(loaded, _version(config.areas, used), zips)
