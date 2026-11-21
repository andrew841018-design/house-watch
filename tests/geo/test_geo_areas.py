"""區域幾何與範圍指標（areas.py、shapes.py），用套件裡真正的 geo 檔。"""

import json
import re
import shutil
from pathlib import Path

import pytest
import shapely

from house_watch.geo import areas
from house_watch.geo.proj import wgs84_to_tm2
from house_watch.geo.shapes import GeoDataError, ceil_m, nest_bands
from house_watch.models import AreaKind, Metrics
from house_watch.settings import load_public

GEO_DIR = Path(areas.default_geo_dir())

THEATRE = (121.2986473, 25.0174685)  # 桃園展演中心（門牌點，R3b）
RCA_SITE = (121.2782023901701, 24.986002702819327)  # RCA 官方場址代表點
ZHONGLU_PLACE = (121.28631, 24.9919)  # OSM「中路」地名點，離管制區約 669 m（R3a）


def _copy_geo(tmp_path: Path) -> Path:
    target = tmp_path / "geo"
    shutil.copytree(GEO_DIR, target)
    return target


# ---------------------------------------------------------------- 載入


def test_load_default_context(ctx):
    assert ctx.area_ids == ["taoyuan_core", "zhongli_ic"]
    core, ic = ctx.area("taoyuan_core"), ctx.area("zhongli_ic")
    assert core.kind == AreaKind.BUFFER and ic.kind == AreaKind.DRIVE
    assert core.districts == ("桃園區",) and ic.districts == ("中壢區",)
    assert ctx.zip_districts == {"330": "桃園區", "320": "中壢區"}
    assert len(ic.markers) == 9
    assert areas.load().version == ctx.version


def test_yiwen_is_picked_by_name(ctx):
    yiwen = ctx.area("taoyuan_core").near["yiwen"]
    # 藝文里約 25.54 公頃；核心街廓 6.23、廣義 18.95（R3a）
    assert yiwen.area / 1e4 == pytest.approx(25.54, abs=0.05)


def test_yiwen_feature_order_does_not_matter(tmp_path, ctx):
    geo_dir = _copy_geo(tmp_path)
    path = geo_dir / "yiwen.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["features"].reverse()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    other = areas.load(geo_dir)
    assert shapely.equals(
        other.area("taoyuan_core").near["yiwen"], ctx.area("taoyuan_core").near["yiwen"]
    )


def test_missing_named_feature_fails(tmp_path):
    geo_dir = _copy_geo(tmp_path)
    path = geo_dir / "yiwen.geojson"
    match = load_public().areas[0].geo["yiwen"].match["name"]
    data = json.loads(path.read_text(encoding="utf-8"))
    for feature in data["features"]:
        if feature["properties"]["name"] == match:
            feature["properties"]["name"] = "改過的名字"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GeoDataError):
        areas.load(geo_dir)


def test_missing_drive_band_fails(tmp_path):
    geo_dir = _copy_geo(tmp_path)
    path = geo_dir / "zhongli_ic_drive.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    top = max(f["properties"]["minutes"] for f in data["features"])
    data["features"] = [f for f in data["features"] if f["properties"]["minutes"] != top]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GeoDataError):
        areas.load(geo_dir)


def test_missing_clip_data_fails(tmp_path):
    geo_dir = _copy_geo(tmp_path)
    (geo_dir / "taoyuan_neighbors.geojson").unlink()
    with pytest.raises(GeoDataError):
        areas.load(geo_dir)


# ---------------------------------------------------------------- 已知地標與距離


def test_theatre_is_inside_yiwen(ctx):
    metrics = ctx.metrics_lonlat(*THEATRE, "taoyuan_core")
    assert metrics["d_yiwen_m"] == 0
    assert metrics["d_zhonglu_m"] > 0 and metrics["d_rca_m"] > 0


def test_official_rca_point_is_inside_restricted_area(ctx):
    assert ctx.metrics_lonlat(*RCA_SITE, "taoyuan_core")["d_rca_m"] == 0


def test_known_distances(ctx):
    core = ctx.area("taoyuan_core")
    # R3a §6：管制區到中路特區（中心線外框）邊界最近 989 m
    assert shapely.distance(core.avoid["rca"], core.near["zhonglu"]) == pytest.approx(989, abs=2)
    # OSM「中路」地名點離管制區約 669 m
    assert ctx.metrics_lonlat(*ZHONGLU_PLACE, "taoyuan_core")["d_rca_m"] == pytest.approx(
        669, abs=3
    )


def test_metric_keys_match_export_model(ctx):
    point = wgs84_to_tm2(*THEATRE)
    for area in ctx.area_ids:
        Metrics.model_validate(ctx.metrics(*point, area))
        frac = ctx.road_frac_points([point[0]], [point[1]], area)
        Metrics.model_validate({"road_frac": frac})
        assert list(frac) == ctx.area(area).road_frac_keys()


def test_distances_round_up():
    assert ceil_m(0.0) == 0
    assert ceil_m(0.2) == 1
    assert ceil_m(299.0001) == 300
    assert ceil_m(300.0) == 300


def test_point_just_outside_is_never_zero(ctx):
    zhonglu = ctx.area("taoyuan_core").near["zhonglu"]
    minx, miny, _, _ = zhonglu.bounds
    point = (minx - 0.3, miny - 0.3)
    assert ctx.metrics(*point, "taoyuan_core")["d_zhonglu_m"] >= 1


# ---------------------------------------------------------------- 車程檔


def test_drive_bands_are_nested(ctx):
    bands = ctx.area("zhongli_ic").bands
    minutes = sorted(bands)
    assert minutes == ctx.area("zhongli_ic").options.drive_min_options
    for small, large in zip(minutes, minutes[1:], strict=False):
        assert shapely.difference(bands[small], bands[large]).area < 1e-6


