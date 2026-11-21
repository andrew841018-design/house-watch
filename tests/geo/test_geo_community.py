"""社區名定位（community.py）。OSM 名稱表的更新只用 httpx.MockTransport 測，不連 Overpass。"""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from geo_city import FIXTURES, NOW

from house_watch import __version__
from house_watch.db import init_db
from house_watch.errors import ErrorCode
from house_watch.geo.community import (
    CACHE_NAME,
    CommunityError,
    CommunityLookup,
    CommunityPoint,
    OsmNames,
    SiteObservation,
    cross_site_point,
    is_residential,
    load_community_points,
    normalize_name,
    overpass_query,
    parse_overpass,
    refresh_osm_names,
    save_community_points,
)

PAYLOAD = json.loads((FIXTURES / "overpass_min.json").read_text(encoding="utf-8"))
BBOX = (24.98, 121.27, 25.03, 121.32)


def test_normalize_name():
    assert normalize_name("示範 晴空") == normalize_name("示範晴空社區") == "示範晴空"
    assert normalize_name("ＡＢＣ．大樓") == normalize_name("abc大樓")
    assert normalize_name("　") is None and normalize_name(None) is None


def test_only_exact_names_match():
    names = OsmNames(parse_overpass(PAYLOAD))
    assert names.lookup("示範晴空") is not None
    assert names.lookup("示範晴空SMART") is None  # 不做子字串比對
    assert names.lookup("示範晴") is None


def test_only_residential_objects_are_kept():
    items = parse_overpass(PAYLOAD)
    names = {item["name"] for item in items}
    assert "示範國小" not in names and "示範商店" not in names  # 學校、商店不算社區
    assert "沒有中心點" not in names
    assert is_residential({"landuse": "residential"})
    assert not is_residential({"building": "school"})
    keys = [(item["name"], item["osm"]) for item in items]
    assert keys == sorted(keys)


def test_same_name_far_apart_is_ambiguous():
    names = OsmNames(parse_overpass(PAYLOAD))
    assert names.lookup("示範雲頂") is None  # 兩處同名，相距好幾公里
    found = names.lookup("示範湖畔")
    assert found == CommunityPoint("示範湖畔", 25.003, 121.294, "osm_name", 1)


def test_cross_site_point():
    obs = [
        SiteObservation("cthouse", "示範大廈", 25.0, 121.3),
        SiteObservation("hbhousing", "示範 大廈", 25.0005, 121.3),
    ]
    point = cross_site_point("示範大廈", obs)
    assert point.method == "cross_site" and point.n == 2
    far = [*obs, SiteObservation("etwarm", "示範大廈", 25.02, 121.3)]
    assert cross_site_point("示範大廈", far) is None
    assert cross_site_point("別的名字", obs) is None


def test_lookup_order_osm_first():
    lookup = CommunityLookup(
        OsmNames(parse_overpass(PAYLOAD)),
        [SiteObservation("cthouse", "示範湖畔", 24.0, 121.0)],
    )
    assert lookup.find("rakuya", "1", "示範湖畔").method == "osm_name"
    assert CommunityLookup().find("rakuya", "1", "示範湖畔") is None
    assert CommunityLookup().find("rakuya", "1", None) is None


def test_cache_is_empty_until_refreshed(tmp_path):
    assert len(OsmNames.load(tmp_path)) == 0


class Overpass:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_refresh_writes_cache(tmp_path):
    server = Overpass(httpx.Response(200, json=PAYLOAD), httpx.Response(200, json=PAYLOAD))
    waits: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        count = refresh_osm_names(client, tmp_path, NOW, [BBOX, BBOX], sleep=waits.append)
    assert count == len(parse_overpass(PAYLOAD))
    assert waits == [4.0]  # 第二個請求前等 4 秒
    request = server.requests[0]
    assert request.method == "POST" and request.url.host == "overpass-api.de"
    assert request.headers["user-agent"] == f"house-watch/{__version__}"
    query = parse_qs(request.content.decode("utf-8"))["data"][0]
    assert query == overpass_query(BBOX) and "out tags center" in query
    cache = json.loads((tmp_path / CACHE_NAME).read_text(encoding="utf-8"))
    assert cache["fetched_at"] == "2026-01-05T03:00:00Z" and "ODbL" in cache["license"]
    assert OsmNames.load(tmp_path).lookup("示範晴空").n == 2


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), ErrorCode.RATE_LIMITED),
        (httpx.Response(403), ErrorCode.BLOCKED),
        (httpx.Response(504), ErrorCode.HTTP_5XX),
        (httpx.Response(200, text="not json"), ErrorCode.PAGE_INVALID),
    ],
)
def test_refresh_stops_and_keeps_the_old_cache(tmp_path, response, code):
    (tmp_path / CACHE_NAME).write_text('{"version": 1, "items": []}', encoding="utf-8")
    before = (tmp_path / CACHE_NAME).read_bytes()
    server = Overpass(response)
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CommunityError) as caught:
            refresh_osm_names(client, tmp_path, NOW, [BBOX, BBOX], sleep=lambda s: None)
    assert caught.value.code == code
    assert len(server.requests) == 1  # 被擋就停，不重試
    assert (tmp_path / CACHE_NAME).read_bytes() == before


def test_refresh_only_talks_to_allowed_hosts(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(Overpass())) as client:
        with pytest.raises(CommunityError):
            refresh_osm_names(client, tmp_path, NOW, [BBOX], url="https://example.com/api")
        with pytest.raises(CommunityError):
            refresh_osm_names(
                client, tmp_path, NOW, [BBOX], url="http://overpass-api.de/api/interpreter"
            )


def test_community_points_table_round_trip(tmp_path):
    conn = init_db(tmp_path / "hw.sqlite")
    try:
        point = CommunityPoint("示範湖畔", 25.003, 121.294, "osm_name", 1)
        conn.execute("BEGIN")
        save_community_points(conn, {("rakuya", "9"): point})
        conn.execute("COMMIT")
        loaded = load_community_points(conn)
    finally:
        conn.close()
    assert loaded[("rakuya", "9")].lat == 25.003 and loaded[("rakuya", "9")].method == "osm_name"
