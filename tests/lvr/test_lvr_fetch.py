"""lvr/fetch.py：發布編號、登記期間、下載頁、zip 只讀指定成員、網址白名單。全部離線。

下載頁與 zip 都是合成的：只保留解析器會讀的結構，其他文字是佔位字。
"""

import datetime as dt
import io
import warnings
import zipfile

import pytest
from lvr_testkit import (
    BUILD,
    MAIN,
    PARK,
    FakeSite,
    active_html,
    batch_zip,
    build_time_xml,
    history_html,
    make_zip,
    season_html,
    season_pages,
)

from house_watch.lvr.fetch import (
    ACTIVE_URL,
    BASE_URL,
    HISTORY_LIST_URL,
    SEASON_LIST_URL,
    ReleaseFiles,
    RemoteRelease,
    batch_id,
    batch_release_date,
    batch_window,
    check_url,
    current_release,
    download,
    list_available,
    parse_build_time,
    parse_history_list,
    parse_period,
    parse_season_list,
    plan,
    read_zip_members,
    release_order,
    release_window,
    season_id,
    season_window,
)
from house_watch.lvr.parse import LvrError

D = dt.date


# ------------------------------------------------------------------ 網址白名單


@pytest.mark.parametrize(
    "url",
    [
        "http://plvr.land.moi.gov.tw/Download?fileName=x",
        "https://plvr.land.moi.gov.tw.example.com/Download",
        "https://example.com/Download",
        "https://user:pw@plvr.land.moi.gov.tw/Download",
        "https://plvr.land.moi.gov.tw:8443/Download",
        "ftp://plvr.land.moi.gov.tw/x",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(LvrError):
        check_url(url)


def test_check_url_accepts_plvr_https():
    url = f"{BASE_URL}/DownloadSeason?season=115S3&fileName=build_time.xml"
    assert check_url(url) == url
    assert check_url("https://plvr.land.moi.gov.tw:443/x")


def test_getter_never_sees_disallowed_url():
    site = FakeSite({})
    bad = RemoteRelease(
        release_id="b20261021",
        kind="batch",
        window=batch_window(D(2026, 10, 21)),
        key="20261021/../../x",
    )
    with pytest.raises(LvrError):
        download(site, bad)
    assert site.calls == []


# ------------------------------------------------------------------ 編號與登記期間


@pytest.mark.parametrize(
    ("code", "window"),
    [
        ("115S3", (D(2026, 6, 11), D(2026, 9, 10))),  # 和 115S3 的 build_time.xml 相同
        ("115S1", (D(2025, 12, 11), D(2026, 3, 10))),
        ("114S4", (D(2025, 9, 11), D(2025, 12, 10))),
        ("109S4", (D(2020, 9, 11), D(2020, 12, 10))),
    ],
)
def test_season_window(code, window):
    assert season_window(code) == window
    assert release_window(season_id(code)) == window


@pytest.mark.parametrize("code", ["109S3", "115S5", "abc", "115s3"])
def test_season_window_rejects(code):
    with pytest.raises(LvrError):
        season_window(code)


@pytest.mark.parametrize(
    ("release", "window"),
    [
        # R1 §2.2 的前期清單與本期
        (D(2026, 10, 1), (D(2026, 9, 11), D(2026, 9, 20))),
        (D(2026, 10, 11), (D(2026, 9, 21), D(2026, 9, 30))),
        (D(2026, 10, 21), (D(2026, 10, 1), D(2026, 10, 10))),
        (D(2026, 11, 1), (D(2026, 10, 11), D(2026, 10, 20))),
        (D(2026, 11, 11), (D(2026, 10, 21), D(2026, 10, 31))),
        (D(2027, 1, 11), (D(2026, 12, 21), D(2026, 12, 31))),
        (D(2028, 3, 11), (D(2028, 2, 21), D(2028, 2, 29))),
    ],
)
def test_batch_window_and_inverse(release, window):
    assert batch_window(release) == window
    assert batch_release_date(window) == release
    assert release_window(batch_id(release)) == window


def test_batch_shapes_rejected():
    with pytest.raises(LvrError):
        batch_window(D(2026, 10, 5))
    with pytest.raises(LvrError):
        batch_release_date((D(2026, 10, 21), D(2026, 10, 30)))
    with pytest.raises(LvrError):
        release_window("x20261021")
    with pytest.raises(LvrError):
        release_window("b20261341")


def test_release_order():
    # 新舊依登記期間的最後一天
    assert release_order("b20261021") < release_order("b20261101")
    assert release_order("s115S3") < release_order("b20261001")
    # 同一天結束：季檔較新（批次 b20260921＝09/01–09/10，和 115S3 同一天結束）
    assert release_window("b20260921")[1] == season_window("115S3")[1]
    assert release_order("b20260921") < release_order("s115S3")


def test_parse_period():
    text = "資料內容：登記日期 115年9月11日至 115年9月20日之買賣案件，及訂約日期…"
    assert parse_period(text) == (D(2026, 9, 11), D(2026, 9, 20))
    assert parse_period("登記日期１１５年９月１１日至１１５年９月２０日之買賣案件") == (
        D(2026, 9, 11),
        D(2026, 9, 20),
    )
    for bad in (
        "沒有期間",
        "登記日期 115年9月31日至 115年10月1日之買賣案件",
        "登記日期 115年9月20日至 115年9月11日之買賣案件",
    ):
        with pytest.raises(LvrError):
            parse_period(bad)


def test_parse_build_time():
    assert parse_build_time(build_time_xml(D(2026, 9, 11), D(2026, 9, 20))) == (
        D(2026, 9, 11),
        D(2026, 9, 20),
    )
    hostile = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'
        b"<lvr_land><lvr_time>&e;</lvr_time></lvr_land>"
    )
    for bad in (hostile, b"<lvr_land>", b"<lvr_land><other/></lvr_land>", b"\xff\xfe"):
        with pytest.raises(LvrError):
            parse_build_time(bad)


# ------------------------------------------------------------------ 下載頁


def test_parse_season_list_only_supported_codes():
    html = season_html("115S3", "115S2", "109S4", "109S3", "101S1", "115S3").decode()
    assert parse_season_list(html) == ["115S3", "115S2", "109S4"]


def test_parse_history_list():
    html = history_html(
        ("20261001", batch_window(D(2026, 10, 1))), ("20261011", batch_window(D(2026, 10, 11)))
    ).decode()
    releases = parse_history_list(html)
    assert [(r.release_id, r.kind, r.key) for r in releases] == [
        ("b20261001", "batch", "20261001"),
        ("b20261011", "batch", "20261011"),
    ]


def test_history_list_with_wrong_period_is_rejected():
    html = history_html(("20261001", (D(2026, 9, 1), D(2026, 9, 10)))).decode()
    with pytest.raises(LvrError):
        parse_history_list(html)


def standard_site(**extra) -> FakeSite:
    pages = {
        SEASON_LIST_URL: season_html("115S3", "115S2"),
        HISTORY_LIST_URL: history_html(
            ("20261001", batch_window(D(2026, 10, 1))), ("20261011", batch_window(D(2026, 10, 11)))
        ),
        ACTIVE_URL: active_html(*batch_window(D(2026, 11, 11))),
    }
    pages.update(extra)
    return FakeSite(pages)


def test_list_available():
    site = standard_site()
    available = list_available(site)
    assert [r.release_id for r in available] == [
        "s115S2",
        "s115S3",
        "b20261001",
        "b20261011",
        "b20261111",
    ]
    assert available[-1].kind == "current"
    assert site.calls == [SEASON_LIST_URL, HISTORY_LIST_URL, ACTIVE_URL]


def test_list_available_prefers_history_over_current_for_same_batch():
    site = standard_site(**{ACTIVE_URL: active_html(*batch_window(D(2026, 10, 11)))})
    kinds = {r.release_id: r.kind for r in list_available(site)}
    assert kinds["b20261011"] == "batch"


# ------------------------------------------------------------------ 要下載哪些


def remote(release_id: str, kind="batch") -> RemoteRelease:
    window = release_window(release_id)
    key = release_id[1:]
    return RemoteRelease(release_id=release_id, kind=kind, window=window, key=key)


def test_plan():
    available = [
        remote("s114S3", "season"),  # 登記期間在 24 個月以前
        remote("s115S2", "season"),
        remote("s115S3", "season"),
        remote("b20260921"),  # 落在 115S3 裡，季檔取代它
        remote("b20261001"),
        remote("b20261011"),
        remote("b20261111", "current"),
    ]
    todo = plan(available, {"s115S2", "b20261001"}, today=D(2027, 10, 1), months=24)
    assert [r.release_id for r in todo] == ["s115S3", "b20261011", "b20261111"]


def test_plan_skips_batches_inside_imported_season():
    todo = plan([remote("b20260921")], {"s115S3"}, today=D(2026, 11, 21), months=24)
    assert todo == []


def test_plan_backfills_missed_batches():
    # R1-26 反例：Mac 睡了 12 天，錯過的那批只剩在前期清單裡
    available = [remote("b20261101"), remote("b20261111"), remote("b20261121", "current")]
    todo = plan(available, {"b20261101"}, today=D(2026, 11, 22), months=24)
    assert [r.release_id for r in todo] == ["b20261111", "b20261121"]


# ------------------------------------------------------------------ zip


def test_read_zip_members_only_opens_wanted_names():
    archive = make_zip(
        {
            "h_lvr_land_a.csv": MAIN,
            "../h_lvr_land_a.csv": b"evil",  # 帶路徑的名稱不會被當成同一個檔
            "A_lvr_land_a.csv": b"other city",
            "build_time.xml": b"<x/>",
            **{f"filler_{i}.txt": b"x" for i in range(50)},
        }
    )
    members = read_zip_members(archive, ["h_lvr_land_a.csv", "build_time.xml"])
    assert members == {"h_lvr_land_a.csv": MAIN, "build_time.xml": b"<x/>"}


@pytest.mark.parametrize(
    ("members", "kwargs"),
    [
        ({"build_time.xml": b"<x/>"}, {}),  # 缺成員
        ({"h_lvr_land_a.csv": b"x" * 2000, "build_time.xml": b"<x/>"}, {"max_bytes": 1000}),
        ({"h_lvr_land_a.csv": b"\0" * 3_000_000, "build_time.xml": b"<x/>"}, {}),  # 壓縮比
    ],
)
def test_read_zip_members_rejects(members, kwargs):
    with pytest.raises(LvrError):
        read_zip_members(make_zip(members), ["h_lvr_land_a.csv", "build_time.xml"], **kwargs)


def test_read_zip_members_rejects_duplicates_and_garbage():
    duplicated = make_zip({"a.csv": b"1"})
    buffer = io.BytesIO(duplicated)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("a.csv", b"2")
    with pytest.raises(LvrError, match="重複"):
        read_zip_members(buffer.getvalue(), ["a.csv"])
    with pytest.raises(LvrError):
        read_zip_members(b"not a zip", ["a.csv"])
    corrupted = bytearray(make_zip({"a.csv": b"hello world" * 100}, zipfile.ZIP_STORED))
    corrupted[60:70] = b"XXXXXXXXXX"  # 改到內容，CRC 對不上
    with pytest.raises(LvrError):
        read_zip_members(bytes(corrupted), ["a.csv"])


# ------------------------------------------------------------------ 下載


def test_download_season():
    release = remote("s115S3", "season")
    site = FakeSite(season_pages("115S3", release.window))
    files = download(site, release)
    assert (files.main, files.build, files.park) == (MAIN, BUILD, PARK)
    assert files.release_id == "s115S3" and files.kind == "season"
    assert len(site.calls) == 4
    assert all(url.startswith("https://plvr.land.moi.gov.tw/") for url in site.calls)


def test_download_season_with_wrong_build_time_is_rejected():
    release = remote("s115S3", "season")
    site = FakeSite(season_pages("115S3", season_window("115S2")))
    with pytest.raises(LvrError, match="登記期間"):
        download(site, release)


def test_download_batch_reads_only_needed_members():
    release = remote("b20261001")
    url = f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001"
    site = FakeSite({url: batch_zip(release.window)})
    files = download(site, release)
    assert (files.main, files.build, files.park) == (MAIN, BUILD, PARK)
    assert site.calls == [url]


def test_mislabelled_batch_zip_is_rejected():
    # R1 §2.2 的坑：要 20261001 時，伺服器給的檔名是別一批；以 build_time.xml 為準
    release = remote("b20261001")
    url = f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001"
    site = FakeSite({url: batch_zip(batch_window(D(2026, 8, 11)))})
    with pytest.raises(LvrError, match="登記期間"):
        download(site, release)


def test_download_current_checks_period_before_and_after():
    window = batch_window(D(2026, 11, 11))
    release = current_release(window)
    files_pages = {
        f"{BASE_URL}/Download?fileName=h_lvr_land_a.csv": MAIN,
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_build.csv": BUILD,
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_park.csv": PARK,
    }
    ok = FakeSite({ACTIVE_URL: [active_html(*window), active_html(*window)], **files_pages})
    files = download(ok, release)
    assert files.release_id == "b20261111" and files.kind == "current"
    assert ok.calls[0] == ACTIVE_URL and ok.calls[-1] == ACTIVE_URL
    switched = FakeSite(
        {
            ACTIVE_URL: [active_html(*window), active_html(*batch_window(D(2026, 11, 21)))],
            **files_pages,
        }
    )
    with pytest.raises(LvrError, match="換版"):
        download(switched, release)


def test_download_size_cap():
    release = remote("s115S3", "season")
    site = FakeSite(season_pages("115S3", release.window))
    with pytest.raises(LvrError, match="大小"):
        download(site, release, max_bytes=10)


def test_release_files_hash():
    a = ReleaseFiles("b20261111", "current", batch_window(D(2026, 11, 11)), b"ab", b"c", b"")
    b = ReleaseFiles("b20261111", "batch", batch_window(D(2026, 11, 11)), b"a", b"bc", b"")
    assert a.sha256 != b.sha256  # 邊界不同，雜湊就不同
    assert a.sha256 == ReleaseFiles("x", "season", a.window, b"ab", b"c", b"").sha256
