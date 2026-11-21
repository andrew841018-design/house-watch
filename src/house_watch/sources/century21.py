"""21世紀不動產（www.century21.com.tw）轉接器。

列表：伺服器渲染的 HTML。
`GET https://www.century21.com.tw/buy/<城市>/<郵遞區號[_郵遞區號]>/residence?priceFilter=below-<上限>&order=<排序>&page=<頁>`
- 網址文法取自網站前端（`index-search.js`）：路徑是城市英文（桃園市＝taoyuan-city）、
  郵遞區號（多個用 `_` 連接）、物件用途；查詢參數由 URLSearchParams 組成。
- 開價條件：前端的自訂區間寫成 `below-${最高價}`，可以填任意數字，所以直接用公開範圍的開價上限
  （`priceFilter=below-<上限>`，單位萬）。
- 伺服器端條件只有行政區、用途「住宅」（residence）與開價上限（R1-11）：用途是刊登時的必填分類，
  每筆都有，不會丟掉缺值的物件。型態、屋齡、坪數都不在伺服器端篩。
- 排序：完整掃描用總價由低到高（price-low-to-high；網站沒有依編號的排序，總價最穩定，
  完整掃描不用上架時間，R1-10）；探測用上架時間新到舊（newly）。同價物件之間的順序不保證固定。
- 只連 www 主機。首頁會轉到 new.century21.com.tw，但列表與詳細頁都在 www，不需要也不跟隨
  其他主機或頁面上的外部連結（new 主機首頁有無關的外部連結）。
- 頁面錨點：`ul#product_list`。總數由前端另外用 JS 載入，伺服器渲染的頁面看不到：
  reported_total 一律是 None，有沒有下一頁看分頁列的「下一頁」連結（`#pagination a[rel=next]`）。
- 2026-11-21 實際連線確認：排序選單的選取項回顯同樣的路徑、priceFilter 與 order（參數都生效），
  第 1 頁的價格遞增。超過最後一頁的頁碼回 200、但沒有 `ul#product_list`，這裡判成 PageInvalid；
  只看「下一頁」連結翻頁就不會讀到那種頁。見 tests/fixtures/sources/LIVE_CHECK.md。

卡片：`ul#product_list > div.item`。
- 編號取自卡片裡指到 www 主機 `/buypage/<數字>` 的連結。沒有這種連結的卡片是廣告，別的主機的
  連結也不算；同一頁重複的編號略過。這兩種都不算 skipped。有 `/buypage/` 連結、但編號不合法
  或一張卡片指到兩個編號，算 skipped（ListPage.skipped）。
- `.info span`：地址是地圖圖示那一列的 span（split_address → 路段＋地址開頭的區名）；其他 span
  依文字樣式判斷（型態「電梯大樓 [住宅]」、格局「3房(室)2廳2衛」、權狀坪「44.08坪」、
  樓層「9 樓 / 15 樓」、屋齡「6.7 年」）。透天這類卡片沒有格局，span 會少一個。
  型態是大樓或華廈時電梯是推定有（DESIGN 4-6）。屋齡照顯示值處理。
- 開價 `samp.price`＋`samp.unit`（萬）。降價時 `samp.order-price` 是原價，只顯示（R1-22）。
  價格旁寫「(含車位價)」→ 總價含車位（R1-13）。
- `.community` 不可靠（有時填的是學校名），不當社區名用。
- 標題只用來找「車位另售」這類片語，不保存。列表與詳細頁都沒有經緯度。

詳細頁：`https://www.century21.com.tw/buypage/<編號>`。
- 先核對編號：回應網址的路徑要是 `/buypage/<編號>`，分享按鈕的 `data-url` 也要都指到這個編號，
  否則 PageInvalid。
- 「標籤／值」是 `h6 span` → 同一格的 `div.val`。只讀下面這幾個標籤；經紀人姓名、電話、
  證號都不讀（頁面的 meta description 也有電話，不讀）。
  - 主建物面積 → 主建物（主建物而已；「公設車位」不是主建物）。
  - 車位「有 (坡道-平面)」→ 括號裡的型式（片語表）；「無」→ 沒有車位；只寫「有」→ 未知。
  - 電梯「1 部」（有的頁面標籤是「電梯部數」）：> 0 部 → 有，0 部 → 沒有。
  - 屋齡 → 完工日區間，和列表的取交集。
"""

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from house_watch.errors import ErrorCode
from house_watch.models import BType, Elevator, ListingFacts, MainKind, Parking
from house_watch.parse.money import parse_money, sane_price
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
    btype_from_word,
    classify_parking_text,
    clean_text,
    district_hints,
    narrow_built,
    outside_scope_btype,
    parse_age_text,
    parse_floor_field,
    ping_text_to_cp,
    price_excl_from_text,
    require_ok,
    response_date,
    split_address,
    updated,
)

