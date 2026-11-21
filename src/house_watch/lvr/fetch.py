"""實價登錄下載（WP3；R1 §2、R1-26、R2-10）。

三種來源，都在 plvr.land.moi.gov.tw：
- 季檔（season）：桃園 a 主檔、build、park 明細各一個檔，加上 build_time.xml。
- 前期（batch）：本季已發布、還沒併進季檔的 10 日批次，只能下載全國整包 zip（約 15 MB）。
  只開需要的成員（ZipFile.open，先看宣告大小、讀的時候再數一次），絕不 extractall。
- 本期（current）：最新的 10 日批次，三個 CSV；登記期間取自下載頁的說明文字，
  下載前後各讀一次，不一致（剛好換版）就整批作廢。

**發布的編號與新舊**（不信任 zip 檔名與 Content-Disposition，R1 §2.2 的坑）
- 季檔：`s115S3`。自 109S4 起季別固定是登記日期「第 1 個月 11 日～第 4 個月 10 日」。
- 10 日批次：`b<發布日>`，例如 `b20261021`。本期與前期是同一批，所以編號相同，不會重複匯入。
  1 日發布＝上月 11–20 日、11 日發布＝上月 21 日–月底、21 日發布＝當月 1–10 日。
- 下載後用 build_time.xml（defusedxml 讀）或下載頁的說明核對登記期間，和編號推出的期間不同就拒絕。
- 新舊依登記期間的最後一天排序；同一天時季檔較新（季檔在批次之後才編成）。

**連線**：一律經過注入的 `Getter`（由 WP5a 的 Fetcher 包裝：限速、timeout、大小上限、
User-Agent、轉址白名單）。這裡另外確認每個網址都是 https 而且主機在白名單內。
"""

import datetime as dt
import hashlib
import io
import re
import urllib.parse
import xml.etree.ElementTree as StdET
import zipfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from house_watch.lvr.parse import LvrError
from house_watch.parse.age import add_months
from house_watch.parse.text import nfkc

HOST = "plvr.land.moi.gov.tw"
BASE_URL = f"https://{HOST}"
ALLOWED_HOSTS = frozenset({HOST})

SEASON_LIST_URL = f"{BASE_URL}/DownloadSeason_ajax_list"
HISTORY_LIST_URL = f"{BASE_URL}/DownloadHistory_ajax_list"
ACTIVE_URL = f"{BASE_URL}/Download_ajax_active"

MEMBER_MAX_BYTES = 64 * 1024 * 1024  # 單一 CSV 的上限；桃園 a 主檔一季約 4 MB
ZIP_MAX_BYTES = 128 * 1024 * 1024  # 前期的全國 zip 約 15 MB
ZIP_MAX_RATIO = 100  # 壓縮比上限（zip bomb）；實際的 CSV 約 5 倍
FIRST_SEASON = (109, 4)  # 這一季起季別才固定切在 11 日

SEASON_FILES = {
    "main": "H_lvr_land_A.csv",
    "build": "H_lvr_land_A_build.csv",
    "park": "H_lvr_land_A_park.csv",
}
BATCH_MEMBERS = {
    "main": "h_lvr_land_a.csv",
    "build": "h_lvr_land_a_build.csv",
    "park": "h_lvr_land_a_park.csv",
}
BUILD_TIME = "build_time.xml"

Kind = Literal["season", "batch", "current"]
Window = tuple[dt.date, dt.date]


class Getter(Protocol):
    """GET 一個網址，回傳 HTTP 200 的內容；其他狀態、逾時、超過大小上限都要丟例外。"""

    def __call__(self, url: str) -> bytes: ...


def check_url(url: str) -> str:
    """只允許 https、白名單主機、沒有帳號密碼與其他連接埠的網址。"""
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise LvrError("不允許的下載網址")
    return url


def _get(get: Getter, url: str, *, max_bytes: int) -> bytes:
    data = get(check_url(url))
    if len(data) > max_bytes:
        raise LvrError("下載內容超過大小上限")
    return data


# ---------------------------------------------------------------- 編號與登記期間


_SEASON = re.compile(r"^(\d{3})S([1-4])$")
_BATCH_ID = re.compile(r"^b(\d{8})$")


