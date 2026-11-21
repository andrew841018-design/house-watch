"""端到端（M2 驗收）：完整掃描 → 探測 → 連續兩次有效完整掃描沒看到 → 下架；
失敗的掃描不計入；基準不算新上架；手動接受；預算；詳細頁不造成版本來回跳。

全部經過 cli.run_update（sources 步驟＋匯出），網站是 MockTransport 的合成網站。數字是示意值。
"""

import datetime as dt

from pipeline_kit import Unit, write_settings

from house_watch.models import Parking, RecordStatus
from house_watch.pipeline import versions

HOUR = 3600
DAY = 24 * HOUR


def five_units():
    return [Unit(id=f"U{n}", floor=n + 2, order=n) for n in range(1, 6)]


def statuses(env):
    return [(row["mode"], row["status"], row["error_code"]) for row in env.scans()]


def group_of(env, source_id):
    groups = env.listings_file().groups
    return next(g for g in groups if any(m.id == source_id for m in g.members))


def test_full_scan_probe_and_two_missed_valid_scans_make_gone(env, site):
    write_settings("[sources.cthouse]\nprobe_pages = 1\n")  # 2 頁的站才不算「小站」
    site.add(*five_units())
    report = env.update()
    assert report.exit_code == 0, [s.describe() for s in report.steps]
    assert statuses(env) == [("full", "ok", None)]
    assert env.scans()[0]["pages"] == 2 and env.scans()[0]["parsed"] == 5
    baseline = group_of(env, "U1")
    assert baseline.baseline is True and baseline.status == RecordStatus.ACTIVE

    # 3 小時後：探測，看到新上架的 U9（不是基準）
    env.clock.advance(3 * HOUR + 60)
    site.add(Unit(id="U9", floor=12, order=99))
    env.update()
    assert statuses(env)[-1] == ("probe", "ok", None)
    assert group_of(env, "U9").baseline is False
    assert env.listing("U9")["baseline"] == 0

    # 隔天：U2 不見了。只有一次有效完整掃描沒看到，還不算下架
    env.clock.advance(DAY)
    site.remove("U2")
    env.update()
    assert statuses(env)[-1] == ("full", "ok", None)
    assert env.listing("U2")["status"] == "active"

    # 第三天：網站 503，掃描失敗，不計入
    env.clock.advance(DAY)
    site.mode = "down"
    report = env.update()
    assert report.exit_code == 1
    assert statuses(env)[-1] == ("full", "failed", "http_5xx")
    assert env.listing("U2")["status"] == "active"

    # 同一天稍後恢復：再做一次完整掃描（暫時性錯誤可以重試）。失敗的那次不歸零：
    # U2 最後一次被看到之後已經有兩次有效完整掃描 → 下架
    env.clock.advance(HOUR)
    site.mode = "normal"
    env.update()
    assert statuses(env)[-1] == ("full", "ok", None)
    row = env.listing("U2")
    assert row["status"] == "gone" and row["gone_at"] is not None
    gone = group_of(env, "U2")
    assert gone.status == RecordStatus.GONE and gone.gone_at is not None
    assert env.listing("U1")["status"] == "active"
    assert env.listing("U9")["status"] == "active", "探測看到的也算「看到」"

    # 第五天：U2 用同一個編號重新上架
    env.clock.advance(2 * DAY)
    site.add(Unit(id="U2", floor=4, order=2))
    env.update()
    row = env.listing("U2")
    assert row["status"] == "active" and row["relisted_at"] is not None and row["gone_at"] is None
    relisted = group_of(env, "U2")
    assert relisted.relisted_at is not None and relisted.status == RecordStatus.ACTIVE


def test_invalid_scan_is_not_counted_until_accepted(env, site):
    site.add(*five_units())
    env.update()
    env.clock.advance(DAY)
    env.update()  # 兩次有效完整掃描
    env.clock.advance(DAY)
    site.shrink_to = 1  # 網站只回一筆，總數也說一筆：比上次少一半以上
    report = env.update()
    assert report.exit_code == 1
    scan_id, *_ = env.scans()[-1]
    assert statuses(env)[-1] == ("full", "invalid", "count_short")
    assert all(env.listing(f"U{n}")["status"] == "active" for n in range(1, 6))
    # 同一天不再重試完整掃描（不是暫時性錯誤），只等探測
    env.clock.advance(HOUR)
    env.update()
    assert len(env.scans()) == 3
    # 擁有者手動接受 → 計入；隔天再一次有效掃描後，沒被看到的才下架
    env.update("export", accept=(str(scan_id),))
    assert env.scans()[-1]["status"] == "accepted"
    env.clock.advance(DAY)
    env.update()
    assert env.listing("U1")["status"] == "active"
    assert {env.listing(f"U{n}")["status"] for n in range(2, 6)} == {"gone"}


