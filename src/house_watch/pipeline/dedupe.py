"""跨站合併成「一戶」（R1-07、R1-08、R1-09；r4 §1 的 R1-07／R1-08 列），與匯出 provider。

**相容（compatible）**：兩筆刊登可以是同一戶。只比同口徑的欄位，缺值不滿足任何條件（R1-07）。
- 強證據：原品牌編號相同（brand_origin_id，例如住商轉載的中信物件 `cthouse:<編號>`）→ 相容，
  不管其他欄位有沒有值；欄位不一致時在群組事實裡標衝突，列出各站的值。
- 否則全部要成立：
  1. 樓層：兩筆都知道，而且區間相同。
  2. 面積：至少一種同口徑的面積兩筆都知道——主建物（同一種 main_kind）差 ≤ 0.30 坪，
     或權狀坪差 ≤ 0.05 坪；兩筆都知道的面積只要有一種超過容差就不相容。
  3. 位置：至少一項同種類的證據——社區名正規化後完全相同、路名正規化後完全相同，
     或兩筆都有建物等級的網站座標而且相距 ≤ 150 m。兩筆的區都知道卻不同、或兩個座標相距
     超過 150 m，就不相容。只有社區名對上只有路名不算證據（R1-07 第 4 點）。
  4. 輔助欄位（R1-08）：兩筆都知道又不同就不相容——總樓層差超過 1、格局（房廳衛）不同、
     車位類型矛盾（平面對機械、坡道平面對升降平面…）、電梯有對無、完工日相差一年以上。
- 價格不是條件：同一戶在不同品牌可能開價不同。

**分群**（complete linkage，群組編號穩定）：
- 全員兩兩相容才在同一群；新刊登依（首次出現、來源、編號）的順序一筆一筆加入，不重新分群。
- 新刊登只加入至少還有一筆在架的群組：跨編號的重新上架延後（R1-09）。
- 剛好一個群組可以加入 → 加入；好幾個 → 只有一個有強證據就加入它，否則自己成一群
  （理由 ambiguous；「疑似同一戶」狀態延後，R1-08）。
- 成員的事實變了、和群裡其他人不再相容 → 從較晚加入的開始拆出來，再當成新刊登重新分配。
- 群組的首次出現＝成員中最早的；基準＝最早那筆的基準（R1-09）。空掉的群組留著，編號不重用。

**群組事實**（匯出）：只用可以發布的成員；有在架的就只用在架的。每個欄位是值、null 或
"conflict"；conflict 的欄位在 conflicts 列出各站的值（R1-08）。完全相同才是同一個值；
例外：主建物與權狀坪在容差內取最小值（保守），總樓層差 1 取較大的，完工日取交集，
電梯與車位的確定值優先於推定或「只知道平面」。
"""

import datetime as dt
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from house_watch.clock import to_iso
from house_watch.geo.community import normalize_name
from house_watch.geo.proj import distance_m
from house_watch.models import (
    CONFLICT,
    Elevator,
    ExportLoc,
    ListingFacts,
    MainKind,
    Parking,
    RecordStatus,
)
from house_watch.parse.text import normalize_address
from house_watch.pipeline import locating, versions
from house_watch.settings import AreaConfig

MAIN_TOLERANCE_CP = 30  # 主建物 ±0.30 坪
TITLE_TOLERANCE_CP = 5  # 權狀坪 ±0.05 坪（R1-08）
SITE_MAX_M = 150.0  # 兩個建物等級座標相距 150 m 以內
FLOORS_TOTAL_TOLERANCE = 1  # 總樓層 ±1（各站登錄不一，R2b §4）
BUILT_TOLERANCE = dt.timedelta(days=366)  # 完工日相差一年以內

