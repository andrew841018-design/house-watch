"""指令列入口（WP7a；契約 C10）。

    house-watch init            建立資料夾與資料庫（資料夾完全是空的才建）
    house-watch update [--only 步驟] [--source 來源] [--area 區域] [--accept-scan 編號]
                                依序跑 doorindex → lvr → sources，最後一定匯出網站
    house-watch export          重新產生網站檔（<資料夾>/export/site）
    house-watch serve [--port]  只在 127.0.0.1 提供網站檔
    house-watch doctor          檢查版本、路徑、資料庫、來源狀態、最近的執行、磁碟（遮掉本機值）
    house-watch tick            排程入口（WP7b，tick.py）：拿鎖、到期的工作、匯出、發布
    house-watch publish [--dry-run]
                                推到獨立的網站 repo（WP7c，publish.py）
    house-watch schedule install [--every-hours N] | schedule remove
                                寫／刪 launchd 的 plist，只寫檔、不載入（WP7b，ops/launchd.py）
    house-watch deploy-local <git-ref> [--repo 資料夾]
                                把 git ref 裝成排程用的固定版本（WP7b，ops/launchd.py）

結束碼（C10）：0 成功或略過；1 部分失敗；2 設定或資料夾錯誤。

- 路徑一律從 settings／platformdirs 算出絕對路徑，不用目前的工作目錄（R2-07）。
- 會寫入的指令（init、update、export）先拿執行鎖（和 tick 同一把，DataPaths.lock）；
  拿不到就提示「排程正在跑」，結束碼 1。
- 資料夾完全是空的才自動初始化（r4 §2.1 (h)、db.open_or_init）；有東西卻沒有 instance
  標記、或有標記卻沒有資料庫，一律拒絕（結束碼 2）。
- update 的每一步各自 try/except：一步失敗不影響其他步驟，最後結束碼 1。
- 其他工作包的函式在執行時才 import（ENTRY_POINTS）；還沒完成時該步驟回報
  not yet available（結束碼 1），其他步驟照跑。
- 印出的文字一律經過遮罩：家目錄換成 ~，聯絡方式與發布設定只顯示「已設定／未設定」（R2-17）。
"""

import argparse
import contextlib
import datetime as dt
import functools
import importlib.metadata
import platform
import shutil
import socketserver
import sqlite3
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from filelock import FileLock, Timeout

from house_watch import __version__, db, export
from house_watch.clock import TAIPEI, Clock, SystemClock, to_iso
from house_watch.errors import ConfigError, ExitCode, HouseWatchError
from house_watch.export import (
    KV_DOORINDEX_LAST_CHECK,
    KV_DOORINDEX_VERSION,
    KV_LVR_AS_OF,
    KV_LVR_LAST_CHECK,
    OK_SCAN_STATUSES,
    NotAvailable,
    Providers,
    export_site,
    latest_full_failures,
    latest_scans,
    load_entry,
    load_manifest,
    manifest_path,
    public_code,
    read_kv,
    scan_times,
    site_dir,
    source_states,
)
from house_watch.paths import DataPaths, settings_file
from house_watch.settings import AreaConfig, Settings, load_settings

# 其他工作包的入口，執行時才 import。除了 C6、C7 寫明的兩個，其餘是提給 WP0 的契約提案。
ENTRY_POINTS: dict[str, str] = {
    "fetcher": "house_watch.http:Fetcher",  # C6：Fetcher.from_settings(conn, settings, adapters)
    "doorindex": "house_watch.geo.doorindex:refresh",  # C7：refresh(fetcher, data_dir, now)，見下
    "lvr": "house_watch.lvr.importer:run",  # 提案：run(ctx: StepContext)
    "sources": "house_watch.pipeline.scan:run",  # 提案：run(ctx: StepContext)
    "accept": "house_watch.pipeline.scan:accept_scans",  # 提案：accept_scans(ctx)
    "tick": "house_watch.tick:run_tick",  # 提案（WP7b）：run_tick() -> int
    "publish": "house_watch.publish:run_publish",  # 提案（WP7c）：run_publish(dry_run=) -> int
    "schedule": "house_watch.ops.launchd:run_schedule",  # WP7b：run_schedule(action=, every_hours=)
    "deploy_local": "house_watch.ops.launchd:run_deploy_local",  # WP7b：run_deploy_local(ref=, repo=)
}

UPDATE_STEPS = ("doorindex", "lvr", "sources")
SERVE_HOST = "127.0.0.1"
DEFAULT_PORT = 18780
LOW_DISK_BYTES = 1 << 30
RECENT_SCANS = 10


