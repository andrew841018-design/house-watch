"""tick：執行鎖、due() 跨台北午夜與睡眠、錯開完整掃描、備份輪替、截止時間（WP7b）。

步驟函式一律換成假的（記下呼叫順序與 StepContext），不連網路、不碰其他工作包的進度。
"""

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import pytest
from filelock import FileLock

from house_watch import cli, db, tick
from house_watch.clock import FakeClock, to_iso
from house_watch.paths import DataPaths
from house_watch.pipeline import scan
from house_watch.settings import load_settings
from house_watch.sources.base import AreaQuery

START = dt.datetime(2026, 11, 21, 0, 0, tzinfo=dt.UTC)  # 台北 11/21 08:00
TWO_AREAS = 'enabled_areas = ["taoyuan_core", "zhongli_ic"]\n'


def write_config(text: str) -> None:
    Path(os.environ["HOUSE_WATCH_CONFIG"]).write_text(text, encoding="utf-8")


def data_paths() -> DataPaths:
    return DataPaths(Path(os.environ["HOUSE_WATCH_DATA_DIR"]))


class Steps:
    """假的 doorindex、lvr、sources、export：記錄呼叫順序與參數。"""

    def __init__(self, monkeypatch, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.contexts: dict[str, cli.StepContext] = {}
        self.ok = {"doorindex": True, "lvr": True, "sources": True, "export": True}
        self.effects: dict[str, object] = {}
        for name in ("doorindex", "lvr", "sources"):
            monkeypatch.setitem(cli.STEP_FUNCTIONS, name, self._make(name))
        self.export = self._make("export")
        self.published: list[dict] = []

    def _make(self, name: str):
        def step(ctx: cli.StepContext) -> cli.StepOutcome:
            self.calls.append(name)
            self.contexts[name] = ctx
            effect = self.effects.get(name)
            if callable(effect):
                effect()
            return cli.StepOutcome(self.ok[name], f"假的 {name}")

        return step

    def publisher(self, settings, data, *, clock, conn, log):
        self.published.append({"now": clock.now()})
        return None

    def run(self, tmp_path: Path, **kwargs) -> int:
        return tick.run_tick(
            clock=self.clock,
            log_directory=tmp_path / "logs",
            console=False,
            publisher=self.publisher,
            export_step=self.export,
            **kwargs,
        )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START, monotonic=1000.0)


@pytest.fixture
def steps(monkeypatch, clock) -> Steps:
    # tick 不自動 init（F1-7）：像正式環境一樣，先用 house-watch init 建好資料夾。
    db.open_or_init(DataPaths(load_settings().data_dir)).close()
    return Steps(monkeypatch, clock)


def _open() -> sqlite3.Connection:
    return db.connect(data_paths().db)


def _kv(key: str) -> str | None:
    conn = _open()
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _set_kv(conn: sqlite3.Connection, **values: dt.datetime) -> None:
    names = {
        "door_ok": tick.KV_DOORINDEX_OK,
        "door_try": tick.KV_DOORINDEX_TRY,
        "lvr_ok": tick.KV_LVR_OK,
        "lvr_try": tick.KV_LVR_TRY,
        "integrity": tick.KV_INTEGRITY_OK,
    }
    with db.transaction(conn):
        for name, when in values.items():
            cli.put_kv(conn, names[name], to_iso(when))


