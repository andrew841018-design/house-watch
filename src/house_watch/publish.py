"""發布：把匯出的網站推到獨立的網站 repo（WP7c；r4 §0 第 5 點、§3.6；R1-18、R1-28、R2-02、R2-06、R2-15）。

    house-watch publish [--dry-run]

網站 repo 的本機 clone 由擁有者事先建好（M4；步驟見 README 的發布段），位置寫在 settings.toml：

    [publish]
    site_repo_dir = "/絕對路徑/網站 repo 的 clone"
    remote = "git@github.com:<帳號>/<網站 repo>.git"   # 必填：要和 clone 的 origin 相同（SA-5）
    deploy_key = "/絕對路徑/deploy_key"                # 只綁網站 repo 的 deploy key，權限 600
    known_hosts = "/絕對路徑/known_hosts"              # 核對過官方指紋的 GitHub 主機金鑰

流程：
1. 讀 `<資料夾>/export/manifest.json`，逐檔核對 export/site 裡的檔（位元組數、sha256）。
   清單一定要有 index.html、.nojekyll、data/meta.json。
2. 和 clone 的 HEAD 比（git 物件雜湊）：
   - meta.json 以外的檔不同、多了或少了 → 資料有變，commit。
   - meta.json 的非時間欄位變了（狀態、錯誤代碼、資料版本、區域、門檻）→ commit。
   - 只有時間戳變了 → 心跳規則（R2-15）：某個來源或資料集已公開的「最後成功時間」比它的
     過期門檻的一半還舊，而且這次的時間比較新，才 commit。「比較新」避免來源停擺時
     每一輪都 commit（R1-28）。
   - 其他情況不 commit、也不動 clone。
3. commit：清單上的檔複製進 clone，清單外的檔刪掉（不碰 .git）；index 先重設成 HEAD，
   只 `git add` 清單上變了的路徑、`git rm --cached` 被拿掉的路徑（不用 `git add -A`）。
   作者是 repo 層級的固定 user.name／user.email（不含任何人的帳號）；訊息只有時間與 build id。
   commit 後核對 HEAD 的檔案和 manifest 完全相同，不同就不推。
4. push：HEAD 比 origin 的追蹤分支新（這次的 commit，或上一輪沒推成功的）就一般 push，
   絕不 force。被拒 → push_rejected；其他失敗 → publish_failed。commit 留在本機，下一輪重試。
5. git 和使用者的設定隔開：不讀全域與系統設定、不跑 hook、不簽章、不用全域 excludes；
   ssh 走 `GIT_SSH_COMMAND`（ssh_command()），只用 deploy key 與專案的 known_hosts。
6. `--dry-run`：只讀，不寫 clone、不推、不連線，印出會做什麼。
"""

import datetime as dt
import hashlib
import json
import logging
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from house_watch import db
from house_watch.clock import Clock, SystemClock, from_iso, to_iso
from house_watch.errors import ErrorCode, ExitCode, HouseWatchError
from house_watch.export import META_PATH, Manifest, load_manifest
from house_watch.paths import DataPaths
from house_watch.settings import Settings, load_settings

AUTHOR_NAME = "house-watch"
AUTHOR_EMAIL = "noreply@house-watch.invalid"
COMMIT_TEMPLATE = "site {timestamp} build {build_id}"
REMOTE_NAME = "origin"
REQUIRED_FILES = ("index.html", ".nojekyll", META_PATH)
GIT_TIMEOUT_S = 120
PUSH_TIMEOUT_S = 300
VOLATILE_SOURCE_KEYS = frozenset({"last_ok", "last_full_ok", "paused_until"})
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "USERPROFILE", "LANG")

KV_LAST_ATTEMPT = "publish.last_attempt"
KV_LAST_OK = "publish.last_ok"
KV_LAST_ERROR = "publish.last_error"
KV_PENDING = "publish.pending"

_log = logging.getLogger("house_watch.publish")


class PublishError(HouseWatchError):
    """發布失敗。code 是公開錯誤代碼（publish_failed、push_rejected）。訊息不放路徑或設定值。"""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.PUBLISH_FAILED,
        exit_code: ExitCode = ExitCode.PARTIAL,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _config_error(message: str) -> PublishError:
    return PublishError(message, ErrorCode.PUBLISH_FAILED, ExitCode.CONFIG)


