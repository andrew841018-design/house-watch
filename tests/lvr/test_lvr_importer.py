"""lvr/importer.py：以編號去重（新的發布為準）、全市匯入、定位注入、中位數綁資料版本。"""

import datetime as dt
import json

import pytest
from lvr_testkit import (
    FakeSite,
    active_html,
    batch_zip,
    history_html,
    main_row,
    park_row,
    release_files,
    season_html,
    season_pages,
    serial,
)

from house_watch import db
from house_watch.lvr import importer, unitprice
from house_watch.lvr.fetch import (
    ACTIVE_URL,
    BASE_URL,
    HISTORY_LIST_URL,
    SEASON_LIST_URL,
    ReleaseFiles,
    batch_window,
    release_window,
)
from house_watch.lvr.importer import (
    MEDIANS_VERSION_KEY,
    import_release,
    imported_releases,
    iter_facts,
    lvr_as_of,
    lvr_version,
    refresh_medians,
    relocate,
    update,
)
from house_watch.lvr.parse import LvrError
from house_watch.models import Location, Precision, TxFacts

NOW = dt.datetime(2026, 11, 21, 2, 0, tzinfo=dt.UTC)


def fake_locate(facts: TxFacts) -> Location:
    """WP2 的定位用替身：一律定位不到。"""
    return Location(precision=Precision.NONE, district=facts.district)


def site_locate(facts: TxFacts) -> Location:
    return Location(lat=25.0, lon=121.3, precision=Precision.DOOR, district=facts.district)


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "hw.sqlite")
    yield connection
    connection.close()


def files(release_id: str, mains, parks=(), kind="batch") -> ReleaseFiles:
    main, build, park = release_files(mains, parks)
    return ReleaseFiles(release_id, kind, release_window(release_id), main, build, park)


def stored(conn, n: int) -> TxFacts:
    row = conn.execute(
        "SELECT facts_json FROM transactions WHERE serial = ?", (serial(n),)
    ).fetchone()
    return TxFacts.model_validate_json(row[0])


# ------------------------------------------------------------------ 匯入


