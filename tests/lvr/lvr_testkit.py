"""實價登錄測試共用的合成資料產生器（不是測試檔）。

格式照實價登錄的 CSV：UTF-8 含 BOM、CRLF、第 2 列是英文欄名。內容全部是合成的示意值，
地址一律是「示意路」，編號是測試用的假編號。
"""

import csv
import datetime as dt
import io
import warnings
import zipfile
from collections.abc import Iterable, Sequence

from house_watch.lvr.fetch import BASE_URL

MAIN_HEADER = (
    "鄉鎮市區",
    "交易標的",
    "土地位置建物門牌",
    "交易年月日",
    "交易筆棟數",
    "移轉層次",
    "總樓層數",
    "建物型態",
    "建築完成年月",
    "建物移轉總面積平方公尺",
    "總價元",
    "單價元平方公尺",
    "車位類別",
    "車位移轉總面積平方公尺",
    "車位總價元",
    "備註",
    "編號",
    "主建物面積",
    "附屬建物面積",
    "陽台面積",
    "電梯",
)
BUILD_HEADER = ("編號", "屋齡", "建築完成日期", "總層數", "建物分層", "移轉情形")
PARK_HEADER = ("編號", "車位類別", "車位價格", "車位面積平方公尺", "車位所在樓層")


def csv_bytes(
    header: Sequence[str],
    rows: Iterable[dict[str, str]],
    *,
    english: bool = True,
    bom: bool = True,
) -> bytes:
    """產生實價登錄格式的 CSV。rows 沒給的欄位留空白。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    if english:
        writer.writerow([f"english header {i}" for i in range(len(header))])
    for row in rows:
        writer.writerow([row.get(column, "") for column in header])
    data = buffer.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def serial(n: int) -> str:
    """測試用的假編號（只有大寫英文與數字）。"""
    return f"TESTSERIAL{n:06d}XYZ"


def main_row(n: int = 1, **overrides: str) -> dict[str, str]:
    """一筆合成的住宅成交（大樓、一個坡道平面車位、官方拆價）。"""
    row = {
        "鄉鎮市區": "甲區",
        "交易標的": "房地(土地+建物)+車位",
        "土地位置建物門牌": "示意市甲區示意路１號五樓",
        "交易年月日": "1150305",
        "交易筆棟數": "土地1建物1車位1",
        "移轉層次": "五層",
        "總樓層數": "十五層",
        "建物型態": "住宅大樓(11層含以上有電梯)",
        "建築完成年月": "1050820",
        "建物移轉總面積平方公尺": "120.50",
        "總價元": "15600000",
        "車位類別": "坡道平面",
        "車位移轉總面積平方公尺": "30.10",
        "車位總價元": "1500000",
        "備註": "",
        "編號": serial(n),
        "主建物面積": "70.20",
        "附屬建物面積": "5.10",
        "陽台面積": "10.30",
        "電梯": "有",
    }
    row.update(overrides)
    return row


def park_row(n: int = 1, ptype: str = "坡道平面", price: str = "1500000", area: str = "30.10"):
    return {
        "編號": serial(n),
        "車位類別": ptype,
        "車位價格": price,
        "車位面積平方公尺": area,
        "車位所在樓層": "地下二樓",
    }


def build_row(n: int = 1, done: str = "105年8月20日", layer: str = "五層"):
    return {
        "編號": serial(n),
        "屋齡": "9",
        "建築完成日期": done,
        "總層數": "十五層",
        "建物分層": layer,
        "移轉情形": "全筆移轉",
    }


def release_files(
    mains: Sequence[dict[str, str]],
    parks: Sequence[dict[str, str]] = (),
    builds: Sequence[dict[str, str]] = (),
) -> tuple[bytes, bytes, bytes]:
    """(主檔, build, park) 三個檔的內容。"""
    return (
        csv_bytes(MAIN_HEADER, mains),
        csv_bytes(BUILD_HEADER, builds),
        csv_bytes(PARK_HEADER, parks),
    )


# ---------------------------------------------------------------- 合成的下載網站


def period_text(start: dt.date, end: dt.date) -> str:
    return (
        f"資料內容：登記日期 {start.year - 1911}年{start.month}月{start.day}日至 "
        f"{end.year - 1911}年{end.month}月{end.day}日之買賣案件，及訂約日期（佔位）之租賃案件"
    )


def build_time_xml(start: dt.date, end: dt.date) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n\n<lvr_land>\n'
        f"  <lvr_time>{period_text(start, end)}</lvr_time>\n</lvr_land>\n"
    ).encode()


def season_html(*codes: str) -> bytes:
    options = "".join(f'<option value="{c}">佔位</option>\n' for c in codes)
    return f'<select name="season" id="historySeason_id">\n{options}</select>'.encode()


def history_html(*releases: tuple[str, tuple[dt.date, dt.date]]) -> bytes:
    rows = "".join(
        f'<td class="form_intro">&nbsp;發布日期 {key}\n'
        f'<span class="i_desc" desc="{period_text(*window)}">\n<img src="x.png"/></span></td>\n'
        for key, window in releases
    )
    return f"<table>{rows}</table>".encode()


def active_html(start: dt.date, end: dt.date) -> bytes:
    return f'<span class="text-danger">{period_text(start, end)}</span>'.encode()


def make_zip(members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 重複成員的測試會觸發 zipfile 的警告
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    return buffer.getvalue()


class FakeSite:
    """離線的 Getter：網址 → 內容；同一網址可以給一串回應（依序回傳）。"""

    def __init__(self, pages: dict[str, bytes | list[bytes]]) -> None:
        self.pages = {k: (list(v) if isinstance(v, list) else v) for k, v in pages.items()}
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        page = self.pages[url]
        return page.pop(0) if isinstance(page, list) else page


MAIN, BUILD, PARK = release_files([main_row(1)], [park_row(1)])


def season_pages(code: str, window) -> dict[str, bytes]:
    base = f"{BASE_URL}/DownloadSeason?season={code}&fileName="
    return {
        base + "H_lvr_land_A.csv": MAIN,
        base + "H_lvr_land_A_build.csv": BUILD,
        base + "H_lvr_land_A_park.csv": PARK,
        base + "build_time.xml": build_time_xml(*window),
    }


def batch_zip(window, **extra: bytes) -> bytes:
    return make_zip(
        {
            "h_lvr_land_a.csv": MAIN,
            "h_lvr_land_a_build.csv": BUILD,
            "h_lvr_land_a_park.csv": PARK,
            "build_time.xml": build_time_xml(*window),
            "a_lvr_land_a.csv": b"other city",
            **extra,
        }
    )