_FLAT = frozenset(
    {
        Parking.RAMP_FLAT,
        Parking.GROUND_FLAT,
        Parking.LIFT_FLAT,
        Parking.FLAT_UNKNOWN_ACCESS,
        Parking.MIXED_WITH_FLAT,
    }
)
_FLAT_SPECIFIC = frozenset({Parking.RAMP_FLAT, Parking.GROUND_FLAT, Parking.LIFT_FLAT})
_NO_FLAT = frozenset({Parking.MECHANICAL, Parking.MIXED_NO_FLAT})
_SURE_ELEVATOR = frozenset({Elevator.YES, Elevator.NO})
_LAYOUT = re.compile(r"(\d+)房(\d+)廳(\d+)衛")
_DOUBLED = re.compile(r"(路|街|大道)\1+")


# ---------------------------------------------------------------- 比對鍵


def normalize_road(text: str | None) -> str | None:
    """路名的比對寫法：全半形、段號國字、重複的「路路」（R1-07 第 5 點）。"""
    if not text:
        return None
    value = _DOUBLED.sub(r"\1", normalize_address(text))
    return value or None


def layout_key(text: str | None) -> tuple[int, int, int] | None:
    if not text:
        return None
    match = _LAYOUT.search(normalize_address(text))
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


@dataclass(frozen=True)
class Key:
    """一筆刊登用來比對的欄位（都已正規化；None 是不知道）。"""

    source: str
    source_id: str
    floors: tuple[int, int] | None
    floors_total: int | None
    main: tuple[MainKind, int] | None
    title_cp: int | None
    districts: frozenset[str]
    community: str | None
    road: str | None
    point: tuple[float, float] | None  # (lat, lon)，只用建物等級的網站座標
    built: tuple[dt.date | None, dt.date | None]
    parking: Parking
    elevator: Elevator
    layout: tuple[int, int, int] | None
    origin: str | None


def key_of(facts: ListingFacts, zip_names: Mapping[str, str] = {}) -> Key:  # noqa: B006
    districts = set()
    for kind, value in facts.district_hints:
        if kind == "zip":
            name = zip_names.get(value[:3])
            if name:
                districts.add(name)
        elif value:
            districts.add(value.replace("臺", "台"))
    floors = None
    if facts.floor_lo is not None and facts.floor_hi is not None:
        floors = (facts.floor_lo, facts.floor_hi)
    main = None
    if facts.main_cp is not None and facts.main_kind is not None:
        main = (facts.main_kind, facts.main_cp)
    point = None
    if (
        facts.source in locating.SITE_PRECISE_SOURCES
        and facts.site_lat is not None
        and facts.site_lon is not None
    ):
        point = (facts.site_lat, facts.site_lon)
    return Key(
        source=facts.source,
        source_id=facts.source_id,
        floors=floors,
        floors_total=facts.floors_total,
        main=main,
        title_cp=facts.title_area_cp,
        districts=frozenset(districts),
        community=normalize_name(facts.community_name),
        road=normalize_road(facts.road_text),
        point=point,
        built=(facts.built.lo, facts.built.hi),
        parking=facts.parking,
        elevator=facts.elevator,
        layout=layout_key(facts.layout),
        origin=facts.brand_origin_id,
    )


# ---------------------------------------------------------------- 相容


def parking_contradicts(a: Parking, b: Parking) -> bool:
    """兩個已知的車位類型互相矛盾（未知不和任何值矛盾）。"""
    if Parking.UNKNOWN in (a, b) or a == b:
        return False

    def kind(value: Parking) -> str:
        if value in _FLAT:
            return "flat"
        if value in _NO_FLAT:
            return "no_flat"
        return "none"

    if kind(a) != kind(b):
        return True
    return a in _FLAT_SPECIFIC and b in _FLAT_SPECIFIC  # 坡道平面對升降平面


def built_far_apart(
    a: tuple[dt.date | None, dt.date | None], b: tuple[dt.date | None, dt.date | None]
) -> bool:
    """兩個完工日區間各放寬一年仍然沒有交集。任一端不知道就當成不設限。"""
    lo = max((d for d in (a[0], b[0]) if d is not None), default=None)
    hi = min((d for d in (a[1], b[1]) if d is not None), default=None)
    return lo is not None and hi is not None and lo - BUILT_TOLERANCE > hi


