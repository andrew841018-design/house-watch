"""流程測試共用（不是測試檔）：合成的轉接器與網站、MockTransport 的抓取器、跑 update 的小工具。

- 合成網站 fake.example 只回 JSON，結構是測試自己定的；數字、路名、社區名都是示意值。
- 轉接器用正式的來源 id（cthouse），讓設定裡的預算與開關照常生效；它沒有 robots.txt。
- 全部離線：抓取器一律注入 httpx.MockTransport，sleep 換成假時鐘的 advance。
"""

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from house_watch import cli, db
from house_watch.clock import FakeClock
from house_watch.errors import ErrorCode
from house_watch.export import site_dir
from house_watch.http import Fetcher
from house_watch.models import BType, DateRange, Elevator, ListingFacts, ListingsFile, MainKind
from house_watch.paths import DataPaths
from house_watch.settings import load_settings
from house_watch.sources.base import (
    AreaQuery,
    Caps,
    FactsBuilder,
    ListPage,
    PageInvalid,
    RequestSpec,
    Resp,
    built_from_floor_years,
    classify_parking_text,
    load_json,
    ping_to_cp,
    require_ok,
    response_date,
    updated,
    wan_to_yuan,
)

HOST = "fake.example"
BASE = f"https://{HOST}"
START = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)  # 台北 09:00
PAGE_SIZE = 3


@dataclass
class Unit:
    """合成網站上的一筆刊登。"""

    id: str
    price_wan: float = 1234
    floor: int = 7
    floors_total: int | None = 15
    main_ping: float | None = 25.5
    title_ping: float | None = 41.2
    road: str | None = "示意路一段"
    community: str | None = None
    district: str = "桃園區"
    lat: float | None = None
    lon: float | None = None
    parking: str | None = None  # 列表上的車位文字
    detail_parking: str | None = "坡道平面"  # 詳細頁的車位文字
    origin: str | None = None
    order: int = 0  # 越大越新（探測的排序）
    age: int | None = None  # 整數屋齡（無條件捨去），轉接器依抓取日換成完工日區間
    area: str = "taoyuan_core"  # 出現在哪個區域的查詢結果
    broken: bool = False  # 卡片解析不出來（算 skipped）

    def card(self) -> dict:
        if self.broken:
            return {"id": None}
        return {
            "id": self.id,
            "price": self.price_wan,
            "floor": self.floor,
            "floors_total": self.floors_total,
            "main": self.main_ping,
            "title": self.title_ping,
            "road": self.road,
            "community": self.community,
            "district": self.district,
            "lat": self.lat,
            "lon": self.lon,
            "parking": self.parking,
            "origin": self.origin,
            "age": self.age,
        }


