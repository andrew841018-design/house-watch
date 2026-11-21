"""Mac 的排程（launchd）與固定版本的安裝（WP7b；r4 §3.5、§3.7；R2-07、R3-18）。

    house-watch deploy-local <git-ref> [--repo 資料夾]
        把某個 tag（或 commit）建成 wheel，用 `uv tool install` 裝進執行區（固定版本）。
    house-watch schedule install [--every-hours N]
        寫 launchd 的 plist（一個 label：LABEL），執行的是固定版本，不是開發資料夾。
        只寫檔，不載入、不啟動：印出 `launchctl bootstrap` 指令，由主 session 部署時執行。
    house-watch schedule remove
        刪掉 plist，印出 `launchctl bootout` 指令。

**執行區**（RuntimeLayout）：platformdirs 的 `house_watch_runtime` 資料夾
（Mac 是 ~/Library/Application Support/house_watch_runtime），環境變數
`HOUSE_WATCH_RUNTIME_DIR` 可以改。和資料夾分開放，因為資料夾在第一次 init 之前只能是空的
（DataPaths.is_empty）。裡面有：
- tools/、bin/：`uv tool install` 的工具環境與執行檔（UV_TOOL_DIR、UV_TOOL_BIN_DIR）。
- python/：uv 管理的 Python（UV_PYTHON_INSTALL_DIR、只用 uv 管理的版本），Homebrew 升級
  Python 不會弄壞排程。
- builds/<commit>/、wheels/<commit>/：從 git ref 匯出的原始碼與建好的 wheel。
- deployed.json：目前裝的是哪個 ref、commit、版本。

**deploy-local** 只從 git ref 建（`git archive`），不讀開發資料夾的工作區；套件版本照
uv.lock（`uv export` 產生 constraints）。升級步驟見 README。

**plist**：
- ProgramArguments 是執行區裡的 `house-watch tick`（絕對路徑）。
- StartCalendarInterval：公開設定 `[schedule] window_hours` 的範圍內，每 N 小時一次的整點
  （預設 N＝TICK_EVERY_HOURS＝1，r4 §3.5：每小時觸發一次）。探測每 probe_every_hours 小時一次
  是 pipeline.scan 依紀錄決定的，不是排程的頻率。時段是台北時間，安裝時換成這台電腦的本機時間
  （launchd 用本機時間，R1-25）。
- 明確的 PATH（含 /usr/bin，發布要用 git 與 ssh）、WorkingDirectory、標準輸出／錯誤導到 log 資料夾。
- 安裝當下設了的 HOUSE_WATCH_CONFIG、HOUSE_WATCH_DATA_DIR、HOUSE_WATCH_LOG_DIR、
  HOUSE_WATCH_RUNTIME_DIR，換成絕對路徑寫進 EnvironmentVariables。排程跑 tick 時沒有安裝
  當下的環境變數，不寫的話 tick 會改用預設位置（另一份設定、另一個資料夾）。設定檔和資料夾
  一樣不能放在隱私保護的位置。
- 沒有 RunAtLoad、KeepAlive：載入時不會馬上執行。睡眠錯過的那一輪，launchd 醒來後補跑一次；
  關機錯過的不補，下一個時段自然補上（R3-18）。
"""

import datetime as dt
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import platformdirs
from filelock import FileLock, Timeout

from house_watch import paths
from house_watch.clock import TAIPEI, SystemClock, to_iso
from house_watch.errors import ConfigError, ExitCode, HouseWatchError
from house_watch.ops.logging import ENV_LOG_DIR, LAUNCHD_STDERR, LAUNCHD_STDOUT, log_dir

LABEL = "com.house-watch.tick"
RUNTIME_APP_NAME = "house_watch_runtime"
ENV_RUNTIME_DIR = "HOUSE_WATCH_RUNTIME_DIR"
# 排程要沿用的路徑變數（安裝當下設了才寫進 plist）；plist 只多寫這幾個。
SCHEDULE_ENV_KEYS = (paths.ENV_CONFIG, paths.ENV_DATA_DIR, ENV_LOG_DIR, ENV_RUNTIME_DIR)
TICK_EVERY_HOURS = 1  # r4 §3.5：時段內每小時觸發一次
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# macOS 隱私保護（TCC）擋住背景程式讀寫的位置；排程的執行檔、工作目錄、log 都不能放這些地方。
TCC_HOME_DIRS = ("Desktop", "Documents", "Downloads")
TCC_ROOTS = ("/Volumes",)
COMMAND_TIMEOUT_S = 1800


