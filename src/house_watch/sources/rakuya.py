"""樂屋網（www.rakuya.com.tw、community.rakuya.com.tw）轉接器。

列表：伺服器渲染的 HTML。
`GET https://www.rakuya.com.tw/sell/result?zipcode=<區[,區]>&price=~<上限>&typecode=R2&sort=<排序>&page=<頁>`
- 只用兩種路徑：`/sell/result`（列表）與詳細頁（`/sell_item/info?ehid=`、
  `https://community.rakuya.com.tw/<社區id>/sell/info?ehid=`）。robots.txt 擋 `/api/`、`/ajax/`、
  `/sell_item/api/`、`/search/sell_search_gaze`、`/doc/` 等路徑；前端用的 XHR
  `/search/sell_search/get-result` 回的也是同樣的卡片，一律不用。
- 不帶 `city`：同時帶 city 與 zipcode 會 301 到只剩 zipcode 的網址，白花一個請求。
- 伺服器端條件只有三個（R1-11）：
  - 行政區（zipcode）與公開範圍的開價上限（price=~<上限>）：每筆刊登一定有區與開價。
  - 型態 typecode=R2（網站的「大樓/華廈」，就是公開範圍的 btypes）。型態是刊登時的必填分類，
    每筆都有；其他分類（公寓 R1、套房 R3、別墅 R4、透天厝 R5、樓中樓 R6、店面、辦公…）在網站上
    都不算大樓/華廈，對應的 BType 都在公開範圍外。所以這個條件不會丟掉缺值的物件。
  - 不加車位（other=）、屋齡（age=）、主建坪（mainsize=）、樓層（floor=）：有的卡片沒有屋齡，
    車位篩選會漏掉沒填車位類型的物件（R2a §2）。
- 排序代碼是「{種類}{遞減 1／遞增 0}」：
  - 完整掃描用總價由低到高（30）。網站沒有依編號的排序，總價是最穩定的一種；完整掃描不用
    更新時間排序（R1-10）。同價的物件之間順序不保證固定，掃描途中可能漏看少數幾筆，
    但下架要連續兩次有效完整掃描都沒看到。
  - 探測用最近更新（21，R2a 實測過）：新刊登與剛更新的物件在最前面。網站大量刷新時，新物件
    可能被擠到前 K 頁之後，由每天的完整掃描補上（R3-09）。
  - 2026-11-21 實際連線確認：sort=30 的回顯是 sortType 3、asc，第 1 頁 19 筆的價格遞增，
    沒有置頂卡；最近更新排序（21）會夾置頂卡（`from=list_favor_upitem`），由同頁去重處理。
    見 tests/fixtures/sources/LIVE_CHECK.md。
- 頁面錨點：`window.sellSearch`（JSON）。用括號配對切出物件（會跳過字串與跳脫字元），
  再用 json.loads 讀，不執行任何 JS。
  - `condition` 是伺服器解讀參數的回顯：區（zipcode）、開價（price）、型態（typecode）要和請求
    一樣，否則 PageInvalid（參數沒生效時，讀到的範圍就不對）。排序的回顯另外在實際連線驗證時核對。
  - `pagination {total, pageCount, pageSize, currentPage}`：total 是回報總數；
    currentPage < pageCount 就有下一頁。

卡片：`section.grid-item.search-obj[data-ehid]`，編號用 section 本身的 data-ehid
（卡片裡的「比較」按鈕也有 data-ehid，不能拿來數）。
- 連結：卡片裡第一個指到詳細頁的 `<a href>`。只認兩種：`www` 主機的 `/sell_item/info?ehid=`，
  以及 `community` 主機的 `/<社區id>/sell/info?ehid=`（順便拿到社區 id → community_key）。
  別的主機、`javascript:` 都不算詳細頁連結。
- 沒有詳細頁連結的卡片是廣告；同一頁重複出現的編號是置頂卡：這兩種直接略過，不算 skipped。
  有詳細頁連結、但編號不合法或和 data-ehid 不同的卡片，算 skipped（ListPage.skipped）。

卡片欄位（R2a §4.2）：
- 區 `.info__geo--area` → district_hints（district）。路名 `.info__geo--road` 或社區名
  `.info__geo--community` 只會出現一個，只有社區名的卡片是正常的。
- `.info__detail-info li` 依文字樣式判斷是哪個欄位，不靠位置（R2a §2 第 2 點、R3-11）：
  屋齡（「6.7年」「10個月」「約…」）、樓層（「x/y樓」，一定要有數字和斜線）、格局（數字接房、廳、衛）、
  型態字（電梯大廈→大樓、華廈…）。型態是大樓或華廈時電梯是推定有（DESIGN 4-6）。
  有的卡片沒有屋齡（只有 3 個 li）。屋齡有小數也有整數（「41年」），一律照顯示值處理，
  不把整數當成 x.0（區間寬一點，不會把邊界上的物件誤判成不符）。
- 面積：`.info__space` 依標題字（`.info__space--title`）分：「總建」→ 權狀坪，「主建」→ 主建物。
- 開價 `.info__price--total`（萬，有千分位）。`.info__price--ori` 是站方原價，只顯示（R1-22）。
- 標籤「平面車位」→ 平面（出入方式不明）；其他一律未知。標籤不可靠（R2a §2），車位以詳細頁為準。
- 標題只用來找「車位另售」這類片語，不保存。更新時間、瀏覽數不讀（C1）。
- 列表與詳細頁都沒有經緯度：site_lat／site_lon 一律是 None，定位靠社區名或路名。

詳細頁：`window.itemInfo`（同樣的括號配對）。`window.itemContact` 是賣方聯絡資料，不讀。
- 先核對 `detail.ehid` 等於這筆的編號，不同就是 PageInvalid（例如被轉到別的物件）。
- `detail.lifts`（有／無）→ 電梯；空白就不改（列表的推定）。
- `detail.mainSize`（「29.61坪」）→ 主建物，只補列表沒有的。
- 車位：`detail.parkingType` 是結構化欄位，優先；空白才看 `detail.parkingGarage`（常是自由文字）。
  結構化欄位只說「平面」時，自由文字可以補出入方式（例如「B2 坡道平面 1 格」），但不能推翻
  結構化的非平面類型。`detail.parking` 寫「無車位」→ 沒有車位；和其他欄位矛盾就是未知。
  自由文字寫公有、私有 → parking_public。
- `price.priceParkingMemo` 寫「含車位」→ 總價含車位（R1-13）；金額（「含車位150萬」）或
  `detail.parkingPrice` → 車位價。`totalsizeParkingMemo` 講的是面積，不用。
- `detail.ageValue`＋`ageUnit`（「約6.7」「年」）→ 完工日區間，和列表的取交集；預售屋 → 完工日不早於抓取日。
- `detail.transFloors`／`surFloors` → 樓層，只補列表沒有的。
- `priceHistorySummary.firstUpPrice`（萬，0 是沒有）→ 站方原價，只在列表沒有原價、而且和現價不同時補。
- 開價一律用列表的（同一欄位、同一精度，R3-22）；詳細頁 itemData 的價格是元，不用。
"""