def test_import_stores_whole_city_and_skips_non_building(conn):
    mains = [
        main_row(1),
        main_row(2, **{"鄉鎮市區": "乙區"}),  # 全市都存，不篩區
        main_row(3, **{"交易標的": "土地", "交易筆棟數": "土地1建物0車位0"}),
        main_row(4, **{"備註": "親友、員工、共有人或其他特殊關係間之交易；"}),
    ]
    stats = import_release(
        conn,
        files("b20261111", mains, [park_row(1), park_row(2), park_row(4)]),
        locate=fake_locate,
        doorindex_version="d1",
        now=NOW,
    )
    assert (stats.rows, stats.inserted, stats.not_stored, stats.replaced) == (4, 3, 1, 0)
    rows = conn.execute(
        "SELECT serial, release_id, trade_date, district, precision, lat,"
        " doorindex_version FROM transactions ORDER BY serial"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (serial(1), "b20261111", "2026-03-05", "甲區", "none", None, "d1"),
        (serial(2), "b20261111", "2026-03-05", "乙區", "none", None, "d1"),
        (serial(4), "b20261111", "2026-03-05", "甲區", "none", None, "d1"),
    ]
    assert stored(conn, 4).exclusion == "special_relation"  # 排除的也存，標上原因
    release = conn.execute("SELECT kind, imported_at, sha256 FROM lvr_releases").fetchone()
    assert tuple(release)[:2] == ("batch", "2026-11-21T02:00:00Z")
    # facts_json 是固定格式（鍵排序、不轉義中文）
    raw = conn.execute(
        "SELECT facts_json FROM transactions WHERE serial = ?", (serial(1),)
    ).fetchone()[0]
    assert raw == json.dumps(
        json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def test_location_is_stored(conn):
    import_release(
        conn,
        files("b20261111", [main_row(1)], [park_row(1)]),
        locate=site_locate,
        doorindex_version=None,
        now=NOW,
    )
    row = conn.execute("SELECT lat, lon, precision FROM transactions").fetchone()
    assert tuple(row) == (25.0, 121.3, "door")


def test_same_release_twice_is_skipped(conn):
    release = files("b20261111", [main_row(1)], [park_row(1)])
    import_release(conn, release, locate=fake_locate, doorindex_version=None, now=NOW)
    again = import_release(conn, release, locate=fake_locate, doorindex_version=None, now=NOW)
    assert again.already_imported
    assert again.inserted == 0


def test_newer_release_wins_regardless_of_import_order(conn):
    older = files("s115S3", [main_row(1, **{"總價元": "11000000"})], [park_row(1)], "season")
    newer = files("b20261011", [main_row(1, **{"總價元": "12000000"})], [park_row(1)])
    import_release(conn, newer, locate=fake_locate, doorindex_version=None, now=NOW)
    stats = import_release(conn, older, locate=fake_locate, doorindex_version=None, now=NOW)
    assert stats.kept == 1
    assert stored(conn, 1).total_yuan == 12_000_000
    assert stored(conn, 1).release_id == "b20261011"


def test_season_replaces_its_own_batch(conn):
    # 同一天結束：季檔較新（季檔在批次之後才編成）
    batch = files("b20260921", [main_row(1, **{"總價元": "11000000"})], [park_row(1)])
    season = files("s115S3", [main_row(1, **{"總價元": "11500000"})], [park_row(1)], "season")
    import_release(conn, batch, locate=fake_locate, doorindex_version=None, now=NOW)
    stats = import_release(conn, season, locate=fake_locate, doorindex_version=None, now=NOW)
    assert stats.replaced == 1
    assert stored(conn, 1).total_yuan == 11_500_000


def test_later_version_without_building_does_not_replace(conn):
    # R1 §3.1：同編號取「有建物」的最新那份
    with_building = files("b20261001", [main_row(1)], [park_row(1)])
    land_only = files(
        "b20261011", [main_row(1, **{"交易標的": "土地", "交易筆棟數": "土地2建物0車位0"})]
    )
    import_release(conn, with_building, locate=fake_locate, doorindex_version=None, now=NOW)
    stats = import_release(conn, land_only, locate=fake_locate, doorindex_version=None, now=NOW)
    assert stats.not_stored == 1
    assert stored(conn, 1).release_id == "b20261001"


def test_changed_content_reimports_same_release(conn):
    first = files("b20261111", [main_row(1, **{"總價元": "11000000"})], [park_row(1)])
    second = files("b20261111", [main_row(1, **{"總價元": "11100000"})], [park_row(1)])
    import_release(conn, first, locate=fake_locate, doorindex_version=None, now=NOW)
    stats = import_release(conn, second, locate=fake_locate, doorindex_version=None, now=NOW)
    assert stats.replaced == 1
    assert stored(conn, 1).total_yuan == 11_100_000
    assert imported_releases(conn) == {"b20261111": second.sha256}


def test_too_many_bad_rows_rejects_whole_release(conn):
    mains = [main_row(i, **{"交易年月日": "bad"}) for i in range(1, 13)] + [main_row(20)]
    with pytest.raises(LvrError):
        import_release(
            conn, files("b20261111", mains), locate=fake_locate, doorindex_version=None, now=NOW
        )
    assert imported_releases(conn) == {}
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_failure_inside_transaction_rolls_back(conn):
    def broken_locate(facts):
        raise RuntimeError("locate failed")

    with pytest.raises(RuntimeError):
        import_release(
            conn,
            files("b20261111", [main_row(1)], [park_row(1)]),
            locate=broken_locate,
            doorindex_version=None,
            now=NOW,
        )
    assert imported_releases(conn) == {}


def test_relocate_when_doorindex_changes(conn):
    import_release(
        conn,
        files("b20261111", [main_row(1), main_row(2)], [park_row(1), park_row(2)]),
        locate=fake_locate,
        doorindex_version="d1",
        now=NOW,
    )
    assert relocate(conn, site_locate, "d2") == 2
    rows = conn.execute("SELECT precision, doorindex_version FROM transactions").fetchall()
    assert {tuple(r) for r in rows} == {("door", "d2")}
    assert relocate(conn, site_locate, "d2") == 0


def test_iter_facts_round_trip(conn):
    import_release(
        conn,
        files("b20261111", [main_row(2), main_row(1)], [park_row(1), park_row(2)]),
        locate=fake_locate,
        doorindex_version=None,
        now=NOW,
    )
    assert [f.serial for f in iter_facts(conn)] == [serial(1), serial(2)]


# ------------------------------------------------------------------ 資料版本與中位數


def many_rows(start: int, count: int, district="甲區") -> tuple[list, list]:
    mains = [
        main_row(i, **{"鄉鎮市區": district, "車位總價元": str(1_000_000 + i * 1_000)})
        for i in range(start, start + count)
    ]
    parks = [
        park_row(i, price=str(1_000_000 + i * 1_000), area="30.00")
        for i in range(start, start + count)
    ]
    return mains, parks


def test_version_and_as_of(conn):
    assert lvr_version(conn) is None and lvr_as_of(conn) is None
    import_release(
        conn,
        files("s115S3", [main_row(1)], [park_row(1)], "season"),
        locate=fake_locate,
        doorindex_version=None,
        now=NOW,
    )
    v1 = lvr_version(conn)
    assert lvr_as_of(conn) == dt.date(2026, 9, 10)
    import_release(
        conn,
        files("b20261111", [main_row(2)], [park_row(2)]),
        locate=fake_locate,
        doorindex_version=None,
        now=NOW,
    )
    assert lvr_version(conn) != v1
    assert lvr_as_of(conn) == dt.date(2026, 10, 31)


def test_refresh_medians_only_when_version_changes(conn, monkeypatch):
    mains, parks = many_rows(1, 25)
    import_release(
        conn, files("b20261111", mains, parks), locate=fake_locate, doorindex_version=None, now=NOW
    )
    table = refresh_medians(conn)
    assert table is not None
    group = table.groups[("district", "甲區", "ramp_flat")]
    assert group.price.n == 25 and group.area.n == 25
    stored_version = conn.execute(
        "SELECT value FROM kv WHERE key = ?", (MEDIANS_VERSION_KEY,)
    ).fetchone()[0]
    assert stored_version == lvr_version(conn)

    calls = []
    real = unitprice.compute_medians

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(importer, "compute_medians", counting)
    assert refresh_medians(conn) == table  # 版本沒變：讀回存好的，不重算
    assert calls == []
    more_mains, more_parks = many_rows(100, 3)
    import_release(
        conn,
        files("b20261121", more_mains, more_parks),
        locate=fake_locate,
        doorindex_version=None,
        now=NOW,
    )
    new_table = refresh_medians(conn)
    assert calls == [1]
    assert new_table.groups[("district", "甲區", "ramp_flat")].price.n == 28


def test_estimated_unit_price_with_stored_medians(conn):
    mains, parks = many_rows(1, 25)
    bundled = main_row(99, **{"車位總價元": "0"})
    import_release(
        conn,
        files("b20261111", [*mains, bundled], [*parks, park_row(99, price="0")]),
        locate=fake_locate,
        doorindex_version=None,
        now=NOW,
    )
    table = refresh_medians(conn)
    result = unitprice.unit_price(stored(conn, 99), table)
    assert result.up.basis == "estimated"
    prices = sorted(1_000_000 + i * 1_000 for i in range(1, 26))
    assert result.up.q_hi == 15_600_000 - unitprice.nearest_rank(prices, 25)
    assert result.up.q_lo == 15_600_000 - unitprice.nearest_rank(prices, 75)


# ------------------------------------------------------------------ 每日更新（端到端，離線）


def full_site(**extra) -> FakeSite:
    pages = {
        SEASON_LIST_URL: season_html("115S3"),
        HISTORY_LIST_URL: history_html(("20261001", batch_window(dt.date(2026, 10, 1)))),
        ACTIVE_URL: active_html(*batch_window(dt.date(2026, 11, 11))),
        **season_pages("115S3", release_window("s115S3")),
        f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001": batch_zip(
            batch_window(dt.date(2026, 10, 1))
        ),
        f"{BASE_URL}/Download?fileName=h_lvr_land_a.csv": release_files(
            [main_row(5)], [park_row(5)]
        )[0],
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_build.csv": release_files(
            [main_row(5)], [park_row(5)]
        )[1],
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_park.csv": release_files(
            [main_row(5)], [park_row(5)]
        )[2],
    }
    pages.update(extra)
    return FakeSite(pages)