# ---------------------------------------------------------------- 設定


@dataclass(frozen=True)
class PublishConfig:
    site_dir: Path
    remote: str | None
    deploy_key: Path
    known_hosts: Path


def is_configured(settings: Settings) -> bool:
    """settings.toml 有寫網站 repo 的位置（M4 之前沒有，tick 就略過發布）。"""
    return settings.local.publish.site_repo_dir is not None


def resolve_config(settings: Settings) -> PublishConfig:
    publish = settings.local.publish
    if publish.site_repo_dir is None:
        raise _config_error("settings.toml 的 [publish] 沒有 site_repo_dir")
    if not publish.remote:
        # 事後審查 SA-5：site_repo_dir 指錯時，靠 remote 比對擋下來，所以一定要寫。
        raise _config_error("settings.toml 的 [publish] 沒有 remote：要用它確認推送的是網站 repo")
    missing = [name for name in ("deploy_key", "known_hosts") if getattr(publish, name) is None]
    if missing:
        raise _config_error(
            f"[publish] 缺少 {', '.join(missing)}：推送一定要用專用的 deploy key（R2-06）"
        )
    assert publish.deploy_key is not None and publish.known_hosts is not None
    key, known = publish.deploy_key.expanduser(), publish.known_hosts.expanduser()
    if not key.is_file():
        raise _config_error("找不到 [publish] deploy_key 指的檔案")
    if os.name == "posix" and stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise _config_error("deploy_key 的權限要是 600（只有自己能讀寫）")
    if not known.is_file() or known.stat().st_size == 0:
        raise _config_error("[publish] known_hosts 指的檔案不存在或是空的")
    if '"' in str(known):
        raise _config_error("known_hosts 的路徑不能有雙引號")
    return PublishConfig(publish.site_repo_dir.expanduser(), publish.remote, key, known)


def ssh_command(deploy_key: Path, known_hosts: Path, ssh: str = "ssh") -> str:
    """`GIT_SSH_COMMAND`：不讀使用者的 ssh 設定與 agent，只用 deploy key 與專案的 known_hosts。

    整串交給 shell 解析，所以每個參數都 shlex.quote；UserKnownHostsFile 的值另外用雙引號包，
    因為 ssh 把沒有引號的空白當成多個檔案的分隔（Application Support 有空白）。
    """
    parts = [
        ssh,
        "-F",
        os.devnull,
        "-i",
        str(deploy_key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        f'UserKnownHostsFile="{known_hosts}"',
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
    ]
    return " ".join(shlex.quote(part) for part in parts)


def git_env(ssh: str | None = None) -> dict[str, str]:
    """git 的環境：只留必要的變數（不繼承 GIT_DIR 這類 GIT_*），不讀全域與系統設定。"""
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LITERAL_PATHSPECS="1",
        LC_ALL="C",
    )
    if ssh is not None:
        env["GIT_SSH_COMMAND"] = ssh
    return env


@dataclass
class Git:
    """在網站 repo 裡跑 git。每次都關掉 hook、簽章、全域 excludes 與 attributes。"""

    git: str
    cwd: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float = GIT_TIMEOUT_S,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            self.git,
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            *args,
        ]
        try:
            result = subprocess.run(  # argv 是清單，不經 shell
                argv,
                cwd=self.cwd,
                env=self.env,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise PublishError(f"git {args[0]} 逾時") from None
        if check and result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
            detail = f"：{' / '.join(lines)}" if lines else ""
            raise PublishError(f"git {args[0]} 失敗（結束碼 {result.returncode}）{detail}")
        return result

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.decode("utf-8", "surrogateescape").strip()


def _paths_input(paths: tuple[str, ...]) -> bytes:
    return b"".join(path.encode("utf-8") + b"\0" for path in paths)


# ---------------------------------------------------------------- 讀 clone 與匯出


def blob_id(payload: bytes, object_format: str = "sha1") -> str:
    """git 物件雜湊（blob），用來不寫任何東西就和 HEAD 比較。"""
    digest = hashlib.sha256() if object_format == "sha256" else hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(payload))
    digest.update(payload)
    return digest.hexdigest()


@dataclass(frozen=True)
class RepoState:
    branch: str
    head: str | None  # None：還沒有任何 commit
    tree: dict[str, str]  # HEAD 的路徑 → 物件雜湊
    object_format: str


