"""發布到網站 repo（WP7c）：只對本機的 bare repo 測試，不連網路。

涵蓋：只發布 manifest 上的檔、心跳規則、絕不 force push、push 失敗下一輪重試、dry-run 不寫入、
git 與使用者設定隔開、GIT_SSH_COMMAND 的寫法。
"""

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from filelock import FileLock

from house_watch import cli, db, publish
from house_watch.clock import FakeClock, to_iso
from house_watch.errors import ErrorCode, ExitCode
from house_watch.export import Manifest, ManifestFile
from house_watch.models import MetaStatus
from house_watch.paths import DataPaths
from house_watch.settings import load_settings

T0 = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)


def git(*args: str, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        env=publish.git_env(),
        capture_output=True,
        check=check,
    )
    return result.stdout.decode().strip()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.remote = tmp_path / "remote.git"
        self.site = tmp_path / "site clone"
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=tmp_path)
        git("clone", "-q", str(self.remote), str(self.site), cwd=tmp_path)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.site)
        keys = tmp_path / "keys dir"  # 路徑有空白（Application Support 也有）
        keys.mkdir()
        self.key = keys / "deploy key"
        self.key.write_text("示意，不是真的金鑰\n", encoding="utf-8")
        self.key.chmod(0o600)
        self.known = keys / "known hosts"
        self.known.write_text("example.invalid ssh-ed25519 AAAA示意\n", encoding="utf-8")
        self.write_config()
        self.data = DataPaths(Path(os.environ["HOUSE_WATCH_DATA_DIR"]))
        self.clock = FakeClock(T0)

    def write_config(self, **overrides: str) -> None:
        values = {
            "site_repo_dir": str(self.site),
            "remote": str(self.remote),
            "deploy_key": str(self.key),
            "known_hosts": str(self.known),
        }
        values.update(overrides)
        body = "".join(f'{key} = "{value}"\n' for key, value in values.items() if value)
        Path(os.environ["HOUSE_WATCH_CONFIG"]).write_text(f"[publish]\n{body}", encoding="utf-8")

    def export(
        self,
        *,
        listings: str = "[1]",
        last_ok: dt.datetime = T0,
        error_code: str | None = None,
        generated_at: dt.datetime | None = None,
        files: dict[str, bytes] | None = None,
        skip: tuple[str, ...] = (),
    ) -> str:
        """寫一份合成的匯出（export/site 與 manifest.json），回傳 build id。"""
        site = self.data.export_site_dir
        if site.exists():
            for path in sorted(site.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
        content = {
            "index.html": b"<!doctype html><title>x</title>\n",
            "app.js": b"export const x = 1;\n",
            ".nojekyll": b"",
            "data/listings_core.json": listings.encode(),
            **(files or {}),
        }
        build_id = hashlib.sha256(
            json.dumps(sorted(content.items(), key=str), default=str).encode()
        ).hexdigest()[:16]
        meta = {
            "schema_version": 1,
            "build_id": build_id,
            "generated_at": to_iso(generated_at or last_ok),
            "status": "ready",
            "sources": [
                {
                    "id": "cthouse",
                    "publish": True,
                    "last_ok": to_iso(last_ok),
                    "last_full_ok": to_iso(last_ok),
                    "paused_until": None,
                    "error_code": error_code,
                }
            ],
            "datasets": {
                "lvr": {"last_check": to_iso(T0), "as_of": "2026-11-01"},
                "doorindex": {"last_check": to_iso(T0), "version": "示意版"},
            },
            "areas": [],
            "freshness_hours": {"cthouse": 12, "lvr": 36, "doorindex": 1080},
        }
        content["data/meta.json"] = (json.dumps(meta, sort_keys=True) + "\n").encode()
        entries = []
        for rel, payload in sorted(content.items()):
            path = site / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if rel not in skip:
                entries.append(
                    ManifestFile(
                        path=rel, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
                    )
                )
        manifest = Manifest(
            build_id=build_id,
            generated_at=generated_at or last_ok,
            status=MetaStatus.READY,
            files=entries,
        )
        self.data.export_manifest.write_text(manifest.model_dump_json() + "\n", encoding="utf-8")
        return build_id

    def publish(self, *, dry_run: bool = False, conn=None) -> publish.PublishResult:
        return publish.publish_site(
            load_settings(), self.data, clock=self.clock, dry_run=dry_run, conn=conn
        )

    def remote_files(self) -> set[str]:
        out = git("ls-tree", "-r", "--name-only", "main", cwd=self.remote, check=False)
        return set(out.splitlines()) if out else set()

    def remote_commits(self) -> int:
        out = git("rev-list", "--count", "main", cwd=self.remote, check=False)
        return int(out) if out else 0


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------- 只發布清單上的檔


def test_first_publish_pushes_only_manifest_files(env):
    env.export(files={"raw.html": b"<p>debug</p>"}, skip=("raw.html",))  # 清單外的檔
    # clone 裡多出來的檔不再自動刪掉，而是停下來（事後審查 SA-5，見下面兩個測試）。
    result = env.publish()
    assert result.committed and result.pushed and result.reason == "first"
    expected = {".nojekyll", "app.js", "data/listings_core.json", "data/meta.json", "index.html"}
    assert env.remote_files() == expected
    message = git("log", "-1", "--format=%s", "main", cwd=env.remote)
    assert re.fullmatch(r"site \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z build [0-9a-f]{16}", message)
    author = git("log", "-1", "--format=%an <%ae>|%cn <%ce>", "main", cwd=env.remote)
    assert (
        author
        == "house-watch <noreply@house-watch.invalid>|house-watch <noreply@house-watch.invalid>"
    )
    assert git("config", "--local", "user.name", cwd=env.site) == "house-watch"
    assert result.pending == 0


def test_unchanged_export_makes_no_commit(env):
    env.export()
    env.publish()
    result = env.publish()
    assert not result.committed and not result.pushed and result.reason == "unchanged"
    assert env.remote_commits() == 1


def test_data_change_commits_and_removed_files_disappear(env):
    env.export(files={"data/tx_core.json": b"[]"})
    env.publish()
    env.clock.advance(3600)
    env.export(listings="[1, 2]")  # tx_core.json 不在新的清單裡
    result = env.publish()
    assert result.reason == "data" and result.committed
    assert "data/tx_core.json" in result.remove
    assert "data/tx_core.json" not in env.remote_files()
    assert env.remote_commits() == 2


# ---------------------------------------------------------------- 心跳規則


def test_timestamp_only_changes_follow_the_heartbeat_rule(env):
    env.export(last_ok=T0)
    env.publish()

    env.clock.advance(3600)  # 已公開的 last_ok 才 1 小時，門檻 12 小時的一半是 6 小時
    env.export(last_ok=T0 + dt.timedelta(hours=1))
    assert env.publish().reason == "unchanged"

    env.clock.advance(6 * 3600)  # 已公開的 last_ok 已經 7 小時
    env.export(last_ok=T0 + dt.timedelta(hours=7))
    result = env.publish()
    assert result.reason == "heartbeat" and result.committed and result.write == ("data/meta.json",)

    # 來源停擺：last_ok 沒有變新，只是 generated_at 一直在走 → 不要每一輪都 commit
    env.clock.advance(13 * 3600)
    env.export(last_ok=T0 + dt.timedelta(hours=7), generated_at=env.clock.now())
    assert env.publish().reason == "unchanged"
    assert env.remote_commits() == 2


def test_meta_change_other_than_timestamps_commits(env):
    env.export()
    env.publish()
    env.export(error_code="blocked", generated_at=T0 + dt.timedelta(minutes=5))
    result = env.publish()
    assert result.reason == "meta" and result.committed


def test_heartbeat_rule_unit():
    old = {"sources": [{"id": "a", "last_ok": "2026-11-21T00:00:00Z"}], "datasets": {}}
    new = {
        "sources": [{"id": "a", "last_ok": "2026-11-21T07:00:00Z"}],
        "datasets": {},
        "freshness_hours": {"a": 12},
    }
    assert publish.heartbeat_due(old, new, dt.datetime(2026, 11, 21, 7, tzinfo=dt.UTC))
    assert not publish.heartbeat_due(old, new, dt.datetime(2026, 11, 21, 5, tzinfo=dt.UTC))
    assert not publish.heartbeat_due(new, new, dt.datetime(2026, 11, 22, tzinfo=dt.UTC))


# ---------------------------------------------------------------- dry-run、push 失敗與重試


def test_dry_run_writes_nothing(env):
    env.export()
    env.publish()
    env.export(listings="[1, 2, 3]")
    before_site, before_remote = _tree_digest(env.site), _tree_digest(env.remote)
    result = env.publish(dry_run=True)
    assert result.dry_run and result.committed and result.reason == "data" and result.pending == 1
    assert _tree_digest(env.site) == before_site and _tree_digest(env.remote) == before_remote
    assert any("dry-run" in line for line in result.lines())


def test_non_fast_forward_is_rejected_and_never_forced(env, tmp_path):
    env.export()
    env.publish()
    other = tmp_path / "other"
    git("clone", "-q", str(env.remote), str(other), cwd=tmp_path)
    (other / "index.html").write_text("別人推的\n", encoding="utf-8")
    git("commit", "-q", "-am", "other", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    theirs = git("rev-parse", "main", cwd=env.remote)

    env.export(listings="[9]")
    with pytest.raises(publish.PublishError) as caught:
        env.publish()
    assert (
        caught.value.code is ErrorCode.PUSH_REJECTED and caught.value.exit_code == ExitCode.PARTIAL
    )
    assert git("rev-parse", "main", cwd=env.remote) == theirs  # 遠端沒有被蓋掉
    assert git("rev-list", "--count", "origin/main..HEAD", cwd=env.site) == "1"  # commit 留在本機


def test_remote_hook_rejection_is_push_rejected(env):
    hook = env.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    env.export()
    with pytest.raises(publish.PublishError) as caught:
        env.publish()
    assert caught.value.code is ErrorCode.PUSH_REJECTED


def test_failed_push_is_retried_on_the_next_run(env, tmp_path):
    conn = db.open_or_init(env.data)  # tick 會把資料庫連線傳進來，記錄發布狀態
    try:
        env.export()
        env.publish(conn=conn)
        moved = tmp_path / "moved.git"
        env.remote.rename(moved)  # 遠端暫時連不到
        env.export(listings="[5]")
        with pytest.raises(publish.PublishError) as caught:
            env.publish(conn=conn)
        assert caught.value.code is ErrorCode.PUBLISH_FAILED
        assert db_value(conn, publish.KV_LAST_ERROR) == "publish_failed"
        moved.rename(env.remote)
        env.clock.advance(3 * 3600)
        result = env.publish(conn=conn)  # 匯出沒變：不新 commit，只把上次的推上去
        assert result.pushed and not result.committed and result.pending == 0
        assert db_value(conn, publish.KV_LAST_ERROR) == ""
        assert db_value(conn, publish.KV_LAST_OK) == to_iso(env.clock.now())
    finally:
        conn.close()
    assert env.remote_commits() == 2


def db_value(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------- 設定與安全


def test_ssh_command_isolates_ssh_and_quotes_paths():
    command = publish.ssh_command(Path("/a b/deploy key"), Path("/c d/known hosts"))
    argv = shlex.split(command)
    assert argv[:5] == ["ssh", "-F", os.devnull, "-i", "/a b/deploy key"]
    options = {argv[i + 1] for i, part in enumerate(argv) if part == "-o"}
    assert {
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        'UserKnownHostsFile="/c d/known hosts"',
        "StrictHostKeyChecking=yes",
        "BatchMode=yes",
    } <= options
    env = publish.git_env(command)
    assert env["GIT_SSH_COMMAND"] == command and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_git_env_drops_inherited_git_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/somewhere/else")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")
    env = publish.git_env()
    assert "GIT_DIR" not in env and "SSH_AUTH_SOCK" not in env


def test_users_global_git_config_is_ignored(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[user]\n\temail = owner@example.invalid\n[commit]\n\tgpgsign = true\n"
        f"[core]\n\thooksPath = {hooks}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    env.export()
    assert env.publish().pushed
    assert "owner@example.invalid" not in git(
        "log", "-1", "--format=%ae%ce", "main", cwd=env.remote
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"deploy_key": ""}, "deploy_key"),
        ({"known_hosts": ""}, "known_hosts"),
        ({"remote": "git@example.invalid:someone-else/other.git"}, "publish.remote"),
        ({"remote": ""}, "remote"),  # 事後審查 SA-5：remote 必填，用來確認推的是網站 repo
    ],
)
def test_configuration_errors_exit_2(env, change, message):
    env.write_config(**change)
    env.export()
    with pytest.raises(publish.PublishError, match=message) as caught:
        env.publish()
    assert caught.value.exit_code == ExitCode.CONFIG
    assert env.remote_commits() == 0


