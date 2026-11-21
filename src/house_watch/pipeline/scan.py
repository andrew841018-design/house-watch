"""每日完整掃描與探測（r4 §3.4；R1-03、R1-10、R1-11、R3-08、R3-09；契約 C5、C6、C8）。

update 的 sources 步驟入口是 `run(ctx)`；`--accept-scan` 的入口是 `accept_scans(ctx)`。
ctx 是 cli.StepContext（settings、conn、data、clock、deadline、areas、sources、fetcher()）。

**單位**：掃描、有效性、下架計數、基準都以（來源, 區域, 範圍版本）為單位（R3-08）；
範圍版本＝scope_hash（公開查詢的區、城市、開價上限，加上轉接器的 scope_version）。
退避以站為單位，由抓取層處理。

**哪一種掃描**（每個（來源, 區域）每次執行至多一次）：
- 完整掃描：今天（台北日期）這個範圍還沒有有效的完整掃描。今天已經試過而失敗的，只有
  暫時性的錯誤（5xx、逾時、TLS）才再試，一天最多 FULL_ATTEMPTS_PER_DAY 次；
  無效（count_short 等）或預算不夠的，今天不再試，改做探測。
- 探測：今天已經有有效的完整掃描，或今天的完整掃描無效、預算不夠而不再試（見上一條），
  而且距上一次掃描 ≥ probe_every_hours 小時。
  用「最新」排序讀前 probe_pages 頁；只更新「最後看到」與新物件，不參與下架計數。
  排序語意還沒確認的站（probe_sort 是 None）不探測。上次有效完整掃描只有 ≤ K 頁的小站，
  每一輪都做完整掃描（r4 §3.4 例外）。
- 區域的順序：上一次有效完整掃描最舊的先做，預算不夠時每個區域輪流先拿到預算（R3-08）。

**有效的完整掃描**（全部符合才算，r4 §3.4）：
1. 每一頁都通過轉接器的檢查（HTTP 200、錨點在、不是挑戰頁）；否則 failed（page_invalid）。
2. 非最後一頁的卡片數至少是每頁筆數的 PAGE_FILL_MIN；否則 invalid（count_short）。
3. 有回報總數的站：去重後的筆數至少是總數的 min_total_ratio（設定，預設九成）；
   沒有總數的站讀到沒有下一頁為止。否則 invalid（count_short）。
4. 筆數 ≥ 1，而且不少於同一範圍上一次有效完整掃描的 PREVIOUS_RATIO_MIN；否則 invalid
   （count_short），擁有者可以用 `update --accept-scan <id>` 手動接受。
5. 卡片的解析成功率 ≥ PARSE_RATE_MIN_PERCENT（95%，r4 §3.4 第 5 條；skipped 的卡片才算失敗，
   也就是轉接器讀不出編號或價格的卡片；廣告、置頂與重複卡不算）；
   否則 invalid（parse_rate_low）。
6. 在本次的請求上限（max_requests_full）、每日上限與截止時間內做完；否則 failed
   （budget_exhausted、timeout）。開始前先用上次的頁數估，確定做不完就不開始（不浪費請求）。

被擋（403、429、挑戰頁、退避中、robots.txt 改了）：這個來源本輪停止，不繞過（R2-09）。
5xx、逾時：這個來源本輪停止；連續 3 輪才長時間退避（抓取層）。

**寫入**：先抓、解析，再開短交易寫（抓取層自己開交易）。每一頁讀完就寫，
所以中途失敗時已讀到的頁仍然算「看到」（R1-10：任何掃描看到都算）。
有效的完整掃描結束後才做下架判定。掃描與詳細頁做完後重新分群（dedupe）。
"""

import datetime as dt
import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from house_watch.clock import Clock, from_iso, taipei_date, to_iso
from house_watch.db import SCAN_ACCEPTED, SCAN_FAILED, SCAN_INVALID, SCAN_OK, transaction
from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.export import load_entry
from house_watch.http import (
    DeadlineReached,
    FetchFailed,
    ResponseRejected,
    prune_failed_responses,
    save_failed_response,
)
from house_watch.models import ListingFacts, canonical_json
from house_watch.pipeline import dedupe, delist, detail_queue, versions
from house_watch.settings import AreaConfig, ListingScope, SourceConfig
from house_watch.sources.base import Adapter, AreaQuery, Blocked, Mode, PageInvalid, Resp