def ls_tree(git: Git, rev: str) -> dict[str, str]:
    raw = git.run("ls-tree", "-r", "-z", "--full-tree", rev).stdout
    tree: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        info, _, path = entry.partition(b"\t")
        tree[path.decode("utf-8", "surrogateescape")] = info.split()[2].decode("ascii")
    return tree


def read_repo(git: Git, site_dir: Path) -> RepoState:
    if not site_dir.is_dir():
        raise _config_error("找不到網站 repo 的 clone（[publish] site_repo_dir）")
    top = git.run("rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or Path(top.stdout.decode().strip()).resolve() != site_dir.resolve():
        raise _config_error("[publish] site_repo_dir 不是網站 repo clone 的最上層資料夾")
    branch = git.run("symbolic-ref", "-q", "--short", "HEAD", check=False)
    if branch.returncode != 0:
        raise _config_error("網站 repo 不在任何分支上（detached HEAD）")
    head = git.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    object_format = git.run("rev-parse", "--show-object-format", check=False)
    fmt = object_format.stdout.decode().strip() if object_format.returncode == 0 else "sha1"
    commit = head.stdout.decode().strip() if head.returncode == 0 else None
    return RepoState(
        branch=branch.stdout.decode().strip(),
        head=commit,
        tree=ls_tree(git, "HEAD") if commit else {},
        object_format=fmt or "sha1",
    )


def check_remote(git: Git, expected: str | None) -> None:
    result = git.run("remote", "get-url", REMOTE_NAME, check=False)
    if result.returncode != 0:
        raise _config_error(f"網站 repo 沒有 {REMOTE_NAME} remote（設定步驟見 README）")
    if expected is not None and result.stdout.decode().strip() != expected:
        raise _config_error(f"網站 repo 的 {REMOTE_NAME} 和 settings.toml 的 publish.remote 不同")


def check_site_identity(git: Git) -> bool:
    """確認這真的是網站 repo，再同步（事後審查 SA-5、P-4；修完再審 F1-4）。回傳有沒有 HEAD。

    同步會刪掉清單外的檔；site_repo_dir 指錯（例如程式 repo）時就會刪掉別人的東西。
    所以每個 commit 都要是發布程式做的（作者是 AUTHOR_NAME／AUTHOR_EMAIL），不是就停，
    不刪任何東西。還沒有 commit 的 clone 認不出身分，工作區要是空的（不乾淨就停）。
    """
    has_head = git.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).returncode == 0
    if has_head:
        authors = git.run("log", "--format=%an <%ae>", "HEAD").stdout.decode().splitlines()
        if any(line and line != f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>" for line in authors):
            raise _config_error(
                "網站 repo 裡有不是發布程式做的 commit：site_repo_dir 可能指錯了，沒有同步任何檔案"
            )
        return True
    status = git.run("status", "--porcelain", "--untracked-files=all").stdout.decode()
    if status.strip():
        raise _config_error(
            "網站 repo 還沒有 commit、工作區卻有檔案：先確認 site_repo_dir，或清乾淨後再發布"
        )
    return False


def restore_worktree(git: Git) -> None:
    """身分確認過的網站 clone：工作區還原成上一次發布的 commit（修完再審 F1-4）。

    Finder 的 .DS_Store、上一輪同步到一半中斷留下的改動，都不能讓發布永遠停住。
    """
    git.run("reset", "-q", "--hard", "HEAD")
    git.run("clean", "-fdq")


def pending_commits(git: Git, state: RepoState) -> int:
    """HEAD 有幾個 commit 還不在 origin 的追蹤分支上（沒有追蹤分支時，全部都算）。"""
    if git.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).returncode != 0:
        return 0
    tracking = f"refs/remotes/{REMOTE_NAME}/{state.branch}"
    if git.run("rev-parse", "--verify", "-q", tracking, check=False).returncode != 0:
        return int(git.out("rev-list", "--count", "HEAD"))
    return int(git.out("rev-list", "--count", f"{tracking}..HEAD"))


@dataclass(frozen=True)
class ExportFiles:
    manifest: Manifest
    root: Path
    blobs: dict[str, str]  # manifest 的路徑 → git 物件雜湊
    meta: dict[str, Any]