def test_a_repo_with_other_peoples_commits_is_never_synced(env):
    """事後審查 SA-5、P-4：site_repo_dir 指錯（例如程式 repo）時，同步會刪掉清單外的檔。
    網站 repo 的 commit 全部是發布程式做的，才可以同步；不是就停下，不刪任何東西。"""
    (env.site / "notes.md").write_text("別人的檔\n", encoding="utf-8")
    git("add", "notes.md", cwd=env.site)
    git("-c", "user.name=someone", "-c", "user.email=s@example.invalid", "commit", "-qm", "x", cwd=env.site)
    (env.site / "draft.txt").write_text("還沒 commit\n", encoding="utf-8")
    env.export()
    with pytest.raises(publish.PublishError, match="不是發布程式做的") as caught:
        env.publish()
    assert caught.value.exit_code == ExitCode.CONFIG
    assert (env.site / "notes.md").is_file() and (env.site / "draft.txt").is_file()


def test_strays_in_a_verified_site_clone_are_cleaned_before_sync(env):
    """修完再審 F1-4：作者與 remote 都確認是網站 repo 之後，工作區多出來的檔（Finder 的
    .DS_Store、上一輪中途斷掉留下的改動）先還原到上一次發布的狀態再同步；不能永遠停在這裡。"""
    env.export()
    assert env.publish().pushed
    (env.site / ".DS_Store").write_bytes(b"\0")
    (env.site / "index.html").write_text("上一輪中途斷掉留下的改動\n", encoding="utf-8")
    env.export(listings="[2]")
    result = env.publish()
    assert result.committed and result.pushed
    assert not (env.site / ".DS_Store").exists()
    assert ".DS_Store" not in env.remote_files()