def strong(a: Key, b: Key) -> bool:
    return a.origin is not None and a.origin == b.origin


def compatible(a: Key, b: Key) -> bool:
    """兩筆刊登可以是同一戶（規則見模組說明）。"""
    if strong(a, b):
        return True
    if a.floors is None or b.floors is None or a.floors != b.floors:
        return False
    area_evidence = False
    if a.main is not None and b.main is not None and a.main[0] == b.main[0]:
        if abs(a.main[1] - b.main[1]) > MAIN_TOLERANCE_CP:
            return False
        area_evidence = True
    if a.title_cp is not None and b.title_cp is not None:
        if abs(a.title_cp - b.title_cp) > TITLE_TOLERANCE_CP:
            return False
        area_evidence = True
    if not area_evidence:
        return False
    if a.districts and b.districts and not a.districts & b.districts:
        return False
    place_evidence = False
    if a.point is not None and b.point is not None:
        if distance_m(a.point[1], a.point[0], b.point[1], b.point[0]) > SITE_MAX_M:
            return False
        place_evidence = True
    if a.community is not None and a.community == b.community:
        place_evidence = True
    if a.road is not None and a.road == b.road:
        place_evidence = True
    if not place_evidence:
        return False
    if (
        a.floors_total is not None
        and b.floors_total is not None
        and abs(a.floors_total - b.floors_total) > FLOORS_TOTAL_TOLERANCE
    ):
        return False
    if a.layout is not None and b.layout is not None and a.layout != b.layout:
        return False
    if parking_contradicts(a.parking, b.parking):
        return False
    if {a.elevator, b.elevator} == {Elevator.YES, Elevator.NO}:
        return False
    return not built_far_apart(a.built, b.built)


# ---------------------------------------------------------------- 分群


@dataclass
class AssignReport:
    joined: int = 0  # 加入既有群組
    created: int = 0  # 新群組
    split: int = 0  # 從群組拆出來的成員
    ambiguous: int = 0  # 可以加入好幾個群組，自己成一群


@dataclass
class _Member:
    source: str
    source_id: str
    first_seen: dt.datetime
    baseline: bool
    active: bool
    key: Key

    @property
    def order(self) -> tuple:
        return (self.first_seen, self.source, self.source_id)


def zip_names_of(settings: Any) -> dict[str, str]:
    """公開設定裡各區域的郵遞區號 → 區名。"""
    return {d.zip: d.name for area in settings.public.areas for d in area.districts}