def read_export(data: DataPaths, object_format: str) -> ExportFiles:
    """讀 manifest，逐檔核對 export/site 的內容；有任何不一致就不發布。"""
    if not data.export_manifest.is_file():
        raise PublishError("還沒有匯出過：先執行 house-watch export 或 update")
    manifest = load_manifest(data.export_manifest)
    root = data.export_site_dir
    paths = [entry.path for entry in manifest.files]
    if len(paths) != len(set(paths)):
        raise PublishError("manifest.json 有重複的路徑")
    missing = [name for name in REQUIRED_FILES if name not in paths]
    if missing:
        raise PublishError(f"匯出清單缺少 {', '.join(missing)}，不發布")
    blobs: dict[str, str] = {}
    for entry in manifest.files:
        path = root / entry.path
        if path.is_symlink() or not path.is_file():
            raise PublishError(f"匯出的檔不存在或不是一般檔案：{entry.path}")
        payload = path.read_bytes()
        if len(payload) != entry.bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise PublishError(f"匯出的檔和 manifest.json 不一致：{entry.path}")
        blobs[entry.path] = blob_id(payload, object_format)
    try:
        meta = json.loads((root / META_PATH).read_bytes())
    except ValueError:
        raise PublishError("data/meta.json 不是合法的 JSON") from None
    if not isinstance(meta, dict) or meta.get("build_id") != manifest.build_id:
        raise PublishError("data/meta.json 和 manifest.json 的 build id 不同")
    return ExportFiles(manifest, root, blobs, meta)


