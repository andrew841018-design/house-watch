"""匯出時的定位與範圍指標（物件與成交共用）。

- 有門牌索引時用 WP2 的 `locate.decide()`（R3-01 的區決策表、精度降級）與 `area_metrics()`。
- 還沒有門牌索引時（第一次執行、門牌步驟失敗）降級：只用網站座標與結構化的區名線索，
  證據互相矛盾就標 conflict；沒有網站座標就是 none。等門牌索引建好，下一次匯出自然變好。
- 網站座標只信建物等級的來源（中信、住商，R3-01）；被很多不相干的刊登共用的座標不信。
- in_district（契約 C2 的 Metrics）：buffer 區域也輸出，依定位決策的區；drive 區域在區內時
  由區界多邊形決定。區已知卻不是這個區域查詢的行政區 → false，網頁直接判不符（R3-21）。
- 這個模組只讀：主資料庫一律不寫。門牌索引若是舊 geo 版本算的指標，開檔時會由 WP2 重算一次
  （那是索引檔自己的快取，不是主資料庫）。
"""

import contextlib
import sqlite3
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from house_watch.geo import areas
from house_watch.geo.community import CommunityLookup, OsmNames, SiteObservation
from house_watch.geo.doorindex import DoorIndex
from house_watch.geo.locate import LocEvidence, area_metrics, decide
from house_watch.geo.proj import wgs84_to_tm2
from house_watch.models import AreaKind, ListingFacts, Location, Precision, TxFacts
from house_watch.paths import DataPaths
from house_watch.settings import AreaConfig, Settings

# 網站座標是建物等級的來源（R3-01）。其他來源的座標（如果有）不拿來定位。
SITE_PRECISE_SOURCES = frozenset({"cthouse", "hbhousing"})

PRECISION_ORDER = (
    Precision.SITE,
    Precision.DOOR,
    Precision.COMMUNITY,
    Precision.ROAD,
    Precision.NONE,
)


def _point_key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 5), round(lon, 5)


class Locator:
    """一次匯出用的定位器。用完要 close()（或用 with）。"""

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        *,
        geo: areas.GeoContext | None = None,
        observations: Iterable[SiteObservation] = (),
    ) -> None:
        self.settings = settings
        self.geo = geo if geo is not None else areas.load(config=settings.public)
        self.idx: DoorIndex | None = None
        path = DataPaths(data_dir).doorindex
        if path.is_file():
            with contextlib.suppress(Exception):  # 索引壞了就降級，不讓匯出失敗
                self.idx = DoorIndex.open(path, self.geo)
        self.communities = CommunityLookup(OsmNames.load(data_dir), observations)
        self._zip_names = dict(self.geo.zip_districts)

    def close(self) -> None:
        if self.idx is not None:
            self.idx.close()
            self.idx = None

    def __enter__(self) -> "Locator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def door_version(self) -> str | None:
        return self.idx.version if self.idx is not None else None

    # ------------------------------------------------------------ 定位

    def locate_listing(self, facts: ListingFacts, *, shared_n: int = 1) -> Location:
        ev = LocEvidence.from_listing(
            facts,
            site_precise=facts.source in SITE_PRECISE_SOURCES,
            site_shared_n=shared_n,
        )
        return self.locate(ev)

    def locate_tx(self, tx: TxFacts) -> Location:
        return self.locate(LocEvidence.from_tx(tx))

    def locate(self, ev: LocEvidence) -> Location:
        if self.idx is not None:
            return decide(ev, self.idx, self.geo, communities=self.communities).location
        return self._fallback(ev)

    def _fallback(self, ev: LocEvidence) -> Location:
        """沒有門牌索引：區名線索＋網站座標（只有建物等級的座標，而且沒被共用）。"""
        names: set[str] = set()
        if ev.district:
            names.add(ev.district)
        for kind, value in ev.district_hints:
            if kind == "zip":
                name = self._zip_names.get(value[:3])
                names.add(name or f"zip:{value[:3]}")
            elif value:
                names.add(value)
        site = (
            ev.site_lat is not None
            and ev.site_lon is not None
            and ev.site_precise
            and ev.site_shared_n < 3
        )
        if site:
            assert ev.site_lat is not None and ev.site_lon is not None
            x, y = wgs84_to_tm2(ev.site_lon, ev.site_lat)
            for name, inside in self.geo.district_sides(x, y):
                if inside:
                    names.add(name)
                elif name in names:
                    names.add(f"not:{name}")
        conflict = len(names) > 1
        district = None if conflict or not names else next(iter(names))
        if district is not None and district.startswith("zip:"):
            district = None
        if site:
            return Location(
                lat=round(ev.site_lat, 7),  # type: ignore[arg-type]
                lon=round(ev.site_lon, 7),  # type: ignore[arg-type]
                precision=Precision.SITE,
                district=district,
                conflict=conflict,
            )
        return Location(precision=Precision.NONE, district=district, conflict=conflict)

    # ------------------------------------------------------------ 指標

    def metrics(self, loc: Location, area: AreaConfig, *, min_floors: int | None) -> dict[str, Any]:
        """C2 的 metrics（含兩種區域的 in_district），不含任何門檻。"""
        districts = {d.name for d in area.districts}
        if loc.conflict or loc.district is None:
            return {}
        if loc.district not in districts:
            return {"in_district": False}
        if self.idx is not None:
            out = dict(area_metrics(loc, self.idx, self.geo, area.id, min_floors=min_floors))
        elif loc.precision != Precision.NONE and loc.lat is not None and loc.lon is not None:
            out = dict(self.geo.metrics_lonlat(loc.lon, loc.lat, area.id))
        else:
            out = {}
        # 區已經決定在這個區域裡：buffer 區域沒有區界多邊形，用定位的區；drive 區域的點位
        # 已經由區界多邊形給了 in_district，路段精度沒有，用定位的區。
        if area.kind == AreaKind.BUFFER or "in_district" not in out:
            out["in_district"] = True
        return out


