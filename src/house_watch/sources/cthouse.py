"""中信房屋（buy.cthouse.com.tw）轉接器。

列表：`POST /api/house_list.ashx`，JSON body `{"arg": "<搜尋路徑>", "page": "<頁>"}`。
- arg 就是網址 `/area/` 後面那串：`桃園市-city/桃園區-town/0-<上限>-price/[<n>-ord/]`。
- 伺服器端條件只用「行政區」與公開範圍的開價上限（R1-11）：開價是每筆刊登的必要欄位，
  這個條件不會丟掉缺值的物件。型態、屋齡、坪數、樓層都不在伺服器端篩。
- 搜尋狀態記在第 1 頁回應設的 cookie：同一次執行內一定要先讀第 1 頁再往後翻；
  換了 session 直接讀第 2 頁會拿到「RS=OK、0 筆」的空頁，parse_list 判成 PageInvalid。
- 回應錨點：`RS == "OK"`、`total`、`totalpage`、`page`。

欄位（R2b §3.1）：
- 價格 `sell_price`（萬，字串，可能有小數）；`origin_trust_fee` 是站方原價，只顯示（R1-22）。
- `age` 是無條件捨去的整數年；完工日區間照 r4 §3.10。詳細頁有「X年Y個月」，補到月。
- `house_type_class`：2＝電梯住宅（有電梯；11 樓以上算大樓、10 樓以下算華廈）、
  1＝公寓、5＝透天、0＝土地；其他代碼未知。
- 車位 `parking_lot_multi[]`：出入方式（坡道／平面／升降）× 型式（平面／機械）。
  他牌物件沒有車位欄位，是未知不是沒有。`parking_lot_price > 0` 時網站在總價旁標
  「(含車位)」，所以總價含車位（R1-13）。
- 社區：`object_tag` 裡種類 1 的那筆（名稱＋社區頁編號）。
- 編號：7 位數是中信自己的物件；8 位數是他牌物件（50…信義、80…住商、90…太平洋）。
  原品牌編號只對中信自己的物件寫（住商的 `c<編號>` 對到這裡，R1-29）。
- 不讀標題以外的文字；標題（case_name）只用來找「車位另售」這類片語，不保存。
"""

import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from house_watch.errors import ErrorCode
from house_watch.models import BType, Elevator, ListingFacts, MainKind, Parking
from house_watch.settings import ListingScope
from house_watch.sources.base import (
    MAIN_AREA_MAX_PING,
    UNKNOWN_BUILT,
    AreaQuery,
    Caps,
    FactsBuilder,
    Fetch,
    ListPage,
    Mode,
    PageInvalid,
    RequestSpec,
    Resp,
    as_list,
    as_mapping,
    btype_by_height,
    built_from_floor_years,
    built_presale,
    built_span_days,
    clean_text,
    combine_parking,
    district_hints,
    load_json,
    narrow_built,
    none_unless_title_mentions_parking,
    outside_scope_btype,
    parse_age_text,
    parse_floor_field,
    parse_floor_range,
    parse_total_floors,
    ping_to_cp,
    price_excl_from_text,
    require_ok,
    response_date,
    site_point,
    split_address,
    to_decimal,
    to_int,
    updated,
    wan_to_yuan,
)

SOURCE = "cthouse"
HOST = "buy.cthouse.com.tw"
BASE_URL = f"https://{HOST}"
LIST_API = f"{BASE_URL}/api/house_list.ashx"
PAGE_SIZE = 20

# 排序代碼（URL 的 `<n>-ord`）。"0" 是網站預設，不加 ord 段。
# 2026-11-21 實際連線確認：預設排序是 created_date 新到舊（兩頁 19/19 遞減、跨頁連續、
# 沒有重複），新刊登一定排在最前面，所以探測也用它。其他 ord 代碼的語意沒有查到
# （選單定義檔沒抓到），完整掃描暫用預設排序：掃描途中被刷新的物件會跳到第 1 頁而
# 這一輪沒看到，但下架要連續兩次有效完整掃描都沒看到，影響很小。見 LIVE_CHECK.md。
SORT_DEFAULT = "0"
FULL_SORT = SORT_DEFAULT
PROBE_SORT: str | None = SORT_DEFAULT

