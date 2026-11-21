"""東森房屋（www.etwarm.com.tw）轉接器。

列表：`POST /houses/buy-list-json`（Laravel），要先在同一個 session 讀一頁 HTML 拿 CSRF token：
- `prepare(fetch)`：GET `/houses?searchKind=buy`，從 `<meta name="csrf-token">` 取 token，
  cookie 由抓取層的 session 保管（只在記憶體）。token 只放在這個轉接器實例裡，不寫進檔案或 log。
- 伺服器端條件只用「縣市＋區」與公開範圍的開價上限（R1-11）；不送型態、坪數、屋齡。
- 排序 `NEW` 是編號遞減（R2b 實測），完整掃描與探測都用它。
- 回應錨點：Laravel 分頁的 `data[]`、`total`、`current_page`、`last_page`。

欄位（R2b §3.2）：
- `刊登售價`（萬，字串）；`價變前金額` 是站方原價，只顯示。
- `建物坪數` 是「主建物＋附屬建物」的合併值（main_kind=main_plus_aux）；`總坪數` 含車位。
  有髒資料（例如 26,327 坪），超出合理範圍就當未知。
- `屋齡` 是一位小數（「19.1年」）。樓層、座標、電梯、社區在列表上沒有。
- `刊登售價備註` 有「含車位價格」時，總價含車位（R1-13）。
- 批次 API `POST /houses/buy-action-json`（`{"ids": [...]}`，一次最多 10 個）回傳樓層，
  是網站「收藏／比較」功能用的端點，用量要節制（batch_request／parse_batch，C5 之外的補充）。
- 詳細頁 `/houses/buy/<編號>`：表格是 `div.d-table` 裡的「標籤／值」兩格。
  座標取物件地圖 `#house-life-tab1[data-component="house-life-map"]` 的 data-lat／data-lng；
  `#store_map_container` 那組是門市位置，不能用。
- 案名只用來找「車位另售」這類片語，不保存。
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup

from house_watch.errors import ErrorCode
from house_watch.models import BType, Elevator, ListingFacts, MainKind, Parking
from house_watch.settings import ListingScope
from house_watch.sources.base import (
    MAIN_AREA_MAX_PING,
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
    classify_parking_text,
    clean_text,
    district_hints,
    load_json,
    narrow_built,
    outside_scope_btype,
    parse_age_text,
    parse_floor_field,
    ping_text_to_cp,
    ping_to_cp,
    price_excl_from_text,
    require_ok,
    response_date,
    site_point,
    split_address,
    to_int,
    updated,
    wan_to_yuan,
)

SOURCE = "etwarm"
HOST = "www.etwarm.com.tw"
BASE_URL = f"https://{HOST}"
LIST_PAGE = f"{BASE_URL}/houses?searchKind=buy"
LIST_API = f"{BASE_URL}/houses/buy-list-json"
ACTION_API = f"{BASE_URL}/houses/buy-action-json"
PAGE_SIZE = 10
BATCH_SIZE = 10

SORT_NEWEST = "NEW"  # 編號遞減
FULL_SORT = SORT_NEWEST
PROBE_SORT: str | None = SORT_NEWEST
# 型態條件：None 表示不在伺服器端篩型態（R1-11）。
LIST_TYPE: str | None = None

AGE_DECIMALS = 1  # 屋齡顯示到小數一位（「19.1年」）；「33年」其實是 33.0

_ID = re.compile(r"[1-9]\d{0,9}")
_TOKEN = re.compile(r"[A-Za-z0-9]{16,128}")
_INCLUDES_PARKING = re.compile(r"含車位價")


def _elevator_for(btype: BType | None) -> Elevator:
    if btype in (BType.BUILDING, BType.MANSION):
        return Elevator.INFERRED
    if btype is BType.WALKUP:
        return Elevator.NO
    return Elevator.UNKNOWN


def _price_excl(remark: Any, title: Any) -> bool | None:
    """備註寫「含車位價格」→ False；案名寫「車位另售」→ True；兩個都有就是矛盾 → None。"""
    includes = bool(clean_text(remark) and _INCLUDES_PARKING.search(clean_text(remark) or ""))
    excludes = price_excl_from_text(title)
    if includes:
        return None if excludes else False
    return excludes


def _layout(value: Any) -> str | None:
    if isinstance(value, list):
        return clean_text("".join(str(part) for part in value if isinstance(part, (str, int))), 40)
    return clean_text(value, 40)


def csrf_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    token = meta.get("content") if meta is not None else None
    return token if isinstance(token, str) and _TOKEN.fullmatch(token) else None


class EtwarmAdapter:
    """東森房屋。prepare() 之後才能組列表與批次請求。"""

    source = SOURCE
    parser_version = "1"
    caps = Caps(
        page_size=PAGE_SIZE,
        has_total=True,
        stable_sort=FULL_SORT,
        probe_sort=PROBE_SORT,
        max_pages=100,
        detail_fields=frozenset(
            {
                "floor_lo",
                "floor_hi",
                "floors_total",
                "site_lat",
                "site_lon",
                "elevator",
                "parking",
                "community_name",
                "built",
                "main_cp",
                "price_excl_parking",
            }
        ),
    )
    hosts = frozenset({HOST})
    robots_urls = (f"{BASE_URL}/robots.txt",)
    batch_size = BATCH_SIZE

    def __init__(self, scope: ListingScope | None = None) -> None:
        self._scope = scope
        self._csrf: str | None = None

    # ------------------------------------------------------------ 列表

    def prepare(self, fetch: Fetch) -> None:
        """讀一頁 HTML 取 CSRF token（同一個 session 的 cookie 由抓取層保管）。"""
        resp = fetch(RequestSpec.get(LIST_PAGE, headers={"Accept": "text/html"}, kind="prepare"))
        require_ok(resp)
        token = csrf_token(resp.text())
        if token is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "沒有 csrf-token")
        self._csrf = token

    def _api_headers(self) -> dict[str, str]:
        if self._csrf is None:
            raise RuntimeError("先呼叫 prepare() 取得 CSRF token")
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-CSRF-TOKEN": self._csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": LIST_PAGE,
        }

    def list_body(self, q: AreaQuery, page: int, mode: Mode) -> dict[str, Any]:
        sort = self.caps.stable_sort if mode == "full" else self.caps.probe_sort
        if sort is None:
            raise ValueError("東森的探測排序未設定")
        empty = dict.fromkeys(
            "mrt ping1 ping2 old1 old2 room feature pos video keyword theme".split()
        )
        return {
            **empty,
            "area": "-".join(f"{q.city}{d.name}" for d in q.districts),
            "type": LIST_TYPE,
            "price1": 0,
            "price2": q.max_price_wan,
            "sort": sort,
            "page": page,
            "pingType": "1",
        }

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec:
        if page < 1:
            raise ValueError("頁碼從 1 開始")
        return RequestSpec.post_json(
            LIST_API, self.list_body(q, page, mode), headers=self._api_headers()
        )

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        doc = as_mapping(load_json(resp))
        if doc is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "不是 JSON 物件")
        rows = as_list(doc.get("data"))
        total, page, last = (to_int(doc.get(k)) for k in ("total", "current_page", "last_page"))
        if rows is None or total is None or page is None or last is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "列表錨點不在")
        if page > 1 and not rows:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "第 1 頁以後的空頁")
        ref = response_date(resp)
        items: list[ListingFacts] = []
        skipped = 0
        for row in rows:
            facts = self._facts(as_mapping(row) or {}, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        return ListPage(items=items, reported_total=total, has_next=page < last, skipped=skipped)

    def _facts(self, x: Any, q: AreaQuery, ref: Any) -> ListingFacts | None:
        source_id = str(x.get("編號") or "")
        if not _ID.fullmatch(source_id):
            return None
        price = wan_to_yuan(x.get("刊登售價"))
        original = wan_to_yuan(x.get("價變前金額"))
        btype = BType.OTHER if x.get("isLand") is True else btype_from_word(x.get("物件類別"))
        road, address_districts = split_address(x.get("地址"))
        return FactsBuilder().set(
            source=SOURCE,
            source_id=source_id,
            area=q.area,
            road_text=road,
            district_hints=district_hints(
                district=x.get("鄉鎮市區"), address_districts=address_districts
            ),
            price_yuan=price,
            price_excl_parking=_price_excl(x.get("刊登售價備註"), x.get("案名")),
            site_original_price_yuan=original if original is not None and original != price else None,
            main_cp=ping_to_cp(x.get("建物坪數"), max_ping=MAIN_AREA_MAX_PING),
            main_kind=MainKind.MAIN_PLUS_AUX,
            title_area_cp=ping_to_cp(x.get("總坪數")),
            built=parse_age_text(x.get("屋齡"), ref, decimals=AGE_DECIMALS),
            btype=btype,
            elevator=_elevator_for(btype),
            parking=classify_parking_text(x.get("車位")),
            layout=_layout(x.get("格局")),
        ).build()

    # ------------------------------------------------------------ 批次（樓層）

    def batch_request(self, source_ids: Sequence[str]) -> RequestSpec:
        """一次最多 10 個編號，補樓層。"""
        ids = list(source_ids)
        if not 1 <= len(ids) <= BATCH_SIZE or not all(_ID.fullmatch(i) for i in ids):
            raise ValueError("批次要 1–10 個數字編號")
        return RequestSpec.post_json(ACTION_API, {"ids": ids}, headers=self._api_headers(), kind="batch")

    def parse_batch(
        self, resp: Resp, bases: Mapping[str, ListingFacts]
    ) -> dict[str, ListingFacts]:
        require_ok(resp)
        rows = as_list(load_json(resp))
        if rows is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "批次回應不是陣列")
        result: dict[str, ListingFacts] = {}
        for row in rows:
            item = as_mapping(row) or {}
            source_id = str(item.get("編號") or "")
            base = bases.get(source_id)
            if base is None:
                continue
            lo, hi, total = parse_floor_field(item.get("樓層"))
            if hi is not None and total is not None and hi > total:
                total = None
            result[source_id] = updated(base, floor_lo=lo, floor_hi=hi, floors_total=total)
        return result

    # ------------------------------------------------------------ 詳細頁

    def needs_detail(self, f: ListingFacts) -> bool:
        """座標、樓層、電梯只在詳細頁有。"""
        if outside_scope_btype(f, self._scope):
            return False
        return (
            f.site_lat is None
            or f.floor_lo is None
            or f.elevator not in (Elevator.YES, Elevator.NO)
        )

    def detail_request(self, source_id: str) -> RequestSpec:
        if not _ID.fullmatch(source_id):
            raise ValueError("東森的編號只有數字")
        return RequestSpec.get(
            f"{BASE_URL}/houses/buy/{source_id}", headers={"Accept": "text/html"}, kind="detail"
        )

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        expected = f"/houses/buy/{base.source_id}"
        if not resp.url.split("?", 1)[0].rstrip("/").endswith(expected):
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁網址不對（可能被轉到別頁）")
        soup = BeautifulSoup(resp.text(), "html.parser")
        pairs = _pairs(soup)
        if "物件編號" not in pairs or "樓層" not in pairs:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁錨點不在")
        changes: dict[str, Any] = {}

        lo, hi, total = parse_floor_field(pairs.get("樓層"))
        if hi is not None and total is not None and hi > total:
            total = None
        changes.update(floor_lo=lo, floor_hi=hi, floors_total=total)

        elevator = {"有": Elevator.YES, "無": Elevator.NO}.get(pairs.get("電梯") or "")
        if elevator is not None:
            changes["elevator"] = elevator

        parking_text = clean_text(pairs.get("車位"), placeholders=False)
        if parking_text and parking_text not in ("－", "-", "--"):
            parking = classify_parking_text(parking_text)
            if parking is not Parking.UNKNOWN:
                changes["parking"] = parking

        changes["community_name"] = clean_text(pairs.get("社區"), 100)
        built = parse_age_text(pairs.get("屋齡"), response_date(resp), decimals=AGE_DECIMALS)
        changes["built"] = narrow_built(base.built, built)
        if base.main_cp is None:
            main = ping_text_to_cp(pairs.get("主建物+附屬建物"), max_ping=MAIN_AREA_MAX_PING)
            if main is not None:
                changes.update(main_cp=main, main_kind=MainKind.MAIN_PLUS_AUX)
        if _INCLUDES_PARKING.search(pairs.get("總價") or "") and base.price_excl_parking is not True:
            changes["price_excl_parking"] = False

        house_map = soup.select_one('#house-life-tab1[data-component="house-life-map"]')
        if house_map is not None:
            lat, lon = site_point(house_map.get("data-lat"), house_map.get("data-lng"))
            changes.update(site_lat=lat, site_lon=lon)
        return updated(base, **changes)


def _pairs(soup: BeautifulSoup) -> dict[str, str]:
    """`div.d-table` 裡剛好兩格的「標籤／值」。同一個標籤以第一次出現的為準。"""
    pairs: dict[str, str] = {}
    for table in soup.select("div.d-table"):
        cells = table.find_all("div", class_="d-table-cell", recursive=False)
        if len(cells) != 2:
            continue
        label = cells[0].get_text("", strip=True).rstrip("：:").strip()
        value = " ".join(cells[1].get_text(" ", strip=True).split())
        if label and label not in pairs:
            pairs[label] = value
    return pairs