# ---------------------------------------------------------------- 輸出與遮罩


def _home_variants() -> list[str]:
    home = Path.home()
    variants = {str(home)}
    with contextlib.suppress(OSError):
        variants.add(str(home.resolve()))
    return sorted((v for v in variants if len(v) > 1), key=len, reverse=True)


class Ui:
    """所有輸出都經過這裡：先遮掉本機的敏感值，再把家目錄換成 ~。"""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def protect(self, settings: Settings) -> None:
        local = settings.local
        publish = local.publish
        for value in (
            local.http.contact,
            publish.remote,
            publish.deploy_key,
            publish.known_hosts,
            publish.site_repo_dir,
        ):
            if value is None:
                continue
            forms = {str(value)}
            if isinstance(value, Path):
                forms.add(str(value.expanduser()))
            self._secrets.update(form for form in forms if len(form) >= 3)

    def _hide_secrets(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text

    def mask(self, text: str) -> str:
        text = self._hide_secrets(text)
        for home in _home_variants():
            text = text.replace(home, "~")
        return self._hide_secrets(text)  # 設定裡寫成 ~/… 的值，換成 ~ 之後才對得上

    def say(self, text: str = "") -> None:
        print(self.mask(text), file=sys.stdout)

    def error(self, text: str) -> None:
        print(self.mask(text), file=sys.stderr)


def _utf8_streams() -> None:
    """排程把輸出導到檔案時，Windows 的系統編碼可能印不出中文（R3-19）。"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 共用


class LockBusy(HouseWatchError):
    """排程或另一個指令正在執行。"""

    exit_code = ExitCode.PARTIAL


@contextlib.contextmanager
def run_lock(data: DataPaths) -> Iterator[None]:
    """作業系統層級的執行鎖，和 tick 共用；不等待（r4 §3.5）。"""
    data.root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(data.lock)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise LockBusy("排程或另一個指令正在執行，這次略過；稍後再試") from None
    try:
        yield
    finally:
        lock.release()


def _load(ui: Ui) -> Settings:
    settings = load_settings()
    ui.protect(settings)
    return settings


def _split(values: list[str] | tuple[str, ...]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def put_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- update 的步驟


@dataclass(frozen=True)
class UpdatePlan:
    steps: tuple[str, ...]
    areas: tuple[str, ...]
    sources: tuple[str, ...]
    accept_scans: tuple[int, ...] = ()


def plan_update(
    settings: Settings,
    *,
    only: list[str] | tuple[str, ...] = (),
    sources: list[str] | tuple[str, ...] = (),
    areas: list[str] | tuple[str, ...] = (),
    accept: list[str] | tuple[str, ...] = (),
) -> UpdatePlan:
    """檢查 update 的參數。--only 可以寫 export，代表不跑抓取步驟、只匯出。"""
    chosen = _split(only)
    unknown = sorted(set(chosen) - {*UPDATE_STEPS, "export"})
    if unknown:
        raise ConfigError(f"--only 只能是 doorindex、lvr、sources、export：{', '.join(unknown)}")
    steps = tuple(step for step in UPDATE_STEPS if not chosen or step in chosen)

    enabled_areas = [area.id for area in settings.enabled_areas]
    wanted_areas = _split(areas)
    bad_areas = [a for a in wanted_areas if a not in enabled_areas]
    if bad_areas:
        raise ConfigError(
            f"--area 不是已啟用的區域：{', '.join(bad_areas)}（先加進 settings.toml 的 enabled_areas）"
        )
    area_ids = tuple(a for a in enabled_areas if not wanted_areas or a in wanted_areas)

    merged = settings.sources
    wanted_sources = _split(sources)
    unknown_sources = [s for s in wanted_sources if s not in merged]
    if unknown_sources:
        raise ConfigError(f"--source 沒有這個來源：{', '.join(unknown_sources)}")
    disabled = [s for s in wanted_sources if not merged[s].enabled]
    if disabled:
        raise ConfigError(f"--source 這些來源沒有啟用：{', '.join(disabled)}")
    source_ids = tuple(
        s
        for s, config in sorted(merged.items())
        if config.enabled and (not wanted_sources or s in wanted_sources)
    )

    scan_ids: list[int] = []
    for text in _split(accept):
        if not text.isdigit() or int(text) < 1:
            raise ConfigError(f"--accept-scan 要是正整數的掃描編號：{text}")
        scan_ids.append(int(text))
    return UpdatePlan(steps, area_ids, source_ids, tuple(dict.fromkeys(scan_ids)))


@dataclass
class StepContext:
    """傳給 update 各步驟的資訊（提給其他工作包的介面）。

    - deadline 是 clock.monotonic() 的截止秒數；每次請求之間都要檢查。
    - areas、sources 是這次要處理的區域與來源（--area、--source 篩過）。
    - 步驟要寫資料庫時用 db.transaction(conn)；不能留下沒結束的交易。
    - 要抓網頁就用 fetcher(轉接器)；這次執行結束時統一關閉。
    """

    settings: Settings
    conn: sqlite3.Connection
    data: DataPaths
    clock: Clock
    deadline: float
    areas: tuple[AreaConfig, ...]
    sources: tuple[str, ...]
    accept_scans: tuple[int, ...] = ()
    _fetchers: list[Any] = field(default_factory=list, repr=False)

    def fetcher(self, adapters: Iterable[Any] = ()) -> Any:
        """C6 的 Fetcher：`Fetcher.from_settings(conn, settings, adapters, clock=…)`。"""
        fetcher_class = load_entry(ENTRY_POINTS["fetcher"])
        fetcher = fetcher_class.from_settings(
            self.conn, self.settings, tuple(adapters), clock=self.clock
        )
        self._fetchers.append(fetcher)
        return fetcher

    def close(self) -> None:
        while self._fetchers:
            with contextlib.suppress(Exception):
                self._fetchers.pop().close()


@dataclass(frozen=True)
class StepOutcome:
    ok: bool = True
    summary: str = ""


def _outcome(result: Any) -> StepOutcome:
    """步驟函式的回傳值：None 代表成功；或是帶 ok（False＝部分失敗）、summary 的物件。"""
    if result is None or isinstance(result, StepOutcome):
        return result or StepOutcome()
    ok = getattr(result, "ok", True)
    summary = getattr(result, "summary", "")
    return StepOutcome(ok=ok is not False, summary=summary if isinstance(summary, str) else "")


def step_doorindex(ctx: StepContext) -> StepOutcome:
    """C7 的 refresh。

    - WP1 的 fetcher 參數收 httpx.Client（自己包成有限速、白名單的抓取器），不是 C6 的
      Fetcher；trust_env=False：不讀 .netrc 帳密與代理的環境變數。
    - 另外傳 GeoContext（C7 的 areas.load），門牌點的範圍指標在建索引時一起算（r4 §3.9）。
    """
    refresh = load_entry(ENTRY_POINTS["doorindex"])
    geo = load_entry(export.GEO_LOAD)(config=ctx.settings.public)
    with httpx.Client(trust_env=False) as client:  # 憑證驗證預設開著
        info = refresh(client, ctx.data.root, ctx.clock.now(), ctx=geo)
    version = getattr(info, "version", None)
    version = version.strip() if isinstance(version, str) and version.strip() else None
    outcome = _outcome(info if hasattr(info, "ok") else None)
    with db.transaction(ctx.conn):
        if outcome.ok:  # 事後審查 AT-1：只記成功的時間，網頁才看得出過期
            put_kv(ctx.conn, KV_DOORINDEX_LAST_CHECK, to_iso(ctx.clock.now()))
        if version is not None:
            put_kv(ctx.conn, KV_DOORINDEX_VERSION, version)
    return StepOutcome(outcome.ok, outcome.summary or (f"版本 {version}" if version else ""))


def step_lvr(ctx: StepContext) -> StepOutcome:
    run = load_entry(ENTRY_POINTS["lvr"])
    result = run(ctx)
    as_of = getattr(result, "as_of", None)
    if isinstance(as_of, dt.datetime):
        as_of = as_of.astimezone(TAIPEI).date()
    outcome = _outcome(result)
    with db.transaction(ctx.conn):
        if outcome.ok:  # 事後審查 AT-1：只記成功的時間，網頁才看得出過期
            put_kv(ctx.conn, KV_LVR_LAST_CHECK, to_iso(ctx.clock.now()))
        if isinstance(as_of, dt.date):
            put_kv(ctx.conn, KV_LVR_AS_OF, as_of.isoformat())
    return outcome


def step_sources(ctx: StepContext) -> StepOutcome:
    run = load_entry(ENTRY_POINTS["sources"])
    return _outcome(run(ctx))


def step_accept(ctx: StepContext) -> StepOutcome:
    accept = load_entry(ENTRY_POINTS["accept"])
    return _outcome(accept(ctx))


def step_export(
    ctx: StepContext, *, providers: Providers | None = None, web_root: Path | None = None
) -> StepOutcome:
    result = export_site(
        ctx.conn,
        ctx.settings,
        data=ctx.data,
        clock=ctx.clock,
        providers=providers,
        web_root=web_root,
    )
    manifest = result.manifest
    summary = f"build {manifest.build_id}，{manifest.status.value}，{len(manifest.files)} 個檔"
    if result.problems:
        return StepOutcome(False, f"{summary}；" + "；".join(result.problems))
    return StepOutcome(True, summary)


STEP_FUNCTIONS: dict[str, Callable[[StepContext], StepOutcome]] = {
    "doorindex": step_doorindex,
    "lvr": step_lvr,
    "sources": step_sources,
}

StepStatus = Literal["ok", "partial", "not_available", "failed"]
_STATUS_TEXT = {
    "ok": "完成",
    "partial": "部分完成",
    "not_available": "沒有執行",
    "failed": "失敗",
}


@dataclass(frozen=True)
class StepResult:
    name: str
    status: StepStatus
    exit_code: ExitCode
    message: str = ""

    def describe(self) -> str:
        text = f"{self.name}：{_STATUS_TEXT[self.status]}"
        return f"{text}（{self.message}）" if self.message else text


def run_step(name: str, fn: Callable[[StepContext], StepOutcome], ctx: StepContext) -> StepResult:
    """跑一個步驟。任何錯誤都只算這一步失敗；留下沒結束的交易就回滾。"""
    try:
        outcome = fn(ctx)
    except NotAvailable as exc:
        result = StepResult(name, "not_available", ExitCode.PARTIAL, str(exc))
    except HouseWatchError as exc:
        result = StepResult(name, "failed", exc.exit_code, str(exc))
    except Exception as exc:  # 每一步互相隔離（r4 §0 第 3 點）
        result = StepResult(name, "failed", ExitCode.PARTIAL, f"{type(exc).__name__}: {exc}")
    else:
        status: StepStatus = "ok" if outcome.ok else "partial"
        code = ExitCode.OK if outcome.ok else ExitCode.PARTIAL
        result = StepResult(name, status, code, outcome.summary)
    if ctx.conn.in_transaction:
        ctx.conn.execute("ROLLBACK")
    return result


@dataclass(frozen=True)
class UpdateReport:
    steps: tuple[StepResult, ...]

    @property
    def exit_code(self) -> ExitCode:
        return max((step.exit_code for step in self.steps), default=ExitCode.OK)


def run_update(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    data: DataPaths,
    clock: Clock,
    plan: UpdatePlan,
    providers: Providers | None = None,
    web_root: Path | None = None,
) -> UpdateReport:
    """依序跑：手動接受掃描 → 選到的抓取步驟 → 匯出。呼叫前要持有執行鎖（tick 也可以用）。"""
    by_id = {area.id: area for area in settings.enabled_areas}
    deadline = clock.monotonic() + settings.public.schedule.run_deadline_minutes * 60
    ctx = StepContext(
        settings=settings,
        conn=conn,
        data=data,
        clock=clock,
        deadline=deadline,
        areas=tuple(by_id[area_id] for area_id in plan.areas),
        sources=plan.sources,
        accept_scans=plan.accept_scans,
    )
    results = []
    try:
        if plan.accept_scans:
            results.append(run_step("accept", step_accept, ctx))
        for name in plan.steps:
            results.append(run_step(name, STEP_FUNCTIONS[name], ctx))
    finally:
        ctx.close()
    export_step = functools.partial(step_export, providers=providers, web_root=web_root)
    results.append(run_step("export", export_step, ctx))
    return UpdateReport(tuple(results))


# ---------------------------------------------------------------- 指令：init、update、export


def cmd_init(args: argparse.Namespace, ui: Ui) -> int:
    settings = _load(ui)
    data = DataPaths(settings.data_dir)
    with run_lock(data):
        fresh = data.is_empty()
        db.open_or_init(data).close()
    ui.say(f"{'已建立' if fresh else '已經初始化過'}：{data.root}")
    return int(ExitCode.OK)


def cmd_update(args: argparse.Namespace, ui: Ui) -> int:
    settings = _load(ui)
    plan = plan_update(
        settings, only=args.only, sources=args.source, areas=args.area, accept=args.accept_scan
    )
    data = DataPaths(settings.data_dir)
    with run_lock(data):
        conn = db.open_or_init(data)
        try:
            report = run_update(settings, conn, data=data, clock=SystemClock(), plan=plan)
        finally:
            conn.close()
    for step in report.steps:
        ui.say(step.describe())
    if report.exit_code != ExitCode.OK:
        ui.error(f"有步驟沒有完成（結束碼 {int(report.exit_code)}）")
    return int(report.exit_code)


def cmd_export(args: argparse.Namespace, ui: Ui) -> int:
    settings = _load(ui)
    data = DataPaths(settings.data_dir)
    with run_lock(data):
        conn = db.open_or_init(data)
        try:
            result = export_site(conn, settings, data=data, clock=SystemClock())
        finally:
            conn.close()
    manifest = result.manifest
    ui.say(f"網站：{result.site_dir}")
    ui.say(f"build {manifest.build_id}，{manifest.status.value}，{len(manifest.files)} 個檔")
    for problem in result.problems:
        ui.error(f"未完成：{problem}")
    return int(ExitCode.OK if result.complete else ExitCode.PARTIAL)


# ---------------------------------------------------------------- 指令：serve


class SiteHandler(SimpleHTTPRequestHandler):
    """只提供網站資料夾裡的檔；不列目錄、不快取。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".json": "application/json",
        ".geojson": "application/geo+json",
    }

    def list_directory(self, path: str) -> None:  # type: ignore[override]
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class LocalServer(ThreadingHTTPServer):
    """只綁 127.0.0.1；不做反查主機名稱。"""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def make_server(root: Path, port: int = DEFAULT_PORT) -> LocalServer:
    """網站伺服器：主機固定是 127.0.0.1（R2-17），只提供 root 底下的檔。"""
    handler = functools.partial(SiteHandler, directory=str(root))
    return LocalServer((SERVE_HOST, port), handler)


