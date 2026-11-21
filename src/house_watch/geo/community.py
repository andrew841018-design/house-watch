"""社區名定位（DESIGN §9、R1-14 第 5 點、R2b）。

定位順序照 DESIGN §9：OSM 社區名 → 他站同社區的座標 → 路名（路名由 locate 處理）。

- **比對只認完全相同**：兩邊都正規化（NFKC、刪空白與標點、異體字歸一、去掉結尾的「社區」）
  之後字串完全相等才算，不做子字串或模糊比對（例如「中路御品」就在 RCA 管制區內，R3a）。
- **OSM 只比住宅類**：21 世紀的社區欄有時填的是學校名（R2b），所以只收
  landuse=residential 或住宅類 building，而且沒有 amenity、shop、office 這類標籤的具名物件。
- **同名但相距太遠**（OSM 300 m、他站 150 m 以上）就當作不確定，不定位。
- **OSM 名稱表只能手動更新**（`refresh_osm_names`）：一次一個 Overpass 請求、同主機間隔 ≥ 4 秒、
  User-Agent 是 house-watch/<版本>，被擋就停。結果存在資料夾的 `osm_names.json`，
  排程與測試都不會連 Overpass。資料授權是 ODbL（© OpenStreetMap contributors）。
"""

import datetime as dt
import json
import os
import re
import statistics
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from house_watch.clock import to_iso
from house_watch.errors import ErrorCode
from house_watch.geo.doorindex import (
    MIN_INTERVAL_S,
    USER_AGENT,
    VARIANTS,
    FetchError,
    check_status,
    check_url,
)
from house_watch.geo.proj import distance_m

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_HOSTS = frozenset({"overpass-api.de", "maps.mail.ru"})
OVERPASS_MAX_BYTES = 32 * 1024 * 1024
CACHE_NAME = "osm_names.json"
CACHE_VERSION = 1
OSM_SPREAD_MAX_M = 300.0
CROSS_SITE_SPREAD_MAX_M = 150.0

RESIDENTIAL_BUILDINGS = frozenset(
    {
        "residential",
        "apartments",
        "house",
        "detached",
        "terrace",
        "dormitory",
        "semidetached_house",
        "yes",
    }
)
NON_RESIDENTIAL_KEYS = (
    "amenity",
    "shop",
    "office",
    "leisure",
    "tourism",
    "craft",
    "healthcare",
    "public_transport",
    "railway",
    "aeroway",
    "religion",
    "government",
    "military",
    "man_made",
    "power",
)

_PUNCT = re.compile(r"[\s·•‧・．.,，、'\"‘’“”()（）\[\]【】「」『』<>《》\-‐‑–—_~～/\\]+")


class CommunityError(FetchError):
    """OSM 名稱表更新失敗。現有的名稱表不會被動到。"""


def normalize_name(name: str | None) -> str | None:
    """社區名正規化，用來比對「完全相同」。正規化後是空的就回傳 None。"""
    text = unicodedata.normalize("NFKC", name or "")
    text = _PUNCT.sub("", text).translate(VARIANTS).casefold()
    text = re.sub(r"社區$", "", text)
    return text or None


@dataclass(frozen=True)
class CommunityPoint:
    """社區名定位的結果。method 是 osm_name 或 cross_site；n 是用了幾個點。"""

    name: str
    lat: float
    lon: float
    method: str
    n: int


@dataclass(frozen=True)
class SiteObservation:
    """一筆有網站座標的刊登，提供「他站同社區」的座標。"""

    source: str
    name: str
    lat: float
    lon: float


def _consensus(
    name: str, points: Sequence[tuple[float, float]], method: str, spread_max_m: float
) -> CommunityPoint | None:
    """多個點的中位數；有任何一點離中位數超過 spread_max_m 就不定位。"""
    if not points:
        return None
    lat = statistics.median(p[0] for p in points)
    lon = statistics.median(p[1] for p in points)
    if any(distance_m(lon, lat, p[1], p[0]) > spread_max_m for p in points):
        return None
    return CommunityPoint(name, round(lat, 7), round(lon, 7), method, len(points))


