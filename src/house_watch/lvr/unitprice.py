"""扣車位單價的分子與分母區間（WP3；R1-05、R3-12、r4 §2.2 #4）。

Python 只輸出區間，不算單價、也不和任何門檻比較：
- q：扣車位後的金額（元）＝總價 − 車位價。
- k：扣車位後的面積（0.01 m²）＝建物移轉總面積 − 車位面積。
- 瀏覽器交叉配對：單價上界＝q_hi ÷ k_lo，下界＝q_lo ÷ k_hi（公式只寫在 rules.js）。

**逐位判斷**（每個車位分開看價格與面積）
- 價格 > 0：用申報值；否則用中位數的 p25～p75 估算（車位價越高，q 越小）。
- 面積 > 0：用申報值；否則用中位數的 p25～p75 估算。
- 所以 q_lo＝總價 − Σ已知價 − Σ估算價 p75，q_hi＝總價 − Σ已知價 − Σ估算價 p25；
  k_lo＝建物面積 − Σ已知面積 − Σ估算面積 p75，k_hi 用 p25。
- basis：每個車位的價格與面積都是申報值（或根本沒有車位）→ official；用到估算 → estimated；
  需要估算卻沒有可用的中位數、或結果不合理 → none（q、k 都是 null）。
- 估算的車位面積登記在共有部分裡，所以估扣量不能讓分母小於
  主建物＋附屬＋陽台（R1-05 第 3 點）；碰到上限時截住並加註 up_area_capped。
- 官方車位價 > 0 一律照用，不會被任何估算或備註取代；這裡完全不讀備註
  （備註車位價的解析照 r4 F4 延後）。

**中位數**（綁實價登錄的資料版本，同一版資料重算結果相同）
- 樣本：不被排除的建物成交、交易日在 (as_of − 24 個月, as_of] 內，逐位取 park 明細的值。
  由主檔總價平均分出來的值（parking_price_split／parking_area_split）不算樣本。
  價格與面積分開取樣、分開計數。
- 分層（不跨到另一個區）：
  1. district：同區、同類型。
  2. district_flat：同區、所有平面類（只給平面類車位）。
  3. city：全市、同類型。
  每層至少 MIN_SAMPLES 個樣本才用；價格與面積各自退層。用到哪一層記在附註
  （up_price_<層>、up_area_<層>）。
- 類型不明（其他、空白）的車位不估算，結果是 none，加註 up_unknown_ptype。
- 百分位數用最近序位法（nearest-rank），結果一定是實際出現過的整數。
"""

import datetime as dt
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from house_watch.models import Parking, TxFacts, UnitPrice, UpBasis
from house_watch.parse.age import add_months
from house_watch.parse.parking import FLAT_TYPES

MIN_SAMPLES = 20
WINDOW_MONTHS = 24
# 單一車位樣本的合理範圍：只用來擋明顯的錯誤值。
PRICE_SAMPLE_RANGE = (50_000, 30_000_000)  # 元
AREA_SAMPLE_RANGE = (100, 20_000)  # 0.01 m²，即 1–200 m²

LEVEL_DISTRICT = "district"
LEVEL_DISTRICT_FLAT = "district_flat"
LEVEL_CITY = "city"
LEVELS = (LEVEL_DISTRICT, LEVEL_DISTRICT_FLAT, LEVEL_CITY)
CITY = "*"  # city 層的 district 欄位
FLAT_GROUP = "flat"  # district_flat 層的 ptype 欄位

Kind = Literal["price", "area"]


@dataclass(frozen=True)
class Quartiles:
    """樣本數與 p25、p50、p75（沒有樣本時是 None）。"""

    n: int
    p25: int | None = None
    p50: int | None = None
    p75: int | None = None


def nearest_rank(values: Sequence[int], percent: int) -> int:
    """最近序位法的百分位數：排序後第 ceil(p/100 × n) 個值。"""
    ordered = sorted(values)
    rank = max(math.ceil(percent * len(ordered) / 100), 1)
    return ordered[rank - 1]


def quartiles(values: Sequence[int]) -> Quartiles:
    if not values:
        return Quartiles(n=0)
    return Quartiles(
        n=len(values),
        p25=nearest_rank(values, 25),
        p50=nearest_rank(values, 50),
        p75=nearest_rank(values, 75),
    )