def cmd_serve(args: argparse.Namespace, ui: Ui) -> int:
    settings = _load(ui)
    root = site_dir(DataPaths(settings.data_dir))
    if not (root / "data" / "meta.json").is_file():
        ui.error("還沒有網站檔：先執行 house-watch update 或 house-watch export")
        return int(ExitCode.CONFIG)
    try:
        server = make_server(root, args.port)
    except OSError as exc:
        ui.error(f"無法使用 port {args.port}（{type(exc).__name__}）；改用 --port 指定別的")
        return int(ExitCode.CONFIG)
    ui.say(f"網站：http://{SERVE_HOST}:{server.server_port}/（只有這台電腦連得到；Ctrl-C 結束）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return int(ExitCode.OK)


# ---------------------------------------------------------------- 指令：doctor


@dataclass
class _Doctor:
    ui: Ui
    lines: list[str] = field(default_factory=list)
    exit_code: ExitCode = ExitCode.OK

    def section(self, title: str) -> None:
        self.lines.append(f"\n{title}" if self.lines else title)

    def item(self, text: str) -> None:
        self.lines.append(f"  {text}")

    def problem(self, text: str, code: ExitCode = ExitCode.PARTIAL) -> None:
        self.lines.append(f"  ！{text}")
        self.exit_code = max(self.exit_code, code)


def _package_versions() -> str:
    names = ("pydantic", "httpx", "shapely", "filelock", "platformdirs", "tzdata")
    parts = []
    for name in names:
        try:
            parts.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{name}（未安裝）")
    return "、".join(parts)


def _doctor_versions(doc: _Doctor) -> None:
    doc.section("版本")
    doc.item(
        f"house-watch {__version__}、Python {platform.python_version()}、"
        f"SQLite {sqlite3.sqlite_version}、{platform.system()} {platform.machine()}"
    )
    doc.item(f"套件：{_package_versions()}")
    try:
        ZoneInfo("Asia/Taipei")
        doc.item("時區資料：Asia/Taipei 可用")
    except Exception:  # zoneinfo 找不到資料時的例外種類依平台而不同
        doc.problem("時區資料：找不到 Asia/Taipei（Windows 要安裝 tzdata）")


def _doctor_settings(doc: _Doctor, settings: Settings) -> None:
    doc.section("設定")
    doc.item(f"啟用的區域：{'、'.join(area.id for area in settings.enabled_areas)}")
    for source_id, config in sorted(settings.sources.items()):
        flags = f"{'啟用' if config.enabled else '停用'}、{'發布' if config.publish else '不發布'}"
        doc.item(
            f"{source_id}：{flags}、間隔 {config.min_interval_s:g} 秒、"
            f"完整掃描上限 {config.max_requests_full} 次"
        )
    publish = settings.local.publish
    marks = {
        "site_repo_dir": publish.site_repo_dir,
        "remote": publish.remote,
        "deploy_key": publish.deploy_key,
        "known_hosts": publish.known_hosts,
    }
    doc.item(
        "發布設定："
        + "、".join(f"{key} {'已設定' if value else '未設定'}" for key, value in marks.items())
    )
    doc.item(f"聯絡方式：{'已設定' if settings.local.http.contact else '未設定'}")


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _doctor_database(doc: _Doctor, data: DataPaths) -> sqlite3.Connection | None:
    doc.section("資料庫")
    if not data.root.exists():
        doc.item("資料夾還不存在；第一次執行 init 或 update 時會建立")
        return None
    if data.is_empty():
        doc.item("資料夾是空的；第一次執行 init 或 update 時會初始化")
        return None
    if not data.instance_marker.is_file():
        doc.problem("資料夾有檔案，卻沒有 house_watch 的 instance 標記", ExitCode.CONFIG)
        return None
    if not data.db.is_file():
        doc.problem("有 instance 標記，卻沒有資料庫", ExitCode.CONFIG)
        return None
    try:
        conn = _open_read_only(data.db)
    except sqlite3.DatabaseError as exc:
        doc.problem(f"資料庫打不開（{type(exc).__name__}）", ExitCode.CONFIG)
        return None
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        conn.close()
        doc.problem(f"資料庫損壞或不是 SQLite 檔（{type(exc).__name__}）", ExitCode.CONFIG)
        return None
    if version != db.SCHEMA_VERSION:
        doc.problem(f"資料庫版本 {version}，程式要 {db.SCHEMA_VERSION}", ExitCode.CONFIG)
        conn.close()
        return None
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in db.TABLES
        }
    except sqlite3.DatabaseError as exc:
        doc.problem(f"資料庫讀取失敗（{type(exc).__name__}）")
        conn.close()
        return None
    doc.item(f"版本 {version}")
    if integrity == "ok":
        doc.item("integrity_check：ok")
    else:
        doc.problem("integrity_check 沒有通過")
    if dangling:
        doc.problem(f"foreign_key_check：{len(dangling)} 筆參照不到")
    else:
        doc.item("foreign_key_check：ok")
    doc.item("筆數：" + "、".join(f"{table} {count}" for table, count in counts.items()))
    return conn


