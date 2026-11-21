"""比較口徑（STATUS 第 14 項）：Python 算的範圍指標和瀏覽器規則（rules.js）要用同一套口徑。

- 距離一律無條件進位到整數公尺，在範圍內是 0（Metrics 的說明）。
- 周邊：到特區的距離「≤ n」算在範圍內（含本數）。
- 避開 RCA：距離「> r」才算避開；剛好 r 是排除（含本數）。不知道距離不算避開。
- 車程：在查詢的行政區內，而且包含這點的最小車程檔「≤ m」才算在範圍內。

rules.js 的 nearCheck、rcaCheck、driveCheck、roadBuffer、roadDrive 用同樣的口徑
（另外在門檻 ±30 m 內加「接近邊界」的待確認）。這裡只固定 Python 這一側；
兩邊的對照表寫在回報裡，由整合時核對。
"""

import pytest
import shapely

from house_watch.geo import areas
from house_watch.geo.proj import tm2_to_wgs84
from house_watch.geo.shapes import ceil_m

BUFFER = "taoyuan_core"
DRIVE = "zhongli_ic"


@pytest.fixture(scope="module")
def ctx() -> areas.GeoContext:
    return areas.load()


def test_distances_round_up_to_whole_meters():
    assert ceil_m(-1.0) == 0
    assert ceil_m(0.0) == 0
    assert ceil_m(0.2) == 1
    assert ceil_m(299.01) == 300
    assert ceil_m(300.0) == 300


def test_point_inside_a_target_zone_has_distance_zero(ctx):
    zone = ctx.area(BUFFER).near["zhonglu"]
    inside = shapely.get_coordinates(shapely.point_on_surface(zone))[0]
    metrics = ctx.metrics(float(inside[0]), float(inside[1]), BUFFER)
    assert metrics["d_zhonglu_m"] == 0
    lon, lat = tm2_to_wgs84(float(inside[0]), float(inside[1]))
    assert ctx.metrics_lonlat(lon, lat, BUFFER)["d_zhonglu_m"] == 0


def test_metric_distances_are_whole_meters_rounded_up(ctx):
    zone = ctx.area(BUFFER).near["zhonglu"]
    minx, miny, maxx, maxy = zone.bounds
    x, y = maxx + 250.4, (miny + maxy) / 2  # 外框右邊 250.4 m：真正的距離不會更短
    distance = shapely.distance(zone, shapely.Point(x, y))
    value = ctx.metrics(x, y, BUFFER)["d_zhonglu_m"]
    assert isinstance(value, int)
    assert value == ceil_m(distance) and value >= distance


def _buffer_frac(ctx, rows):
    return ctx.road_frac_from_raw(rows, BUFFER)


def test_near_is_inclusive_and_rca_is_exclusive(ctx):
    options = ctx.area(BUFFER).options
    n = next(v for v in options.near_m_options if v > 0)
    r = next(v for v in options.rca_m_options if v > 0)
    key = f"n{n}_r{r}"
    assert _buffer_frac(ctx, [(n, r + 1, None, None)])[key] == 1.0, "剛好 n 公尺算在範圍內"
    assert _buffer_frac(ctx, [(n + 1, r + 1, None, None)])[key] == 0.0
    assert _buffer_frac(ctx, [(n, r, None, None)])[key] == 0.0, "到 RCA 剛好 r 公尺要排除"
    assert _buffer_frac(ctx, [(n, None, None, None)])[key] == 0.0, "不知道到 RCA 的距離不算避開"
    assert _buffer_frac(ctx, [None])[key] == 0.0, "沒有指標的門牌點算在範圍外"


def test_drive_band_needs_the_district_and_is_inclusive(ctx):
    options = ctx.area(DRIVE).options.drive_min_options
    m = options[0]
    key = str(m)
    frac = ctx.road_frac_from_raw
    assert frac([(None, None, True, m)], DRIVE)[key] == 1.0, "剛好 m 分鐘算在範圍內"
    assert frac([(None, None, True, m + 1)], DRIVE)[key] == 0.0
    assert frac([(None, None, False, m)], DRIVE)[key] == 0.0, "不在查詢的行政區裡就不算"
    assert frac([(None, None, True, None)], DRIVE)[key] == 0.0, "在每一檔之外"
    assert frac([(None, None, None, m)], DRIVE)[key] == 0.0, "不知道在不在區內就不算"