SOURCE = "century21"
HOST = "www.century21.com.tw"
BASE_URL = f"https://{HOST}"
PAGE_SIZE = 20
CITY_EN = {"桃園市": "taoyuan-city"}
PURPOSE = "residence"  # 住宅

SORT_PRICE_ASC = "price-low-to-high"
SORT_NEWLY = "newly"  # 上架時間新到舊
FULL_SORT = SORT_PRICE_ASC
PROBE_SORT: str | None = SORT_NEWLY

_ID = re.compile(r"[1-9]\d{0,11}")
_BUYPAGE = re.compile(r"/buypage/([^/]*)/?")
_SHARE_URL = re.compile(r"https://www\.century21\.com\.tw/buypage/(\d{1,12})/?")
_ZIP = re.compile(r"\d{3}")

# `.info span` 的文字樣式（先 compact：NFKC 後去掉空白）。
_AGE_TEXT = re.compile(
    r"(?:約|未滿)?\d+(?:\.\d+)?(?:年(?:\d+個?月)?|個?月)(?:以上|以下|以內|內)?|預售屋?|新成屋"
)
_FLOOR_TEXT = re.compile(r"[^/]+/\D*\d+(?:樓|F|層)")
_AREA_TEXT = re.compile(r"[\d,]+(?:\.\d+)?坪")
_LAYOUT_TEXT = re.compile(r"\d+(?:房|廳|衛|室)")
_TYPE_TEXT = re.compile(
    r"大廈|大樓|華廈|公寓|透天|別墅|套房|店面|店鋪|辦公|商辦|廠房|廠辦|土地|倉庫|農舍|車位|住辦|\[.+\]"
)

_WANTED_LABELS = frozenset({"主建物面積", "車位", "電梯", "電梯部數", "屋齡"})
_LIFT_COUNT = re.compile(r"(\d+)部")
_PAREN = re.compile(r"[(（]([^)）]*)[)）]")
_NO_PARKING = re.compile(r"無(?:車位)?")


def _text(node: Tag | None, separator: str = " ") -> str | None:
    return node.get_text(separator, strip=True) if node is not None else None


def money_yuan(text: Any) -> int | None:
    """「1,780萬」「1,088 萬」（要帶單位）→ 整數元；不合理（R1-22）或不只一個金額就是 None。"""
    cleaned = clean_text(text, 60)
    if cleaned is None:
        return None
    return sane_price(parse_money(cleaned), lo=PRICE_MIN_YUAN, hi=PRICE_MAX_YUAN)


def _elevator_for(btype: BType | None) -> Elevator:
    if btype in (BType.BUILDING, BType.MANSION):
        return Elevator.INFERRED
    if btype is BType.WALKUP:
        return Elevator.NO
    return Elevator.UNKNOWN


def _span_kind(text: str) -> str | None:
    """`.info span`（地址以外）的文字 → age、floor、area、layout、type；認不得就是 None。"""
    core = compact(text)
    if _AGE_TEXT.fullmatch(core):
        return "age"
    if _FLOOR_TEXT.fullmatch(core):
        return "floor"
    if _AREA_TEXT.fullmatch(core):
        return "area"
    if _LAYOUT_TEXT.search(core):
        return "layout"
    if _TYPE_TEXT.search(core):
        return "type"
    return None


def _card_id(card: Tag) -> tuple[str, str]:
    """卡片的編號 →（狀態, 編號）。狀態：ok、ad（沒有本站的 /buypage/ 連結）、broken。"""
    found: set[str] = set()
    malformed = False
    for anchor in card.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or "/buypage/" not in href:
            continue
        parts = urlsplit(urljoin(f"{BASE_URL}/", href.strip()))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host != HOST or parts.port not in (None, 443):
            continue  # 別的主機：不是這張卡片的詳細頁
        match = _BUYPAGE.fullmatch(parts.path)
        if match is not None and _ID.fullmatch(match.group(1)):
            found.add(match.group(1))
        else:
            malformed = True
    if len(found) == 1:
        return "ok", next(iter(found))
    if found or malformed:
        return "broken", ""
    return "ad", ""


def _price_excl(includes: bool, title: Any) -> bool | None:
    """價格旁寫「(含車位價)」→ False；標題寫「車位另售」→ True；兩個都有就是矛盾 → None。"""
    excludes = price_excl_from_text(title)
    if includes:
        return None if excludes else False
    return excludes