def assign_groups(
    conn: sqlite3.Connection,
    *,
    zip_names: Mapping[str, str] = {},  # noqa: B006
) -> AssignReport:
    """依模組說明的規則分群並寫回 listings.group_id 與 groups。呼叫端要在寫入交易裡。"""
    report = AssignReport()
    current = versions.latest_all(conn)
    members: dict[tuple[str, str], _Member] = {}
    groups: dict[int, list[_Member]] = defaultdict(list)
    loose: list[_Member] = []
    for row in versions.listings(conn):
        version = current.get((row.source, row.source_id))
        if version is None:
            continue
        member = _Member(
            row.source,
            row.source_id,
            row.first_seen,
            row.baseline,
            row.status == RecordStatus.ACTIVE,
            key_of(version.facts, zip_names),
        )
        members[(row.source, row.source_id)] = member
        if row.group_id is None:
            loose.append(member)
        else:
            groups[row.group_id].append(member)

    assignment: dict[tuple[str, str], int | None] = {}
    touched: set[int] = set()
    reasons: dict[tuple[str, str], str] = {}
    for gid, group in list(groups.items()):
        kept: list[_Member] = []
        for member in sorted(group, key=lambda m: m.order):
            if all(compatible(member.key, other.key) for other in kept):
                kept.append(member)
            else:
                loose.append(member)
                reasons[(member.source, member.source_id)] = "split"
                report.split += 1
                touched.add(gid)
        groups[gid] = kept

    by_floors: dict[tuple[int, int] | None, set[int]] = defaultdict(set)
    by_origin: dict[str, set[int]] = defaultdict(set)

    def index(gid: int, member: _Member) -> None:
        by_floors[member.key.floors].add(gid)
        if member.key.origin:
            by_origin[member.key.origin].add(gid)

    for gid, group in groups.items():
        for member in group:
            index(gid, member)

    for member in sorted(loose, key=lambda m: m.order):
        key = member.key
        candidates = set(by_floors.get(key.floors, set())) if key.floors is not None else set()
        if key.origin:
            candidates |= by_origin.get(key.origin, set())
        fits = sorted(
            gid
            for gid in candidates
            if any(m.active for m in groups[gid])
            and all(compatible(key, other.key) for other in groups[gid])
        )
        if len(fits) > 1:
            linked = [gid for gid in fits if any(strong(key, m.key) for m in groups[gid])]
            if len(linked) == 1:
                fits = linked
        name = (member.source, member.source_id)
        if len(fits) == 1:
            gid = fits[0]
            report.joined += 1
        else:
            reason = reasons.get(name, "ambiguous" if fits else "new")
            if reason == "ambiguous":
                report.ambiguous += 1
            gid = _new_group(conn, member, reason)
            report.created += 1
        groups[gid].append(member)
        index(gid, member)
        assignment[name] = gid
        touched.add(gid)

    for (source, source_id), gid in sorted(assignment.items()):
        conn.execute(
            "UPDATE listings SET group_id = ? WHERE source = ? AND source_id = ?",
            (gid, source, source_id),
        )
    for gid in sorted(touched):
        group = groups.get(gid) or []
        if not group:
            continue
        earliest = min(group, key=lambda m: m.order)
        conn.execute(
            'UPDATE "groups" SET first_seen = ?, baseline = ? WHERE group_id = ?',
            (to_iso(earliest.first_seen), int(earliest.baseline), gid),
        )
    return report