@dataclass(frozen=True)
class MedianGroup:
    """一組中位數：(層, 區, 類型) 的價格與面積。"""

    level: str
    district: str
    ptype: str
    price: Quartiles
    area: Quartiles


def lookup_chain(district: str, ptype: Parking) -> list[tuple[str, str, str]]:
    """這個車位依序要查的 (層, 區, 類型)；類型不明時是空的。"""
    if ptype in FLAT_TYPES:
        return [
            (LEVEL_DISTRICT, district, ptype.value),
            (LEVEL_DISTRICT_FLAT, district, FLAT_GROUP),
            (LEVEL_CITY, CITY, ptype.value),
        ]
    if ptype == Parking.MECHANICAL:
        return [(LEVEL_DISTRICT, district, ptype.value), (LEVEL_CITY, CITY, ptype.value)]
    return []


@dataclass
class MedianTable:
    """某一版實價登錄資料算出來的車位中位數。"""

    version: str
    groups: dict[tuple[str, str, str], MedianGroup]

    def lookup(
        self, district: str, ptype: Parking, kind: Kind, *, min_n: int = MIN_SAMPLES
    ) -> tuple[Quartiles, str] | None:
        """依分層順序找第一個樣本數夠的組，回傳（四分位數, 層）；都不夠回 None。"""
        for key in lookup_chain(district, ptype):
            group = self.groups.get(key)
            if group is None:
                continue
            stats = group.price if kind == "price" else group.area
            if stats.n >= min_n and stats.p25 is not None and stats.p75 is not None:
                return stats, key[0]
        return None


def compute_medians(
    facts: Iterable[TxFacts], *, version: str, as_of: dt.date, months: int = WINDOW_MONTHS
) -> MedianTable:
    """從成交算出各層的中位數（樣本規則見模組說明）。"""
    start = add_months(as_of, -months)
    prices: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    areas: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for tx in facts:
        if tx.exclusion is not None or not start < tx.trade_date <= as_of:
            continue
        price_split = "parking_price_split" in tx.flags
        area_split = "parking_area_split" in tx.flags
        for space in tx.parkings:
            keys = lookup_chain(tx.district, space.ptype)
            price, area = space.price_yuan, space.area_cm2
            if not price_split and price is not None:
                if PRICE_SAMPLE_RANGE[0] <= price <= PRICE_SAMPLE_RANGE[1]:
                    for key in keys:
                        prices[key].append(price)
            if not area_split and area is not None:
                if AREA_SAMPLE_RANGE[0] <= area <= AREA_SAMPLE_RANGE[1]:
                    for key in keys:
                        areas[key].append(area)
    groups = {
        key: MedianGroup(
            level=key[0],
            district=key[1],
            ptype=key[2],
            price=quartiles(prices.get(key, [])),
            area=quartiles(areas.get(key, [])),
        )
        for key in sorted(set(prices) | set(areas))
    }
    return MedianTable(version=version, groups=groups)


# ---------------------------------------------------------------- 資料庫


def store_medians(conn: sqlite3.Connection, table: MedianTable) -> None:
    """整張 parking_medians 換成這一版（舊版本的列一起刪掉）。交易由呼叫端負責。"""
    conn.execute("DELETE FROM parking_medians")
    conn.executemany(
        "INSERT INTO parking_medians (lvr_version, level, district, ptype, price_n,"
        " price_p25_yuan, price_p50_yuan, price_p75_yuan, area_n, area_p25_cm2, area_p50_cm2,"
        " area_p75_cm2) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                table.version,
                g.level,
                g.district,
                g.ptype,
                g.price.n,
                g.price.p25,
                g.price.p50,
                g.price.p75,
                g.area.n,
                g.area.p25,
                g.area.p50,
                g.area.p75,
            )
            for g in table.groups.values()
        ],
    )