# ---------------------------------------------------------------- 執行區


def runtime_dir() -> Path:
    """執行區：環境變數 > platformdirs 的 `house_watch_runtime`。"""
    override = os.environ.get(ENV_RUNTIME_DIR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(platformdirs.user_data_dir(RUNTIME_APP_NAME, appauthor=False))


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def python_dir(self) -> Path:
        return self.root / "python"

    @property
    def builds_dir(self) -> Path:
        return self.root / "builds"

    @property
    def wheels_dir(self) -> Path:
        return self.root / "wheels"

    @property
    def deployed(self) -> Path:
        return self.root / "deployed.json"

    @property
    def executable(self) -> Path:
        name = "house-watch.exe" if sys.platform == "win32" else "house-watch"
        return self.bin_dir / name


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir if agents_dir is not None else launch_agents_dir()) / f"{LABEL}.plist"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def protected_location(
    path: Path, *, dev_root: Path | None = None, home: Path | None = None
) -> str | None:
    """path 在開發資料夾或 macOS 隱私保護的位置裡時，回傳原因；否則 None。"""
    if dev_root is not None and _inside(path, dev_root):
        return "在開發資料夾裡（排程不能執行改到一半的程式，R2-07）"
    home = home if home is not None else Path.home()
    for name in TCC_HOME_DIRS:
        if _inside(path, home / name):
            return f"在 ~/{name} 裡（macOS 隱私保護會擋住背景程式）"
    for root in TCC_ROOTS:
        if _inside(path, Path(root)):
            return f"在 {root} 底下（外接磁碟會被 macOS 隱私保護擋住）"
    return None


