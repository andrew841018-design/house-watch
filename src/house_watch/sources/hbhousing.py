"""住商不動產（www.hbhousing.com.tw）轉接器。

列表：`POST /proxy/api/HB/BuyHouseRelated/GetHouseDataCount`，JSON body（欄位名取自網站前端）。
- 伺服器端條件只用城市代碼、郵遞區號與公開範圍的開價上限（R1-11）。
  型態（style）、坪數、屋齡都不在伺服器端篩。每頁 50 筆（pageRows=50，原型已驗證可用）。
- 排序代碼（前端選單）：null 預設、8「上架新到舊」。完整掃描與探測都用 8。
- 回應錨點：`code == 200`、`data.cnts`（總數）、`data.buyHouseListDatas[]`。
  回應不帶頁碼，所以 has_next 只能看這頁是不是滿的；總數剛好是 50 的倍數時，
  最後會多讀一個空頁（WP6 可以用 reported_total 提早停）。

欄位（R2b §3.3）：
- `price`、`originalPrice`（萬，整數）；`originalPrice` 是站方原價，只顯示。
- `age` 是一位小數；`style` 是型態（大樓、華廈、公寓…）；`parking` 是權屬，不是型式。
- 詳細頁 `/detail?sn=<sn>`：`__NUXT_DATA__`（devalue 格式），自己寫解碼器，不執行任何 JS
  （R2-10）。讀 `parkingMethod`、`elevator_YN`、`completionDate`、`community`、`communityNo`。
  詳細頁也有經紀人資料，不讀。
- 編號：`c<7 位數>` 是中信的物件（原品牌編號 `cthouse:<n>`，R1-29）；`s…` 是信義原品牌，
  不抓詳細頁。其他前綴（太平洋、大家…）沒有驗證過對應，不寫原品牌編號。
- 標題（objName）只用來找「車位另售」這類片語，不保存。
"""

import json
import re
from collections import deque
from typing import Any
from urllib.parse import quote

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
    btype_from_word,
    built_from_decimal_years,
    built_from_iso_date,
    built_presale,
    built_span_days,
    classify_parking_text,
    clean_text,
    district_hints,
    load_json,
    narrow_built,
    none_unless_title_mentions_parking,
    outside_scope_btype,
    parse_floor_range,
    parse_total_floors,
    ping_to_cp,
    price_excl_from_text,
    require_ok,
    response_date,
    site_point,
    split_address,
    to_decimal,
    updated,
    wan_to_yuan,
)

SOURCE = "hbhousing"
HOST = "www.hbhousing.com.tw"
BASE_URL = f"https://{HOST}"
LIST_API = f"{BASE_URL}/proxy/api/HB/BuyHouseRelated/GetHouseDataCount"
PAGE_ROWS = 50
CITY_NO = {"桃園市": 5}

SORT_LISTED_NEWEST = "8"  # 上架新到舊
FULL_SORT = SORT_LISTED_NEWEST
PROBE_SORT: str | None = SORT_LISTED_NEWEST

