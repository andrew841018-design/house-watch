"""排程入口 `house-watch tick`（WP7b；r4 §0、§3.4、§3.5；R1-18、R1-25、R2-08、R2-15、R3-18）。

launchd（或 cron、Windows 工作排程器）在時段內定時呼叫。每一次：

1. 取得執行鎖 `FileLock(<資料夾>/run.lock)`（作業系統層級，和 update／export／publish 同一把），
   不等待。拿不到就在 log 記一筆 skipped，結束碼 0。
2. 開資料庫：資料夾完全是空的才自動 init；有 instance 標記卻沒有資料庫 → 結束碼 2。
3. `due()` 用這一輪的開始時間（一輪算開始的那一天，R3-18）與資料庫紀錄，決定要做的工作：
   - integrity_check：每 INTEGRITY_EVERY_DAYS 天（台北日期）一次。沒通過就停在這裡，
     不備份、也不輪替掉舊的好備份。
   - 備份：今天（台北日期）還沒有每日備份就做（SQLite backup API）。
     每日保留 `[schedule] backup_keep` 份，每週（每個 ISO 週的第一份）保留 WEEKLY_KEEP 份。
   - 門牌詮釋資料：上次成功距今 ≥ `doorindex_every_days` 天（台北日期）。
   - 實價登錄：今天（台北日期）還沒成功過（或距上次成功 ≥ `lvr_every_hours`）。
     成功與否記在 tick 自己的 kv（KV_*）。`lvr.last_check` 也只在成功時更新（給網頁判斷過期）。
   - 門牌與實價登錄失敗後，至少隔 RETRY_HOURS 小時才再試（禮貌規則）。
   - 各（來源, 區域）的完整掃描或探測、詳細頁佇列：規則在 pipeline.scan（decide_mode），
     和 update 同一套；這裡只決定這一輪讓哪些來源跑（見 4）。
4. 錯開完整掃描（plan_sources，G4+ 實測樂屋兩區一次完整掃描約 518 頁 ≈ 35 分）：
   - 每個來源的時間估計＝Σ（上次有效完整掃描的頁數；沒有就用 max_requests_full）
     × max(min_interval_s, 4 秒) × COST_MARGIN，加上探測與詳細頁。
   - 估計 ≥ 整輪時間的 LARGE_SHARE 的是「大的」：一輪最多一個，而且排在最前面、
     先跑，拿到整輪的時間（中途逾時算暫時性錯誤，下一輪會重試）。
     有好幾個大的時，上次有效完整掃描最舊的先。
   - 其餘要做完整掃描的來源依「上次有效完整掃描最舊」排，放得進剩餘時間的才跑，其他延到下一輪。
   - 只需要探測或詳細頁的來源每一輪都跑（便宜；時間到了 scan 會自己停）。
   - 這一輪有大的來源時，門牌與實價登錄排在掃描之後，剩下的時間夠才做，不夠就延到下一輪；
     沒有大的來源時，門牌與實價登錄先做。
5. 每一步有自己的截止時間（StepContext.deadline）；步驟之間檢查截止時間與牆上時鐘（Guard）：
   牆上時鐘比單調時鐘多走 SLEEP_GAP_S 以上代表中途睡著，牆上時鐘經過的時間超過整輪的時間也停
   （R3-18）。睡著或超過牆上時鐘上限時，剩下的步驟、匯出與發布都不做，讓下一輪重來。
6. 匯出，再依心跳規則發布（publish.publish_site）。`[publish]` 沒設定時略過發布。

結束碼（C10）：0 成功或略過；1 部分失敗；2 設定或資料夾錯誤。
log 寫在 ops.logging 的 log 資料夾（輪替，5 MB × 5）。
"""

import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from house_watch import cli, db, export, paths
from house_watch.clock import Clock, SystemClock, from_iso, taipei_date, to_iso
from house_watch.errors import ExitCode, HouseWatchError
from house_watch.ops.logging import tick_logging
from house_watch.paths import DataPaths
from house_watch.settings import Settings, load_settings