_OWN_ID = re.compile(r"[1-9]\d{0,6}")
_ANY_ID = re.compile(r"[1-9]\d{0,8}")
_SINYI_ORIGIN = re.compile(r"5\d{7}")  # 原品牌是信義：不抓詳細頁（信義條款禁止自動讀取，保守處理）
_COMMUNITY_URL = re.compile(r"/community/(\d{6,24})\.html")
_HOUSE_ID_VAR = re.compile(r"var\s+house_id\s*=\s*[\"'](\d{1,9})[\"']")

_CLASS_ELEVATOR = 2
_CLASS_WALKUP = 1
_CLASS_TOWNHOUSE = 5
_CLASS_LAND = 0

PARKING_PRICE_MAX_YUAN = 50_000_000


def _parking_price_yuan(value: Any) -> int | None:
    number = to_decimal(value)
    if number is None or number <= 0:
        return None
    yuan = int(number * 10_000)
    return yuan if yuan <= PARKING_PRICE_MAX_YUAN else None


def _space_kind(space: Any) -> Parking:
    """一個車位的（出入方式, 型式）→ Parking。"""
    item = as_mapping(space) or {}
    exit_way = clean_text(item.get("parking_lot_exit"), 20, placeholders=False) or ""
    kind = clean_text(item.get("parking_lot_type"), 20, placeholders=False) or ""
    if kind == "平面":
        if "升降" in exit_way or "昇降" in exit_way:
            return Parking.LIFT_FLAT
        if exit_way == "坡道":
            return Parking.RAMP_FLAT
        if exit_way in ("平面", "一樓"):
            return Parking.GROUND_FLAT
        return Parking.FLAT_UNKNOWN_ACCESS
    if "機械" in kind or "塔式" in kind:
        return Parking.MECHANICAL
    return Parking.UNKNOWN


def _community(tags: Any) -> tuple[str | None, str | None]:
    for tag in as_list(tags) or []:
        if isinstance(tag, list) and len(tag) >= 2 and str(tag[0]) == "1":
            name = clean_text(tag[1], 100)
            link = tag[2] if len(tag) > 2 and isinstance(tag[2], str) else ""
            match = _COMMUNITY_URL.search(link)
            return name, (match.group(1) if match else None)
    return None, None


def _btype_elevator(
    type_class: int | None, floors_total: int | None, floor_hi: int | None
) -> tuple[BType | None, Elevator]:
    if type_class == _CLASS_ELEVATOR:
        if floors_total is None and floor_hi is not None and floor_hi >= 11:
            return BType.BUILDING, Elevator.YES  # 物件本身就在 11 樓以上
        return btype_by_height(floors_total), Elevator.YES
    if type_class == _CLASS_WALKUP:
        return BType.WALKUP, Elevator.NO
    if type_class == _CLASS_TOWNHOUSE:
        return BType.TOWNHOUSE, Elevator.UNKNOWN
    if type_class == _CLASS_LAND:
        return BType.OTHER, Elevator.UNKNOWN
    return None, Elevator.UNKNOWN