class Century21Adapter:
    """21世紀不動產。scope 給公開抓取範圍時，型態不在範圍內的物件不排詳細頁。"""

    source = SOURCE
    parser_version = "1"
    # 伺服器端條件（區＋住宅＋開價上限）或排序改了就改這個字串（R1-09、R1-10）。
    scope_version = "1"
    caps = Caps(
        page_size=PAGE_SIZE,
        has_total=False,
        stable_sort=FULL_SORT,
        probe_sort=PROBE_SORT,
        max_pages=80,
        detail_fields=frozenset({"main_cp", "main_kind", "parking", "elevator", "built"}),
    )
    hosts = frozenset({HOST})
    robots_urls = (f"{BASE_URL}/robots.txt",)

    def __init__(self, scope: ListingScope | None = None) -> None:
        self._scope = scope

    # ------------------------------------------------------------ 列表

    def prepare(self, fetch: Fetch) -> None:
        """不需要準備：列表是一般的 GET。"""
        del fetch

    def list_url(self, q: AreaQuery, page: int, mode: Mode) -> str:
        if page < 1:
            raise ValueError("頁碼從 1 開始")
        city = CITY_EN.get(q.city)
        if city is None:
            raise ValueError(f"21世紀的城市代碼沒有這個城市：{q.city}")
        sort = self.caps.stable_sort if mode == "full" else self.caps.probe_sort
        if sort is None:
            raise ValueError("21世紀的探測排序未設定")
        zips = [d.zip for d in q.districts]
        if not zips or not all(_ZIP.fullmatch(z) for z in zips):
            raise ValueError("21世紀的行政區要是 3 碼郵遞區號")
        if q.max_price_wan < 1:
            raise ValueError("開價上限要是正整數")
        return (
            f"{BASE_URL}/buy/{city}/{'_'.join(zips)}/{PURPOSE}"
            f"?priceFilter=below-{q.max_price_wan}&order={sort}&page={page}"
        )

    def list_request(self, q: AreaQuery, page: int, mode: Mode) -> RequestSpec:
        return RequestSpec.get(self.list_url(q, page, mode), headers={"Accept": "text/html"})

    def parse_list(self, resp: Resp, q: AreaQuery) -> ListPage:
        require_ok(resp)
        soup = BeautifulSoup(resp.text(), "html.parser")
        product_list = soup.select_one("ul#product_list")
        if product_list is None:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "列表錨點不在")
        ref = response_date(resp)
        items: list[ListingFacts] = []
        seen: set[str] = set()
        skipped = 0
        for card in product_list.find_all("div", class_="item", recursive=False):
            status, source_id = _card_id(card)
            if status == "ad":
                continue
            if status == "broken":
                skipped += 1
                continue
            if source_id in seen:
                continue
            seen.add(source_id)
            facts = self._facts(card, source_id, q, ref)
            if facts is None:
                skipped += 1
            else:
                items.append(facts)
        has_next = soup.select_one('#pagination a[rel~="next"]') is not None
        return ListPage(items=items, reported_total=None, has_next=has_next, skipped=skipped)

    def _facts(self, card: Tag, source_id: str, q: AreaQuery, ref: Any) -> ListingFacts | None:
        title = _text(card.select_one(".list_info_name a"))  # 只讀片語，不保存
        address: Tag | None = None
        for row in card.select(".info div.d-flex"):
            if row.select_one(".fa-map-marker-alt") is not None:
                address = row.find("span")
                break
        road, address_districts = split_address(_text(address))

        btype: BType | None = None
        layout: str | None = None
        built = UNKNOWN_BUILT
        title_cp = None
        floor_lo = floor_hi = floors_total = None
        for span in card.select(".info span"):
            if span is address:
                continue
            text = clean_text(span.get_text(" ", strip=True), 60)
            kind = _span_kind(text) if text else None
            if kind == "age":
                built = parse_age_text(text, ref)
            elif kind == "floor":
                floor_lo, floor_hi, floors_total = parse_floor_field(text)
            elif kind == "area":
                title_cp = ping_text_to_cp(text)
            elif kind == "layout":
                layout = clean_text(text, 40)
            elif kind == "type":
                btype = btype_from_word(text)
        if floor_hi is not None and floors_total is not None and floor_hi > floors_total:
            floors_total = None  # 自相矛盾的總樓層當成未知

        box = card.select_one(".price_wrap div.price") or card.select_one("div.price")
        price = None
        includes = False
        if box is not None:
            number = _text(box.select_one("samp.price"), "") or ""
            unit = _text(box.select_one("samp.unit"), "") or ""
            price = money_yuan(f"{number}{unit}")
            includes = "含車位價" in box.get_text("", strip=True)
        old = card.select_one(".price_wrap samp.order-price") or card.select_one(".original-price")
        original = money_yuan(_text(old, ""))

        return FactsBuilder().set(
            source=SOURCE,
            source_id=source_id,
            area=q.area,
            road_text=road,
            district_hints=district_hints(address_districts=address_districts),
            price_yuan=price,
            price_excl_parking=_price_excl(includes, title),
            site_original_price_yuan=original if original is not None and original != price else None,
            main_kind=MainKind.MAIN,
            title_area_cp=title_cp,
            floor_lo=floor_lo,
            floor_hi=floor_hi,
            floors_total=floors_total,
            built=built,
            btype=btype,
            elevator=_elevator_for(btype),
            layout=layout,
        ).build()

    # ------------------------------------------------------------ 詳細頁

    def needs_detail(self, f: ListingFacts) -> bool:
        """主建物、車位型式、電梯（列表只有推定）都要看詳細頁。型態不在公開範圍內的不抓。"""
        if outside_scope_btype(f, self._scope):
            return False
        return (
            f.main_cp is None
            or f.parking in (Parking.UNKNOWN, Parking.FLAT_UNKNOWN_ACCESS)
            or f.elevator not in (Elevator.YES, Elevator.NO)
        )

    def detail_request(self, source_id: str) -> RequestSpec:
        if not _ID.fullmatch(source_id):
            raise ValueError("21世紀的編號只有數字")
        return RequestSpec.get(
            f"{BASE_URL}/buypage/{source_id}", headers={"Accept": "text/html"}, kind="detail"
        )

    def parse_detail(self, resp: Resp, base: ListingFacts) -> ListingFacts:
        require_ok(resp)
        path = urlsplit(resp.url).path.rstrip("/")
        if path != f"/buypage/{base.source_id}":
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁網址不對（可能被轉到別頁）")
        soup = BeautifulSoup(resp.text(), "html.parser")
        shared = set()
        for button in soup.select(".social-sharing[data-url]"):
            match = _SHARE_URL.fullmatch(str(button.get("data-url") or "").strip())
            shared.add(match.group(1) if match else "")
        if shared != {base.source_id}:
            raise PageInvalid(ErrorCode.PAGE_INVALID, "詳細頁錨點不在或編號不同")
        pairs = _pairs(soup)
        changes: dict[str, Any] = {}

        main = ping_text_to_cp(pairs.get("主建物面積"), max_ping=MAIN_AREA_MAX_PING)
        if main is not None:
            changes.update(main_cp=main, main_kind=MainKind.MAIN)

        parking = detail_parking(pairs.get("車位"))
        if parking is not Parking.UNKNOWN:
            changes["parking"] = parking

        elevator = detail_elevator(pairs.get("電梯") or pairs.get("電梯部數"))
        if elevator is not None:
            changes["elevator"] = elevator

        built = parse_age_text(pairs.get("屋齡"), response_date(resp))
        changes["built"] = narrow_built(base.built, built)
        return updated(base, **changes)