def _code_text(value: Any) -> str:
    if value is None:
        return "—"
    code = public_code(value)
    return code.value if code is not None else "（不是公開代碼，已隱藏）"


def _time_text(value: dt.datetime | None) -> str:
    return to_iso(value) if value is not None else "—"


def _doctor_sources(doc: _Doctor, conn: sqlite3.Connection, settings: Settings) -> None:
    doc.section("來源狀態")
    area_ids = [area.id for area in settings.enabled_areas]
    times = scan_times(conn, area_ids)
    latest = latest_scans(conn, area_ids)
    full_failures = latest_full_failures(conn, area_ids)
    states = source_states(conn)
    now = SystemClock().now()
    for source_id in sorted(settings.sources):
        stamp = times.get(source_id)
        state = states.get(source_id)
        last = latest.get(source_id)
        parts = [
            f"最後成功 {_time_text(stamp.last_ok if stamp else None)}",
            f"最後完整 {_time_text(stamp.last_full_ok if stamp else None)}",
        ]
        if last is not None:
            parts.append(f"最近一次 #{last.scan_id} {last.status} {_code_text(last.error_code)}")
        failed = full_failures.get(source_id)
        if failed is not None and (last is None or failed.scan_id != last.scan_id):
            parts.append(
                f"某區完整掃描沒成功 #{failed.scan_id} {failed.status} {_code_text(failed.error_code)}"
            )
        if state is not None:
            if state.paused_until is not None and state.paused_until > now:
                parts.append(f"暫停到 {to_iso(state.paused_until)}")
            parts.append(f"退避 {state.backoff_level}")
            if state.last_error is not None:
                parts.append(f"錯誤 {_code_text(state.last_error)}")
            parts.append(f"robots 雜湊{'已記錄' if state.has_robots_hash else '未記錄'}")
        doc.item(f"{source_id}：" + "、".join(parts))
    marks = ", ".join("?" for _ in OK_SCAN_STATUSES)
    pending = conn.execute(
        f"SELECT scan_id, source, area FROM scans WHERE error_code = 'count_short'"
        f" AND status NOT IN ({marks}) ORDER BY scan_id DESC LIMIT {RECENT_SCANS}",
        OK_SCAN_STATUSES,
    ).fetchall()
    for scan_id, source, area in pending:
        doc.item(
            f"等待手動接受：#{scan_id} {source} {area}（count_short）"
            f"→ house-watch update --accept-scan {scan_id}"
        )