def _log_text(tmp_path: Path) -> str:
    return (tmp_path / "logs" / "tick.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 執行鎖與資料夾


def test_lock_contention_logs_skipped_and_exits_0(tmp_path, steps):
    data = data_paths()
    with FileLock(data.lock):  # 模擬另一輪正在跑
        assert steps.run(tmp_path) == 0
    assert steps.calls == []
    assert "skipped" in _log_text(tmp_path)
    assert _kv(tick.KV_LAST_RUN) is None  # 拿不到鎖：什麼都沒做


def test_first_tick_after_init(tmp_path, steps):
    data = data_paths()  # steps 已經像 house-watch init 一樣建好資料夾（tick 不自動 init，F1-7）
    assert steps.run(tmp_path) == 0
    assert data.db.is_file() and data.instance_marker.is_file()
    # 還沒掃過：樂屋用 max_requests_full 估計，是大的來源 → 掃描先跑，門牌、實價登錄排在後面
    assert steps.calls == ["sources", "doorindex", "lvr", "export"]
    assert len(steps.published) == 0  # 沒有 [publish] 設定 → 略過
    assert tick.daily_backup_path(data, dt.date(2026, 11, 21)).is_file()
    assert _kv(tick.KV_LVR_OK) == to_iso(START)
    assert json.loads(_kv(tick.KV_LAST_RUN))["exit_code"] == 0
    assert "logs" not in {p.name for p in data.root.iterdir()}  # log 不放資料夾


def test_folder_with_marker_but_no_database_exits_2(tmp_path, steps):
    data = data_paths()
    data.db.unlink()  # 只剩 instance 標記、沒有資料庫
    assert steps.run(tmp_path) == 2
    assert steps.calls == []


def test_daily_jobs_run_first_without_a_large_source(tmp_path, steps):
    # 估計各約 8 分，都不算大的（中信的單次上限 11/21 調成 220，不壓低的話會被當成大的）
    write_config("[sources.rakuya]\nmax_requests_full = 100\n[sources.cthouse]\nmax_requests_full = 100\n")
    assert steps.run(tmp_path) == 0
    assert steps.calls == ["doorindex", "lvr", "sources", "export"]


def test_second_tick_same_day_skips_daily_jobs(tmp_path, steps, clock):
    assert steps.run(tmp_path) == 0
    steps.calls.clear()
    clock.advance(3 * 3600)
    assert steps.run(tmp_path) == 0
    assert steps.calls == ["sources", "export"]


def test_publisher_is_called_only_when_configured(tmp_path, steps):
    site = tmp_path / "site"
    site.mkdir()
    key, known = tmp_path / "key", tmp_path / "known_hosts"
    write_config(
        f'[publish]\nsite_repo_dir = "{site}"\ndeploy_key = "{key}"\nknown_hosts = "{known}"\n'
    )
    assert steps.run(tmp_path) == 0
    assert len(steps.published) == 1


# ---------------------------------------------------------------- due()：台北日期與重試


def _settings_and_conn():
    settings = load_settings()
    data = DataPaths(settings.data_dir)
    conn = db.open_or_init(data)
    return settings, data, conn


def test_due_daily_jobs_follow_the_taipei_date():
    settings, data, conn = _settings_and_conn()
    try:
        last = dt.datetime(2026, 11, 21, 15, 30, tzinfo=dt.UTC)  # 台北 11/21 23:30
        week_ago = dt.datetime(2026, 11, 15, 1, 0, tzinfo=dt.UTC)  # 台北 11/15 09:00
        _set_kv(conn, lvr_ok=last, door_ok=week_ago, integrity=week_ago)
        tick.daily_backup_path(data, dt.date(2026, 11, 21)).parent.mkdir(parents=True)
        tick.daily_backup_path(data, dt.date(2026, 11, 21)).write_bytes(b"")

        before = tick.due(conn, settings, data, dt.datetime(2026, 11, 21, 15, 50, tzinfo=dt.UTC))
        assert before == tick.Due(integrity=False, backup=False, doorindex=False, lvr=False)

        # 台北 11/22 00:10：同一個 UTC 日期，但台北已經是隔天
        after = tick.due(conn, settings, data, dt.datetime(2026, 11, 21, 16, 10, tzinfo=dt.UTC))
        assert after == tick.Due(integrity=True, backup=True, doorindex=True, lvr=True)
    finally:
        conn.close()


def test_due_failed_daily_job_waits_before_retrying():
    settings, data, conn = _settings_and_conn()
    try:
        now = dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.UTC)
        _set_kv(
            conn,
            lvr_ok=now - dt.timedelta(days=1),
            lvr_try=now - dt.timedelta(hours=2),  # 今天試過、失敗
            door_try=now - dt.timedelta(hours=7),  # 從來沒成功過，7 小時前試過
        )
        jobs = tick.due(conn, settings, data, now)
        assert jobs.lvr is False and jobs.doorindex is True
        later = tick.due(conn, settings, data, now + dt.timedelta(hours=tick.RETRY_HOURS))
        assert later.lvr is True
    finally:
        conn.close()