def test_blocked_site_stops_the_source_without_counting(env, site):
    site.add(*five_units())
    env.update()
    env.clock.advance(DAY)
    site.mode = "blocked"
    report = env.update()
    assert report.exit_code == 1
    assert statuses(env)[-1] == ("full", "failed", "blocked")
    paused = env.conn.execute("SELECT paused_until FROM source_state WHERE source = 'cthouse'")
    assert paused.fetchone()[0] is not None
    # 暫停中：下一輪不再送任何請求，也不記掃描
    count = len(site.requests)
    env.clock.advance(HOUR)
    env.update()
    assert len(site.requests) == count and len(env.scans()) == 2


def test_broken_page_fails_the_scan_and_parse_rate_is_checked(env, site):
    site.add(*five_units())
    site.mode = "broken"
    env.update()
    assert statuses(env)[-1] == ("full", "failed", "page_invalid")
    assert env.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    env.clock.advance(DAY)
    site.mode = "normal"
    site.add(Unit(id="U7", broken=True))  # 一張卡片解析不出來：6 張裡壞 1 張，低於 95%
    env.update()
    assert statuses(env)[-1] == ("full", "invalid", "parse_rate_low")
    assert env.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 5, "看到的照樣記"


def test_parse_rate_threshold_is_95_percent(env, site):
    """r4 §3.4 第 5 條：解析率 ≥ 95%。20 張壞 1 張（剛好 95%）有效；21 張壞 2 張（約 90%）無效。"""
    site.add(*[Unit(id=f"G{n}", floor=n % 10 + 2) for n in range(19)], Unit(id="B1", broken=True))
    env.update()
    assert statuses(env)[-1] == ("full", "ok", None)
    env.clock.advance(DAY)
    site.add(Unit(id="B2", broken=True))
    env.update()
    assert statuses(env)[-1] == ("full", "invalid", "parse_rate_low")


def test_budget_too_small_for_a_full_scan_is_not_started(env, site):
    write_settings("[sources.cthouse]\nmax_requests_full = 3\nmax_requests_day = 300\n")
    site.add(*[Unit(id=f"U{n}", floor=n + 2) for n in range(1, 11)])  # 4 頁
    env.update()
    assert statuses(env)[-1] == ("full", "failed", "budget_exhausted")
    first = len(site.requests)
    # 隔天：上次沒有有效的完整掃描可以估，照樣試，用完上限就停
    write_settings("[sources.cthouse]\nmax_requests_full = 6\nmax_requests_day = 300\n")
    env.clock.advance(DAY)
    env.update()
    assert statuses(env)[-1] == ("full", "ok", None)
    # 再隔天：上限改小到比上次的頁數少 → 不開始、不送列表請求
    write_settings("[sources.cthouse]\nmax_requests_full = 3\nmax_requests_day = 300\n")
    env.clock.advance(DAY)
    before = len(site.requests)
    env.update()
    assert statuses(env)[-1] == ("full", "failed", "budget_exhausted")
    assert not any("/list" in r for r in site.requests[before:])
    assert first > 0


def test_detail_fill_does_not_churn_versions(env, site):
    site.add(Unit(id="U1", parking=None, detail_parking="坡道平面"))
    env.update()
    facts = versions.latest(env.conn, "cthouse", "U1").facts
    assert facts.parking == Parking.RAMP_FLAT, "詳細頁補上車位"
    assert env.listing("U1")["detail_fetched_at"] is not None
    count = env.conn.execute("SELECT count(*) FROM listing_versions").fetchone()[0]
    assert count == 2  # 列表一版、詳細頁一版
    for _ in range(2):
        env.clock.advance(DAY)
        env.update()
    assert env.conn.execute("SELECT count(*) FROM listing_versions").fetchone()[0] == count
    assert versions.latest(env.conn, "cthouse", "U1").facts.parking == Parking.RAMP_FLAT


def test_price_history_and_drop_in_export(env, site):
    site.add(Unit(id="U1", price_wan=1500))
    env.update()
    env.clock.advance(DAY)
    site.units["U1"].price_wan = 1499.5  # 少 5,000 元：不算一次變動（R3-22）
    env.update()
    env.clock.advance(DAY)
    site.units["U1"].price_wan = 1450
    env.update()
    member = group_of(env, "U1").members[0]
    assert member.price == 14_500_000 and member.price_first == 15_000_000
    assert member.price_drop_at is not None
    assert member.price_drop_at > env.clock.now() - dt.timedelta(hours=1)