def test_deploy_key_must_be_private(env):
    env.key.chmod(0o644)
    env.export()
    with pytest.raises(publish.PublishError, match="600"):
        env.publish()


def test_export_must_match_its_manifest(env):
    env.export(skip=(".nojekyll",))
    with pytest.raises(publish.PublishError, match=".nojekyll"):
        env.publish()
    env.export()
    (env.data.export_site_dir / "app.js").write_text("改過\n", encoding="utf-8")
    with pytest.raises(publish.PublishError, match="manifest"):
        env.publish()
    assert env.remote_commits() == 0


# ---------------------------------------------------------------- 指令


def test_publish_command_dry_run_and_lock(env, capsys):
    env.export()
    assert cli.main(["publish", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and str(env.site) not in out
    env.data.root.mkdir(parents=True, exist_ok=True)
    with FileLock(env.data.lock):  # 排程正在跑
        assert cli.main(["publish"]) == 1
    assert env.remote_commits() == 0


def test_publish_command_without_configuration_exits_2(capsys):
    Path(os.environ["HOUSE_WATCH_CONFIG"]).write_text("", encoding="utf-8")
    assert cli.main(["publish"]) == 2
    assert "README" in capsys.readouterr().err


def test_a_stale_index_lock_in_a_verified_clone_is_removed(env):
    """修完再審 F2-4：git 拿著 .git/index.lock 時被強制結束（看門狗、斷電），之後每一輪的
    reset 都會失敗、發布一直停著。身分確認後，夠舊的 index.lock 刪掉再繼續。"""
    import os
    import time

    env.export()
    assert env.publish().pushed
    stale = env.site / ".git" / "index.lock"
    stale.write_bytes(b"")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    env.export(listings="[3]")
    assert env.publish().pushed
    assert not stale.exists()