def season_window(code: str) -> Window:
    """季別 → 登記期間，例如 115S3 → 2026-06-11～2026-09-10。109S4 以前不支援。"""
    m = _SEASON.match(code)
    if m is None:
        raise LvrError("季別格式不對")
    roc, quarter = int(m.group(1)), int(m.group(2))
    if (roc, quarter) < FIRST_SEASON:
        raise LvrError("109S4 以前的季別切法不同，不支援")
    end = dt.date(roc + 1911, quarter * 3, 10)
    start = add_months(dt.date(end.year, end.month, 11), -3)
    return start, end


def _month_end(day: dt.date) -> dt.date:
    return add_months(day.replace(day=1), 1) - dt.timedelta(days=1)


def batch_window(release: dt.date) -> Window:
    """10 日批次的發布日 → 登記期間（發布日只會是 1、11、21 日）。"""
    if release.day == 21:
        return release.replace(day=1), release.replace(day=10)
    previous = add_months(release.replace(day=1), -1)
    if release.day == 1:
        return previous.replace(day=11), previous.replace(day=20)
    if release.day == 11:
        return previous.replace(day=21), _month_end(previous)
    raise LvrError("10 日批次的發布日只會是 1、11、21 日")


def batch_release_date(window: Window) -> dt.date:
    """登記期間 → 10 日批次的發布日；不是 10 日批次的形狀就丟例外。"""
    start, end = window
    for candidate in (
        add_months(start.replace(day=1), 1),  # 11–20 日 → 下月 1 日
        add_months(start.replace(day=11), 1),  # 21 日–月底 → 下月 11 日
        start.replace(day=21),  # 1–10 日 → 當月 21 日
    ):
        if candidate > start and batch_window(candidate) == window:
            return candidate
    raise LvrError("不是 10 日批次的登記期間")


def _yyyymmdd(text: str) -> dt.date:
    try:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        raise LvrError("日期格式不對") from None


def season_id(code: str) -> str:
    season_window(code)
    return f"s{code}"


def batch_id(release: dt.date) -> str:
    batch_window(release)
    return f"b{release:%Y%m%d}"


def release_window(release_id: str) -> Window:
    """發布編號 → 登記期間。"""
    if release_id.startswith("s"):
        return season_window(release_id[1:])
    m = _BATCH_ID.match(release_id)
    if m is None:
        raise LvrError("發布編號格式不對")
    return batch_window(_yyyymmdd(m.group(1)))


def release_order(release_id: str) -> tuple[dt.date, int]:
    """新舊的排序鍵：登記期間的最後一天，同一天時季檔較新。"""
    return release_window(release_id)[1], 1 if release_id.startswith("s") else 0


_PERIOD = re.compile(
    r"登記日期\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日\s*至\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日\s*之買賣"
)


def parse_period(text: str) -> Window:
    """「登記日期 115年9月11日至 115年9月20日之買賣案件」→ 登記期間。"""
    m = _PERIOD.search(nfkc(text))
    if m is None:
        raise LvrError("找不到買賣案件的登記期間")
    y1, m1, d1, y2, m2, d2 = (int(g) for g in m.groups())
    try:
        start = dt.date(y1 + 1911, m1, d1)
        end = dt.date(y2 + 1911, m2, d2)
    except ValueError:
        raise LvrError("登記期間的日期不對") from None
    if start > end:
        raise LvrError("登記期間的起點晚於終點")
    return start, end


def parse_build_time(data: bytes) -> Window:
    """zip 或季檔裡的 build_time.xml → 登記期間。用 defusedxml，不接受 DTD。"""
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (StdET.ParseError, DefusedXmlException):
        raise LvrError("build_time.xml 不合法") from None
    node = root.find("lvr_time")
    if node is None or not node.text:
        raise LvrError("build_time.xml 沒有資料期間")
    return parse_period(node.text)


# ---------------------------------------------------------------- 下載頁


@dataclass(frozen=True)
class RemoteRelease:
    """網站上可以下載的一個發布。key 是季別（115S3）或發布日（20261021）。"""

    release_id: str
    kind: Kind
    window: Window
    key: str