_SN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,23}")
_CTHOUSE_ORIGIN = re.compile(r"c(\d{7})")
_SINYI_ORIGIN = re.compile(r"s\d")
_NUXT_DATA = re.compile(
    r"<script\b[^>]*\bid=[\"']__NUXT_DATA__[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)

_PUBLIC_WORDS = frozenset({"公有", "抽籤"})
_PRIVATE_WORDS = frozenset({"私有"})


def _parking_from_ownership(word: str | None, title: Any) -> tuple[Parking, bool | None]:
    """列表的 parking 是權屬（私有、公有、無、有車、抽籤…），型式要看詳細頁。"""
    if word is None:
        return Parking.UNKNOWN, None
    if word == "無":
        return none_unless_title_mentions_parking(title), None
    if word in _PUBLIC_WORDS:
        return Parking.UNKNOWN, True
    if word in _PRIVATE_WORDS:
        return Parking.UNKNOWN, False
    return Parking.UNKNOWN, None


def _elevator_for(btype: BType | None) -> Elevator:
    if btype in (BType.BUILDING, BType.MANSION):
        return Elevator.INFERRED
    if btype is BType.WALKUP:
        return Elevator.NO
    return Elevator.UNKNOWN


class HbhousingAdapter:
    """住商不動產。scope 給公開抓取範圍時，型態不在範圍內的物件不排詳細頁。"""

    source = SOURCE
    parser_version = "1"
    caps = Caps(
        page_size=PAGE_ROWS,
        has_total=True,
        stable_sort=FULL_SORT,
        probe_sort=PROBE_SORT,
        max_pages=120,
        detail_fields=frozenset(
            {
                "built",
                "parking",
                "elevator",
                "community_name",
                "community_key",
                "price_excl_parking",
                "site_lat",
                "site_lon",
            }
        ),
    )
    hosts = frozenset({HOST})
    robots_urls = (f"{BASE_URL}/robots.txt",)

    def __init__(self, scope: ListingScope | None = None) -> None:
        self._scope = scope

    # ------------------------------------------------------------ 列表

    def prepare(self, fetch: Fetch) -> None:
        """不需要準備（API 不要 token）。"""
        del fetch

    def list_body(self, q: AreaQuery, page: int, mode: Mode) -> dict[str, Any]:
        city_no = CITY_NO.get(q.city)
        if city_no is None:
            raise ValueError(f"住商的城市代碼沒有這個城市：{q.city}")
        sort = self.caps.stable_sort if mode == "full" else self.caps.probe_sort
        if sort is None:
            raise ValueError("住商的探測排序未設定")
        empty: dict[str, Any] = dict.fromkeys(
            (
                "priceStart areaStart areaFinish ageStart ageFinish roomStart roomFinish "
                "bathStart bathFinish hallStart hallFinish floorStart floorFinish location "
                "keyWord searchTopics storeID employeeID upriceStart upriceFinish schoolId "
                "partnerNo other parkingMethod"
            ).split()
        )
        return {
            **empty,
            "vrType": 0,
            "page": page,
            "pageRows": PAGE_ROWS,
            "sort": int(sort),
            "cityNo": city_no,
            "zipCode": [d.zip for d in q.districts],
            "style": [],
            "type": [],
            "priceFinish": q.max_price_wan,
            "areaType": "P",
            "tag": [],
            "theme": [],
        }

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec:
        if page < 1:
            raise ValueError("頁碼從 1 開始")
        body = self.list_body(q, page, mode)
        first_zip = q.districts[0].zip if q.districts else ""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/buyhouse/{quote(q.city)}/{first_zip}",
        }
        return RequestSpec.post_json(LIST_API, body, headers=headers)

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        doc = as_mapping(load_json(resp))
        data = as_mapping(doc.get("data")) if doc is not None else None
        if doc is None or doc.get("code") != 200 or data is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "code != 200")
        total = to_decimal(data.get("cnts"))
        rows = data.get("buyHouseListDatas")
        rows = [] if rows is None and total == 0 else as_list(rows)
        if rows is None or total is None or total < 0:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "列表錨點不在")
        ref = response_date(resp)
        items: list[ListingFacts] = []
        skipped = 0
        for row in rows:
            facts = self._facts(as_mapping(row) or {}, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        has_next = len(rows) >= PAGE_ROWS and int(total) > PAGE_ROWS
        return ListPage(items=items, reported_total=int(total), has_next=has_next, skipped=skipped)

    def _facts(self, x: Any, q: AreaQuery, ref: Any) -> ListingFacts | None:
        sn = x.get("sn")
        if not isinstance(sn, str) or not _SN.fullmatch(sn):
            return None
        title = x.get("objName")  # 只讀片語，不保存
        price = wan_to_yuan(x.get("price"))
        original = wan_to_yuan(x.get("originalPrice"))
        lat, lon = site_point(x.get("lat"), x.get("lon"))
        road, address_districts = split_address(x.get("doorplate"))
        floor_lo, floor_hi = parse_floor_range(x.get("floor"))
        floors_total = parse_total_floors(x.get("floorTotal"))
        if floor_hi is not None and floors_total is not None and floor_hi > floors_total:
            floors_total = None
        btype = btype_from_word(x.get("style"))
        age = to_decimal(x.get("age"))  # 一位小數；JSON 的 33 其實是 33.0
        if age is None or age == 0:
            built = UNKNOWN_BUILT  # 0 年是未知（r4 §3.10）
        else:
            built = built_from_decimal_years(age, ref, decimals=1)
        word = clean_text(x.get("parking"), 20, placeholders=False)
        parking, public = _parking_from_ownership(word, title)
        origin = _CTHOUSE_ORIGIN.fullmatch(sn)
        return FactsBuilder().set(
            source=SOURCE,
            source_id=sn,
            area=q.area,
            road_text=road,
            district_hints=district_hints(
                zip_code=x.get("zipCode"), address_districts=address_districts
            ),
            site_lat=lat,
            site_lon=lon,
            price_yuan=price,
            price_excl_parking=price_excl_from_text(title),
            site_original_price_yuan=original if original is not None and original != price else None,
            main_cp=ping_to_cp(x.get("mainArea"), max_ping=MAIN_AREA_MAX_PING),
            main_kind=MainKind.MAIN,
            title_area_cp=ping_to_cp(x.get("area")),
            floor_lo=floor_lo,
            floor_hi=floor_hi,
            floors_total=floors_total,
            built=built,
            btype=btype,
            elevator=_elevator_for(btype),
            parking=parking,
            parking_public=public,
            layout=clean_text(x.get("special"), 40),
            brand_origin_id=f"cthouse:{origin.group(1)}" if origin else None,
        ).build()

    # ------------------------------------------------------------ 詳細頁

    def needs_detail(self, f: ListingFacts) -> bool:
        """車位型式、電梯、完工日都要看詳細頁。信義原品牌的物件不抓。"""
        if _SINYI_ORIGIN.match(f.source_id) or outside_scope_btype(f, self._scope):
            return False
        span = built_span_days(f.built)
        coarse_built = span is None or span > 0
        return (
            f.parking is Parking.UNKNOWN
            or f.elevator not in (Elevator.YES, Elevator.NO)
            or coarse_built
        )

    def detail_request(self, source_id: str) -> RequestSpec:
        if not _SN.fullmatch(source_id):
            raise ValueError("住商的編號格式不對")
        return RequestSpec.get(
            f"{BASE_URL}/detail?sn={quote(source_id)}", headers={"Accept": "text/html"}, kind="detail"
        )

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        html = resp.text()
        match = _NUXT_DATA.search(html)
        if match is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "沒有 __NUXT_DATA__")
        try:
            payload = json.loads(match.group(1))
        except (ValueError, RecursionError):
            raise PageInvalid(ErrorCode.PAGE_INVALID, "__NUXT_DATA__ 不是 JSON") from None
        house = find_house(devalue_decode(payload), base.source_id)
        if house is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "__NUXT_DATA__ 裡沒有這筆物件")
        ref = response_date(resp)
        changes: dict[str, Any] = {}

        built = built_from_iso_date(house.get("completionDate"), ref)
        if built is None and house.get("presale") in (1, True):
            built = built_presale(ref)
        if built is not None:
            changes["built"] = narrow_built(base.built, built)

        parking = None
        if clean_text(house.get("parkingMethod")):
            parking = classify_parking_text(house.get("parkingMethod"))
        if parking in (None, Parking.UNKNOWN) and house.get("parking_YN") in (0, False):
            parking = Parking.NONE
        if parking is not None and parking is not Parking.UNKNOWN:
            changes["parking"] = parking

        elevator = {1: Elevator.YES, 0: Elevator.NO}.get(_flag(house.get("elevator_YN")))
        if elevator is not None:
            changes["elevator"] = elevator

        changes["community_name"] = clean_text(house.get("community"), 100)
        changes["community_key"] = clean_text(house.get("communityNo"), 40)
        if base.site_lat is None:
            lat, lon = site_point(house.get("lat"), house.get("lon"))
            changes.update(site_lat=lat, site_lon=lon)
        visible = _SCRIPT.sub("", html)
        if "含車位價" in visible and base.price_excl_parking is not True:
            changes["price_excl_parking"] = False
        return updated(base, **changes)