KV_DOORINDEX_OK = "tick.doorindex.last_ok"
KV_DOORINDEX_TRY = "tick.doorindex.last_attempt"
KV_LVR_OK = "tick.lvr.last_ok"
KV_LVR_TRY = "tick.lvr.last_attempt"
KV_INTEGRITY_OK = "tick.integrity.last_ok"
KV_LAST_RUN = "tick.last_run"  # JSON：started、finished、exit_code、stopped

RETRY_HOURS = 6
INTEGRITY_EVERY_DAYS = 7
WEEKLY_KEEP = 4
SECONDS_PER_REQUEST = 4.0  # 抓取層保證同一主機兩次請求至少間隔 4 秒
COST_MARGIN = 1.15
LARGE_SHARE = 0.25
EXPORT_RESERVE_S = 300.0  # 留給匯出與發布
DAILY_STEP_BUDGET_S = {"doorindex": 900.0, "lvr": 900.0}
SLEEP_GAP_S = 300.0

EPOCH_UTC = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
DAILY_PATTERN = re.compile(r"^house_watch-\d{4}-\d{2}-\d{2}\.sqlite$")
WEEKLY_PATTERN = re.compile(r"^house_watch-\d{4}-W\d{2}\.sqlite$")


# ---------------------------------------------------------------- kv


def _kv_time(conn: sqlite3.Connection, key: str) -> dt.datetime | None:
    raw = export.read_kv(conn, key)
    if raw is None:
        return None
    try:
        return from_iso(raw)
    except ValueError:
        return None


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    with db.transaction(conn):
        cli.put_kv(conn, key, value)


# ---------------------------------------------------------------- 到期的工作


@dataclass(frozen=True)
class Due:
    integrity: bool
    backup: bool
    doorindex: bool
    lvr: bool

    def names(self) -> list[str]:
        return [name for name in ("integrity", "backup", "doorindex", "lvr") if getattr(self, name)]


def _retry_allowed(conn: sqlite3.Connection, ok_key: str, try_key: str, now: dt.datetime) -> bool:
    """上次嘗試成功了，或失敗後已經隔了 RETRY_HOURS 小時。"""
    last_try, last_ok = _kv_time(conn, try_key), _kv_time(conn, ok_key)
    if last_try is None or (last_ok is not None and last_ok >= last_try):
        return True
    return now - last_try >= dt.timedelta(hours=RETRY_HOURS)


def daily_backup_path(data: DataPaths, day: dt.date) -> Path:
    return data.backups_dir / "daily" / f"house_watch-{day.isoformat()}.sqlite"


def weekly_backup_path(data: DataPaths, day: dt.date) -> Path:
    iso = day.isocalendar()
    return data.backups_dir / "weekly" / f"house_watch-{iso.year}-W{iso.week:02d}.sqlite"


def due(conn: sqlite3.Connection, settings: Settings, data: DataPaths, now: dt.datetime) -> Due:
    """這一輪到期的工作（掃描另外由 plan_sources 決定）。now 是這一輪的開始時間。"""
    schedule = settings.public.schedule
    today = taipei_date(now)
    door_ok = _kv_time(conn, KV_DOORINDEX_OK)
    doorindex = (
        door_ok is None or (today - taipei_date(door_ok)).days >= schedule.doorindex_every_days
    ) and _retry_allowed(conn, KV_DOORINDEX_OK, KV_DOORINDEX_TRY, now)
    lvr_ok = _kv_time(conn, KV_LVR_OK)
    lvr = (
        lvr_ok is None
        or taipei_date(lvr_ok) < today
        or now - lvr_ok >= dt.timedelta(hours=schedule.lvr_every_hours)
    ) and _retry_allowed(conn, KV_LVR_OK, KV_LVR_TRY, now)
    checked = _kv_time(conn, KV_INTEGRITY_OK)
    integrity = checked is None or (today - taipei_date(checked)).days >= INTEGRITY_EVERY_DAYS
    backup = not daily_backup_path(data, today).is_file()
    return Due(integrity=integrity, backup=backup, doorindex=doorindex, lvr=lvr)