import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from house_watch.errors import ErrorCode
from house_watch.models import BType, Elevator, ListingFacts, MainKind, Parking
from house_watch.parse.money import parse_money, sane_price
from house_watch.parse.parking import is_public
from house_watch.parse.text import compact
from house_watch.settings import ListingScope
from house_watch.sources.base import (
    MAIN_AREA_MAX_PING,
    PRICE_MAX_YUAN,
    PRICE_MIN_YUAN,
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
    built_presale,
    classify_parking_text,
    clean_text,
    district_hints,
    narrow_built,
    outside_scope_btype,
    parse_age_text,
    parse_floor_field,
    parse_floor_range,
    parse_total_floors,
    ping_text_to_cp,
    price_excl_from_text,
    require_ok,
    response_date,
    split_address,
    to_int,
    updated,
    wan_to_yuan,
)

SOURCE = "rakuya"
WWW_HOST = "www.rakuya.com.tw"
COMMUNITY_HOST = "community.rakuya.com.tw"
WWW_URL = f"https://{WWW_HOST}"
COMMUNITY_URL = f"https://{COMMUNITY_HOST}"
LIST_URL = f"{WWW_URL}/sell/result"
PAGE_SIZE = 19
TYPECODE = "R2"  # 大樓/華廈

SORT_PRICE_ASC = "30"  # 總價由低到高
SORT_UPDATED = "21"  # 最近更新
FULL_SORT = SORT_PRICE_ASC
PROBE_SORT: str | None = SORT_UPDATED