def _decode(data: bytes, what: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise LvrError(f"{what} 不是 UTF-8") from None


def parse_season_list(html: str) -> list[str]:
    """季別下拉選單 → 支援的季別（109S4 以後），新的在前。"""
    codes = re.findall(r"<option\s+value=\"(\d{3}S[1-4])\"", html)
    supported = []
    for code in codes:
        roc, quarter = int(code[:3]), int(code[-1])
        if (roc, quarter) >= FIRST_SEASON and code not in supported:
            supported.append(code)
    return supported


_HISTORY_ITEM = re.compile(r"發布日期\s*(\d{8})\s*<span[^>]*\bdesc=\"([^\"]*)\"", re.S)


def parse_history_list(html: str) -> list[RemoteRelease]:
    """前期清單 → 10 日批次。說明寫的登記期間和發布日推出的不同就拒絕（切法可能改了）。"""
    releases = []
    for date_text, desc in _HISTORY_ITEM.findall(html):
        release = _yyyymmdd(date_text)
        window = batch_window(release)
        if parse_period(desc) != window:
            raise LvrError("前期清單的登記期間和發布日對不上")
        releases.append(
            RemoteRelease(release_id=batch_id(release), kind="batch", window=window, key=date_text)
        )
    return releases


def parse_active_window(html: str) -> Window:
    """本期下載頁的說明 → 登記期間。"""
    return parse_period(html)


def current_release(window: Window) -> RemoteRelease:
    release = batch_release_date(window)
    return RemoteRelease(
        release_id=batch_id(release), kind="current", window=window, key=f"{release:%Y%m%d}"
    )


def list_available(get: Getter, *, max_bytes: int = MEMBER_MAX_BYTES) -> list[RemoteRelease]:
    """讀三個下載頁（3 個請求），列出網站上目前可以下載的發布。

    同一批同時出現在本期與前期時，取前期（zip 裡有 build_time.xml 可以核對）。
    """
    seasons = parse_season_list(
        _decode(_get(get, SEASON_LIST_URL, max_bytes=max_bytes), "季別清單")
    )
    history = parse_history_list(
        _decode(_get(get, HISTORY_LIST_URL, max_bytes=max_bytes), "前期清單")
    )
    active = parse_active_window(_decode(_get(get, ACTIVE_URL, max_bytes=max_bytes), "本期頁"))
    found: dict[str, RemoteRelease] = {}
    for code in seasons:
        found[season_id(code)] = RemoteRelease(
            release_id=season_id(code), kind="season", window=season_window(code), key=code
        )
    current = current_release(active)
    found[current.release_id] = current
    for release in history:
        found[release.release_id] = release
    return sorted(found.values(), key=lambda r: release_order(r.release_id))


def plan(
    available: Iterable[RemoteRelease],
    imported: Collection[str],
    *,
    today: dt.date,
    months: int,
) -> list[RemoteRelease]:
    """要下載哪些發布，舊的在前（R1-26）。

    - 登記期間的最後一天在「今天往前 months 個月」之後的才要。
    - 已匯入的編號跳過；10 日批次落在某個季檔的期間內就跳過（季檔取代它）。
    """
    cutoff = add_months(today, -months)
    available = list(available)
    seasons = [r.window for r in available if r.kind == "season"]
    seasons += [release_window(rid) for rid in imported if rid.startswith("s")]
    wanted = []
    for release in sorted(available, key=lambda r: release_order(r.release_id)):
        if release.release_id in imported or release.window[1] < cutoff:
            continue
        if release.kind != "season" and any(
            lo <= release.window[0] and release.window[1] <= hi for lo, hi in seasons
        ):
            continue
        wanted.append(release)
    return wanted


# ---------------------------------------------------------------- 下載


@dataclass(frozen=True)
class ReleaseFiles:
    """一個發布的三個檔（主檔、build、park）。"""

    release_id: str
    kind: Kind
    window: Window
    main: bytes
    build: bytes
    park: bytes

    @property
    def sha256(self) -> str:
        """三個檔內容的雜湊（每個檔前面加長度，避免邊界混淆）。"""
        digest = hashlib.sha256()
        for part in (self.main, self.build, self.park):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()


def read_zip_members(
    data: bytes,
    names: Collection[str],
    *,
    max_bytes: int = MEMBER_MAX_BYTES,
    max_ratio: int = ZIP_MAX_RATIO,
) -> dict[str, bytes]:
    """只讀 zip 裡指定名稱的成員（名稱要完全相同、不含路徑），其他成員一律不碰。

    - 成員缺少、重複、加密都拒絕。
    - 先看宣告的大小與壓縮比，讀的時候再數一次實際的位元組數（宣告可能是假的）。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise LvrError("不是合法的 zip") from None
    with archive:
        infos: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if info.filename not in names:
                continue
            if info.filename in infos:
                raise LvrError("zip 裡有重複的成員")
            infos[info.filename] = info
        missing = sorted(set(names) - set(infos))
        if missing:
            raise LvrError(f"zip 裡缺少 {', '.join(missing)}")
        out: dict[str, bytes] = {}
        for name, info in infos.items():
            if info.flag_bits & 0x1:
                raise LvrError("zip 成員有加密")
            if info.file_size > max_bytes:
                raise LvrError("zip 成員超過大小上限")
            if info.file_size > 1_000_000 and info.file_size > max_ratio * max(
                info.compress_size, 1
            ):
                raise LvrError("zip 成員的壓縮比不合理")
            out[name] = _read_bounded(archive, info, max_bytes)
    return out


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    limit = min(max_bytes, info.file_size)
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info) as handle:
            while chunk := handle.read(1 << 16):
                total += len(chunk)
                if total > limit:
                    raise LvrError("zip 成員的實際大小超過宣告或上限")
                chunks.append(chunk)
    except (zipfile.BadZipFile, OSError, EOFError):
        raise LvrError("zip 成員損壞") from None
    return b"".join(chunks)


def season_file_url(code: str, file_name: str) -> str:
    season_window(code)
    query = urllib.parse.urlencode({"season": code, "fileName": file_name})
    return f"{BASE_URL}/DownloadSeason?{query}"


def batch_zip_url(key: str) -> str:
    release_window(f"b{key}")
    query = urllib.parse.urlencode({"type": "history", "fileName": key})
    return f"{BASE_URL}/DownloadHistory?{query}"


def current_file_url(file_name: str) -> str:
    return f"{BASE_URL}/Download?{urllib.parse.urlencode({'fileName': file_name})}"


def _check_window(actual: Window, release: RemoteRelease) -> None:
    if actual != release.window:
        raise LvrError("下載內容的登記期間和發布編號對不上")


def download(
    get: Getter, release: RemoteRelease, *, max_bytes: int = MEMBER_MAX_BYTES
) -> ReleaseFiles:
    """下載一個發布並核對登記期間。季檔 4 個請求、前期 1 個、本期 5 個。"""
    if release.kind == "season":
        files = {
            part: _get(get, season_file_url(release.key, name), max_bytes=max_bytes)
            for part, name in SEASON_FILES.items()
        }
        build_time = _get(get, season_file_url(release.key, BUILD_TIME), max_bytes=max_bytes)
        _check_window(parse_build_time(build_time), release)
    elif release.kind == "batch":
        archive = _get(get, batch_zip_url(release.key), max_bytes=ZIP_MAX_BYTES)
        members = read_zip_members(
            archive, [*BATCH_MEMBERS.values(), BUILD_TIME], max_bytes=max_bytes
        )
        _check_window(parse_build_time(members[BUILD_TIME]), release)
        files = {part: members[name] for part, name in BATCH_MEMBERS.items()}
    else:
        before = _get(get, ACTIVE_URL, max_bytes=max_bytes)
        _check_window(parse_active_window(_decode(before, "本期頁")), release)
        files = {
            part: _get(get, current_file_url(name), max_bytes=max_bytes)
            for part, name in BATCH_MEMBERS.items()
        }
        after = _get(get, ACTIVE_URL, max_bytes=max_bytes)
        if parse_active_window(_decode(after, "本期頁")) != release.window:
            raise LvrError("本期在下載途中換版，這次作廢")
    return ReleaseFiles(
        release_id=release.release_id,
        kind=release.kind,
        window=release.window,
        main=files["main"],
        build=files["build"],
        park=files["park"],
    )