def _pairs(soup: BeautifulSoup) -> dict[str, str]:
    """`h6 span`（標籤）→ 同一格的 `div.val`（值）。只收需要的標籤，其他（含經紀人資料）不讀。"""
    pairs: dict[str, str] = {}
    for heading in soup.find_all("h6"):
        label_node = heading.find("span")
        label = (label_node.get_text("", strip=True) if label_node is not None else "").rstrip("：:")
        if label not in _WANTED_LABELS or label in pairs:
            continue
        value = heading.find_next_sibling("div", class_="val")
        if value is not None:
            pairs[label] = " ".join(value.get_text(" ", strip=True).split())
    return pairs


def detail_parking(text: Any) -> Parking:
    """「有 (坡道-平面)」→ 括號裡的型式；「無」→ none；只寫「有」或看不懂 → unknown。"""
    cleaned = clean_text(text, 100, placeholders=False)
    if cleaned is None:
        return Parking.UNKNOWN
    if _NO_PARKING.fullmatch(compact(cleaned)):
        return Parking.NONE
    inner = _PAREN.search(cleaned)
    if inner is None:
        return Parking.UNKNOWN
    return classify_parking_text(inner.group(1))


def detail_elevator(text: Any) -> Elevator | None:
    """「1 部」→ yes；「0 部」→ no；「有」「無」也收；看不懂是 None（不改）。"""
    cleaned = clean_text(text, 20, placeholders=False)
    if cleaned is None:
        return None
    core = compact(cleaned)
    count = _LIFT_COUNT.fullmatch(core)
    if count is not None:
        return Elevator.YES if int(count.group(1)) > 0 else Elevator.NO
    return {"有": Elevator.YES, "無": Elevator.NO}.get(core)