@dataclass
class FakeSite:
    """合成的列表網站。mode：normal、down（503）、blocked（403）、broken（不是 JSON）。"""

    units: dict[str, Unit] = field(default_factory=dict)
    mode: str = "normal"
    shrink_to: int | None = None  # 只回前 n 筆，總數也照 n 回報（模擬網站少回）
    down_areas: set[str] = field(default_factory=set)  # 這些區域的列表回 503
    requests: list[str] = field(default_factory=list)

    def add(self, *units: Unit) -> None:
        for unit in units:
            self.units[unit.id] = unit

    def remove(self, *ids: str) -> None:
        for unit_id in ids:
            self.units.pop(unit_id, None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if request.url.host != HOST:
            return httpx.Response(404)
        if self.mode == "down":
            return httpx.Response(503, text="maintenance")
        if self.mode == "blocked":
            return httpx.Response(403, text="forbidden")
        if self.mode == "broken":
            return httpx.Response(200, text="<html>改版了</html>")
        path = request.url.path
        if path.startswith("/detail/"):
            unit = self.units.get(path.rsplit("/", 1)[1])
            if unit is None:
                return httpx.Response(404)
            body = {"id": unit.id, "parking": unit.detail_parking}
            return httpx.Response(200, json=body)
        query = parse_qs(urlsplit(str(request.url)).query)
        page = int(query["page"][0])
        mode = query["mode"][0]
        area = query["area"][0]
        if area in self.down_areas:
            return httpx.Response(503, text="maintenance")
        here = [u for u in self.units.values() if u.area == area]
        units = sorted(here, key=lambda u: u.id)
        if mode == "probe":
            units = sorted(here, key=lambda u: (-u.order, u.id))
        if self.shrink_to is not None:
            units = units[: self.shrink_to]
        pages = max(1, -(-len(units) // PAGE_SIZE))
        chunk = units[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        body = {
            "total": len(units),
            "pages": pages,
            "page": page,
            "items": [u.card() for u in chunk],
        }
        return httpx.Response(200, json=body)


class FakeAdapter:
    """合成網站的轉接器（照 C5）。"""

    source = "cthouse"
    parser_version = "t1"
    scope_version = "1"
    caps = Caps(
        page_size=PAGE_SIZE,
        has_total=True,
        stable_sort="id",
        probe_sort="new",
        max_pages=50,
        detail_fields=frozenset({"parking", "elevator"}),
    )
    hosts = frozenset({HOST})
    robots_urls: tuple[str, ...] = ()

    def __init__(self, scope=None) -> None:
        self.scope = scope

    def prepare(self, fetch) -> None:
        del fetch

    def list_request(self, q: AreaQuery, page: int, mode) -> RequestSpec:
        return RequestSpec.get(f"{BASE}/list?area={q.area}&mode={mode}&page={page}")

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        doc = load_json(resp)
        if not isinstance(doc, dict) or "total" not in doc:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "錨點不在")
        items, skipped = [], 0
        ref = response_date(resp)
        for card in doc["items"]:
            facts = self._facts(card, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        return ListPage(items, doc["total"], doc["page"] < doc["pages"], skipped)

    def _facts(self, card: dict, q: AreaQuery, ref) -> ListingFacts | None:
        if not card.get("id"):
            return None
        age = card.get("age")
        built = built_from_floor_years(age, ref) if age is not None else DateRange()
        return (
            FactsBuilder()
            .set(
                source=self.source,
                source_id=card["id"],
                area=q.area,
                price_yuan=wan_to_yuan(card["price"]),
                floor_lo=card["floor"],
                floor_hi=card["floor"],
                floors_total=card["floors_total"],
                main_cp=ping_to_cp(card["main"]),
                main_kind=MainKind.MAIN if card["main"] is not None else None,
                title_area_cp=ping_to_cp(card["title"]),
                road_text=card["road"],
                community_name=card["community"],
                district_hints=[("district", card["district"])],
                site_lat=card["lat"],
                site_lon=card["lon"],
                parking=classify_parking_text(card["parking"]),
                btype=BType.BUILDING,
                elevator=Elevator.INFERRED,
                brand_origin_id=card["origin"],
                built=built,
            )
            .build()
        )

    def needs_detail(self, f: ListingFacts) -> bool:
        return f.parking.value in ("unknown", "flat_unknown_access")

    def detail_request(self, source_id: str) -> RequestSpec:
        return RequestSpec.get(f"{BASE}/detail/{source_id}", kind="detail")

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        doc = load_json(resp)
        if not isinstance(doc, dict) or doc.get("id") != base.source_id:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "編號不同")
        return updated(base, parking=classify_parking_text(doc.get("parking")))


# ---------------------------------------------------------------- 抓取器（cli 的 fetcher 入口）

SITE = FakeSite()


class MockFetcher:
    """cli.ENTRY_POINTS["fetcher"] 的替身：正式的 Fetcher，只是換成 MockTransport 與假時鐘。"""

    @classmethod
    def from_settings(cls, conn, settings, adapters, *, clock):
        return Fetcher.from_settings(
            conn,
            settings,
            adapters,
            clock=clock,
            transport=httpx.MockTransport(SITE.handler),
            sleep=clock.advance,
        )


def install(monkeypatch, site: FakeSite) -> None:
    """把合成網站、轉接器與抓取器接到 cli 與 scan 上。"""
    import pipeline_kit

    from house_watch.pipeline import scan

    monkeypatch.setattr(pipeline_kit, "SITE", site)
    monkeypatch.setitem(cli.ENTRY_POINTS, "fetcher", "pipeline_kit:MockFetcher")
    monkeypatch.setattr(scan, "ADAPTERS", {"cthouse": "pipeline_kit:FakeAdapter"})


# ---------------------------------------------------------------- 跑 update


@dataclass
class Env:
    clock: FakeClock
    data: DataPaths
    conn: sqlite3.Connection

    def update(
        self, *argv: str, accept: tuple[str, ...] = (), areas: tuple[str, ...] = ("taoyuan_core",)
    ) -> cli.UpdateReport:
        settings = load_settings()
        args = list(argv) or ["sources"]
        plan = cli.plan_update(
            settings, only=args, sources=["cthouse"], areas=list(areas), accept=accept
        )
        return cli.run_update(settings, self.conn, data=self.data, clock=self.clock, plan=plan)

    def listings_file(self, area: str = "taoyuan_core") -> ListingsFile:
        path = site_dir(self.data) / "data" / f"listings_{area}.json"
        return ListingsFile.model_validate_json(path.read_bytes())

    def scans(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT scan_id, mode, status, error_code, pages, parsed FROM scans ORDER BY scan_id"
        ).fetchall()

    def listing(self, source_id: str, source: str = "cthouse") -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()


def new_env(start: dt.datetime = START) -> Env:
    data = DataPaths(load_settings().data_dir)
    conn = db.open_or_init(data)
    return Env(FakeClock(start), data, conn)


def write_settings(text: str) -> None:
    import os

    Path(os.environ["HOUSE_WATCH_CONFIG"]).write_text(text, encoding="utf-8")


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