def _doctor_runs(doc: _Doctor, conn: sqlite3.Connection) -> None:
    doc.section("最近的執行")
    rows = conn.execute(
        "SELECT scan_id, source, area, mode, status, started_at, finished_at, pages, parsed,"
        f" error_code FROM scans ORDER BY started_at DESC, scan_id DESC LIMIT {RECENT_SCANS}"
    ).fetchall()
    if not rows:
        doc.item("還沒有任何掃描紀錄")
    for scan_id, source, area, mode, status, started, finished, pages, parsed, code in rows:
        doc.item(
            f"#{scan_id} {source} {area} {mode} {status}：{started} → {finished or '（未結束）'}，"
            f"{pages} 頁、{parsed} 筆、錯誤 {_code_text(code)}"
        )
    doc.section("資料集")
    doc.item(
        f"實價登錄：最後成功 {read_kv(conn, KV_LVR_LAST_CHECK) or '—'}、"
        f"資料截至 {read_kv(conn, KV_LVR_AS_OF) or '—'}"
    )
    doc.item(
        f"門牌：最後成功 {read_kv(conn, KV_DOORINDEX_LAST_CHECK) or '—'}、"
        f"版本 {read_kv(conn, KV_DOORINDEX_VERSION) or '—'}"
    )


def _doctor_site(doc: _Doctor, data: DataPaths) -> None:
    doc.section("網站")
    path = manifest_path(data)
    if not path.is_file():
        doc.item("還沒有匯出過")
        return
    try:
        manifest = load_manifest(path)
    except HouseWatchError as exc:
        doc.problem(str(exc))
        return
    doc.item(
        f"build {manifest.build_id}、{manifest.status.value}、{len(manifest.files)} 個檔、"
        f"產生於 {to_iso(manifest.generated_at)}"
    )


