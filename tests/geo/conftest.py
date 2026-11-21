"""tests/geo 的 fixtures：真正的 geo 檔，加上合成城市的門牌索引（見 geo_city.py）。"""

from pathlib import Path

import pytest
from geo_city import MIN_ROWS, NOW, city_doors, csv_text

from house_watch.geo import areas
from house_watch.geo.doorindex import DoorIndex, build_index


@pytest.fixture(scope="session")
def ctx() -> areas.GeoContext:
    return areas.load()


@pytest.fixture(scope="session")
def city_csv(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("city") / "doors.csv"
    path.write_text(csv_text(city_doors()), encoding="utf-8", newline="\n")
    return path


@pytest.fixture(scope="session")
def city_index_path(tmp_path_factory, city_csv, ctx) -> Path:
    path = tmp_path_factory.mktemp("cityidx") / "doorindex.sqlite"
    build_index(city_csv, path, version="11501", now=NOW, min_rows=MIN_ROWS, ctx=ctx)
    return path


@pytest.fixture
def idx(city_index_path, ctx):
    with DoorIndex.open(city_index_path, ctx) as index:
        yield index
