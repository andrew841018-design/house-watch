"""TWD97／TM2 ↔ WGS84（proj.py）。"""

import itertools
import math

import pytest
import shapely

from house_watch.geo.proj import (
    distance_m,
    geom_to_tm2,
    geom_to_wgs84,
    tm2_to_wgs84,
    wgs84_to_tm2,
)

# 環境部開放資料 CDX_SitePoint 的同一個點，同時給了 TWD97 與 WGS84（RCA 場址代表點）。
OFFICIAL_TM2 = (278085.00001, 2764256.00001)
OFFICIAL_LONLAT = (121.2782023901701, 24.986002702819327)


def test_inverse_matches_official_pair():
    lon, lat = tm2_to_wgs84(*OFFICIAL_TM2)
    assert distance_m(lon, lat, *OFFICIAL_LONLAT) < 0.001


def test_forward_matches_official_pair():
    x, y = wgs84_to_tm2(*OFFICIAL_LONLAT)
    assert math.hypot(x - OFFICIAL_TM2[0], y - OFFICIAL_TM2[1]) < 0.001


@pytest.mark.parametrize(
    ("lon", "lat"),
    list(itertools.product([120.95, 121.0, 121.2, 121.45], [24.6, 24.9, 25.1])),
)
def test_round_trip_is_sub_millimetre(lon, lat):
    x, y = wgs84_to_tm2(lon, lat)
    lon2, lat2 = tm2_to_wgs84(x, y)
    x2, y2 = wgs84_to_tm2(lon2, lat2)
    assert math.hypot(x2 - x, y2 - y) < 0.001


def test_central_meridian_maps_to_false_easting():
    x, _ = wgs84_to_tm2(121.0, 25.0)
    assert x == pytest.approx(250000.0, abs=1e-6)


def test_geometry_projection_round_trip():
    square = shapely.box(121.29, 25.0, 121.30, 25.01)
    projected = geom_to_tm2(square)
    # 0.01° × 0.01° 在北緯 25 度約 1.01 km × 1.11 km
    assert 1.05e6 < projected.area < 1.20e6
    back = geom_to_wgs84(projected)
    assert shapely.equals_exact(back, square, tolerance=1e-9)
