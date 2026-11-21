"""定位：一筆物件或成交屬於哪個區、在哪裡、精度多少（契約 C7）。

**區的決策表**（R3-01）。依序收集證據，並記下用了哪一種：

1. 網站座標（`site_precise` 是 True 才用）：
   - 看座標 100 m 內的門牌：全都屬於同一區才算決定性證據；100 m 內有別區的門牌（離區界太近）
     或附近沒有門牌，就不拿門牌判斷。
   - 設定裡有完整區界多邊形的區（中壢區）：離區界 100 m 以上時，在區內＝這個區的證據，
     在區外＝「不是這個區」的證據（和其他說是這個區的證據矛盾）。
2. 結構化欄位：成交的「鄉鎮市區」欄、站方的區名欄、郵遞區號。郵遞區號只認公開設定裡
   各區域的行政區；其他號碼記成「zip:<號碼>」，仍然會和別的證據比對出矛盾。
3. 地址字串開頭的區名（每一個都算，不是只取最後一個）。

- 任兩項證據的區不同 → 區域矛盾：`conflict = True`、不給區，網頁列待確認（`loc_conflict`）。
- 沒有任何證據時，只有路名剛好只在一個區找得到，才用那個區（記成 door 證據）。
- 地址說某區、但這條路的門牌只在別區找得到 → 也是矛盾。
- 這裡完全不知道「是用哪個區查到的」，所以不可能從查詢的區推出一筆物件的區。

**位置與精度**（DESIGN §9、R1-14、R3-07）：網站座標 → 門牌 → 社區名（OSM → 他站）→ 路段 → 無。

- 網站座標先做兩項合理性檢查：在桃園市框內、沒有被很多不相干的刊登共用（共用數由呼叫端算）。
- 門牌精度只給「完全相符、主號、上下鄰號都在、號碼範圍夠集中」的比對。沒寫段而該區有分段的
  同名路時，號碼只在其中一段存在才算門牌（section_inferred）；正式門牌（實價登錄）沒寫段
  就是不帶段的那條路（section_bare）；其餘（section_unknown）、「附近」、路口，都降成路段
  精度（R3-07）。
- 路段精度給 `road_key`，指標改用路段比例（`area_metrics`）：只用已確定的區裡、
  最高樓層 ≥ 物件樓層的門牌點（R1-14、R3-06），篩完沒有門牌時改用不篩的。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from house_watch.geo.areas import GeoContext
from house_watch.geo.community import CommunityPoint
from house_watch.geo.doorindex import (
    CITY_BOX_TM2,
    DECISIVE_M,
    DISTRICT_CODES,
    DISTRICT_NAMES,
    DoorIndex,
    DoorMatch,
    district_code,
    merge_flags,
    norm,
    split_prefix,
)
from house_watch.geo.proj import wgs84_to_tm2
from house_watch.models import ListingFacts, Location, Precision, TxFacts

# district_hints 的 kind（契約 C1 的 ListingFacts.district_hints；建議轉接器只用這三種）。
# 其他 kind 也收：值像郵遞區號就當郵遞區號，否則當地址字串找區名。
HINT_DISTRICT = "district"  # 站方結構化欄位的區名，例如「中壢區」
HINT_ZIP = "zip"  # 郵遞區號，例如「320」
HINT_ADDRESS = "address"  # 站方的地址或門牌欄，可能帶區名
HINT_KINDS = (HINT_DISTRICT, HINT_ZIP, HINT_ADDRESS)

SHARED_SITE_MIN = 3  # 幾筆不相干的刊登共用同一個座標，就不信這個座標
DOOR_LEVELS = frozenset({"exact", "main_number", "nearby_number", "number_range"})
_DOWNGRADE_FLAGS = frozenset({"section_unknown", "fuzzy", "intersection", "number_not_found"})
_ZIP = re.compile(r"\d{3}(?:\d{2,3})?")


class CommunitySource(Protocol):
    """社區名定位（community.CommunityLookup 實作）。"""

    def find(
        self, source: str | None, community_key: str | None, name: str | None
    ) -> CommunityPoint | None: ...


@dataclass(frozen=True)
class LocEvidence:
    """定位用的證據。用 `from_listing()`、`from_tx()` 建立，或直接填。"""

    source: str | None = None
    district: str | None = None  # 權威的區欄（實價登錄的「鄉鎮市區」）
    district_hints: tuple[tuple[str, str], ...] = ()
    address_text: str | None = None  # 物件的路名欄或成交的地址
    site_lat: float | None = None
    site_lon: float | None = None
    site_precise: bool = True  # 座標是建物等級（R3-01：中信、住商）；False 就完全不用座標
    site_shared_n: int = 1  # 用同一座標的不相干刊登數（含自己）
    community_name: str | None = None
    community_key: str | None = None
    floor_hi: int | None = None  # 物件所在樓層的上界，用來篩門牌最高樓層（R1-14）
    exact_address: bool = False  # 地址是正式門牌（實價登錄）：有段一定會寫，沒寫段就是不帶段的路

    @classmethod
    def from_listing(
        cls, facts: ListingFacts, *, site_precise: bool = True, site_shared_n: int = 1
    ) -> "LocEvidence":
        return cls(
            source=facts.source,
            district_hints=tuple(facts.district_hints),
            address_text=facts.road_text,
            site_lat=facts.site_lat,
            site_lon=facts.site_lon,
            site_precise=site_precise,
            site_shared_n=site_shared_n,
            community_name=facts.community_name,
            community_key=facts.community_key,
            floor_hi=facts.floor_hi if facts.floor_hi is not None else facts.floor_lo,
        )

    @classmethod
    def from_tx(cls, tx: TxFacts) -> "LocEvidence":
        return cls(
            source="lvr",
            district=tx.district,
            address_text=tx.address_text,
            floor_hi=tx.floor_hi if tx.floor_hi is not None else tx.floor_lo,
            exact_address=True,
        )


@dataclass(frozen=True)
class Evidence:
    """一項區的證據。district 是區名；對不到區名時是代號（例如 zip:334）。"""

    kind: str
    value: str
    district: str


@dataclass(frozen=True)
class LocDecision:
    """定位的完整結果（locate 只回傳其中的 location）。"""

    location: Location
    evidence: tuple[Evidence, ...]
    method: str  # site、door、community、road、none、conflict
    flags: tuple[str, ...] = ()
    match: DoorMatch | None = None
    community: CommunityPoint | None = None


def _district_name(value: str) -> str | None:
    try:
        return DISTRICT_NAMES[district_code(value)]
    except KeyError:
        return None


def _hint_evidence(kind: str, value: str, zips: dict[str, str]) -> list[Evidence]:
    text = norm(value)
    if not text:
        return []
    if _ZIP.fullmatch(text):
        code = text[:3]
        return [Evidence("zip", code, zips.get(code) or f"zip:{code}")]
    if kind == HINT_ZIP:
        return []
    name = _district_name(text)
    if name is not None:
        return [Evidence("district" if kind == HINT_DISTRICT else kind, text, name)]
    found, _ = split_prefix(text)
    return [Evidence(kind, name, name) for name in found]


def _usable_site(ev: LocEvidence, flags: list[str]) -> tuple[float, float] | None:
    if ev.site_lat is None or ev.site_lon is None:
        return None
    if not ev.site_precise:
        flags.append("site_imprecise")
        return None
    x, y = wgs84_to_tm2(ev.site_lon, ev.site_lat)
    minx, miny, maxx, maxy = CITY_BOX_TM2
    if not (minx <= x <= maxx and miny <= y <= maxy):
        flags.append("site_outside_city")
        return None
    if ev.site_shared_n >= SHARED_SITE_MIN:
        flags.append("site_shared")
        return None
    return x, y


def _location(
    precision: Precision,
    district: str | None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    conflict: bool = False,
    door_id: str | None = None,
    road_key: str | None = None,
) -> Location:
    if lat is not None and lon is not None:
        lat, lon = round(lat, 7), round(lon, 7)
    return Location(
        lat=lat,
        lon=lon,
        precision=precision,
        district=district,
        conflict=conflict,
        door_id=door_id,
        road_key=road_key,
    )


def decide(
    ev: LocEvidence,
    idx: DoorIndex,
    ctx: GeoContext,
    *,
    communities: CommunitySource | None = None,
) -> LocDecision:
    """依 R3-01 的決策表定出區，再依精度順序定位。"""
    flags: list[str] = []
    evidence: list[Evidence] = []
    ruled_out: list[Evidence] = []
    site = _usable_site(ev, flags)
    if site is not None:
        value = f"{ev.site_lat:.6f},{ev.site_lon:.6f}"
        near = idx.district_at(*site)
        if near.district is not None and near.decisive:
            evidence.append(Evidence("coords", value, DISTRICT_NAMES[near.district]))
        else:
            flags.append("site_near_edge" if near.district else "site_far_from_doors")
        for name, inside in ctx.district_sides(*site, margin_m=DECISIVE_M):
            if inside:
                evidence.append(Evidence("polygon", value, name))
            else:
                ruled_out.append(Evidence("polygon", value, f"not:{name}"))
    if ev.district:
        name = _district_name(ev.district)
        evidence.append(Evidence("field", norm(ev.district), name or f"field:{norm(ev.district)}"))
    for kind, value in ev.district_hints:
        evidence.extend(_hint_evidence(kind, value, ctx.zip_districts))
    text_districts, rest = split_prefix(ev.address_text)
    evidence.extend(Evidence("address", name, name) for name in text_districts)

    districts = list(dict.fromkeys(e.district for e in evidence))
    excluded = {e.district.removeprefix("not:") for e in ruled_out}
    evidence.extend(ruled_out)
    if len(districts) > 1 or excluded & set(districts):
        return _conflict(ev, site, evidence, flags)
    decided = districts[0] if districts else None
    if decided is not None and decided not in DISTRICT_CODES:
        flags.append("district_unknown_code")
        decided = None

    if rest:
        if decided is None:
            where = idx.road_districts(rest)
            if len(where) == 1:
                decided = DISTRICT_NAMES[where[0]]
                evidence.append(Evidence("door", rest, decided))
            elif where:
                flags.append("road_in_several_districts")
        elif site is None and idx.parse_road(decided, rest).base is None:
            elsewhere = [DISTRICT_NAMES[c] for c in idx.road_districts(rest)]
            if elsewhere:
                evidence.extend(Evidence("door", rest, name) for name in elsewhere)
                return _conflict(ev, site, evidence, flags)

    if site is not None:
        assert ev.site_lat is not None and ev.site_lon is not None
        loc = _location(Precision.SITE, decided, lat=ev.site_lat, lon=ev.site_lon)
        return LocDecision(loc, tuple(evidence), "site", tuple(flags))
    if decided is None:
        flags.append("no_district")
        return LocDecision(_location(Precision.NONE, None), tuple(evidence), "none", tuple(flags))
    return _text_location(ev, idx, decided, rest, evidence, flags, communities)


def _conflict(
    ev: LocEvidence,
    site: tuple[float, float] | None,
    evidence: Sequence[Evidence],
    flags: list[str],
) -> LocDecision:
    flags.append("district_conflict")
    if site is not None and ev.site_lat is not None and ev.site_lon is not None:
        loc = _location(Precision.SITE, None, lat=ev.site_lat, lon=ev.site_lon, conflict=True)
    else:
        loc = _location(Precision.NONE, None, conflict=True)
    return LocDecision(loc, tuple(evidence), "conflict", tuple(flags))


def _text_location(
    ev: LocEvidence,
    idx: DoorIndex,
    decided: str,
    rest: str,
    evidence: list[Evidence],
    flags: list[str],
    communities: CommunitySource | None,
) -> LocDecision:
    match: DoorMatch | None = None
    if rest:
        parts = idx.parse_road(decided, rest)
        if parts.base is not None:
            if ev.exact_address and parts.section is None:
                bare = idx.lookup(decided, parts.base, "", parts.number)
                if bare is not None and bare.level in DOOR_LEVELS:
                    flags_ = [f for f in parts.flags if f != "section_unknown"]
                    match = merge_flags(bare, [*flags_, "section_bare"])
            if match is None:
                match = idx.lookup(decided, parts.base, parts.section, parts.number)
                if match is not None:
                    match = merge_flags(match, parts.flags)
        elif parts.failure:
            flags.append(parts.failure)
    if match is not None:
        flags.extend(f for f in match.flags if f not in flags)
        if match.level in DOOR_LEVELS and not _DOWNGRADE_FLAGS & set(match.flags):
            if match.level == "nearby_number":
                flags.append("nearby_number")
            loc = _location(
                Precision.DOOR,
                decided,
                lat=match.lat,
                lon=match.lon,
                door_id=match.door_key,
                road_key=match.road_key,
            )
            return LocDecision(loc, tuple(evidence), "door", tuple(flags), match=match)
    point = _community(ev, idx, decided, flags, communities)
    if point is not None:
        loc = _location(Precision.COMMUNITY, decided, lat=point.lat, lon=point.lon)
        return LocDecision(
            loc, tuple(evidence), "community", tuple(flags), match=match, community=point
        )
    if match is not None:
        loc = _location(
            Precision.ROAD, decided, lat=match.lat, lon=match.lon, road_key=match.road_key
        )
        return LocDecision(loc, tuple(evidence), "road", tuple(flags), match=match)
    loc = _location(Precision.NONE, decided)
    return LocDecision(loc, tuple(evidence), "none", tuple(flags))


def _community(
    ev: LocEvidence,
    idx: DoorIndex,
    decided: str,
    flags: list[str],
    communities: CommunitySource | None,
) -> CommunityPoint | None:
    if communities is None or not ev.community_name:
        return None
    point = communities.find(ev.source, ev.community_key, ev.community_name)
    if point is None:
        return None
    near = idx.district_at(*wgs84_to_tm2(point.lon, point.lat))
    if near.district is not None and DISTRICT_NAMES[near.district] != decided:
        flags.append("community_other_district")
        return None
    return point


def locate(
    ev: LocEvidence,
    idx: DoorIndex,
    ctx: GeoContext,
    *,
    communities: CommunitySource | None = None,
) -> Location:
    """契約 C7 的 `locate.locate`：只回傳 Location。完整的證據與旗標見 `decide()`。"""
    return decide(ev, idx, ctx, communities=communities).location


def area_metrics(
    loc: Location,
    idx: DoorIndex,
    ctx: GeoContext,
    area: str,
    *,
    min_floors: int | None = None,
) -> dict[str, Any]:
    """一個定位結果在某區域的指標（C2 的 metrics）。

    - 區域矛盾、區不確定，或區不在這個區域查詢的行政區裡：空的（不套這個區域，網頁列待確認）。
    - 網站座標、門牌、社區名精度：`ctx.metrics()` 的距離或車程檔。
    - 路段精度：`road_frac`，只用最高樓層 ≥ min_floors 的門牌點（建議傳物件的 floor_hi）。
    - 沒有位置：空的。
    """
    geo = ctx.area(area)
    if loc.conflict or loc.district is None or loc.district not in geo.districts:
        return {}
    if loc.precision in (Precision.SITE, Precision.DOOR, Precision.COMMUNITY):
        if loc.lat is None or loc.lon is None:
            return {}
        return ctx.metrics_lonlat(loc.lon, loc.lat, area)
    if loc.precision == Precision.ROAD and loc.road_key:
        door_ids = idx.road_doors_by_key(loc.road_key, min_floors)
        if not door_ids and min_floors is not None:
            door_ids = idx.road_doors_by_key(loc.road_key, None)
        fractions = idx.road_frac(door_ids, area)
        return {"road_frac": fractions} if fractions else {}
    return {}