def best_location(locations: list[Location]) -> Location:
    """一戶的位置：精度最好的那一個；成員的區互相矛盾時標 conflict、不給區（R3-01）。"""
    if not locations:
        return Location(precision=Precision.NONE)
    usable = [loc for loc in locations if not loc.conflict]
    if not usable:
        return locations[0]
    districts = {loc.district for loc in usable if loc.district is not None}
    best = min(usable, key=lambda loc: PRECISION_ORDER.index(loc.precision))
    if len(districts) > 1:
        return best.model_copy(update={"district": None, "conflict": True})
    if best.district is None and districts:
        return best.model_copy(update={"district": next(iter(districts))})
    return best


def site_observations(rows: Iterable[tuple[str, ListingFacts]]) -> list[SiteObservation]:
    """他站同社區的座標來源：有社區名、而且座標是建物等級的刊登。"""
    found = []
    for source, facts in rows:
        if (
            source in SITE_PRECISE_SOURCES
            and facts.community_name
            and facts.site_lat is not None
            and facts.site_lon is not None
        ):
            found.append(
                SiteObservation(source, facts.community_name, facts.site_lat, facts.site_lon)
            )
    return found


def shared_counts(rows: Iterable[tuple[int | None, ListingFacts]]) -> Counter:
    """每個網站座標被幾個不同的群組（或沒有群組的刊登）用到。"""
    owners: dict[tuple[float, float], set] = {}
    for gid, facts in rows:
        if facts.site_lat is None or facts.site_lon is None:
            continue
        owner = gid if gid is not None else (facts.source, facts.source_id)
        owners.setdefault(_point_key(facts.site_lat, facts.site_lon), set()).add(owner)
    return Counter({point: len(group) for point, group in owners.items()})


def shared_n(counts: Counter, facts: ListingFacts) -> int:
    if facts.site_lat is None or facts.site_lon is None:
        return 1
    return max(counts.get(_point_key(facts.site_lat, facts.site_lon), 1), 1)


def read_only(conn: sqlite3.Connection) -> None:
    """匯出的 provider 不能留下寫入交易。"""
    if conn.in_transaction:
        raise RuntimeError("匯出不能在寫入交易裡執行")