def test_same_unit_listed_twice_is_one_group_with_stable_id(env, site):
    site.add(
        Unit(id="A1", floor=6, main_ping=25.5, road="示意路一段"),
        Unit(id="A2", floor=6, main_ping=25.6, road="示意路1段"),  # 同一戶，另一個仲介
        Unit(id="A3", floor=6, main_ping=25.5, road="示意路一段", parking="機械"),
    )
    env.update()
    groups = env.listings_file().groups
    ids = sorted(sorted(m.id for m in g.members) for g in groups)
    assert ["A1", "A2"] in ids and ["A3"] in ids
    gid = group_of(env, "A1").gid
    env.clock.advance(DAY)
    env.update()
    assert group_of(env, "A1").gid == gid, "群組編號穩定"


BOTH = ("taoyuan_core", "zhongli_ic")


def area_scans(env):
    rows = env.conn.execute(
        "SELECT area, mode, status, error_code FROM scans ORDER BY scan_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_one_area_failing_does_not_affect_the_other(env, site):
    """R3-08：同一來源，一個區域成功、另一個區域失敗，只有失敗的那一組不計數。"""
    write_settings('enabled_areas = ["taoyuan_core", "zhongli_ic"]\n')
    site.add(*five_units())
    site.add(*[Unit(id=f"Z{n}", floor=n + 2, area="zhongli_ic") for n in range(1, 4)])
    site.down_areas = {"zhongli_ic"}
    env.update(areas=BOTH)
    assert area_scans(env) == [
        ("taoyuan_core", "full", "ok", None),
        ("zhongli_ic", "full", "failed", "http_5xx"),
    ]
    assert env.listing("U1")["status"] == "active" and env.listing("Z1") is None


def test_areas_take_turns_when_the_daily_budget_is_short(env, site):
    """R3-08：預算只夠一個區域時，上次完整掃描較舊的區域先做，兩區輪流。"""
    write_settings(
        'enabled_areas = ["taoyuan_core", "zhongli_ic"]\n'
        "[sources.cthouse]\nmax_requests_day = 3\nmax_requests_full = 3\ndetail_cap = 0\n"
    )
    site.add(*[Unit(id=f"T{n}", floor=n + 2, parking="坡道平面") for n in range(1, 5)])
    site.add(*[Unit(id=f"Z{n}", floor=n + 2, area="zhongli_ic") for n in range(1, 5)])
    env.update(areas=BOTH)
    assert area_scans(env) == [
        ("taoyuan_core", "full", "ok", None),
        ("zhongli_ic", "full", "failed", "budget_exhausted"),
    ]
    env.clock.advance(DAY)
    env.update(areas=BOTH)
    assert area_scans(env)[2:] == [
        ("zhongli_ic", "full", "ok", None),
        ("taoyuan_core", "full", "failed", "budget_exhausted"),
    ], "中壢區還沒有有效完整掃描，先做；桃園區估計不夠就不開始"
    env.clock.advance(DAY)
    env.update(areas=BOTH)
    assert area_scans(env)[4][:3] == ("taoyuan_core", "full", "ok")


def test_age_based_built_range_does_not_add_daily_versions(env, site):
    site.add(Unit(id="U1", age=9, parking="坡道平面"), Unit(id="U2", age=15, parking="機械"))
    env.update()
    count = env.conn.execute("SELECT count(*) FROM listing_versions").fetchone()[0]
    assert count == 2
    for _ in range(3):
        env.clock.advance(DAY)
        env.update()
    assert env.conn.execute("SELECT count(*) FROM listing_versions").fetchone()[0] == count
    built = versions.latest(env.conn, "cthouse", "U1").facts.built
    assert built.lo is not None and built.hi is not None and (built.hi - built.lo).days >= 364


def test_time_overrun_is_reported_as_partial(env, site, monkeypatch):
    from house_watch.pipeline import scan

    site.add(*five_units())
    calls = {"n": 0}

    def short(self):
        calls["n"] += 1
        return calls["n"] <= 1  # 第一個檢查通過，之後就沒時間了

    monkeypatch.setattr(scan._Run, "in_time", short)
    report = env.update()
    step = next(s for s in report.steps if s.name == "sources")
    assert step.status == "partial" and "時間不夠" in step.message


def test_an_adapter_crash_fails_only_that_scan(env, site, monkeypatch):
    """事後審查 P-2：轉接器遇到沒預料到的資料丟出例外時，這個來源本輪停止、記成失敗，
    不能讓例外穿出去（後面的來源、分群都不做）。"""
    import pipeline_kit

    site.add(*five_units())

    def boom(self, resp, q):
        raise KeyError("unexpected card shape")

    monkeypatch.setattr(pipeline_kit.FakeAdapter, "parse_list", boom)
    env.update()
    assert statuses(env)[-1] == ("full", "failed", "page_invalid")