def test_failed_lvr_is_not_recorded_as_success(tmp_path, steps, clock):
    steps.ok["lvr"] = False
    assert steps.run(tmp_path) == 1
    assert _kv(tick.KV_LVR_OK) is None and _kv(tick.KV_LVR_TRY) == to_iso(START)
    steps.calls.clear()
    clock.advance(3 * 3600)  # 還沒到重試時間
    steps.ok["lvr"] = True
    steps.run(tmp_path)
    assert "lvr" not in steps.calls
    steps.calls.clear()
    clock.advance(3 * 3600)
    steps.run(tmp_path)
    assert "lvr" in steps.calls


# ---------------------------------------------------------------- 睡眠與截止時間


def test_sleep_in_a_late_tick_stops_safely_and_keeps_the_start_day(tmp_path, steps, clock):
    late = FakeClock(dt.datetime(2026, 11, 21, 15, 0, tzinfo=dt.UTC), monotonic=50.0)  # 台北 23:00
    steps.clock = late
    steps.effects["sources"] = lambda: late.advance(9 * 3600, asleep=True)  # 睡到隔天早上
    assert steps.run(tmp_path) == 1
    assert steps.calls == ["sources"]  # 之後的步驟、匯出、發布都不做
    assert steps.published == []
    summary = json.loads(_kv(tick.KV_LAST_RUN))
    assert summary["stopped"] == "slept" and summary["started"] == "2026-11-21T15:00:00Z"
    data = data_paths()
    assert tick.daily_backup_path(data, dt.date(2026, 11, 21)).is_file()  # 算開始的那一天
    assert not tick.daily_backup_path(data, dt.date(2026, 11, 22)).exists()
    assert "slept" in _log_text(tmp_path)


def test_deadline_stops_fetching_but_still_exports(tmp_path, steps, clock):
    total = load_settings().public.schedule.run_deadline_minutes * 60
    steps.effects["sources"] = lambda: clock.advance(total)  # 醒著，但時間用完了
    assert steps.run(tmp_path) == 1
    assert steps.calls == ["sources", "export"]  # 門牌、實價登錄不做，匯出照做
    assert json.loads(_kv(tick.KV_LAST_RUN))["stopped"] == "deadline"


def test_guard_detects_sleep_and_wall_limit():
    clock = FakeClock(START)
    guard = tick.Guard(clock, 3000)
    assert guard.problem() is None
    clock.advance(tick.SLEEP_GAP_S + 1, asleep=True)
    assert guard.problem() == "slept"
    awake = FakeClock(START)
    guard = tick.Guard(awake, 3000)
    awake.advance(3000)
    assert guard.problem() == "deadline"


def test_each_step_gets_its_own_deadline(tmp_path, steps, clock):
    assert steps.run(tmp_path) == 0
    total = load_settings().public.schedule.run_deadline_minutes * 60
    start = 1000.0
    assert steps.contexts["lvr"].deadline == start + tick.DAILY_STEP_BUDGET_S["lvr"]
    assert steps.contexts["sources"].deadline == start + total - tick.EXPORT_RESERVE_S
    assert steps.contexts["export"].deadline == start + total


# ---------------------------------------------------------------- 錯開完整掃描


def _plan(source, position, full_s, *, oldest=START, full=True, other_s=40.0):
    modes = {"taoyuan_core": "full" if full else "probe"}
    return tick.SourcePlan(source, position, modes, full_s if full else 0.0, other_s, oldest)


def test_select_sources_runs_one_large_source_first():
    yesterday = START - dt.timedelta(days=1)
    plans = [
        _plan("century21", 0, 0, full=False),
        _plan("cthouse", 1, 960, oldest=yesterday),  # 大的（≥ 750 秒）
        _plan("etwarm", 2, 200, oldest=yesterday),
        _plan("hbhousing", 3, 480, oldest=yesterday),
        _plan("rakuya", 4, 2380, oldest=yesterday - dt.timedelta(hours=3)),  # 大的、最舊
    ]
    chosen = tick.select_sources(plans, run_total_s=3000, budget_s=2700)
    assert chosen.large == "rakuya"
    assert chosen.order[0] == "rakuya"  # 大的排第一，拿到整輪的時間
    assert "cthouse" in chosen.deferred  # 一輪最多一個大的
    assert chosen.order == ("rakuya", "etwarm", "century21")  # hbhousing 放不進剩下的時間
    assert chosen.deferred == ("cthouse", "hbhousing")