# 來源 id → 轉接器類別（執行時才 import；建構子收公開抓取範圍）。
ADAPTERS: dict[str, str] = {
    "cthouse": "house_watch.sources.cthouse:CthouseAdapter",
    "hbhousing": "house_watch.sources.hbhousing:HbhousingAdapter",
    "rakuya": "house_watch.sources.rakuya:RakuyaAdapter",
    "etwarm": "house_watch.sources.etwarm:EtwarmAdapter",
    "century21": "house_watch.sources.century21:Century21Adapter",
}

PAGE_FILL_MIN = 0.9
PREVIOUS_RATIO_MIN = 0.5
PARSE_RATE_MIN_PERCENT = 95  # 用整數比，避開浮點邊界
FULL_ATTEMPTS_PER_DAY = 3
TRANSIENT_CODES = frozenset({ErrorCode.HTTP_5XX, ErrorCode.TIMEOUT, ErrorCode.TLS_ERROR})

Stop = Literal["source", "all"] | None
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


# ---------------------------------------------------------------- 範圍版本與掃描紀錄


def scope_hash(adapter: Adapter, q: AreaQuery) -> str:
    """範圍版本：公開查詢加上轉接器的 scope_version（不含標頭：東森的 CSRF 每次都不同）。"""
    payload = {
        "source": adapter.source,
        "area": q.area,
        "city": q.city,
        "districts": [[d.name, d.zip] for d in q.districts],
        "max_price_wan": q.max_price_wan,
        "scope_version": str(getattr(adapter, "scope_version", "1")),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ScanRow:
    scan_id: int
    mode: str
    status: str
    started_at: dt.datetime
    pages: int
    parsed: int
    error_code: str | None


def scan_rows(conn: sqlite3.Connection, source: str, area: str, scope: str) -> list[ScanRow]:
    """這個（來源, 區域, 範圍版本）的掃描，新的在前。"""
    rows = conn.execute(
        "SELECT scan_id, mode, status, started_at, pages, parsed, error_code FROM scans"
        " WHERE source = ? AND area = ? AND scope_hash = ? ORDER BY started_at DESC, scan_id DESC",
        (source, area, scope),
    ).fetchall()
    return [
        ScanRow(int(r[0]), r[1], r[2], from_iso(r[3]), int(r[4]), int(r[5]), r[6]) for r in rows
    ]


def close_stale(conn: sqlite3.Connection, now: dt.datetime) -> int:
    """上一次執行中斷留下的 running 掃描一律改成 failed（執行鎖保證沒有別的執行在跑）。"""
    with transaction(conn):
        return conn.execute(
            "UPDATE scans SET status = ?, finished_at = ? WHERE status = 'running'",
            (SCAN_FAILED, to_iso(now)),
        ).rowcount


def _start(conn: sqlite3.Connection, source: str, area: str, mode: str, scope: str, now) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO scans (source, area, mode, started_at, status, scope_hash)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (source, area, mode, to_iso(now), scope),
        )
    return int(cursor.lastrowid)


def _finish(conn: sqlite3.Connection, result: "ScanResult", now: dt.datetime) -> None:
    conn.execute(
        "UPDATE scans SET finished_at = ?, status = ?, pages = ?, parsed = ?, reported_total = ?,"
        " error_code = ? WHERE scan_id = ?",
        (
            to_iso(now),
            result.status,
            result.pages,
            result.parsed,
            result.reported_total,
            result.error_code.value if result.error_code else None,
            result.scan_id,
        ),
    )


# ---------------------------------------------------------------- 要做哪一種掃描