PARKING_PRICE_MAX_YUAN = 50_000_000

_EHID = re.compile(r"[0-9a-f]{15}")
_COMMUNITY_ID = re.compile(r"[1-9]\d{0,11}")
_COMMUNITY_PATH = re.compile(r"/([1-9]\d{0,11})/sell/info/?")
_ZIP = re.compile(r"\d{3}")

# `.info__detail-info li` 的文字樣式（先 compact：NFKC 後去掉空白）。
_AGE_TEXT = re.compile(
    r"(?:約|未滿)?\d+(?:\.\d+)?(?:年(?:\d+個?月)?|個?月)(?:以上|以下|以內|內)?|預售屋?|新成屋"
)
_FLOOR_TEXT = re.compile(r"[^/]+/\D*\d+(?:樓|F|層)")
_LAYOUT_TEXT = re.compile(r"\d+(?:房|廳|衛|室)")
_TYPE_TEXT = re.compile(
    r"大廈|大樓|華廈|公寓|透天|別墅|套房|樓中樓|店面|店鋪|辦公|商辦|廠房|廠辦|土地|倉庫|農舍|住辦"
)

_FLAT_WITH_ACCESS = frozenset({Parking.RAMP_FLAT, Parking.LIFT_FLAT, Parking.GROUND_FLAT})
_ELEVATOR_WORDS = {"有": Elevator.YES, "無": Elevator.NO}
_NO_PARKING_WORDS = frozenset({"無車位", "無"})


# ---------------------------------------------------------------- 內嵌 JSON（不執行 JS）


def _reject_constant(name: str) -> Any:
    raise ValueError(f"不接受的 JSON 常數 {name}")