class CthouseAdapter:
    """中信房屋。scope 給公開抓取範圍時，型態不在範圍內的物件不排詳細頁。"""

    source = SOURCE
    parser_version = "1"
    caps = Caps(
        page_size=PAGE_SIZE,
        has_total=True,
        stable_sort=FULL_SORT,
        probe_sort=PROBE_SORT,
        max_pages=250,
        detail_fields=frozenset({"built"}),
    )
    hosts = frozenset({HOST})
    robots_urls = (f"{BASE_URL}/robots.txt",)

    def __init__(self, scope: ListingScope | None = None) -> None:
        self._scope = scope

    # ------------------------------------------------------------ 列表

    def prepare(self, fetch: Fetch) -> None:
        """不需要準備：第 1 頁的回應自己會設搜尋狀態的 cookie。"""
        del fetch

    def search_path(self, q: AreaQuery, mode: Mode) -> str:
        if len(q.districts) != 1:
            raise ValueError("中信的搜尋一次只查一個行政區")
        sort = self.caps.stable_sort if mode == "full" else self.caps.probe_sort
        if sort is None:
            raise ValueError("中信的探測排序未設定，只做每日完整掃描")
        district = q.districts[0]
        path = f"{q.city}-city/{district.name}-town/0-{q.max_price_wan}-price/"
        return path if sort == SORT_DEFAULT else f"{path}{sort}-ord/"

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec:
        if page < 1:
            raise ValueError("頁碼從 1 開始")
        arg = self.search_path(q, mode)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/area/{quote(arg)}",
        }
        return RequestSpec.post_json(LIST_API, {"arg": arg, "page": str(page)}, headers=headers)

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        doc = as_mapping(load_json(resp))
        if doc is None or doc.get("RS") != "OK":
            raise PageInvalid(ErrorCode.PAGE_INVALID, "RS != OK")
        total, total_pages, page = (to_int(doc.get(k)) for k in ("total", "totalpage", "page"))
        houses = doc.get("houses")
        houses = [] if houses is None else as_list(houses)
        if houses is None or total is None or total_pages is None or page is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "列表錨點不在")
        if page > 1 and not houses:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "第 1 頁以後的空頁（搜尋狀態遺失或超過最後一頁）")
        ref = response_date(resp)
        items: list[ListingFacts] = []
        skipped = 0
        for house in houses:
            facts = self._facts(as_mapping(house) or {}, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        return ListPage(items=items, reported_total=total, has_next=page < total_pages, skipped=skipped)

    def _facts(self, h: Any, q: AreaQuery, ref: Any) -> ListingFacts | None:
        raw_id = h.get("id")
        source_id = str(raw_id) if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
        if not _ANY_ID.fullmatch(source_id):
            return None
        target = clean_text(h.get("target"), 40)
        own = target == SOURCE and _OWN_ID.fullmatch(source_id) is not None
        title = h.get("case_name")  # 只讀片語，不保存

        price = wan_to_yuan(h.get("sell_price"))
        original = wan_to_yuan(h.get("origin_trust_fee"))
        lat, lon = site_point(h.get("latitude"), h.get("longitude"))
        road, address_districts = split_address(h.get("address"))
        community_name, community_key = _community(h.get("object_tag"))

        floor_lo, floor_hi, floors_total = parse_floor_field(h.get("floor_val"))
        if floor_lo is None:
            floor_lo, floor_hi = parse_floor_range(h.get("position_from"))
            high, _ = parse_floor_range(h.get("position_to"))
            if floor_lo is not None and high is not None and high >= floor_lo:
                floor_hi = high
        if floors_total is None:
            floors_total = parse_total_floors(h.get("storey_above_ground"))
        if floor_hi is not None and floors_total is not None and floor_hi > floors_total:
            floors_total = None  # 自相矛盾的總樓層當成未知

        btype, elevator = _btype_elevator(to_int(h.get("house_type_class")), floors_total, floor_hi)
        parking, parking_price = self._parking(h, own, title)
        excluded = price_excl_from_text(title)
        if parking_price is not None:  # 網站在總價旁標「(含車位)」
            price_excl = None if excluded else False
        else:
            price_excl = excluded

        return FactsBuilder().set(
            source=SOURCE,
            source_id=source_id,
            area=q.area,
            community_name=community_name,
            community_key=community_key,
            road_text=road,
            district_hints=district_hints(
                zip_code=h.get("township_id"), address_districts=address_districts
            ),
            site_lat=lat,
            site_lon=lon,
            price_yuan=price,
            price_excl_parking=price_excl,
            parking_price_yuan=parking_price,
            site_original_price_yuan=original if original is not None and original != price else None,
            main_cp=ping_to_cp(h.get("main_build"), max_ping=MAIN_AREA_MAX_PING),
            main_kind=MainKind.MAIN,
            title_area_cp=ping_to_cp(h.get("house_area")),
            floor_lo=floor_lo,
            floor_hi=floor_hi,
            floors_total=floors_total,
            built=self._built(h, ref),
            btype=btype,
            elevator=elevator,
            parking=parking,
            layout=clean_text(h.get("r_type"), 40),
            brand_origin_id=f"{SOURCE}:{source_id}" if own else None,
        ).build()

    @staticmethod
    def _built(h: Any, ref: Any) -> Any:
        label = clean_text(h.get("age_val"), 20, placeholders=False) or ""
        if "預售" in label:
            return built_presale(ref)
        years = to_int(h.get("age"))
        if years is not None and years > 0 and label == f"{years}年":
            return built_from_floor_years(years, ref)
        return UNKNOWN_BUILT  # 0 年、「--」、空白都是未知（r4 §3.10）；0 年的物件詳細頁有「0年N個月」

    @staticmethod
    def _parking(h: Any, own: bool, title: Any) -> tuple[Parking, int | None]:
        spaces = [s for s in as_list(h.get("parking_lot_multi")) or [] if isinstance(s, dict)]
        price = _parking_price_yuan(h.get("parking_lot_price"))
        if price is None:
            prices = [_parking_price_yuan(s.get("parking_lot_price")) for s in spaces]
            known = [p for p in prices if p is not None]
            price = sum(known) if known else None
        if spaces:
            return combine_parking(_space_kind(s) for s in spaces), price
        if to_int(h.get("parking_lot_belong")) == 1 or not own:
            return Parking.UNKNOWN, price  # 有車位但沒填型式；他牌物件沒有車位欄位
        return none_unless_title_mentions_parking(title), price

    # ------------------------------------------------------------ 詳細頁

    def needs_detail(self, f: ListingFacts) -> bool:
        """整數屋齡只知道到年、0 年當成未知；詳細頁有「X年Y個月」。信義原品牌與土地不抓。"""
        if _SINYI_ORIGIN.fullmatch(f.source_id) or outside_scope_btype(f, self._scope):
            return False
        if f.btype is BType.OTHER:
            return False
        if f.built.lo is not None and f.built.hi is None:
            return False  # 預售屋：詳細頁也只寫預售
        span = built_span_days(f.built)
        return span is None or span > 62

    def detail_request(self, source_id: str) -> RequestSpec:
        if not _ANY_ID.fullmatch(source_id):
            raise ValueError("中信的編號只有數字")
        return RequestSpec.get(
            f"{BASE_URL}/house/{source_id}.html", headers={"Accept": "text/html"}, kind="detail"
        )

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        html = resp.text()
        match = _HOUSE_ID_VAR.search(html)
        if match is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁錨點不在（可能已下架）")
        if match.group(1) != base.source_id:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁的編號不同")
        soup = BeautifulSoup(html, "html.parser")
        built = parse_age_text(_labeled_value(soup, "屋齡"), response_date(resp), floor_years=True)
        return updated(base, built=narrow_built(base.built, built))


def _labeled_value(soup: BeautifulSoup, label: str) -> str | None:
    """依序找「標籤／值」的三種版面；HTML 註解裡的舊版面不算。"""
    for th in soup.select(".item__th"):
        if th.get_text(strip=True) == label:
            td = th.find_next_sibling(class_="item__td")
            if td is not None:
                return td.get_text(" ", strip=True)
    for box in soup.select(".spec-box"):
        name = box.select_one(".spec-box-label")
        value = box.select_one(".spec-box-value")
        if name is not None and value is not None and name.get_text(strip=True) == label:
            return value.get_text(" ", strip=True)
    for item in soup.find_all("li"):
        text = item.get_text(" ", strip=True)
        if text.startswith(f"{label}："):
            return text.split("：", 1)[1].strip()
    return None