def source_checkout() -> Path | None:
    """目前執行的程式如果是從原始碼資料夾（有 pyproject.toml 的 repo）跑的，回傳那個資料夾。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "house_watch").is_dir():
            return parent
    return None


# ---------------------------------------------------------------- plist


def calendar_hours(window: tuple[int, int], every_hours: int) -> list[int]:
    """window 範圍內（含兩端）每 every_hours 小時一次的整點（台北時間）。"""
    start, end = window
    if every_hours < 1:
        raise ConfigError("--every-hours 要是正整數")
    return list(range(start, end + 1, every_hours))


def local_times(
    taipei_hours: Sequence[int], *, today: dt.date, local_tz: dt.tzinfo
) -> list[tuple[int, int]]:
    """台北時間的整點換成本機時間的 (時, 分)，依時間排序、去重。"""
    out = set()
    for hour in taipei_hours:
        when = dt.datetime.combine(today, dt.time(hour), TAIPEI).astimezone(local_tz)
        out.add((when.hour, when.minute))
    return sorted(out)


def schedule_environment(environ: Mapping[str, str], *, cwd: Path | None = None) -> dict[str, str]:
    """安裝當下設了的 SCHEDULE_ENV_KEYS，換成絕對路徑。

    相對路徑以安裝當下的工作目錄為準（和 paths.py、log_dir() 的規則一樣），因為排程的
    工作目錄是執行區。只展開 ~、補成絕對路徑，不解析 symlink：tick 讀到後照原本的規則解析。
    沒設或空字串的不寫，排程端會用和安裝端一樣的預設。
    """
    base = cwd if cwd is not None else Path.cwd()
    out: dict[str, str] = {}
    for key in SCHEDULE_ENV_KEYS:
        value = environ.get(key)
        if value:
            path = Path(value).expanduser()
            out[key] = str(path if path.is_absolute() else base / path)
    return out


def build_plist(
    *,
    executable: Path,
    working_dir: Path,
    logs: Path,
    times: Sequence[tuple[int, int]],
    path_env: str = LAUNCHD_PATH,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """plist 的內容。沒有 RunAtLoad／KeepAlive：載入時不會執行，也不會常駐。

    env 只能是 SCHEDULE_ENV_KEYS 裡的路徑變數（schedule_environment 的結果）。
    """
    extra = dict(env or {})
    unknown = sorted(set(extra) - set(SCHEDULE_ENV_KEYS))
    if unknown:
        raise ValueError(f"plist 只能多寫 HOUSE_WATCH_* 路徑變數，不能寫：{', '.join(unknown)}")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "tick"],
        "StartCalendarInterval": [{"Hour": hour, "Minute": minute} for hour, minute in times],
        "WorkingDirectory": str(working_dir),
        "EnvironmentVariables": {"PATH": path_env, "LANG": "en_US.UTF-8", **extra},
        "StandardOutPath": str(logs / LAUNCHD_STDOUT),
        "StandardErrorPath": str(logs / LAUNCHD_STDERR),
        "RunAtLoad": False,
    }


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


def bootstrap_command(path: Path, uid: int) -> str:
    return f"launchctl bootstrap gui/{uid} '{path}'"


def bootout_command(uid: int) -> str:
    return f"launchctl bootout gui/{uid}/{LABEL}"


@dataclass(frozen=True)
class InstallResult:
    plist: Path
    content: dict[str, Any]
    changed: bool
    commands: tuple[str, ...]


def install_schedule(
    *,
    window: tuple[int, int],
    every_hours: int,
    layout: RuntimeLayout,
    logs: Path,
    agents_dir: Path,
    local_tz: dt.tzinfo,
    today: dt.date,
    uid: int,
    dev_root: Path | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    other_paths: Sequence[tuple[str, Path]] = (),
) -> InstallResult:
    """寫 plist；不執行 launchctl。執行檔不存在或位置不對時丟 ConfigError。

    env：寫進 plist 的路徑變數（schedule_environment）。config_path、data_dir、other_paths
    （發布用的網站 repo、deploy key、known_hosts）：tick 會讀寫的檔案，只檢查隱私保護的位置
    （不檢查開發資料夾）。
    """
    executable = layout.executable
    if not executable.is_file():
        raise ConfigError(
            "找不到固定版本的執行檔；先執行 house-watch deploy-local <tag>（見 README）"
        )
    for what, path in (("執行檔", executable), ("執行區", layout.root), ("log 資料夾", logs)):
        reason = protected_location(path, dev_root=dev_root, home=home)
        if reason is not None:
            raise ConfigError(f"{what}{reason}，排程不能用")
    for what, maybe in (("設定檔", config_path), ("資料夾", data_dir), *other_paths):
        if maybe is None:
            continue
        reason = protected_location(maybe, home=home)
        if reason is not None:
            raise ConfigError(f"{what}{reason}，排程不能用")
    times = local_times(calendar_hours(window, every_hours), today=today, local_tz=local_tz)
    content = build_plist(
        executable=executable, working_dir=layout.root, logs=logs, times=times, env=env
    )
    logs.mkdir(parents=True, exist_ok=True)  # launchd 不會自己建 log 資料夾
    target = plist_path(agents_dir)
    payload = plistlib.dumps(content, fmt=plistlib.FMT_XML, sort_keys=True)
    old = target.read_bytes() if target.is_file() else None
    if old != payload:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
    commands = (bootout_command(uid), bootstrap_command(target, uid))
    return InstallResult(target, content, old != payload, commands)


def remove_schedule(*, agents_dir: Path, uid: int) -> tuple[bool, str]:
    """刪掉 plist（不執行 launchctl），回傳 (有沒有刪到, 要執行的 bootout 指令)。"""
    target = plist_path(agents_dir)
    existed = target.is_file()
    target.unlink(missing_ok=True)
    return existed, bootout_command(uid)


def _publish_paths(settings: Any) -> list[tuple[str, Path]]:
    """發布設定裡排程要讀寫的檔案（事後審查 SA-2）。"""
    publish = settings.local.publish
    named = (("網站 repo", publish.site_repo_dir), ("deploy key", publish.deploy_key))
    named += (("known_hosts", publish.known_hosts),)
    return [(what, path.expanduser()) for what, path in named if path is not None]


def run_schedule(*, action: str, every_hours: int | None = None) -> int:
    """`house-watch schedule install|remove`（只給 Mac）。"""
    from house_watch.cli import Ui, _load

    ui = Ui()
    if sys.platform != "darwin":
        ui.error(
            "schedule 只支援 macOS；其他系統請照 README 用 cron 或工作排程器呼叫 house-watch tick"
        )
        return int(ExitCode.CONFIG)
    agents = launch_agents_dir()
    uid = _uid()
    if action == "remove":
        existed, command = remove_schedule(agents_dir=agents, uid=uid)
        ui.say(f"{'已刪除' if existed else '沒有找到'}：{plist_path(agents)}")
        ui.say(f"如果排程已載入，執行：{command}")
        return int(ExitCode.OK)
    settings = _load(ui)
    schedule = settings.public.schedule
    local_tz = dt.datetime.now().astimezone().tzinfo or TAIPEI
    config_file, explicit = paths.settings_file()
    env = schedule_environment(os.environ)
    result = install_schedule(
        window=schedule.window_hours,
        every_hours=every_hours or TICK_EVERY_HOURS,
        layout=RuntimeLayout(runtime_dir()),
        logs=log_dir(),
        agents_dir=agents,
        local_tz=local_tz,
        today=dt.datetime.now(TAIPEI).date(),
        uid=uid,
        dev_root=source_checkout(),
        env=env,
        config_path=config_file if explicit or config_file.is_file() else None,
        data_dir=settings.data_dir,
        other_paths=_publish_paths(settings),
    )
    times = "、".join(f"{h:02d}:{m:02d}" for h, m in _times_of(result.content))
    ui.say(f"{'已寫入' if result.changed else '沒有變動'}：{result.plist}")
    ui.say(f"執行時間（本機時間）：{times}；只寫檔，沒有載入或啟動任何東西")
    if env:
        ui.say("排程沿用的路徑：" + "；".join(f"{key}={value}" for key, value in env.items()))
    ui.say("部署時由主 session 執行（已載入過的話先執行第一行）：")
    for command in result.commands:
        ui.say(f"  {command}")
    return int(ExitCode.OK)


def _times_of(content: Mapping[str, Any]) -> list[tuple[int, int]]:
    return [(item["Hour"], item["Minute"]) for item in content["StartCalendarInterval"]]


# ---------------------------------------------------------------- deploy-local


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


def run_command(
    argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # argv 是清單，不經 shell
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


class DeployError(HouseWatchError):
    exit_code = ExitCode.CONFIG


def _check(result: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise DeployError(f"{what} 失敗（結束碼 {result.returncode}）：{' / '.join(tail)}")
    return result.stdout


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if Path(candidate).is_file():
            return candidate
    raise DeployError("找不到 uv；請先安裝 uv（https://docs.astral.sh/uv/）")


def uv_env(layout: RuntimeLayout, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """uv tool install 用的環境：工具、執行檔、Python 都放在執行區；不讀使用者的 uv 設定。"""
    env = dict(base if base is not None else os.environ)
    for key in [k for k in env if k.startswith("VIRTUAL_ENV") or k == "UV_PROJECT_ENVIRONMENT"]:
        env.pop(key)
    env.update(
        UV_TOOL_DIR=str(layout.tools_dir),
        UV_TOOL_BIN_DIR=str(layout.bin_dir),
        UV_PYTHON_INSTALL_DIR=str(layout.python_dir),
        UV_PYTHON_PREFERENCE="only-managed",
        UV_NO_CONFIG="1",
    )
    return env


@dataclass(frozen=True)
class DeployResult:
    ref: str
    commit: str
    tagged: bool
    version: str
    executable: Path


def deploy_local(
    ref: str,
    *,
    repo: Path,
    layout: RuntimeLayout,
    run: Runner = run_command,
    uv: str | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> DeployResult:
    """把 git ref 建成 wheel，裝成固定版本（見模組說明）。不讀開發資料夾的工作區內容。"""
    git = shutil.which("git") or "git"
    uv = uv or find_uv()
    now = now or SystemClock().now
    if not ref or ref.startswith("-"):
        raise DeployError("git ref 不能是空的，也不能以 - 開頭")
    toplevel = _check(run([git, "-C", str(repo), "rev-parse", "--show-toplevel"]), "git rev-parse")
    top = Path(toplevel.decode().strip())
    commit = (
        _check(
            run([git, "-C", str(top), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]),
            f"找不到 git ref {ref}",
        )
        .decode()
        .strip()
    )
    tags = _check(run([git, "-C", str(top), "tag", "--points-at", commit]), "git tag").decode()
    tagged = bool(tags.strip())
    short = commit[:12]

    build = layout.builds_dir / short
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    archive = _check(run([git, "-C", str(top), "archive", "--format=tar", commit]), "git archive")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(build, filter="data")
    python = (build / ".python-version").read_text(encoding="utf-8").strip() or "3.13"

    env = uv_env(layout)
    constraints = build / "constraints.txt"
    _check(
        run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",  # TODO(deferred.md 2026-11-21 SA-8)：改成安裝時驗證雜湊
                "--project",
                str(build),
                "--output-file",
                str(constraints),
            ],
            cwd=build,
            env=env,
        ),
        "uv export",
    )
    wheels = layout.wheels_dir / short
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(parents=True)
    _check(
        run([uv, "build", "--wheel", "--out-dir", str(wheels), str(build)], cwd=build, env=env),
        "uv build",
    )
    built = sorted(wheels.glob("*.whl"))
    if len(built) != 1:
        raise DeployError(f"uv build 應該產生 1 個 wheel，實際 {len(built)} 個")
    _check(
        run(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--python",
                python,
                "--constraints",
                str(constraints),
                str(built[0]),
            ],
            cwd=layout.root,
            env=env,
        ),
        "uv tool install",
    )
    executable = layout.executable
    output = _check(run([str(executable), "--version"], cwd=layout.root), "house-watch --version")
    version = output.decode("utf-8", "replace").strip().removeprefix("house-watch").strip()
    record = {
        "ref": ref,
        "commit": commit,
        "tagged": tagged,
        "version": version,
        "wheel": built[0].name,
        "python": python,
        "installed_at": to_iso(now()),
    }
    layout.deployed.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return DeployResult(ref, commit, tagged, version, executable)


def _data_run_lock() -> FileLock | None:
    """資料夾的執行鎖（和 tick、update、export、publish 同一把；事後審查 SA-6）。

    換版（uv tool install --force）和正在跑的 tick 重疊時，同一個程序會新舊模組混用。
    只有本機設定檔存在、資料夾也存在時才拿得到；否則回 None（呼叫端提醒）。
    """
    from house_watch.paths import DataPaths
    from house_watch.settings import load_settings

    config_file, _explicit = paths.settings_file()
    if not config_file.is_file():
        return None
    try:
        data = DataPaths(load_settings().data_dir)
    except HouseWatchError:
        return None
    return FileLock(data.lock) if data.root.is_dir() else None


def run_deploy_local(*, ref: str, repo: str | None = None) -> int:
    """`house-watch deploy-local <git-ref>`：在開發資料夾裡執行，把那個 ref 裝成固定版本。"""
    from house_watch.cli import Ui

    ui = Ui()
    layout = RuntimeLayout(runtime_dir())
    reason = protected_location(layout.root)
    if reason is not None:
        ui.error(f"執行區{reason}；請用 {ENV_RUNTIME_DIR} 指到別的位置")
        return int(ExitCode.CONFIG)
    layout.root.mkdir(parents=True, exist_ok=True)
    lock = _data_run_lock()
    if lock is None:
        ui.error(
            "注意：找不到本機設定檔或資料夾，沒辦法確認排程沒有在跑；"
            "建議用平常的 HOUSE_WATCH_* 環境變數執行，或先停排程（launchctl bootout）"
        )
    else:
        try:
            lock.acquire(timeout=0)
        except Timeout:
            ui.error("排程或另一個指令正在執行（執行鎖拿不到）；等它跑完再換版")
            return int(ExitCode.CONFIG)
    try:
        result = deploy_local(ref, repo=Path(repo) if repo else Path.cwd(), layout=layout)
    finally:
        if lock is not None:
            lock.release()
    ui.say(f"已安裝 house-watch {result.version}（{result.ref} → {result.commit[:12]}）")
    ui.say(f"執行檔：{result.executable}")
    if not result.tagged:
        ui.error(f"注意：{result.ref} 不是 tag；排程建議只裝 tag")
    ui.say("接著（用固定版本執行）：doctor → publish --dry-run → schedule install（見 README）")
    return int(ExitCode.OK)