# ---------------------------------------------------------------- 備份與 integrity_check


def integrity_ok(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return len(rows) == 1 and rows[0][0] == "ok"


def _sqlite_copy(conn: sqlite3.Connection, target: Path) -> None:
    """SQLite backup API 寫到暫存檔，完成後才換名；備份檔不留 WAL。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
    os.replace(tmp, target)


def rotate(directory: Path, pattern: re.Pattern[str], keep: int) -> list[Path]:
    """只動符合 pattern 的檔：依檔名（日期）排序，保留最新的 keep 份。"""
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.iterdir() if p.is_file() and pattern.match(p.name))
    removed = files[: max(len(files) - keep, 0)]
    for path in removed:
        path.unlink()
    return removed


def make_backup(
    conn: sqlite3.Connection, data: DataPaths, day: dt.date, keep_daily: int
) -> tuple[Path, list[Path]]:
    """每日備份＋每週第一份另存；成功後才輪替。回傳 (每日備份, 刪掉的舊備份)。"""
    daily = daily_backup_path(data, day)
    _sqlite_copy(conn, daily)
    weekly = weekly_backup_path(data, day)
    if not weekly.is_file():
        weekly.parent.mkdir(parents=True, exist_ok=True)
        tmp = weekly.with_name(f".{weekly.name}.tmp")
        shutil.copyfile(daily, tmp)
        os.replace(tmp, weekly)
    removed = rotate(daily.parent, DAILY_PATTERN, keep_daily)
    removed += rotate(weekly.parent, WEEKLY_PATTERN, WEEKLY_KEEP)
    return daily, removed


# ---------------------------------------------------------------- 錯開完整掃描


@dataclass(frozen=True)
class SourcePlan:
    source: str
    position: int
    modes: dict[str, str | None]  # 區域 → full／probe／None（scan.decide_mode）
    full_s: float  # 這一輪完整掃描的估計秒數
    other_s: float  # 探測與詳細頁的估計秒數
    oldest_full: dt.datetime  # 要做完整掃描的區域裡，上次有效完整掃描最舊的時間

    @property
    def needs_full(self) -> bool:
        return any(mode == "full" for mode in self.modes.values())

    @property
    def cost_s(self) -> float:
        return self.full_s + self.other_s


@dataclass(frozen=True)
class SourcesPlan:
    order: tuple[str, ...]  # 這一輪要跑的來源，依序
    deferred: tuple[str, ...]  # 延到下一輪的來源
    large: str | None  # 這一輪的大來源（排在最前面）
    estimate_s: float
    plans: tuple[SourcePlan, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        parts = [f"這一輪：{'、'.join(self.order) or '（無）'}"]
        if self.large:
            parts.append(f"大的來源 {self.large} 先跑")
        if self.deferred:
            parts.append(f"延到下一輪：{'、'.join(self.deferred)}")
        parts.append(f"估計 {self.estimate_s / 60:.0f} 分")
        return "；".join(parts)


def select_sources(
    plans: Sequence[SourcePlan], *, run_total_s: float, budget_s: float
) -> SourcesPlan:
    """依估計時間挑這一輪要跑的來源（見模組說明第 4 點）。"""
    large_limit = LARGE_SHARE * run_total_s
    fulls = [p for p in plans if p.needs_full]
    large = [p for p in fulls if p.full_s >= large_limit]
    small = sorted(
        (p for p in fulls if p.full_s < large_limit), key=lambda p: (p.oldest_full, p.position)
    )
    chosen: list[SourcePlan] = []
    deferred: list[SourcePlan] = []
    used = 0.0
    big = min(large, key=lambda p: (p.oldest_full, p.position)) if large else None
    if big is not None:
        chosen.append(big)
        used += big.cost_s
        deferred.extend(p for p in large if p is not big)
    for plan in small:
        if used + plan.cost_s <= budget_s:
            chosen.append(plan)
            used += plan.cost_s
        else:
            deferred.append(plan)
    for plan in sorted((p for p in plans if not p.needs_full), key=lambda p: p.position):
        chosen.append(plan)
        used += plan.cost_s
    deferred.sort(key=lambda p: p.position)
    return SourcesPlan(
        order=tuple(p.source for p in chosen),
        deferred=tuple(p.source for p in deferred),
        large=big.source if big is not None else None,
        estimate_s=used,
        plans=tuple(plans),
    )


def source_plans(
    conn: sqlite3.Connection, settings: Settings, now: dt.datetime
) -> list[SourcePlan]:
    """每個啟用的來源這一輪要做什麼、估計多久（掃描規則用 pipeline.scan 的同一套函式）。"""
    from house_watch.http import StateStore
    from house_watch.pipeline import scan
    from house_watch.sources.base import AreaQuery

    public = settings.public
    scope = public.scope.listing
    schedule = public.schedule
    configs = settings.sources
    wanted = [source for source, config in sorted(configs.items()) if config.enabled]
    adapters = scan.build_adapters(wanted, scope)
    states = export.source_states(conn)
    store = StateStore(conn)
    today = taipei_date(now)
    plans = []
    for position, source in enumerate(wanted):
        config = configs[source]
        seconds = max(config.min_interval_s, SECONDS_PER_REQUEST) * COST_MARGIN
        adapter = adapters.get(source)
        state = states.get(source)
        paused = state is not None and state.paused_until is not None and state.paused_until > now
        modes: dict[str, str | None] = {}
        full_pages = other_pages = 0
        oldest: dt.datetime | None = None
        remaining = config.max_requests_day - store.requests_on(source, today)
        if adapter is not None and not paused:
            for area in settings.enabled_areas:
                q = AreaQuery.from_config(area, scope)
                rows = scan.scan_rows(conn, source, area.id, scan.scope_hash(adapter, q))
                mode = scan.decide_mode(
                    rows,
                    now=now,
                    probe_sort=adapter.caps.probe_sort,
                    probe_pages=config.probe_pages,
                    probe_every_hours=schedule.probe_every_hours,
                )
                modes[area.id] = mode
                last_valid = next(
                    (r for r in rows if r.mode == "full" and r.status in db.SCAN_OK_STATUSES), None
                )
                if mode == "full":
                    pages = last_valid.pages if last_valid is not None else config.max_requests_full
                    if pages <= min(config.max_requests_full, remaining):
                        full_pages += (
                            pages  # 估計做不完的，scan 會直接記 budget_exhausted、不送請求
                        )
                    when = last_valid.started_at if last_valid is not None else EPOCH_UTC
                    oldest = when if oldest is None else min(oldest, when)
                elif mode == "probe":
                    other_pages += config.probe_pages
            other_pages += config.detail_cap  # 詳細頁佇列每一輪都跑
        plans.append(
            SourcePlan(
                source=source,
                position=position,
                modes=modes,
                full_s=full_pages * seconds,
                other_s=other_pages * seconds,
                oldest_full=oldest or EPOCH_UTC,
            )
        )
    return plans


def plan_sources(
    conn: sqlite3.Connection,
    settings: Settings,
    now: dt.datetime,
    *,
    run_total_s: float,
    budget_s: float,
) -> SourcesPlan:
    return select_sources(
        source_plans(conn, settings, now), run_total_s=run_total_s, budget_s=budget_s
    )


# ---------------------------------------------------------------- 截止時間與睡眠偵測


class Guard:
    """整輪的截止時間（單調時鐘）與牆上時鐘的上限（R3-18）。"""

    def __init__(self, clock: Clock, total_s: float) -> None:
        self.clock = clock
        self.total_s = total_s
        self.wall0 = clock.now()
        self.mono0 = clock.monotonic()

    @property
    def deadline(self) -> float:
        return self.mono0 + self.total_s

    def remaining(self) -> float:
        return self.deadline - self.clock.monotonic()

    def problem(self) -> str | None:
        """slept（中途睡著）、wall_limit（牆上時鐘超過上限）、deadline，或 None。"""
        wall = (self.clock.now() - self.wall0).total_seconds()
        mono = self.clock.monotonic() - self.mono0
        if wall - mono > SLEEP_GAP_S:
            return "slept"
        if mono >= self.total_s:
            return "deadline"
        if wall >= self.total_s:
            return "wall_limit"
        return None


STOP_ALL = frozenset({"slept", "wall_limit"})  # 這兩種連匯出與發布都不做


# ---------------------------------------------------------------- 一輪


@dataclass
class TickReport:
    steps: list[cli.StepResult] = field(default_factory=list)
    stopped: str | None = None
    exit_code: ExitCode = ExitCode.OK

    def add(self, result: cli.StepResult) -> None:
        self.steps.append(result)
        self.exit_code = max(self.exit_code, result.exit_code)

    def fail(self, name: str, message: str, code: ExitCode = ExitCode.PARTIAL) -> None:
        self.add(cli.StepResult(name, "failed", code, message))


class Tick:
    """持有執行鎖之後的一輪（run_tick 負責拿鎖與 log）。"""

    def __init__(
        self,
        settings: Settings,
        data: DataPaths,
        clock: Clock,
        log: logging.Logger,
        *,
        publisher: Callable[..., Any] | None = None,
        export_step: Callable[[cli.StepContext], cli.StepOutcome] | None = None,
    ) -> None:
        self.settings = settings
        self.data = data
        self.clock = clock
        self.log = log
        self.total_s = settings.public.schedule.run_deadline_minutes * 60.0
        self.report = TickReport()
        self._publisher = publisher
        self._export_step = export_step or cli.step_export

    # ------------------------------------------------------------ 步驟

    def _context(
        self, conn: sqlite3.Connection, deadline: float, sources: Sequence[str] = ()
    ) -> cli.StepContext:
        return cli.StepContext(
            settings=self.settings,
            conn=conn,
            data=self.data,
            clock=self.clock,
            deadline=deadline,
            areas=tuple(self.settings.enabled_areas),
            sources=tuple(sources),
        )

    def _step(
        self,
        name: str,
        fn: Callable[[cli.StepContext], cli.StepOutcome],
        conn: sqlite3.Connection,
        deadline: float,
        sources: Sequence[str] = (),
    ) -> cli.StepResult:
        ctx = self._context(conn, deadline, sources)
        try:
            result = cli.run_step(name, fn, ctx)
        finally:
            ctx.close()
        self.report.add(result)
        level = logging.INFO if result.status == "ok" else logging.WARNING
        self.log.log(level, result.describe())
        return result

    def _stopped(self, guard: Guard) -> bool:
        problem = guard.problem()
        if problem is None:
            return False
        if self.report.stopped is None:
            self.report.stopped = problem
            self.report.exit_code = max(self.report.exit_code, ExitCode.PARTIAL)
            self.log.warning("停止這一輪的其餘步驟：%s", problem)
        return True

    def _daily(self, conn: sqlite3.Connection, guard: Guard, names: Sequence[str]) -> None:
        keys = {
            "doorindex": (KV_DOORINDEX_OK, KV_DOORINDEX_TRY),
            "lvr": (KV_LVR_OK, KV_LVR_TRY),
        }
        for name in names:
            if self._stopped(guard):
                return
            budget = DAILY_STEP_BUDGET_S[name]
            if guard.remaining() - EXPORT_RESERVE_S < budget:
                self.log.info("%s：這一輪剩下的時間不夠，延到下一輪", name)
                continue
            ok_key, try_key = keys[name]
            _put(conn, try_key, to_iso(self.clock.now()))
            deadline = min(guard.deadline - EXPORT_RESERVE_S, self.clock.monotonic() + budget)
            result = self._step(name, cli.STEP_FUNCTIONS[name], conn, deadline)
            if result.status == "ok":
                _put(conn, ok_key, to_iso(self.clock.now()))

    def _plan(self, conn: sqlite3.Connection, guard: Guard) -> SourcesPlan | None:
        try:
            return plan_sources(
                conn,
                self.settings,
                self.clock.now(),
                run_total_s=self.total_s,
                budget_s=guard.remaining() - EXPORT_RESERVE_S,
            )
        except Exception as exc:  # 規劃失敗時不錯開，交給 scan 自己判斷
            self.log.warning(
                "錯開完整掃描的規劃失敗（%s: %s），這一輪不錯開", type(exc).__name__, exc
            )
            return None

    def _sources(self, conn: sqlite3.Connection, guard: Guard, plan: SourcesPlan | None) -> None:
        if self._stopped(guard):
            return
        if plan is None:
            sources = tuple(s for s, c in sorted(self.settings.sources.items()) if c.enabled)
        else:
            self.log.info("掃描：%s", plan.describe())
            sources = plan.order
        if not sources:
            return
        self._step(
            "sources",
            cli.STEP_FUNCTIONS["sources"],
            conn,
            guard.deadline - EXPORT_RESERVE_S,
            sources,
        )

    def _backup(self, conn: sqlite3.Connection, day: dt.date) -> None:
        try:
            path, removed = make_backup(
                conn, self.data, day, self.settings.public.schedule.backup_keep
            )
        except Exception as exc:  # 備份失敗不影響其他工作，但結束碼是 1
            self.report.fail("backup", f"{type(exc).__name__}: {exc}")
            self.log.error("備份失敗：%s: %s", type(exc).__name__, exc)
            return
        self.log.info("備份：%s（刪掉舊備份 %d 份）", path.name, len(removed))

    def _publish(self, conn: sqlite3.Connection) -> None:
        from house_watch import publish

        if not publish.is_configured(self.settings):
            self.log.info("發布：settings.toml 沒有 [publish] 設定，略過")
            return
        publisher = self._publisher or publish.publish_site
        try:
            result = publisher(self.settings, self.data, clock=self.clock, conn=conn, log=self.log)
        except HouseWatchError as exc:
            self.report.fail("publish", str(exc), exc.exit_code)
            self.log.error("發布失敗：%s", exc)
            return
        code = getattr(result, "exit_code", ExitCode.OK)
        self.report.exit_code = max(self.report.exit_code, code)

    # ------------------------------------------------------------ 主流程

    def run(self) -> TickReport:
        start = self.clock.now()
        guard = Guard(self.clock, self.total_s)
        conn = db.open_or_init(self.data)
        try:
            self._run(conn, guard, start)
            finished = self.clock.now()
            summary = {
                "started": to_iso(start),
                "finished": to_iso(finished),
                "exit_code": int(self.report.exit_code),
                "stopped": self.report.stopped,
            }
            _put(conn, KV_LAST_RUN, json.dumps(summary, sort_keys=True))
        finally:
            conn.close()
        return self.report

    def _run(self, conn: sqlite3.Connection, guard: Guard, start: dt.datetime) -> None:
        jobs = due(conn, self.settings, self.data, start)
        self.log.info(
            "開始（台北日期 %s）；到期：%s", taipei_date(start), "、".join(jobs.names()) or "無"
        )
        if jobs.integrity:
            if not integrity_ok(conn):
                self.report.fail("integrity", "integrity_check 沒有通過")
                self.log.error("integrity_check 沒有通過：停止這一輪（不備份、不覆蓋舊備份）")
                return
            _put(conn, KV_INTEGRITY_OK, to_iso(self.clock.now()))
            self.log.info("integrity_check：ok")
        if jobs.backup:
            self._backup(conn, taipei_date(start))

        daily = [name for name in ("doorindex", "lvr") if getattr(jobs, name)]
        plan = self._plan(conn, guard)
        if plan is not None and plan.large is not None:
            self._sources(conn, guard, plan)  # 大的來源先跑，拿到整輪的時間
            self._daily(conn, guard, daily)
        else:
            self._daily(conn, guard, daily)
            if daily:
                plan = self._plan(conn, guard)  # 用剩下的時間重新挑
            self._sources(conn, guard, plan)

        # 事後審查 CX-6：先記到 deadline、之後才睡著時，第一次記下的原因不是 STOP_ALL，
        # 所以也要看「現在」的問題。
        if self._stopped(guard) and (
            self.report.stopped in STOP_ALL or guard.problem() in STOP_ALL
        ):
            self.log.warning("這一輪不匯出、不發布，留給下一輪")
            return
        result = self._step("export", self._export_step, conn, guard.deadline)
        if result.status == "failed":
            self.log.warning("匯出失敗：舊的網站檔沒有動")
        self._publish(conn)


def run_tick(
    *,
    clock: Clock | None = None,
    settings: Settings | None = None,
    log_directory: Path | None = None,
    console: bool | None = None,
    publisher: Callable[..., Any] | None = None,
    export_step: Callable[[cli.StepContext], cli.StepOutcome] | None = None,
) -> int:
    """`house-watch tick` 的入口（cli.ENTRY_POINTS["tick"]）。參數只給測試用。"""
    # TODO(deferred.md 2026-11-21 SA-3)：HTTP 讀取以外的卡住沒有看門狗
    ui = cli.Ui()
    clock = clock or SystemClock()
    with tick_logging(log_directory, mask=ui.mask, console=console) as log:
        if settings is None:
            config_file, _explicit = paths.settings_file()
            if not config_file.is_file():
                # 事後審查 SA-2、P-11：排程檔漏了 HOUSE_WATCH_CONFIG 時，不能用空設定在預設位置
                # 另建資料夾、另起一個爬蟲，網站也會默默停更。
                log.error(
                    "找不到本機設定檔 %s；排程不會用空設定另起一套資料。"
                    "請設 HOUSE_WATCH_CONFIG、在預設位置建立 settings.toml，"
                    "或用 house-watch schedule install 重寫排程檔。",
                    config_file,
                )
                return int(ExitCode.CONFIG)
        try:
            settings = settings if settings is not None else load_settings()
        except HouseWatchError as exc:
            log.error("設定錯誤：%s", exc)
            return int(exc.exit_code)
        ui.protect(settings)
        data = DataPaths(settings.data_dir)
        if data.is_empty():
            # 修完再審 F1-7：資料夾不存在或是空的（路徑設錯、資料夾被搬走、指到被清掉的暫存區）時，
            # 不自動建新資料庫、從頭抓、再把新資料發布出去。第一次用要先執行 house-watch init。
            log.error("資料夾不存在或是空的：%s；排程不會自動建立，請先執行 house-watch init", data.root)
            return int(ExitCode.CONFIG)
        lock = FileLock(data.lock)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            log.info("skipped：排程或另一個指令正在執行（執行鎖拿不到）")
            return int(ExitCode.OK)
        try:
            tick = Tick(settings, data, clock, log, publisher=publisher, export_step=export_step)
            report = tick.run()
        except HouseWatchError as exc:
            log.error("%s", exc)
            return int(exc.exit_code)
        except Exception:  # 沒預料到的錯誤：記下來，結束碼 1
            log.exception("沒預料到的錯誤")
            return int(ExitCode.PARTIAL)
        finally:
            lock.release()
        log.info("結束，結束碼 %d", int(report.exit_code))
        return int(report.exit_code)