def test_select_sources_without_a_large_source_fits_the_budget():
    plans = [
        _plan("a", 0, 600),
        _plan("b", 1, 600),
        _plan("c", 2, 600),
        _plan("d", 3, 0, full=False),
    ]
    chosen = tick.select_sources(plans, run_total_s=3000, budget_s=1300)
    assert chosen.large is None
    assert chosen.order == ("a", "b", "d") and chosen.deferred == ("c",)


def _insert_full(conn, settings, source: str, area_id: str, pages: int, started: dt.datetime):
    area = next(a for a in settings.public.areas if a.id == area_id)
    adapter = scan.build_adapters([source], settings.public.scope.listing)[source]
    sh = scan.scope_hash(adapter, AreaQuery.from_config(area, settings.public.scope.listing))
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO scans (source, area, mode, started_at, finished_at, status, pages, parsed,"
            " scope_hash) VALUES (?, ?, 'full', ?, ?, 'ok', ?, ?, ?)",
            (source, area_id, to_iso(started), to_iso(started), pages, pages * 20, sh),
        )


def test_large_sources_are_staggered_across_ticks(tmp_path, steps, clock):
    write_config(TWO_AREAS)
    settings, _data, conn = _settings_and_conn()
    yesterday = START - dt.timedelta(days=1)
    pages = {
        "rakuya": (286, 232),
        "cthouse": (108, 100),
        "hbhousing": (53, 50),
        "etwarm": (11, 9),
        "century21": (11, 8),
    }
    try:
        for source, (core, zhongli) in pages.items():
            when = yesterday - (dt.timedelta(hours=1) if source == "rakuya" else dt.timedelta())
            _insert_full(conn, settings, source, "taoyuan_core", core, when)
            _insert_full(conn, settings, source, "zhongli_ic", zhongli, when)
        plan = tick.plan_sources(conn, settings, START, run_total_s=3000, budget_s=2700)
    finally:
        conn.close()
    by_source = {p.source: p for p in plan.plans}
    assert by_source["rakuya"].full_s == pytest.approx((286 + 232) * 4 * tick.COST_MARGIN)
    assert plan.large == "rakuya" and plan.order[0] == "rakuya"
    assert "cthouse" in plan.deferred  # 中信也是大的，下一輪
    # 有大的來源時，掃描先跑，門牌與實價登錄排在後面（時間不夠就延後）
    assert steps.run(tmp_path) == 0
    assert steps.calls == ["sources", "doorindex", "lvr", "export"]
    assert steps.contexts["sources"].sources[0] == "rakuya"
    assert "cthouse" not in steps.contexts["sources"].sources

    # 樂屋今天的完整掃描做完了 → 下一輪換中信（大的）先跑
    settings, _data, conn = _settings_and_conn()
    try:
        for area_id, count in (("taoyuan_core", 286), ("zhongli_ic", 232)):
            _insert_full(conn, settings, "rakuya", area_id, count, START + dt.timedelta(minutes=5))
    finally:
        conn.close()
    clock.advance(3 * 3600)
    steps.calls.clear()
    assert steps.run(tmp_path) == 0
    assert steps.calls == ["sources", "export"]
    assert steps.contexts["sources"].sources[0] == "cthouse"
    # 樂屋只剩詳細頁（距上次掃描不到 3 小時，scan 不探測），便宜，照跑
    assert "rakuya" in steps.contexts["sources"].sources


def test_plan_falls_back_to_max_requests_full_when_never_scanned():
    write_config(TWO_AREAS)
    settings, _data, conn = _settings_and_conn()
    try:
        plans = {p.source: p for p in tick.source_plans(conn, settings, START)}
    finally:
        conn.close()
    rakuya = settings.sources["rakuya"]
    assert plans["rakuya"].full_s == pytest.approx(
        2 * rakuya.max_requests_full * 4 * tick.COST_MARGIN
    )
    assert all(p.needs_full for p in plans.values())


# ---------------------------------------------------------------- 備份與 integrity_check


