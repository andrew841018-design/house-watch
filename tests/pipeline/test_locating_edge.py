"""車程區的區界判斷（事後審查 CE-6）：地址定位在區內、點只在區界外不到 DECISIVE_M 時，
照地址算在區內，不能硬判「不在區內」（district_out 是不符，不是待確認）。"""

import shapely

from house_watch.geo.doorindex import DECISIVE_M
from house_watch.geo.proj import tm2_to_wgs84
from house_watch.models import Location, Precision
from house_watch.pipeline.locating import Locator
from house_watch.settings import load_settings


def _outside_point(poly, offset_m):
    """區界多邊形外、離邊界約 offset_m 公尺的一個點（TM2）。"""
    center = poly.centroid
    for x, y in list(poly.exterior.coords)[::25]:
        dx, dy = x - center.x, y - center.y
        norm = (dx * dx + dy * dy) ** 0.5
        point = shapely.Point(x + dx / norm * offset_m, y + dy / norm * offset_m)
        if not shapely.covers(poly, point) and abs(shapely.distance(poly, point) - offset_m) < 15:
            return point
    raise AssertionError("找不到合適的點")


def test_a_point_just_outside_the_district_follows_the_address(tmp_path):
    settings = load_settings()
    area = next(a for a in settings.public.areas if a.id == "zhongli_ic")
    with Locator(settings, tmp_path) as locator:
        poly = locator.geo.area("zhongli_ic").district
        near = _outside_point(poly, 40.0)
        far = _outside_point(poly, DECISIVE_M * 5)
        for point, expected in ((near, True), (far, False)):
            lon, lat = tm2_to_wgs84(point.x, point.y)
            loc = Location(lat=lat, lon=lon, precision=Precision.SITE, district="中壢區", conflict=False)
            assert locator.metrics(loc, area, min_floors=None)["in_district"] is expected