def _doctor_disk(doc: _Doctor, data: DataPaths) -> None:
    doc.section("磁碟")
    probe = data.root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    free = f"剩餘 {usage.free / (1 << 30):.1f} GiB／共 {usage.total / (1 << 30):.1f} GiB"
    if usage.free < LOW_DISK_BYTES:
        doc.problem(f"{free}，空間不足")
    else:
        doc.item(free)


def _doctor_modules(doc: _Doctor) -> None:
    doc.section("其他工作包")
    specs = {**ENTRY_POINTS}
    for name in ("listing_groups", "tx_records", "area_geojson"):
        value = getattr(export.PROVIDERS, name)
        if isinstance(value, str):
            specs[f"export.{name}"] = value
    for name, spec in specs.items():
        try:
            load_entry(spec)
            state = "可用"
        except NotAvailable as exc:
            state = exc.reason
        except Exception as exc:  # 模組存在但匯入失敗：照實列出種類
            state = f"匯入失敗（{type(exc).__name__}）"
        doc.item(f"{name}：{state}")


def doctor_report(ui: Ui) -> tuple[list[str], ExitCode]:
    """doctor 的內容與結束碼。不建立任何資料夾或資料庫。"""
    doc = _Doctor(ui)
    _doctor_versions(doc)
    doc.section("路徑")
    config_path, explicit = settings_file()
    exists = "存在" if config_path.is_file() else "不存在"
    doc.item(f"設定檔：{config_path}（{exists}{'，由環境變數指定' if explicit else ''}）")
    try:
        settings = _load(ui)
    except ConfigError as exc:
        doc.problem(f"設定錯誤：{exc}", ExitCode.CONFIG)
        return doc.lines, doc.exit_code
    data = DataPaths(settings.data_dir)
    doc.item(f"資料夾：{data.root}")
    doc.item(f"網站：{site_dir(data)}")
    _doctor_settings(doc, settings)
    conn = _doctor_database(doc, data)
    if conn is not None:
        try:
            _doctor_sources(doc, conn, settings)
            _doctor_runs(doc, conn)
        finally:
            conn.close()
    _doctor_site(doc, data)
    _doctor_disk(doc, data)
    _doctor_modules(doc)
    return doc.lines, doc.exit_code