def test_nest_bands_enforces_containment():
    a = shapely.box(0, 0, 2, 2)
    b = shapely.box(1, 1, 3, 3)  # 沒有包住 a
    nested = nest_bands({6: a, 8: b})
    assert nested[8].covers(nested[6])


def test_entry_points_are_in_the_smallest_band(ctx):
    smallest = min(ctx.area("zhongli_ic").options.drive_min_options)
    for marker in ctx.area("zhongli_ic").markers:
        props = marker["properties"]
        point = (props["routing_point_lon"], props["routing_point_lat"])
        assert ctx.metrics_lonlat(*point, "zhongli_ic")["drive_min"] == smallest


def test_far_point_has_no_drive_band(ctx):
    metrics = ctx.metrics_lonlat(*THEATRE, "zhongli_ic")
    assert metrics == {"in_district": False, "drive_min": None}


# ---------------------------------------------------------------- 有效範圍與裁切


def test_widest_near_option_is_clipped_to_the_district(ctx):
    core = ctx.area("taoyuan_core")
    options = core.options.near_m_options
    widest = max(options)
    for option in options:
        effective = ctx.effective("taoyuan_core", option)
        unclipped = (
            core.near_union
            if option == 0
            else shapely.buffer(core.near_union, option, quad_segs=16)
        )
        lost = unclipped.area - effective.area
        if option == widest:
            assert lost > 1e5  # 跨進蘆竹區的那一塊（約 0.5 km²）
        else:
            assert lost < 1.0
        # 有效範圍只碰到查詢的行政區（R1-15、R3-21）
        assert shapely.intersection(effective, core.clip_exclude).area < 1.0


def test_clip_line_is_drawn_and_noted(ctx):
    assert ctx.clip_line("taoyuan_core").length > 500
    site = ctx.site_geojson("taoyuan_core")
    lines = [f for f in site["features"] if f["properties"]["layer"] == "clip_line"]
    assert len(lines) == 1 and "桃園區" in lines[0]["properties"]["note"]


def test_drive_ranges_stay_inside_the_district(ctx):
    district = ctx.area("zhongli_ic").district
    for option in ctx.area("zhongli_ic").options.drive_min_options:
        assert shapely.difference(ctx.effective("zhongli_ic", option), district).area < 1.0
    assert not ctx.clip_line("zhongli_ic").is_empty


def test_unknown_option_is_rejected(ctx):
    with pytest.raises(GeoDataError):
        ctx.effective("taoyuan_core", 123)
    with pytest.raises(GeoDataError):
        ctx.effective("zhongli_ic", 7)


def test_site_geojson_lists_every_option_the_same_way(ctx):
    for area in ctx.area_ids:
        geo = ctx.area(area)
        site = ctx.site_geojson(area)
        ranges = [f["properties"] for f in site["features"] if f["properties"]["layer"] == "range"]
        expected = geo.options.near_m_options or geo.options.drive_min_options
        assert [r["option"] for r in ranges] == expected
        assert all(set(r) == {"layer", "option_key", "option"} for r in ranges)
        text = json.dumps(site, ensure_ascii=False)
        assert not re.search(r"selected|chosen|default|profile", text, re.IGNORECASE)


def test_query_bbox_covers_the_widest_option(ctx):
    south, west, north, east = ctx.query_bbox("taoyuan_core")
    assert south < THEATRE[1] < north and west < THEATRE[0] < east


# ---------------------------------------------------------------- 路段比例的規則


def test_road_frac_semantics_buffer(ctx):
    geo = ctx.area("taoyuan_core")
    near_opts, rca_opts = geo.options.near_m_options, geo.options.rca_m_options
    n, r = near_opts[1], rca_opts[1]
    rows = [
        (n, r + 1, None, None),  # 剛好在周邊上（含本數）、在 RCA 外 → 在範圍內
        (n, r, None, None),  # 離 RCA 剛好 r → 排除
        (n + 1, 10_000, None, None),  # 超出周邊
        None,  # 沒有指標的門牌算在範圍外
    ]
    frac = ctx.road_frac_from_raw(rows, "taoyuan_core")
    assert frac[f"n{n}_r{r}"] == 0.25
    assert frac[f"n{n}_r{rca_opts[0]}"] == 0.5
    assert frac[f"n{near_opts[-1]}_r{rca_opts[0]}"] == 0.75
    assert list(frac) == geo.road_frac_keys()


def test_road_frac_semantics_drive(ctx):
    opts = ctx.area("zhongli_ic").options.drive_min_options
    rows = [
        (None, None, True, opts[0]),
        (None, None, True, opts[-1]),
        (None, None, False, opts[0]),  # 不在行政區內
        (None, None, True, None),
    ]
    frac = ctx.road_frac_from_raw(rows, "zhongli_ic")
    assert frac[str(opts[0])] == 0.25
    assert frac[str(opts[-1])] == 0.5
    assert ctx.road_frac_from_raw([], "zhongli_ic") == {}


# ---------------------------------------------------------------- 套件裡的資料檔


def test_packaged_geo_files_have_no_local_paths():
    pattern = re.compile(r"/Users/|/private/|/Volumes/|/home/|/tmp/|file:|[A-Za-z]:\\\\")
    files = sorted(GEO_DIR.glob("*.geojson"))
    assert len(files) == 8
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), path.name
        assert "Profile C" not in text


def test_attribution_covers_every_geo_file():
    attribution = (GEO_DIR / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for path in GEO_DIR.glob("*.geojson"):
        assert f"`{path.name}`" in attribution
    assert "ODbL" in attribution and "政府資料開放授權條款" in attribution