def is_residential(tags: Mapping[str, Any]) -> bool:
    """OSM 物件是不是住宅類（只用這些物件的名字定位社區）。"""
    if any(key in tags for key in NON_RESIDENTIAL_KEYS):
        return False
    if tags.get("landuse") == "residential":
        return True
    return str(tags.get("building", "")).lower() in RESIDENTIAL_BUILDINGS


def parse_overpass(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Overpass 的 JSON（`out tags center`）→ 名稱表的項目，依名稱與編號排序。"""
    items = []
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name or not is_residential(tags):
            continue
        where = element.get("center") or element
        lat, lon = where.get("lat"), where.get("lon")
        if not isinstance(lat, int | float) or not isinstance(lon, int | float):
            continue
        items.append(
            {
                "name": name,
                "lat": round(float(lat), 7),
                "lon": round(float(lon), 7),
                "osm": f"{element.get('type')}/{element.get('id')}",
            }
        )
    items.sort(key=lambda item: (item["name"], item["osm"]))
    return items


class OsmNames:
    """OSM 具名住宅物件的名稱表。"""

    def __init__(self, items: Iterable[Mapping[str, Any]] = ()) -> None:
        self.items = [dict(item) for item in items]
        self._by_name: dict[str, list[tuple[float, float]]] = {}
        for item in self.items:
            key = normalize_name(item.get("name"))
            if key:
                self._by_name.setdefault(key, []).append((float(item["lat"]), float(item["lon"])))

    @classmethod
    def load(cls, data_dir: Path) -> "OsmNames":
        """讀資料夾裡的名稱表；還沒有就是空的（不會自動去抓）。"""
        path = data_dir / CACHE_NAME
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION:
            return cls()
        return cls(data.get("items") or [])

    def __len__(self) -> int:
        return len(self.items)

    def lookup(self, name: str | None) -> CommunityPoint | None:
        """名稱正規化後完全相同的住宅物件；同名的物件彼此相距太遠就不定位。"""
        key = normalize_name(name)
        if not key or key not in self._by_name:
            return None
        return _consensus(str(name), self._by_name[key], "osm_name", OSM_SPREAD_MAX_M)


def cross_site_point(
    name: str | None, observations: Sequence[SiteObservation]
) -> CommunityPoint | None:
    """他站同社區（正規化後名稱完全相同）的網站座標中位數；彼此太遠就不定位。"""
    key = normalize_name(name)
    if not key:
        return None
    points = [(o.lat, o.lon) for o in observations if normalize_name(o.name) == key]
    return _consensus(str(name), points, "cross_site", CROSS_SITE_SPREAD_MAX_M)


class CommunityLookup:
    """locate 用的社區定位：OSM 社區名 → 他站同社區的座標（DESIGN §9）。"""

    def __init__(
        self, osm: OsmNames | None = None, observations: Iterable[SiteObservation] = ()
    ) -> None:
        self.osm = osm
        self._observations: dict[str, list[SiteObservation]] = {}
        for obs in observations:
            key = normalize_name(obs.name)
            if key:
                self._observations.setdefault(key, []).append(obs)

    def find(
        self, source: str | None, community_key: str | None, name: str | None
    ) -> CommunityPoint | None:
        del community_key  # 站內社區 id 只當快取鍵（community_points 表），比對只看名稱
        if self.osm is not None:
            found = self.osm.lookup(name)
            if found is not None:
                return found
        key = normalize_name(name)
        if not key:
            return None
        others = [o for o in self._observations.get(key, []) if o.source != source]
        return cross_site_point(name, others)


# ---------------------------------------------------------------- 手動更新 OSM 名稱表


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """bbox 是 (south, west, north, east)。只要具名的建物與住宅區，輸出中心點與標籤。"""
    south, west, north, east = (f"{v:.5f}" for v in bbox)
    box = f"({south},{west},{north},{east})"
    return (
        "[out:json][timeout:90];\n(\n"
        f'  way["name"]["building"]{box};\n'
        f'  relation["name"]["building"]{box};\n'
        f'  way["name"]["landuse"="residential"]{box};\n'
        f'  relation["name"]["landuse"="residential"]{box};\n'
        ");\nout tags center;\n"
    )


def refresh_osm_names(
    client: httpx.Client,
    data_dir: Path,
    now: dt.datetime,
    bboxes: Sequence[tuple[float, float, float, float]],
    *,
    url: str = OVERPASS_URL,
    user_agent: str = USER_AGENT,
    min_interval_s: float = MIN_INTERVAL_S,
    timeout_s: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """手動更新 OSM 名稱表（只由使用者明確下指令時呼叫）。回傳項目數。

    每個 bbox 一個請求，請求之間至少隔 min_interval_s 秒。任何一個請求失敗（被擋、
    逾時、格式不對）就整個停下並丟 CommunityError，現有的名稱表不動。
    """
    check_url(url, OVERPASS_HOSTS, error=CommunityError, invalid=ErrorCode.PAGE_INVALID)
    items: dict[str, dict[str, Any]] = {}
    base = None
    for i, bbox in enumerate(bboxes):
        if i:
            sleep(min_interval_s)
        try:
            response = client.post(
                url,
                data={"data": overpass_query(bbox)},
                headers={"User-Agent": user_agent},
                timeout=timeout_s,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise CommunityError(ErrorCode.TIMEOUT, "Overpass 逾時") from None
        except httpx.TransportError:
            raise CommunityError(ErrorCode.TIMEOUT, "連不上 Overpass") from None
        check_status(response, error=CommunityError, invalid=ErrorCode.PAGE_INVALID)
        if len(response.content) > OVERPASS_MAX_BYTES:
            raise CommunityError(ErrorCode.PAGE_INVALID, "Overpass 回應超過大小上限")
        try:
            payload = response.json()
        except ValueError:
            raise CommunityError(ErrorCode.PAGE_INVALID, "Overpass 回應不是 JSON") from None
        if not isinstance(payload, dict) or "elements" not in payload:
            raise CommunityError(ErrorCode.PAGE_INVALID, "Overpass 回應格式不對")
        base = (payload.get("osm3s") or {}).get("timestamp_osm_base") or base
        for item in parse_overpass(payload):
            items[item["osm"]] = item
    ordered = sorted(items.values(), key=lambda item: (item["name"], item["osm"]))
    data = {
        "version": CACHE_VERSION,
        "fetched_at": to_iso(now),
        "osm_base": base,
        "license": "ODbL 1.0, © OpenStreetMap contributors",
        "items": ordered,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / CACHE_NAME
    tmp = target.with_name(target.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return len(ordered)


# ---------------------------------------------------------------- community_points 表（C8）


def save_community_points(conn: Any, points: Mapping[tuple[str, str], CommunityPoint]) -> None:
    """把（來源, 站內社區 id）的定位結果寫進 community_points。呼叫端負責交易。"""
    conn.executemany(
        "INSERT OR REPLACE INTO community_points "
        "(source, community_key, lat, lon, method, precision) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, key, point.lat, point.lon, point.method, "community")
            for (source, key), point in sorted(points.items())
        ],
    )


def load_community_points(conn: Any) -> dict[tuple[str, str], CommunityPoint]:
    """讀回 community_points（名稱欄用站內社區 id）。"""
    rows = conn.execute(
        "SELECT source, community_key, lat, lon, method FROM community_points "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL ORDER BY source, community_key"
    ).fetchall()
    return {
        (row[0], row[1]): CommunityPoint(row[1], float(row[2]), float(row[3]), row[4], 1)
        for row in rows
    }