def test_update_end_to_end(conn):
    site = full_site()
    report = update(conn, site, now=NOW, locate=fake_locate, doorindex_version="d1", months=24)
    assert report.planned == ["s115S3", "b20261001", "b20261111"]
    assert [s.release_id for s in report.imports] == report.planned
    assert set(imported_releases(conn)) == {"s115S3", "b20261001", "b20261111"}
    assert report.as_of == dt.date(2026, 10, 31)
    assert report.version == lvr_version(conn)
    assert all(url.startswith("https://plvr.land.moi.gov.tw/") for url in site.calls)
    # 第二天：沒有新的發布，只讀三個下載頁
    again = full_site()
    report2 = update(conn, again, now=NOW, locate=fake_locate, doorindex_version="d1", months=24)
    assert report2.planned == []
    assert again.calls == [SEASON_LIST_URL, HISTORY_LIST_URL, ACTIVE_URL]


def test_update_stops_at_first_failure_and_keeps_earlier_imports(conn):
    bad_zip = {
        f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001": batch_zip(
            batch_window(dt.date(2026, 8, 11))
        )
    }  # 標錯期間的 zip
    with pytest.raises(LvrError):
        update(
            conn,
            full_site(**bad_zip),
            now=NOW,
            locate=fake_locate,
            doorindex_version=None,
            months=24,
        )
    assert set(imported_releases(conn)) == {"s115S3"}
    # 中位數仍然和已匯入的資料一致
    assert conn.execute("SELECT value FROM kv WHERE key = ?", (MEDIANS_VERSION_KEY,)).fetchone()[
        0
    ] == lvr_version(conn)