def published_meta(git: Git, state: RepoState) -> dict[str, Any] | None:
    blob = state.tree.get(META_PATH)
    if blob is None:
        return None
    try:
        value = json.loads(git.run("cat-file", "blob", blob).stdout)
    except (ValueError, PublishError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------- 要不要 commit


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return from_iso(value)
    except ValueError:
        return None


def stable_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """meta 去掉每一輪都會變的時間戳之後的內容。"""
    out = {key: value for key, value in meta.items() if key != "generated_at"}
    out["sources"] = [
        {k: v for k, v in source.items() if k not in VOLATILE_SOURCE_KEYS}
        if isinstance(source, dict)
        else source
        for source in meta.get("sources") or []
    ]
    datasets = meta.get("datasets")
    if isinstance(datasets, dict):
        out["datasets"] = {
            name: {k: v for k, v in item.items() if k != "last_check"}
            if isinstance(item, dict)
            else item
            for name, item in datasets.items()
        }
    return out


def freshness_stamps(meta: dict[str, Any]) -> dict[str, dt.datetime | None]:
    """瀏覽器判斷過期用的時間：各來源的 last_ok、實價登錄與門牌的 last_check。"""
    stamps: dict[str, dt.datetime | None] = {}
    for source in meta.get("sources") or []:
        if isinstance(source, dict) and isinstance(source.get("id"), str):
            stamps[source["id"]] = _time(source.get("last_ok"))
    datasets = meta.get("datasets") if isinstance(meta.get("datasets"), dict) else {}
    for name in ("lvr", "doorindex"):
        item = datasets.get(name)
        stamps[name] = _time(item.get("last_check")) if isinstance(item, dict) else None
    return stamps


def heartbeat_due(published: dict[str, Any], new: dict[str, Any], now: dt.datetime) -> bool:
    """有某個時間：這次比較新，而且已公開的那個比門檻的一半還舊（或還沒公開過）。"""
    limits = new.get("freshness_hours")
    limits = limits if isinstance(limits, dict) else {}
    before = freshness_stamps(published)
    for key, value in freshness_stamps(new).items():
        old = before.get(key)
        if value is None or (old is not None and value <= old):
            continue
        hours = limits.get(key)
        if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
            continue
        if old is None or now - old > dt.timedelta(hours=hours) / 2:
            return True
    return False


@dataclass(frozen=True)
class Plan:
    write: tuple[str, ...]  # 內容和 HEAD 不同（或 HEAD 沒有）的清單路徑
    remove: tuple[str, ...]  # HEAD 有、清單沒有的路徑
    reason: str  # first、data、meta、heartbeat、unchanged
    commit: bool


def decide(
    state: RepoState, files: ExportFiles, published: dict[str, Any] | None, now: dt.datetime
) -> Plan:
    write = tuple(sorted(p for p, blob in files.blobs.items() if state.tree.get(p) != blob))
    remove = tuple(sorted(set(state.tree) - set(files.blobs)))
    if state.head is None:
        return Plan(write, remove, "first", True)
    if remove or any(path != META_PATH for path in write):
        return Plan(write, remove, "data", True)
    if not write:
        return Plan(write, remove, "unchanged", False)
    if published is None or stable_meta(published) != stable_meta(files.meta):
        return Plan(write, remove, "meta", True)
    if heartbeat_due(published, files.meta, now):
        return Plan(write, remove, "heartbeat", True)
    return Plan(write, remove, "unchanged", False)


# ---------------------------------------------------------------- 寫入 clone、commit、push


def _sync_worktree(site: Path, files: ExportFiles, plan: Plan) -> None:
    """清單外的檔與所有符號連結先刪掉（不碰 .git），再把清單上變了的、或工作區沒有的檔寫進去。"""
    keep = set(files.blobs)
    for dirpath, dirnames, filenames in os.walk(site, topdown=True):
        here = Path(dirpath)
        if here == site:
            dirnames[:] = [d for d in dirnames if d != ".git"]
            filenames = [f for f in filenames if f != ".git"]  # worktree 的 .git 是檔案（P-13）
        for name in list(dirnames):
            if (here / name).is_symlink():  # 不跟著連結走出 clone
                (here / name).unlink()
                dirnames.remove(name)
        for name in filenames:
            path = here / name
            if path.is_symlink() or path.relative_to(site).as_posix() not in keep:
                path.unlink()
    for rel in sorted(keep):
        target = site / rel
        if rel in plan.write or not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(files.root / rel, target)
    for dirpath, _dirnames, _filenames in os.walk(site, topdown=False):
        here = Path(dirpath)
        if here == site or ".git" in here.relative_to(site).parts:
            continue
        if not any(here.iterdir()):
            here.rmdir()


def _ensure_identity(git: Git) -> None:
    for key, value in (("user.name", AUTHOR_NAME), ("user.email", AUTHOR_EMAIL)):
        current = git.run("config", "--local", "--get", key, check=False)
        if current.returncode != 0 or current.stdout.decode().strip() != value:
            git.run("config", "--local", key, value)


def _commit(git: Git, state: RepoState, files: ExportFiles, plan: Plan, now: dt.datetime) -> None:
    _sync_worktree(git.cwd, files, plan)
    if state.head is not None:
        git.run("read-tree", "HEAD")  # index＝HEAD，再只加清單上的變動
    else:
        git.run("read-tree", "--empty")
    if plan.write:
        git.run(
            "add", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=_paths_input(plan.write)
        )
    if plan.remove:
        git.run(
            "rm",
            "--cached",
            "-q",
            "--ignore-unmatch",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin=_paths_input(plan.remove),
        )
    _ensure_identity(git)
    message = COMMIT_TEMPLATE.format(timestamp=to_iso(now), build_id=files.manifest.build_id)
    git.run("commit", "-q", "--no-verify", "-m", message)


def _verify_head(git: Git, files: ExportFiles, *, allow_meta: bool) -> None:
    """HEAD 的檔案清單一定要和 manifest 相同；allow_meta 時 meta.json 的內容可以不同。"""
    tree = ls_tree(git, "HEAD")
    same_paths = set(tree) == set(files.blobs)
    same_content = all(
        tree.get(path) == blob
        for path, blob in files.blobs.items()
        if not (allow_meta and path == META_PATH)
    )
    if not (same_paths and same_content):
        raise PublishError("網站 repo 的 HEAD 和 manifest 不一致，不推送")


def _push(git: Git, branch: str) -> None:
    ref = f"refs/heads/{branch}"
    result = git.run(
        "push", "--porcelain", REMOTE_NAME, f"{ref}:{ref}", timeout=PUSH_TIMEOUT_S, check=False
    )
    if result.returncode == 0:
        return
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    rejected = any(
        line.startswith("!") for line in result.stdout.decode("utf-8", "replace").splitlines()
    )
    if rejected or "[rejected]" in text or "[remote rejected]" in text:
        raise PublishError(
            "推送被拒（不 force push；commit 留在本機，下一輪重試）", ErrorCode.PUSH_REJECTED
        )
    raise PublishError("推送失敗（commit 留在本機，下一輪重試）", ErrorCode.PUBLISH_FAILED)


# ---------------------------------------------------------------- 對外


@dataclass(frozen=True)
class PublishResult:
    reason: str
    committed: bool
    pushed: bool
    pending: int  # 結束時還沒推上去的 commit 數
    dry_run: bool
    build_id: str
    write: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    exit_code: ExitCode = ExitCode.OK

    def lines(self) -> list[str]:
        prefix = "發布（dry-run，不寫入、不推送）" if self.dry_run else "發布"
        verb = "會" if self.dry_run else "已"
        action = f"{verb} commit" if self.committed else "不 commit"
        out = [f"{prefix}：build {self.build_id}，{action}（原因：{self.reason}）"]
        if self.committed:
            out.append(f"寫入 {len(self.write)} 個檔、刪除 {len(self.remove)} 個檔")
            out.extend(f"  + {path}" for path in self.write[:20])
            out.extend(f"  - {path}" for path in self.remove[:20])
        if self.dry_run:
            out.append(f"還沒推送的 commit：{self.pending}" + ("，會推送" if self.pending else ""))
        elif self.pushed:
            out.append("已推送")
        return out


def _record(conn: sqlite3.Connection | None, values: dict[str, str]) -> None:
    if conn is None:
        return
    with db.transaction(conn):
        for key, value in values.items():
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def publish_site(
    settings: Settings,
    data: DataPaths,
    *,
    clock: Clock,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
    log: logging.Logger | None = None,
    git_executable: str | None = None,
) -> PublishResult:
    """發布一次（呼叫前要持有執行鎖）。失敗時丟 PublishError（commit 留在本機）。"""
    log = log or _log
    config = resolve_config(settings)
    executable = git_executable or shutil.which("git")
    if executable is None:
        raise PublishError("找不到 git")
    git = Git(
        executable, config.site_dir, git_env(ssh_command(config.deploy_key, config.known_hosts))
    )
    now = clock.now()
    if not dry_run:
        _record(conn, {KV_LAST_ATTEMPT: to_iso(now)})
    try:
        state = read_repo(git, config.site_dir)
        check_remote(git, config.remote)
        if check_site_identity(git) and not dry_run:
            restore_worktree(git)
        files = read_export(data, state.object_format)
        plan = decide(state, files, published_meta(git, state), now)
        pending = pending_commits(git, state)
        build_id = files.manifest.build_id
        if dry_run:
            return PublishResult(
                plan.reason,
                plan.commit,
                False,
                pending + int(plan.commit),
                True,
                build_id,
                plan.write,
                plan.remove,
            )
        if plan.commit:
            _commit(git, state, files, plan, now)
            _verify_head(git, files, allow_meta=False)
            log.info("發布：已 commit build %s（原因：%s）", build_id, plan.reason)
        elif state.head is not None:
            _verify_head(git, files, allow_meta=True)
        pending = pending_commits(git, state)
        pushed = False
        if pending:
            _push(git, state.branch)
            pushed = True
            pending = pending_commits(git, state)
            log.info("發布：已推送")
    except PublishError as exc:
        if not dry_run:
            _record(conn, {KV_LAST_ERROR: exc.code.value})
        raise
    _record(conn, {KV_LAST_OK: to_iso(now), KV_LAST_ERROR: "", KV_PENDING: str(pending)})
    if not plan.commit and not pushed:
        log.info("發布：沒有變動（原因：%s）", plan.reason)
    return PublishResult(
        plan.reason, plan.commit, pushed, pending, False, build_id, plan.write, plan.remove
    )


def run_publish(*, dry_run: bool = False) -> int:
    """`house-watch publish [--dry-run]`（cli.ENTRY_POINTS["publish"]）。和 tick 用同一把執行鎖。"""
    from house_watch.cli import Ui, run_lock

    ui = Ui()
    settings = load_settings()
    ui.protect(settings)
    if not is_configured(settings):
        ui.error("settings.toml 沒有 [publish] site_repo_dir；發布的設定步驟見 README")
        return int(ExitCode.CONFIG)
    data = DataPaths(settings.data_dir)
    with run_lock(data):
        conn = None
        if not dry_run and data.db.is_file():
            try:
                conn = db.connect(data.db)
            except HouseWatchError:
                conn = None
        try:
            result = publish_site(settings, data, clock=SystemClock(), dry_run=dry_run, conn=conn)
        finally:
            if conn is not None:
                conn.close()
    for line in result.lines():
        ui.say(line)
    return int(result.exit_code)