def decide_mode(
    rows: Sequence[ScanRow],
    *,
    now: dt.datetime,
    probe_sort: str | None,
    probe_pages: int,
    probe_every_hours: int,
) -> Mode | None:
    """依這個範圍的掃描紀錄（新的在前）決定這一輪做完整掃描、探測或不做（見模組說明）。"""
    today = taipei_date(now)
    fulls_today = [r for r in rows if r.mode == "full" and taipei_date(r.started_at) == today]
    valid_today = any(r.status in (SCAN_OK, SCAN_ACCEPTED) for r in fulls_today)
    if not valid_today:
        failures = [r for r in fulls_today if r.status not in (SCAN_OK, SCAN_ACCEPTED)]
        retryable = all(_code(r.error_code) in TRANSIENT_CODES for r in failures)
        if not failures or (retryable and len(failures) < FULL_ATTEMPTS_PER_DAY):
            return "full"
    ran = [r for r in rows if r.status != SCAN_FAILED or r.pages > 0]
    if ran and now - ran[0].started_at < dt.timedelta(hours=probe_every_hours):
        return None
    last_valid = next(
        (r for r in rows if r.mode == "full" and r.status in (SCAN_OK, SCAN_ACCEPTED)), None
    )
    if last_valid is not None and valid_today and last_valid.pages <= probe_pages:
        return "full"  # 小站：完整掃描和探測一樣便宜
    return "probe" if probe_sort is not None else None


