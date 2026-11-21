"""TWD97／TM2 121°（EPSG:3826）與 WGS84 互換，純 Python，不需要 pyproj（r4 §3.9）。

- 橢球 GRS80：a = 6378137，f = 1/298.257222101。
- k0 = 0.9999，中央經線 121°E，東偏移 250000 m，北偏移 0。
- TWD97 和 WGS84 相差不到 1 m，不另做基準轉換（R3b §2.4）。
- 反算公式取自 R3b 原型（Snyder 1987，USGS PP1395 式 8-18～8-25，和 pyproj 比對 306 點，
  最大差 0.06 mm）；正算用同一本的式 8-9、8-10。
- 距離與面積一律在 TM2 平面上算，單位公尺。

座標順序一律是 (x, y) 與 (lon, lat)，和 GeoJSON 一樣「經度在前」。
"""

import math

import shapely
from shapely.geometry.base import BaseGeometry

_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9999
_LON0 = math.radians(121.0)
_FE = 250000.0

# 子午線弧長的係數（Snyder 式 3-21）
_M1 = 1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256
_M2 = 3 * _E2 / 8 + 3 * _E2**2 / 32 + 45 * _E2**3 / 1024
_M3 = 15 * _E2**2 / 256 + 45 * _E2**3 / 1024
_M4 = 35 * _E2**3 / 3072
_E1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))


def wgs84_to_tm2(lon: float, lat: float) -> tuple[float, float]:
    """(經度, 緯度) → TM2 的 (x, y)，單位公尺。"""
    phi = math.radians(lat)
    sin_phi, cos_phi, tan_phi = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _A / math.sqrt(1 - _E2 * sin_phi * sin_phi)
    t = tan_phi * tan_phi
    c = _EP2 * cos_phi * cos_phi
    a = cos_phi * (math.radians(lon) - _LON0)
    m = _A * (
        _M1 * phi - _M2 * math.sin(2 * phi) + _M3 * math.sin(4 * phi) - _M4 * math.sin(6 * phi)
    )
    x = _FE + _K0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a**5 / 120
    )
    y = _K0 * (
        m
        + n
        * tan_phi
        * (
            a * a / 2
            + (5 - t + 9 * c + 4 * c * c) * a**4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a**6 / 720
        )
    )
    return x, y


def tm2_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """TM2 的 (x, y) → (經度, 緯度)。"""
    x = x - _FE
    mu = (y / _K0) / (_A * _M1)
    phi1 = (
        mu
        + (3 * _E1 / 2 - 27 * _E1**3 / 32) * math.sin(2 * mu)
        + (21 * _E1**2 / 16 - 55 * _E1**4 / 32) * math.sin(4 * mu)
        + (151 * _E1**3 / 96) * math.sin(6 * mu)
        + (1097 * _E1**4 / 512) * math.sin(8 * mu)
    )
    s, c, t = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * c * c
    t1 = t * t
    n1 = _A / math.sqrt(1 - _E2 * s * s)
    r1 = _A * (1 - _E2) / (1 - _E2 * s * s) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * t / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lon = (
        _LON0
        + (
            d
            - (1 + 2 * t1 + c1) * d**3 / 6
            + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
        )
        / c
    )
    return math.degrees(lon), math.degrees(lat)


def _apply(fn, coords) -> list[list[float]]:
    return [list(fn(float(u), float(v))) for u, v in coords.tolist()]


def geom_to_tm2[G: BaseGeometry](geom: G) -> G:
    """把 WGS84 的 shapely 幾何投影到 TM2。"""
    return shapely.transform(geom, lambda coords: _apply(wgs84_to_tm2, coords))


def geom_to_wgs84[G: BaseGeometry](geom: G) -> G:
    """把 TM2 的 shapely 幾何換回 WGS84。"""
    return shapely.transform(geom, lambda coords: _apply(tm2_to_wgs84, coords))


def distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """兩個 WGS84 點在 TM2 平面上的距離（公尺）。桃園範圍內的投影誤差約萬分之一。"""
    x1, y1 = wgs84_to_tm2(lon1, lat1)
    x2, y2 = wgs84_to_tm2(lon2, lat2)
    return math.hypot(x2 - x1, y2 - y1)