def cmd_doctor(args: argparse.Namespace, ui: Ui) -> int:
    lines, code = doctor_report(ui)
    for line in lines:
        ui.say(line)
    return int(code)


# ---------------------------------------------------------------- 指令：tick、publish、schedule


def _delegate(command: str, ui: Ui, **kwargs: Any) -> int:
    try:
        entry = load_entry(ENTRY_POINTS[command])
    except NotAvailable as exc:
        ui.error(f"{command}：{exc}（由 WP7b／WP7c 實作）")
        return int(ExitCode.PARTIAL)
    return int(entry(**kwargs))


def cmd_tick(args: argparse.Namespace, ui: Ui) -> int:
    return _delegate("tick", ui)


def cmd_publish(args: argparse.Namespace, ui: Ui) -> int:
    return _delegate("publish", ui, dry_run=args.dry_run)


def cmd_schedule(args: argparse.Namespace, ui: Ui) -> int:
    return _delegate("schedule", ui, action=args.action, every_hours=args.every_hours)


def cmd_deploy_local(args: argparse.Namespace, ui: Ui) -> int:
    return _delegate("deploy_local", ui, ref=args.ref, repo=args.repo, force=args.force)


def _hours(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= 24:
        raise argparse.ArgumentTypeError("要是 1–24 的整數")
    return int(text)


# ---------------------------------------------------------------- 入口


def _port(text: str) -> int:
    if not text.isdigit() or not 0 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError("port 要是 0–65535 的整數")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="house-watch", description="桃園找房雷達：整理開價物件與實價登錄，產生靜態網站。"
    )
    parser.add_argument("--version", action="version", version=f"house-watch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="指令")

    sub.add_parser("init", help="建立資料夾與資料庫").set_defaults(handler=cmd_init)

    update = sub.add_parser("update", help="抓取並匯出（doorindex → lvr → sources → export）")
    update.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="步驟",
        help="只跑這些步驟，逗號分隔：doorindex、lvr、sources、export（匯出一定會跑）",
    )
    update.add_argument(
        "--source", action="append", default=[], metavar="來源", help="只掃描這些來源，逗號分隔"
    )
    update.add_argument(
        "--area", action="append", default=[], metavar="區域", help="只掃描這些區域，逗號分隔"
    )
    update.add_argument(
        "--accept-scan",
        action="append",
        default=[],
        metavar="掃描編號",
        help="手動接受筆數偏少（count_short）的完整掃描，編號見 doctor",
    )
    update.set_defaults(handler=cmd_update)

    sub.add_parser("export", help="重新產生網站檔").set_defaults(handler=cmd_export)

    serve = sub.add_parser("serve", help="在 127.0.0.1 提供網站檔")
    serve.add_argument("--port", type=_port, default=DEFAULT_PORT, help=f"預設 {DEFAULT_PORT}")
    serve.set_defaults(handler=cmd_serve)

    sub.add_parser("doctor", help="檢查環境與狀態").set_defaults(handler=cmd_doctor)
    sub.add_parser("tick", help="排程入口：做到期的工作、匯出、發布").set_defaults(handler=cmd_tick)

    publish = sub.add_parser("publish", help="發布到獨立的網站 repo")
    publish.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不寫入、不推送")
    publish.set_defaults(handler=cmd_publish)

    schedule = sub.add_parser("schedule", help="寫入或刪除 Mac 的排程檔（只寫檔，不載入）")
    schedule.add_argument("action", choices=["install", "remove"])
    schedule.add_argument(
        "--every-hours",
        type=_hours,
        default=None,
        metavar="N",
        help="時段內每 N 小時執行一次（預設每小時；探測間隔另由公開設定 probe_every_hours 決定）",
    )
    schedule.set_defaults(handler=cmd_schedule)

    deploy = sub.add_parser("deploy-local", help="把 git ref 裝成排程用的固定版本")
    deploy.add_argument("ref", metavar="git-ref", help="要安裝的 tag（或 commit）")
    deploy.add_argument("--repo", default=None, metavar="資料夾", help="程式 repo（預設是目前資料夾）")
    deploy.add_argument("--force", action="store_true", help="找不到排程用的資料夾或執行區不一致時，照樣安裝")
    deploy.set_defaults(handler=cmd_deploy_local)
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse：--help、--version 是 0，參數錯誤是 2
        return exc.code if isinstance(exc.code, int) else int(ExitCode.CONFIG)
    ui = Ui()
    try:
        return int(args.handler(args, ui))
    except HouseWatchError as exc:
        ui.error(str(exc))
        return int(exc.exit_code)
    except KeyboardInterrupt:
        ui.error("中斷")
        return int(ExitCode.PARTIAL)
    except Exception:  # 沒預料到的錯誤：照樣印出 traceback，但先遮掉本機值
        ui.error(traceback.format_exc().rstrip())
        return int(ExitCode.PARTIAL)


if __name__ == "__main__":
    raise SystemExit(main())