def _code(value: str | None) -> ErrorCode | None:
    try:
        return ErrorCode(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 一次掃描


@dataclass
class ScanResult:
    scan_id: int
    source: str
    area: str
    mode: str
    status: str = SCAN_FAILED
    error_code: ErrorCode | None = None
    pages: int = 0
    parsed: int = 0
    reported_total: int | None = None
    new: int = 0
    gone: int = 0
    stop: Stop = None

    def describe(self) -> str:
        text = f"{self.source}/{self.area} {self.mode} #{self.scan_id} {self.status}"
        if self.error_code is not None:
            text += f"（{self.error_code.value}）"
        return f"{text}：{self.pages} 頁、{self.parsed} 筆、新 {self.new}、下架 {self.gone}"


@dataclass
class _Run:
    """一次 run() 共用的東西。"""

    conn: sqlite3.Connection
    clock: Clock
    deadline: float
    wall_limit: dt.datetime
    failed_dir: Any
    prepared: set[str] = field(default_factory=set)

    def in_time(self) -> bool:
        return self.clock.monotonic() < self.deadline and self.clock.now() < self.wall_limit


def _has_floors(conn: sqlite3.Connection, facts: ListingFacts) -> bool:
    current = versions.latest(conn, facts.source, facts.source_id)
    return current is not None and current.facts.floor_lo is not None


def _fill_batch(
    conn: sqlite3.Connection, fetch: Callable, adapter: Adapter, items: list[ListingFacts]
) -> list[ListingFacts]:
    """批次端點（BatchAdapter，例如東森）補樓層：只補列表沒有、上一版也沒有的。"""
    batch_request = getattr(adapter, "batch_request", None)
    parse_batch = getattr(adapter, "parse_batch", None)
    size = int(getattr(adapter, "batch_size", 0) or 0)
    if batch_request is None or parse_batch is None or size <= 0:
        return items
    need = [f for f in items if f.floor_lo is None and not _has_floors(conn, f)]
    filled: dict[str, ListingFacts] = {}
    for start in range(0, len(need), size):
        chunk = need[start : start + size]
        resp = fetch(batch_request([f.source_id for f in chunk]))
        try:
            filled.update(parse_batch(resp, {f.source_id: f for f in chunk}))
        except PageInvalid:
            continue  # 批次失敗只是少了樓層，列表照樣算數
    return [filled.get(f.source_id, f) for f in items]


def _validity(
    result: ScanResult,
    *,
    cards: int,
    skipped: int,
    short_page: bool,
    has_total: bool,
    min_total_ratio: float,
    previous: ScanRow | None,
) -> tuple[str, ErrorCode | None]:
    """讀完的完整掃描算不算有效（r4 §3.4 第 2–5 條）。"""
    if cards == 0 or result.parsed == 0:
        return SCAN_INVALID, ErrorCode.COUNT_SHORT
    if 100 * (cards - skipped) < PARSE_RATE_MIN_PERCENT * cards:
        return SCAN_INVALID, ErrorCode.PARSE_RATE_LOW
    if short_page:
        return SCAN_INVALID, ErrorCode.COUNT_SHORT
    if has_total and result.reported_total is not None:
        if result.parsed < min_total_ratio * result.reported_total:
            return SCAN_INVALID, ErrorCode.COUNT_SHORT
    if previous is not None and result.parsed < PREVIOUS_RATIO_MIN * previous.parsed:
        return SCAN_INVALID, ErrorCode.COUNT_SHORT
    return SCAN_OK, None


def run_scan(
    run: _Run,
    fetcher: Any,
    adapter: Adapter,
    config: SourceConfig,
    q: AreaQuery,
    mode: Mode,
    scope: str,
) -> ScanResult:
    """做一次掃描並寫進資料庫（見模組說明）。"""
    conn, clock = run.conn, run.clock
    source = adapter.source
    history = scan_rows(conn, source, q.area, scope)
    previous = next(
        (r for r in history if r.mode == "full" and r.status in (SCAN_OK, SCAN_ACCEPTED)), None
    )
    baseline = previous is None
    scan_id = _start(conn, source, q.area, mode, scope, clock.now())
    result = ScanResult(scan_id, source, q.area, mode)
    fetch = fetcher.bind(source, run.deadline)
    sent_before = fetcher.requests_sent.get(source, 0)
    seen: set[str] = set()
    cards = skipped = 0
    short_page = False
    complete = False
    try:
        if source not in run.prepared:
            adapter.prepare(fetch)
            run.prepared.add(source)
        page = 1
        while True:
            if not run.in_time():
                result.error_code, result.stop = ErrorCode.TIMEOUT, "all"
                break
            used = fetcher.requests_sent.get(source, 0) - sent_before
            if mode == "full" and (
                used >= config.max_requests_full or page > adapter.caps.max_pages
            ):
                result.error_code = ErrorCode.BUDGET_EXHAUSTED
                break
            resp: Resp | None = None
            try:
                resp = fetch(adapter.list_request(q, page, mode))
                listed = adapter.parse_list(resp, q)
            except PageInvalid as exc:
                if resp is not None and run.failed_dir is not None:
                    save_failed_response(resp, run.failed_dir, clock.now())
                result.error_code = exc.code
                break
            result.pages += 1
            items = [f for f in listed.items if f.source_id not in seen]
            items = _fill_batch(conn, fetch, adapter, items)
            now = clock.now()
            with transaction(conn):
                for facts in items:
                    sighting = versions.record_sighting(
                        conn,
                        facts,
                        now=now,
                        scan_id=scan_id,
                        full=mode == "full",
                        baseline=baseline,
                        parser_version=adapter.parser_version,
                        detail_fields=adapter.caps.detail_fields,
                    )
                    result.new += int(sighting.new)
            seen.update(f.source_id for f in items)
            cards += len(listed.items) + listed.skipped
            skipped += listed.skipped
            if result.reported_total is None:
                result.reported_total = listed.reported_total
            total = result.reported_total
            last = not listed.has_next or (total is not None and len(seen) >= total)
            if (
                not last
                and len(listed.items) + listed.skipped < PAGE_FILL_MIN * adapter.caps.page_size
            ):
                short_page = True
            if last:
                complete = True
                break
            if mode == "probe" and page >= config.probe_pages:
                complete = True
                break
            page += 1
    except Blocked as exc:
        result.error_code, result.stop = exc.code, "source"
    except DeadlineReached:
        result.error_code, result.stop = ErrorCode.TIMEOUT, "all"
    except ResponseRejected:
        result.error_code = ErrorCode.PAGE_INVALID
    except FetchFailed as exc:
        result.error_code, result.stop = exc.code, "source"
    except PageInvalid as exc:  # prepare() 或批次以外的頁面錯誤
        result.error_code = exc.code
    except Exception as exc:
        # 轉接器遇到沒預料到的資料（KeyError、ValidationError…）：這個來源本輪停止、記成失敗，
        # 其他來源與分群照做（事後審查 P-2）。設定類的錯誤（ConfigError 等）照樣往外丟。
        if isinstance(exc, HouseWatchError):
            raise
        result.error_code, result.stop = ErrorCode.PAGE_INVALID, "source"
    result.parsed = len(seen)
    if complete and mode == "full":
        result.status, result.error_code = _validity(
            result,
            cards=cards,
            skipped=skipped,
            short_page=short_page,
            has_total=adapter.caps.has_total,
            min_total_ratio=config.min_total_ratio,
            previous=previous,
        )
    elif complete:
        result.status = SCAN_OK
    else:
        result.status = SCAN_FAILED
    with transaction(conn):
        _finish(conn, result, clock.now())
        if result.status == SCAN_OK and mode == "full":
            result.gone = len(delist.mark_gone(conn, source, q.area, scope, clock.now()))
    return result


def _skip_for_budget(run: _Run, source: str, area: str, scope: str) -> ScanResult:
    """估計做不完的完整掃描：記一筆 failed（budget_exhausted），不送任何請求。"""
    scan_id = _start(run.conn, source, area, "full", scope, run.clock.now())
    result = ScanResult(scan_id, source, area, "full", error_code=ErrorCode.BUDGET_EXHAUSTED)
    with transaction(run.conn):
        _finish(run.conn, result, run.clock.now())
    return result


# ---------------------------------------------------------------- 一次執行（update 的 sources 步驟）


@dataclass
class RunReport:
    """cli 的 StepOutcome 相容物件：ok=False 代表部分失敗。"""

    ok: bool = True
    scans: list[ScanResult] = field(default_factory=list)
    details: dict[str, detail_queue.DetailReport] = field(default_factory=dict)
    groups: dedupe.AssignReport | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [scan.describe() for scan in self.scans]
        for source, report in sorted(self.details.items()):
            if report.fetched or report.error:
                text = f"{source} 詳細頁 {report.fetched} 個（更新 {report.updated}）"
                if report.error:
                    text += f"（{report.error.value}）"
                parts.append(text)
        if self.groups is not None:
            g = self.groups
            parts.append(f"分群：加入 {g.joined}、新群組 {g.created}、拆出 {g.split}")
        parts.extend(self.notes)
        return "；".join(parts) if parts else "沒有要掃描的來源或區域"


def build_adapters(source_ids: Sequence[str], scope: ListingScope) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {}
    for source in source_ids:
        spec = ADAPTERS.get(source)
        if spec is None:
            continue
        adapters[source] = load_entry(spec)(scope)
    return adapters


def _paused(fetcher: Any, source: str, now: dt.datetime) -> bool:
    store = getattr(fetcher, "store", None)
    if store is None:
        return False
    state = store.state(source)
    return state.paused_until is not None and state.paused_until > now


def _remaining_today(fetcher: Any, source: str, config: SourceConfig, now: dt.datetime) -> int:
    store = getattr(fetcher, "store", None)
    used = store.requests_on(source, taipei_date(now)) if store is not None else 0
    return config.max_requests_day - used


def run(ctx: Any) -> RunReport:
    """update 的 sources 步驟（cli.ENTRY_POINTS["sources"]）。"""
    settings = ctx.settings
    conn, clock = ctx.conn, ctx.clock
    report = RunReport()
    close_stale(conn, clock.now())
    scope = settings.public.scope.listing
    configs = settings.sources
    wanted = [s for s in ctx.sources if s in configs and configs[s].enabled]
    adapters = build_adapters(wanted, scope)
    missing = sorted(set(wanted) - set(adapters))
    if missing:
        report.notes.append(f"沒有轉接器：{', '.join(missing)}")
    areas: Sequence[AreaConfig] = ctx.areas
    if not adapters or not areas:
        return report
    schedule = settings.public.schedule
    run_state = _Run(
        conn=conn,
        clock=clock,
        deadline=ctx.deadline,
        wall_limit=clock.now() + dt.timedelta(minutes=schedule.run_deadline_minutes),
        failed_dir=getattr(ctx.data, "failed_dir", None),
    )
    fetcher = ctx.fetcher(list(adapters.values()))
    stop_all = False
    unfinished: list[str] = []
    for source, adapter in adapters.items():
        if stop_all or not run_state.in_time():
            unfinished.append(source)
            continue
        config = configs[source]
        if _paused(fetcher, source, clock.now()):
            report.notes.append(f"{source} 暫停中")
            continue
        stopped = False
        plans = []
        for position, area in enumerate(areas):
            q = AreaQuery.from_config(area, scope)
            sh = scope_hash(adapter, q)
            rows = scan_rows(conn, source, area.id, sh)
            last_valid = next(
                (r for r in rows if r.mode == "full" and r.status in (SCAN_OK, SCAN_ACCEPTED)),
                None,
            )
            oldest_first = last_valid.started_at if last_valid else _EPOCH
            plans.append((oldest_first, position, q, sh, rows, last_valid))
        plans.sort(key=lambda item: (item[0], item[1]))
        for _, _, q, sh, rows, last_valid in plans:
            if not run_state.in_time():
                stop_all = True
                unfinished.append(f"{source}/{q.area}")
                break
            mode = decide_mode(
                rows,
                now=clock.now(),
                probe_sort=adapter.caps.probe_sort,
                probe_pages=config.probe_pages,
                probe_every_hours=schedule.probe_every_hours,
            )
            if mode is None:
                continue
            if mode == "full":
                budget = min(
                    config.max_requests_full, _remaining_today(fetcher, source, config, clock.now())
                )
                estimate = last_valid.pages if last_valid is not None else None
                if budget <= 0 or (estimate is not None and estimate > budget):
                    report.scans.append(_skip_for_budget(run_state, source, q.area, sh))
                    continue
            result = run_scan(run_state, fetcher, adapter, config, q, mode, sh)
            report.scans.append(result)
            if result.stop == "all":
                stop_all = stopped = True
                break
            if result.stop == "source":
                stopped = True
                break
        if not stopped and not stop_all and run_state.in_time():
            report.details[source] = detail_queue.run(
                conn,
                fetcher.bind(source, ctx.deadline),
                adapter,
                scope=scope,
                now=clock.now,
                limit=config.detail_cap,
                failed_dir=run_state.failed_dir,
                in_time=run_state.in_time,
            )
            if report.details[source].error is not None:
                stop_all = stop_all or report.details[source].error == ErrorCode.TIMEOUT
    if unfinished:
        report.notes.append(f"時間不夠，這一輪沒有做完：{', '.join(unfinished)}")
    with transaction(conn):
        report.groups = dedupe.assign_groups(conn, zip_names=dedupe.zip_names_of(settings))
    if run_state.failed_dir is not None:
        prune_failed_responses(run_state.failed_dir, clock.now())
    report.ok = (
        not unfinished
        and all(scan.status in (SCAN_OK, SCAN_ACCEPTED) for scan in report.scans)
        and all(d.error is None for d in report.details.values())
    )
    return report


# ---------------------------------------------------------------- 手動接受（update --accept-scan）


@dataclass
class AcceptReport:
    accepted: list[int] = field(default_factory=list)
    refused: list[int] = field(default_factory=list)
    gone: int = 0

    @property
    def ok(self) -> bool:
        return not self.refused

    @property
    def summary(self) -> str:
        parts = []
        if self.accepted:
            parts.append(f"接受 {', '.join(f'#{i}' for i in self.accepted)}，下架 {self.gone} 筆")
        if self.refused:
            parts.append(
                f"不能接受 {', '.join(f'#{i}' for i in self.refused)}（只限 count_short 的無效完整掃描）"
            )
        return "；".join(parts)


def accept_scans(ctx: Any) -> AcceptReport:
    """把 count_short 的無效完整掃描改成 accepted，接著做下架判定（r4 §3.4 第 4 條）。"""
    conn = ctx.conn
    report = AcceptReport()
    for scan_id in ctx.accept_scans:
        row = conn.execute(
            "SELECT source, area, mode, status, error_code, scope_hash FROM scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if (
            row is None
            or row[2] != "full"
            or row[3] != SCAN_INVALID
            or row[4] != ErrorCode.COUNT_SHORT.value
        ):
            report.refused.append(scan_id)
            continue
        with transaction(conn):
            conn.execute("UPDATE scans SET status = ? WHERE scan_id = ?", (SCAN_ACCEPTED, scan_id))
            report.gone += len(delist.mark_gone(conn, row[0], row[1], row[5], ctx.clock.now()))
        report.accepted.append(scan_id)
    return report