def load_medians(conn: sqlite3.Connection, version: str) -> MedianTable | None:
    """讀回某一版的中位數；沒有這一版回 None。"""
    rows = conn.execute(
        "SELECT level, district, ptype, price_n, price_p25_yuan, price_p50_yuan, price_p75_yuan,"
        " area_n, area_p25_cm2, area_p50_cm2, area_p75_cm2 FROM parking_medians"
        " WHERE lvr_version = ? ORDER BY level, district, ptype",
        (version,),
    ).fetchall()
    if not rows:
        return None
    groups = {}
    for row in rows:
        level, district, ptype = row[0], row[1], row[2]
        groups[(level, district, ptype)] = MedianGroup(
            level=level,
            district=district,
            ptype=ptype,
            price=Quartiles(n=row[3], p25=row[4], p50=row[5], p75=row[6]),
            area=Quartiles(n=row[7], p25=row[8], p50=row[9], p75=row[10]),
        )
    return MedianTable(version=version, groups=groups)


# ---------------------------------------------------------------- 單價區間


@dataclass(frozen=True)
class UnitPriceResult:
    """匯出用的 up，以及要加進 TxRecord.flags 的附註。"""

    up: UnitPrice
    flags: tuple[str, ...] = ()


def _none(*flags: str) -> UnitPriceResult:
    return UnitPriceResult(up=UnitPrice(basis=UpBasis.NONE), flags=tuple(sorted(set(flags))))


class _NoEstimate(Exception):
    """需要估算卻估不出來；args[0] 是附註代碼。"""


def _estimate(
    medians: MedianTable | None, district: str, ptype: Parking, kind: Kind, min_n: int
) -> tuple[int, int, str]:
    """估算一個車位的價格或面積，回傳 (p25, p75, 層)。"""
    if ptype not in FLAT_TYPES and ptype != Parking.MECHANICAL:
        raise _NoEstimate("up_unknown_ptype")
    found = medians.lookup(district, ptype, kind, min_n=min_n) if medians is not None else None
    if found is None:
        raise _NoEstimate("up_no_median")
    stats, level = found
    if stats.p25 is None or stats.p75 is None:  # lookup 已經保證，這裡只是讓型別明確
        raise _NoEstimate("up_no_median")
    return stats.p25, stats.p75, level


def unit_price(
    facts: TxFacts, medians: MedianTable | None, *, min_n: int = MIN_SAMPLES
) -> UnitPriceResult:
    """一筆成交的 q、k 區間（規則見模組說明）。"""
    total, bldg = facts.total_yuan, facts.bldg_cm2
    if not total or not bldg:
        return _none("up_no_base")
    flags: set[str] = set()
    known_price = known_area = 0
    price_lo = price_hi = area_lo = area_hi = 0  # 估算值的 p25、p75 總和
    estimated_area = estimated = False
    try:
        for space in facts.parkings:
            if space.price_yuan:
                known_price += space.price_yuan
            else:
                lo, hi, level = _estimate(medians, facts.district, space.ptype, "price", min_n)
                price_lo, price_hi = price_lo + lo, price_hi + hi
                flags.add(f"up_price_{level}")
                estimated = True
            if space.area_cm2:
                known_area += space.area_cm2
            else:
                lo, hi, level = _estimate(medians, facts.district, space.ptype, "area", min_n)
                area_lo, area_hi = area_lo + lo, area_hi + hi
                flags.add(f"up_area_{level}")
                estimated = estimated_area = True
    except _NoEstimate as missing:
        return _none(str(missing.args[0]))

    q_lo = total - known_price - price_hi
    q_hi = total - known_price - price_lo
    k_known = bldg - known_area
    k_lo = k_hi = k_known
    if estimated_area:
        if facts.main_cm2 is None:
            flags.add("up_no_main")
        floor = (facts.main_cm2 or 0) + (facts.aux_cm2 or 0) + (facts.balcony_cm2 or 0)
        room = max(k_known - floor, 0)
        if area_hi > room:
            flags.add("up_area_capped")
        k_hi = k_known - min(area_lo, room)
        k_lo = k_known - min(area_hi, room)
    if q_lo < 1 or k_lo < 1:
        return _none("up_nonpositive", *flags)
    basis = UpBasis.ESTIMATED if estimated else UpBasis.OFFICIAL
    return UnitPriceResult(
        up=UnitPrice(basis=basis, q_lo=q_lo, q_hi=q_hi, k_lo=k_lo, k_hi=k_hi),
        flags=tuple(sorted(flags)),
    )