def _new_group(conn: sqlite3.Connection, member: _Member, reason: str) -> int:
    cursor = conn.execute(
        'INSERT INTO "groups" (first_seen, baseline, created_reason) VALUES (?, ?, ?)',
        (to_iso(member.first_seen), int(member.baseline), reason),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------- 群組事實


@dataclass
class _Facts:
    values: dict[str, Any] = field(default_factory=dict)
    conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def conflict(self, name: str, known: list[tuple[str, Any]]) -> None:
        self.values[name] = CONFLICT
        self.conflicts[name] = [{"src": src, "value": _plain(value)} for src, value in known]


def _plain(value: Any) -> Any:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _known(members: list[tuple[str, ListingFacts]], name: str) -> list[tuple[str, Any]]:
    return [
        (src, getattr(facts, name)) for src, facts in members if getattr(facts, name) is not None
    ]


def _same(out: _Facts, name: str, known: list[tuple[str, Any]], *, norm=lambda v: v) -> None:
    distinct = {norm(value) for _, value in known}
    if not distinct:
        out.values[name] = None
    elif len(distinct) == 1:
        out.values[name] = known[0][1]
    else:
        out.conflict(name, known)


def _within(out: _Facts, name: str, known: list[tuple[str, int]], tolerance: int, pick) -> None:
    if not known:
        out.values[name] = None
        return
    values = [value for _, value in known]
    if max(values) - min(values) <= tolerance:
        out.values[name] = pick(values)
    else:
        out.conflict(name, known)


def group_facts(members: list[tuple[str, ListingFacts]]) -> tuple[dict[str, Any], dict]:
    """成員（來源, 事實）→ GroupFacts 的欄位與 conflicts（規則見模組說明）。"""
    out = _Facts()
    _same(out, "community_name", _known(members, "community_name"), norm=normalize_name)
    _same(out, "road_text", _known(members, "road_text"), norm=normalize_road)
    _same(out, "layout", _known(members, "layout"), norm=lambda v: layout_key(v) or v)
    _same(out, "price_excl_parking", _known(members, "price_excl_parking"))
    _same(out, "parking_price_yuan", _known(members, "parking_price_yuan"))
    _same(out, "parking_public", _known(members, "parking_public"))
    _same(out, "btype", _known(members, "btype"))
    _same(out, "floor_lo", _known(members, "floor_lo"))
    _same(out, "floor_hi", _known(members, "floor_hi"))
    _within(out, "floors_total", _known(members, "floors_total"), FLOORS_TOTAL_TOLERANCE, max)
    _within(out, "title_area_cp", _known(members, "title_area_cp"), TITLE_TOLERANCE_CP, min)

    # 主建物：有只算主建物的值就只用它們（比較精確），否則用主建物＋附屬的合併值。
    mains = [(src, f.main_kind, f.main_cp) for src, f in members if f.main_cp is not None]
    chosen = [m for m in mains if m[1] == MainKind.MAIN] or [
        m for m in mains if m[1] == MainKind.MAIN_PLUS_AUX
    ]
    if not chosen:
        out.values["main_cp"] = out.values["main_kind"] = None
    else:
        out.values["main_kind"] = chosen[0][1]
        _within(out, "main_cp", [(src, cp) for src, _, cp in chosen], MAIN_TOLERANCE_CP, min)

    los = [f.built.lo for _, f in members if f.built.lo is not None]
    his = [f.built.hi for _, f in members if f.built.hi is not None]
    lo, hi = (max(los) if los else None), (min(his) if his else None)
    if lo is not None and hi is not None and lo > hi:
        ranges = [
            (src, f"{_plain(f.built.lo) or ''}~{_plain(f.built.hi) or ''}")
            for src, f in members
            if f.built.lo is not None or f.built.hi is not None
        ]
        out.conflict("built_lo", ranges)
        out.conflict("built_hi", ranges)
    else:
        out.values["built_lo"], out.values["built_hi"] = lo, hi

    elevators = [(src, f.elevator) for src, f in members]
    sure = {value for _, value in elevators if value in _SURE_ELEVATOR}
    if len(sure) > 1:
        out.conflict("elevator", [(src, v) for src, v in elevators if v in _SURE_ELEVATOR])
    elif sure:
        out.values["elevator"] = next(iter(sure))
    elif any(value == Elevator.INFERRED for _, value in elevators):
        out.values["elevator"] = Elevator.INFERRED
    else:
        out.values["elevator"] = Elevator.UNKNOWN

    parkings = [(src, f.parking) for src, f in members if f.parking != Parking.UNKNOWN]
    distinct = {value for _, value in parkings}
    refined = distinct - {Parking.FLAT_UNKNOWN_ACCESS}
    if not distinct:
        out.values["parking"] = Parking.UNKNOWN
    elif len(distinct) == 1:
        out.values["parking"] = next(iter(distinct))
    elif len(refined) == 1 and next(iter(refined)) in _FLAT_SPECIFIC | {Parking.MIXED_WITH_FLAT}:
        out.values["parking"] = next(iter(refined))  # 「只知道平面」被確定的平面類型取代
    else:
        out.conflict("parking", parkings)
    return out.values, out.conflicts


# ---------------------------------------------------------------- 匯出 provider（WP7a 的 listing_groups）


@dataclass(frozen=True)
class _Row:
    source: str
    source_id: str
    area: str
    group_id: int
    first_seen: dt.datetime
    baseline: bool
    active: bool
    gone_at: dt.datetime | None
    relisted_at: dt.datetime | None
    facts: ListingFacts


def _rows(conn: sqlite3.Connection, publish: frozenset[str]) -> list[_Row]:
    current = versions.latest_all(conn)
    rows = []
    for row in versions.listings(conn, "group_id IS NOT NULL"):
        version = current.get((row.source, row.source_id))
        if version is None or row.source not in publish or row.group_id is None:
            continue
        rows.append(
            _Row(
                row.source,
                row.source_id,
                row.area,
                row.group_id,
                row.first_seen,
                row.baseline,
                row.status == RecordStatus.ACTIVE,
                row.gone_at,
                row.relisted_at,
                version.facts,
            )
        )
    return rows


def export_groups(ctx: Any, area: AreaConfig) -> list[dict[str, Any]]:
    """匯出 provider（export.Providers.listing_groups）：這個區域的群組，C2 的 ListingGroup 格式。

    只讀資料庫；只用 ctx.publish_sources 的成員；每次呼叫自己開關門牌索引。
    """
    conn: sqlite3.Connection = ctx.conn
    locating.read_only(conn)
    rows = _rows(conn, ctx.publish_sources)
    by_group: dict[int, list[_Row]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)
    wanted = sorted(gid for gid, group in by_group.items() if any(r.area == area.id for r in group))
    if not wanted:
        return []
    geo = ctx.cache.get("geo")
    data_dir = ctx.settings.data_dir
    observations = locating.site_observations((r.source, r.facts) for r in rows)
    counts = locating.shared_counts((r.group_id, r.facts) for r in rows)
    out: list[dict[str, Any]] = []
    with locating.Locator(ctx.settings, data_dir, geo=geo, observations=observations) as locator:
        if geo is None:
            ctx.cache["geo"] = locator.geo
        for gid in wanted:
            out.append(_group_record(conn, gid, by_group[gid], area, locator, counts))
    return out


def _group_record(
    conn: sqlite3.Connection,
    gid: int,
    group: list[_Row],
    area: AreaConfig,
    locator: locating.Locator,
    counts,
) -> dict[str, Any]:
    group = sorted(group, key=lambda r: (r.first_seen, r.source, r.source_id))
    active = [r for r in group if r.active]
    basis = active or group
    values, conflicts = group_facts([(r.source, r.facts) for r in basis])
    members = []
    prices = {}
    for r in group:
        summary = versions.price_summary(versions.price_points(conn, r.source, r.source_id))
        current = summary.price
        if r.facts.price_yuan is not None and versions.price_rejected(conn, r.source, r.source_id):
            current = None  # 最新看到的價格被擋下、還沒確認（CX-1）：現價未知，網頁判待確認
        prices[(r.source, r.source_id)] = current
        members.append(
            {
                "src": r.source,
                "id": r.source_id,
                "price": current,
                "price_first": summary.price_first,
                "price_drop_at": summary.price_drop_at,
                "site_original_price": r.facts.site_original_price_yuan,
                "first_seen": r.first_seen,
                "status": RecordStatus.ACTIVE if r.active else RecordStatus.GONE,
            }
        )
    shown = [prices[(r.source, r.source_id)] for r in basis]
    shown = [p for p in shown if p is not None]
    locations = []
    for r in basis:
        try:
            locations.append(
                locator.locate_listing(r.facts, shared_n=locating.shared_n(counts, r.facts))
            )
        except Exception:  # 單筆定位失敗只降級成沒有位置，不讓整次匯出失敗
            continue
    loc = locating.best_location(locations)
    floor_hi = values.get("floor_hi") if isinstance(values.get("floor_hi"), int) else None
    try:
        metrics = locator.metrics(loc, area, min_floors=floor_hi)
    except Exception:
        metrics = {}
    earliest = group[0]
    status = RecordStatus.ACTIVE if active else RecordStatus.GONE
    gone_at = None
    if not active:
        gone_times = [r.gone_at for r in group if r.gone_at is not None]
        gone_at = max(gone_times) if gone_times else None
    relisted = [r.relisted_at for r in active if r.relisted_at is not None]
    return {
        "gid": gid,
        "first_seen": earliest.first_seen,
        "baseline": earliest.baseline,
        "status": status,
        "gone_at": gone_at,
        "relisted_at": max(relisted) if relisted else None,
        "price_min": min(shown) if shown else None,
        "price_max": max(shown) if shown else None,
        "facts": values,
        "conflicts": conflicts,
        "loc": ExportLoc(
            lat=loc.lat,
            lon=loc.lon,
            precision=loc.precision,
            district=loc.district,
            conflict=loc.conflict,
        ).model_dump(mode="json"),
        "metrics": metrics,
        "members": members,
    }