def _flag(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.strip().upper() in ("Y", "1", "N", "0"):
        return 1 if value.strip().upper() in ("Y", "1") else 0
    return None


# ---------------------------------------------------------------- devalue 解碼（不執行 JS）

_MAX_DEPTH = 64
_SPECIAL = {-1: None, -2: None, -3: None, -4: None, -5: None, -6: 0.0}
_WRAPPERS = frozenset(
    {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef", "Skip"}
)
_SCALAR_TAGS = frozenset({"Date", "RegExp", "BigInt", "URL", "URLSearchParams", "Object"})


def devalue_decode(values: Any) -> Any:
    """把 devalue 的扁平陣列還原成 Python 物件。

    - 陣列第 0 個元素是根。物件與陣列裡的整數是索引；負數是特殊值（undefined、NaN…→ None）。
    - `["Reactive", i]` 這類 Nuxt 包裝直接取內容；Date、BigInt 等保留原字串；
      Set 變成 list，Map 變成 dict（鍵轉成字串）；認不得的種類當成 None。
    - 有深度上限，循環參照也安全；格式不對就是 PageInvalid。
    """
    if not isinstance(values, list) or not values:
        raise PageInvalid(ErrorCode.PAGE_INVALID, "devalue 不是非空陣列")
    done: dict[int, Any] = {}

    def hydrate(index: Any, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "devalue 太深")
        if not isinstance(index, int) or isinstance(index, bool):
            raise PageInvalid(ErrorCode.PAGE_INVALID, "devalue 索引不是整數")
        if index < 0:
            return _SPECIAL.get(index)
        if index >= len(values):
            raise PageInvalid(ErrorCode.PAGE_INVALID, "devalue 索引超出範圍")
        if index in done:
            return done[index]
        value = values[index]
        if value is None or isinstance(value, (bool, int, float, str)):
            done[index] = value
            return value
        if isinstance(value, dict):
            obj: dict[str, Any] = {}
            done[index] = obj
            for key, ref in value.items():
                obj[str(key)] = hydrate(ref, depth + 1)
            return obj
        if not isinstance(value, list):
            raise PageInvalid(ErrorCode.PAGE_INVALID, "devalue 值的型別不對")
        if value and isinstance(value[0], str):
            tag, args = value[0], value[1:]
            if tag == "Set":
                items: list[Any] = []
                done[index] = items
                items.extend(hydrate(ref, depth + 1) for ref in args)
                return items
            if tag in ("Map", "null"):
                mapping: dict[str, Any] = {}
                done[index] = mapping
                for pos in range(0, len(args) - 1, 2):
                    key = hydrate(args[pos], depth + 1) if tag == "Map" else args[pos]
                    mapping[str(key)] = hydrate(args[pos + 1], depth + 1)
                return mapping
            if tag in _WRAPPERS:
                result = hydrate(args[0], depth + 1) if args else None
            elif tag in _SCALAR_TAGS:
                result = args[0] if args and isinstance(args[0], (str, int, float, bool)) else None
            else:
                result = None
            done[index] = result
            return result
        array: list[Any] = []
        done[index] = array
        array.extend(None if ref == -2 else hydrate(ref, depth + 1) for ref in value)
        return array

    return hydrate(0, 0)


def find_house(doc: Any, sn: str, *, limit: int = 50_000) -> dict[str, Any] | None:
    """在解碼後的資料裡找 `sn` 等於這筆、而且有物件欄位的那個 dict（寬度優先，有節點上限）。"""
    queue: deque[Any] = deque([doc])
    seen: set[int] = set()
    visited = 0
    while queue and visited < limit:
        node = queue.popleft()
        if id(node) in seen:
            continue
        seen.add(id(node))
        visited += 1
        if isinstance(node, dict):
            if node.get("sn") == sn and ("parkingMethod" in node or "completionDate" in node):
                return node
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None