def _object_end(text: str, start: int) -> int | None:
    """從 text[start] 的 `{` 開始做括號配對，回傳配對結束後的位置；字串裡的括號不算。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
    return None


def window_json(html: str, name: str) -> Any:
    """頁面裡 `window.<name> = {...};` 的 JSON 物件。

    只切出這一個物件（括號配對、跳過字串與跳脫字元），再用 json.loads 讀；後面緊接的
    其他指派（例如 `window.itemContact`）不會被讀到。找不到或不是合法 JSON 就是 PageInvalid。
    """
    match = re.search(rf"window\.{re.escape(name)}\s*=\s*(?=\{{)", html)
    if match is None:
        raise PageInvalid(ErrorCode.PAGE_INVALID, f"沒有 window.{name}")
    end = _object_end(html, match.end())
    if end is None:
        raise PageInvalid(ErrorCode.PAGE_INVALID, f"window.{name} 的括號不配對")
    try:
        return json.loads(html[match.end() : end], parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise PageInvalid(ErrorCode.PAGE_INVALID, f"window.{name} 不是 JSON") from None


# ---------------------------------------------------------------- 小工具


def _text(node: Tag | None, separator: str = " ") -> str | None:
    return node.get_text(separator, strip=True) if node is not None else None


def money_yuan(text: Any) -> int | None:
    """「1,288萬」「898萬」（要帶單位）→ 整數元；不合理（R1-22）或不只一個金額就是 None。"""
    cleaned = clean_text(text, 60)
    if cleaned is None:
        return None
    return sane_price(parse_money(cleaned), lo=PRICE_MIN_YUAN, hi=PRICE_MAX_YUAN)


def _parking_price(text: Any) -> int | None:
    """車位價「150萬」「含車位150萬」→ 元；沒有金額或不合理就是 None。"""
    cleaned = clean_text(text, 60)
    if cleaned is None:
        return None
    yuan = parse_money(cleaned)
    return yuan if yuan is not None and 0 < yuan <= PARKING_PRICE_MAX_YUAN else None


def _elevator_for(btype: BType | None) -> Elevator:
    if btype in (BType.BUILDING, BType.MANSION):
        return Elevator.INFERRED
    if btype is BType.WALKUP:
        return Elevator.NO
    return Elevator.UNKNOWN


def _detail_kind(text: str) -> str | None:
    """`.info__detail-info li` 的文字 → age、floor、layout、type；認不得就是 None。"""
    core = compact(text)
    if _AGE_TEXT.fullmatch(core):
        return "age"
    if _FLOOR_TEXT.fullmatch(core):
        return "floor"
    if _LAYOUT_TEXT.search(core):
        return "layout"
    if _TYPE_TEXT.search(core):
        return "type"
    return None


def _listing_link(card: Tag) -> tuple[str, str, str | None]:
    """卡片的詳細頁連結 →（狀態, 編號, 社區 id）。

    狀態：ok、ad（沒有詳細頁連結：廣告）、broken（有連結，但編號不合法或和 data-ehid 不同）。
    """
    own = card.get("data-ehid")
    own_id = own.strip() if isinstance(own, str) else ""
    for anchor in card.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or "ehid=" not in href:
            continue
        parts = urlsplit(urljoin(LIST_URL, href.strip()))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username or parts.port not in (None, 443):
            continue
        community: str | None = None
        if host == WWW_HOST and parts.path.rstrip("/") == "/sell_item/info":
            community = None
        elif host == COMMUNITY_HOST and (match := _COMMUNITY_PATH.fullmatch(parts.path)):
            community = match.group(1)
        else:
            continue
        values = parse_qs(parts.query).get("ehid", [])
        if len(values) != 1 or not _EHID.fullmatch(values[0]) or values[0] != own_id:
            return "broken", "", None
        return "ok", values[0], community
    return "ad", "", None


def _check_condition(condition: Any, q: AreaQuery) -> None:
    """伺服器的條件回顯要和請求一樣：區、開價上限、型態。"""
    cond = as_mapping(condition)
    if cond is None:
        raise PageInvalid(ErrorCode.PAGE_INVALID, "沒有條件回顯")
    zips = cond.get("zipcode")
    zips = [zips] if isinstance(zips, str) else as_list(zips)
    requested = {d.zip for d in q.districts}
    if zips is None or {str(z) for z in zips} != requested:
        raise PageInvalid(ErrorCode.PAGE_INVALID, "區的回顯和請求不同")
    if cond.get("price") != f"~{q.max_price_wan}":
        raise PageInvalid(ErrorCode.PAGE_INVALID, "開價的回顯和請求不同")
    types = cond.get("typecode")
    types = [types] if isinstance(types, str) else as_list(types)
    if types is None or [str(t) for t in types] != [TYPECODE]:
        raise PageInvalid(ErrorCode.PAGE_INVALID, "型態的回顯和請求不同")


def _pagination(value: Any) -> tuple[int, int, int]:
    """（總數, 總頁數, 目前頁）；不合理就是 PageInvalid。"""
    page = as_mapping(value)
    keys = ("total", "pageCount", "currentPage", "pageSize")
    numbers = [to_int(page.get(k)) for k in keys] if page else []
    if len(numbers) != 4 or any(n is None for n in numbers):
        raise PageInvalid(ErrorCode.PAGE_INVALID, "沒有分頁資料")
    total, pages, current, size = (int(n) for n in numbers if n is not None)
    if total < 0 or pages < 0 or current < 1 or size < 1:
        raise PageInvalid(ErrorCode.PAGE_INVALID, "分頁資料不合理")
    return total, pages, current


class RakuyaAdapter:
    """樂屋網。scope 給公開抓取範圍時，型態不在範圍內的物件不排詳細頁。"""

    source = SOURCE
    parser_version = "1"
    # 伺服器端條件（zipcode＋開價上限＋typecode=R2）或排序改了就改這個字串（R1-09、R1-10）。
    scope_version = "1"
    caps = Caps(
        page_size=PAGE_SIZE,
        has_total=True,
        stable_sort=FULL_SORT,
        probe_sort=PROBE_SORT,
        max_pages=400,
        detail_fields=frozenset(
            {
                "elevator",
                "main_cp",
                "main_kind",
                "parking",
                "parking_public",
                "parking_price_yuan",
                "price_excl_parking",
                "built",
                "floor_lo",
                "floor_hi",
                "floors_total",
                "site_original_price_yuan",
            }
        ),
    )
    hosts = frozenset({WWW_HOST, COMMUNITY_HOST})
    robots_urls = (f"{WWW_URL}/robots.txt", f"{COMMUNITY_URL}/robots.txt")

    def __init__(self, scope: ListingScope | None = None) -> None:
        self._scope = scope

    # ------------------------------------------------------------ 列表

    def prepare(self, fetch: Fetch) -> None:
        """不需要準備：列表是一般的 GET，不要 token。"""
        del fetch

    def list_url(self, q: AreaQuery, page: int, mode: Mode) -> str:
        if page < 1:
            raise ValueError("頁碼從 1 開始")
        sort = self.caps.stable_sort if mode == "full" else self.caps.probe_sort
        if sort is None:
            raise ValueError("樂屋網的探測排序未設定")
        zips = [d.zip for d in q.districts]
        if not zips or not all(_ZIP.fullmatch(z) for z in zips):
            raise ValueError("樂屋網的行政區要是 3 碼郵遞區號")
        if q.max_price_wan < 1:
            raise ValueError("開價上限要是正整數")
        return (
            f"{LIST_URL}?zipcode={','.join(zips)}&price=~{q.max_price_wan}"
            f"&typecode={TYPECODE}&sort={sort}&page={page}"
        )

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec:
        return RequestSpec.get(self.list_url(q, page, mode), headers={"Accept": "text/html"})

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        html = resp.text()
        doc = as_mapping(window_json(html, "sellSearch"))
        if doc is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "window.sellSearch 不是物件")
        _check_condition(doc.get("condition"), q)
        total, pages, current = _pagination(doc.get("pagination"))
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("section.grid-item.search-obj[data-ehid]")
        if current > 1 and not cards:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "第 1 頁以後的空頁")
        ref = response_date(resp)
        items: list[ListingFacts] = []
        seen: set[str] = set()
        skipped = 0
        for card in cards:
            status, ehid, community = _listing_link(card)
            if status == "ad":
                continue
            if status == "broken":
                skipped += 1
                continue
            if ehid in seen:
                continue  # 置頂卡：同一頁重複
            seen.add(ehid)
            facts = self._facts(card, ehid, community, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        return ListPage(items=items, reported_total=total, has_next=current < pages, skipped=skipped)

    def _facts(
        self, card: Tag, ehid: str, community_key: str | None, q: AreaQuery, ref: Any
    ) -> ListingFacts | None:
        title = _text(card.select_one(".card__head h2"))  # 只讀片語，不保存
        road, address_districts = split_address(_text(card.select_one(".info__geo--road")))
        community_name = clean_text(_text(card.select_one(".info__geo--community")), 100)

        btype: BType | None = None
        layout: str | None = None
        built = UNKNOWN_BUILT
        floor_lo = floor_hi = floors_total = None
        for item in card.select(".info__detail-info li"):
            text = clean_text(item.get_text(" ", strip=True), 60)
            kind = _detail_kind(text) if text else None
            if kind == "age":
                built = parse_age_text(text, ref)
            elif kind == "floor":
                floor_lo, floor_hi, floors_total = parse_floor_field(text)
            elif kind == "layout":
                layout = clean_text(text, 40)
            elif kind == "type":
                btype = btype_from_word(text)
        if floor_hi is not None and floors_total is not None and floor_hi > floors_total:
            floors_total = None  # 自相矛盾的總樓層當成未知

        main_cp = title_cp = None
        for item in card.select(".info__space"):
            label = _text(item.select_one(".info__space--title"), "") or ""
            value = item.get_text("", strip=True)
            value = value[len(label) :] if label and value.startswith(label) else value
            if label == "主建":
                main_cp = ping_text_to_cp(value, max_ping=MAIN_AREA_MAX_PING)
            elif label == "總建":
                title_cp = ping_text_to_cp(value)

        price = money_yuan(_text(card.select_one(".info__price--total"), ""))
        original = money_yuan(_text(card.select_one(".info__price--ori"), ""))
        tags = {_text(tag, "") for tag in card.select(".group__tags .tag")}
        parking = Parking.FLAT_UNKNOWN_ACCESS if "平面車位" in tags else Parking.UNKNOWN

        return FactsBuilder().set(
            source=SOURCE,
            source_id=ehid,
            area=q.area,
            community_name=community_name,
            community_key=community_key,
            road_text=road,
            district_hints=district_hints(
                district=_text(card.select_one(".info__geo--area")),
                address_districts=address_districts,
            ),
            price_yuan=price,
            price_excl_parking=price_excl_from_text(title),
            site_original_price_yuan=original if original is not None and original != price else None,
            main_cp=main_cp,
            main_kind=MainKind.MAIN,
            title_area_cp=title_cp,
            floor_lo=floor_lo,
            floor_hi=floor_hi,
            floors_total=floors_total,
            built=built,
            btype=btype,
            elevator=_elevator_for(btype),
            parking=parking,
            layout=layout,
        ).build()

    # ------------------------------------------------------------ 詳細頁

    def needs_detail(self, f: ListingFacts) -> bool:
        """車位型式、電梯（列表只有推定）、主建物要看詳細頁。型態不在公開範圍內的不抓。"""
        if outside_scope_btype(f, self._scope):
            return False
        return (
            f.parking in (Parking.UNKNOWN, Parking.FLAT_UNKNOWN_ACCESS)
            or f.elevator not in (Elevator.YES, Elevator.NO)
            or f.main_cp is None
        )

    def detail_request(self, source_id: str) -> RequestSpec:
        """www 主機的詳細頁；屬於社區的物件會 301 到 community 主機（同一個網站，允許）。"""
        if not _EHID.fullmatch(source_id):
            raise ValueError("樂屋網的編號格式不對")
        return RequestSpec.get(
            f"{WWW_URL}/sell_item/info?ehid={source_id}", headers={"Accept": "text/html"}, kind="detail"
        )

    def detail_request_for(self, base: ListingFacts) -> RequestSpec:
        """有社區 id 就直接連 community 主機，省掉一次 301。"""
        if not _EHID.fullmatch(base.source_id):
            raise ValueError("樂屋網的編號格式不對")
        key = base.community_key
        if key is None or not _COMMUNITY_ID.fullmatch(key):
            return self.detail_request(base.source_id)
        return RequestSpec.get(
            f"{COMMUNITY_URL}/{key}/sell/info?ehid={base.source_id}",
            headers={"Accept": "text/html"},
            kind="detail",
        )

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        info = as_mapping(window_json(resp.text(), "itemInfo"))
        detail = as_mapping(info.get("detail")) if info is not None else None
        if info is None or detail is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁錨點不在")
        if detail.get("ehid") != base.source_id:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁的編號不同")
        price_info = as_mapping(info.get("price")) or {}
        history = as_mapping(info.get("priceHistorySummary")) or {}
        ref = response_date(resp)
        changes: dict[str, Any] = {}

        lifts = clean_text(detail.get("lifts"), 10, placeholders=False) or ""
        elevator = _ELEVATOR_WORDS.get(lifts)
        if elevator is not None:
            changes["elevator"] = elevator

        if base.main_cp is None:
            main = ping_text_to_cp(detail.get("mainSize"), max_ping=MAIN_AREA_MAX_PING)
            if main is not None:
                changes.update(main_cp=main, main_kind=MainKind.MAIN)

        parking = detail_parking(detail)
        if parking is not Parking.UNKNOWN:
            changes["parking"] = parking
        words = (clean_text(detail.get(k), 100) for k in ("parkingType", "parkingGarage"))
        public = is_public(" ".join(w for w in words if w))
        if public is not None:
            changes["parking_public"] = public

        memo = clean_text(price_info.get("priceParkingMemo"), 100) or ""
        if price_excl_from_text(memo):
            changes["price_excl_parking"] = True
        elif "含車位" in memo and base.price_excl_parking is not True:
            changes["price_excl_parking"] = False
        parking_price = _parking_price(detail.get("parkingPrice"))
        if parking_price is None and "車位" in memo:
            parking_price = _parking_price(memo)
        if parking_price is not None:
            changes["parking_price_yuan"] = parking_price

        changes["built"] = narrow_built(base.built, _detail_built(detail, ref))

        lo, hi = parse_floor_range(detail.get("transFloors"))
        total = parse_total_floors(detail.get("surFloors"))
        if base.floor_lo is None and lo is not None:
            changes.update(floor_lo=lo, floor_hi=hi)
        floor_hi = changes.get("floor_hi", base.floor_hi)
        consistent = floor_hi is None or (total is not None and floor_hi <= total)
        if base.floors_total is None and total is not None and consistent:
            changes["floors_total"] = total

        if base.site_original_price_yuan is None:
            first = wan_to_yuan(history.get("firstUpPrice"))  # 0 是沒有資料：不合理範圍 → None
            if first is not None and first != base.price_yuan:
                changes["site_original_price_yuan"] = first
        return updated(base, **changes)


def detail_parking(detail: Any) -> Parking:
    """詳細頁的車位（R1-12）：結構化的 parkingType 優先，空白才用 parkingGarage 的自由文字。

    - 結構化欄位只說「平面」時，自由文字可以補出入方式；不能推翻結構化的非平面類型。
    - parkingGarage 只寫「無」不算沒有車位（多半是沒填說明），有沒有車位看 `parking` 欄。
    - `parking` 寫無車位：其他欄位沒寫類型 → none；寫了類型 → 矛盾，未知。
    """
    item = as_mapping(detail) or {}
    type_text = clean_text(item.get("parkingType"), 100, placeholders=False)
    garage_text = clean_text(item.get("parkingGarage"), 100, placeholders=False)
    has = clean_text(item.get("parking"), 20, placeholders=False) or ""
    structured = classify_parking_text(type_text) if type_text else Parking.UNKNOWN
    free = classify_parking_text(garage_text) if garage_text else Parking.UNKNOWN
    if free is Parking.NONE:
        free = Parking.UNKNOWN
    if structured is Parking.FLAT_UNKNOWN_ACCESS and free in _FLAT_WITH_ACCESS:
        found = free
    elif structured is not Parking.UNKNOWN:
        found = structured
    else:
        found = free
    if has in _NO_PARKING_WORDS:
        return Parking.NONE if found in (Parking.UNKNOWN, Parking.NONE) else Parking.UNKNOWN
    if found is Parking.NONE and has == "有車位":
        return Parking.UNKNOWN
    return found


def _detail_built(detail: Any, ref: Any) -> Any:
    item = as_mapping(detail) or {}
    value = clean_text(item.get("ageValue"), 20)
    if value:
        unit = clean_text(item.get("ageUnit"), 10) or ""
        return parse_age_text(f"{value}{unit}", ref)
    if "預售" in (clean_text(item.get("agetype"), 20) or ""):
        return built_presale(ref)
    return UNKNOWN_BUILT