def test_backup_is_a_consistent_copy_and_rotates(tmp_path):
    settings, data, conn = _settings_and_conn()
    try:
        with db.transaction(conn):
            cli.put_kv(conn, "test.key", "示意值")
        day = dt.date(2026, 10, 1)
        for offset in range(45):
            tick.make_backup(conn, data, day + dt.timedelta(days=offset), keep_daily=7)
    finally:
        conn.close()
    daily = sorted((data.backups_dir / "daily").iterdir())
    weekly = sorted((data.backups_dir / "weekly").iterdir())
    assert [p.name for p in daily][-1] == "house_watch-2026-11-14.sqlite"
    assert len(daily) == 7 and len(weekly) == tick.WEEKLY_KEEP
    copy = sqlite3.connect(daily[-1])
    try:
        assert copy.execute("SELECT value FROM kv WHERE key = 'test.key'").fetchone()[0] == "示意值"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        copy.close()


def test_rotation_only_touches_backup_files(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    for name in ("house_watch-2026-01-01.sqlite", "house_watch-2026-01-02.sqlite", "notes.txt"):
        (folder / name).write_text("", encoding="utf-8")
    removed = tick.rotate(folder, tick.DAILY_PATTERN, 1)
    assert [p.name for p in removed] == ["house_watch-2026-01-01.sqlite"]
    assert (folder / "notes.txt").exists()


def test_failed_integrity_check_stops_before_backup(tmp_path, steps, monkeypatch):
    monkeypatch.setattr(tick, "integrity_ok", lambda conn: False)
    assert steps.run(tmp_path) == 1
    assert steps.calls == []
    assert not (data_paths().backups_dir / "daily").exists()
    assert "integrity_check" in _log_text(tmp_path)


def test_unexpected_error_is_logged_with_masked_paths(tmp_path, steps, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"壞掉了：{Path.home() / 'hw-not-real'}")

    monkeypatch.setattr(tick, "due", boom)
    assert steps.run(tmp_path) == 1
    text = _log_text(tmp_path)
    assert "RuntimeError" in text and str(Path.home()) not in text


def test_tick_refuses_to_start_without_a_settings_file(tmp_path, monkeypatch):
    """事後審查 SA-2、P-11：排程檔漏了環境變數時，tick 找不到設定檔就停（結束碼 2），
    不能用空設定在預設位置另建資料夾、另起一個爬蟲。"""
    from house_watch import paths
    from house_watch.errors import ExitCode

    monkeypatch.delenv("HOUSE_WATCH_CONFIG")
    monkeypatch.setattr(paths, "default_config_dir", lambda: tmp_path / "no_config")
    data = Path(os.environ["HOUSE_WATCH_DATA_DIR"])
    code = tick.run_tick(clock=FakeClock(START), log_directory=tmp_path / "logs", console=False)
    assert code == int(ExitCode.CONFIG)
    assert not data.exists()


def test_a_sleep_after_the_deadline_still_skips_export(tmp_path, steps, monkeypatch):
    """事後審查 CX-6：先記到 deadline、之後電腦睡著（牆上時鐘跳走）時，最後的判斷要看「現在」
    的問題，不能只看第一次記下的原因；睡著過就不匯出、不發布。"""
    state = {"phase": 0}
    real = tick.Guard.problem

    def fake(self):
        if state["phase"] == 0:
            return real(self)
        state["phase"] += 1
        return "deadline" if state["phase"] == 2 else "slept"

    monkeypatch.setattr(tick.Guard, "problem", fake)
    steps.effects["sources"] = lambda: state.__setitem__("phase", 1)
    assert steps.run(tmp_path) == 1
    assert "export" not in steps.calls and steps.published == []
    assert json.loads(_kv(tick.KV_LAST_RUN))["stopped"] == "deadline"


def test_tick_refuses_a_missing_or_empty_data_folder(tmp_path, monkeypatch):
    """修完再審 F1-7：設定檔在、資料夾卻不存在（路徑設錯、資料夾被搬走、指到被清掉的暫存區）時，
    tick 不能自動建新資料庫、從頭抓、再把新資料發布出去。第一次用要先 house-watch init。"""
    from house_watch.errors import ExitCode

    data = Path(os.environ["HOUSE_WATCH_DATA_DIR"])
    published = []
    code = tick.run_tick(
        clock=FakeClock(START),
        log_directory=tmp_path / "logs",
        console=False,
        publisher=lambda *a, **k: published.append(1),
    )
    assert code == int(ExitCode.CONFIG)
    assert not data.exists() and published == []
