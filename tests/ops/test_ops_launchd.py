"""launchd 的 plist 與 deploy-local（WP7b）：只寫檔、不執行 launchctl；固定版本從 git ref 建。

這些測試不需要 macOS：平台、LaunchAgents 資料夾、執行區、log 資料夾都由測試注入。
"""

import datetime as dt
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from house_watch import tick
from house_watch.clock import TAIPEI, FakeClock
from house_watch.errors import ConfigError
from house_watch.ops import launchd
from house_watch.ops.launchd import RuntimeLayout

TODAY = dt.date(2026, 11, 21)


def _fake_runtime(root: Path) -> RuntimeLayout:
    layout = RuntimeLayout(root)
    layout.bin_dir.mkdir(parents=True)
    layout.executable.write_text("#!/bin/sh\n", encoding="utf-8")
    return layout


@pytest.fixture
def no_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"不應該執行任何外部指令：{args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _install(tmp_path: Path, **overrides):
    kwargs = {
        "window": (8, 23),
        "every_hours": 3,
        "layout": _fake_runtime(tmp_path / "runtime"),
        "logs": tmp_path / "logs",
        "agents_dir": tmp_path / "LaunchAgents",
        "local_tz": TAIPEI,
        "today": TODAY,
        "uid": 501,
        "home": tmp_path / "home",  # pytest 的暫存資料夾在 repo 裡（可能在 ~/Desktop）
    }
    kwargs.update(overrides)
    return launchd.install_schedule(**kwargs)


# ---------------------------------------------------------------- 時間


def test_calendar_hours_every_three_hours_in_the_window():
    assert launchd.calendar_hours((8, 23), 3) == [8, 11, 14, 17, 20, 23]
    assert len(launchd.calendar_hours((8, 23), 1)) == 16
    with pytest.raises(ConfigError):
        launchd.calendar_hours((8, 23), 0)


def test_taipei_hours_are_converted_to_the_local_time_zone():
    hours = launchd.calendar_hours((8, 23), 3)
    assert launchd.local_times(hours, today=TODAY, local_tz=TAIPEI) == [(h, 0) for h in hours]
    west = launchd.local_times(hours, today=TODAY, local_tz=ZoneInfo("America/Los_Angeles"))
    assert (16, 0) in west and (7, 0) in west  # 台北 08:00＝洛杉磯前一天 16:00；23:00＝07:00


# ---------------------------------------------------------------- plist


def test_install_writes_the_plist_without_running_anything(tmp_path, no_subprocess):
    result = _install(tmp_path)
    content = plistlib.loads(result.plist.read_bytes())
    layout = RuntimeLayout(tmp_path / "runtime")
    assert content["Label"] == launchd.LABEL == "com.house-watch.tick"
    assert content["ProgramArguments"] == [str(layout.executable), "tick"]
    assert content["StartCalendarInterval"] == [
        {"Hour": h, "Minute": 0} for h in (8, 11, 14, 17, 20, 23)
    ]
    assert content["WorkingDirectory"] == str(layout.root)
    assert content["EnvironmentVariables"]["PATH"].split(":")[0] == "/usr/bin"
    assert content["StandardOutPath"] == str(tmp_path / "logs" / "launchd.out.log")
    assert content["StandardErrorPath"] == str(tmp_path / "logs" / "launchd.err.log")
    assert content["RunAtLoad"] is False and "KeepAlive" not in content
    assert "ProcessType" not in content  # 不降低優先權：4 秒間隔的掃描不能被拉長
    assert (tmp_path / "logs").is_dir()  # launchd 不會自己建 log 資料夾
    bootout, bootstrap = result.commands
    assert bootstrap == f"launchctl bootstrap gui/501 '{result.plist}'"
    assert bootout == "launchctl bootout gui/501/com.house-watch.tick"
    assert result.changed


def test_reinstall_with_same_content_reports_no_change(tmp_path, no_subprocess):
    first = _install(tmp_path)
    layout = RuntimeLayout(tmp_path / "runtime")
    second = launchd.install_schedule(
        window=(8, 23),
        every_hours=3,
        layout=layout,
        logs=tmp_path / "logs",
        agents_dir=tmp_path / "LaunchAgents",
        local_tz=TAIPEI,
        today=TODAY,
        uid=501,
        home=tmp_path / "home",
    )
    assert first.changed and not second.changed


def test_install_needs_the_pinned_runtime(tmp_path, no_subprocess):
    with pytest.raises(ConfigError, match="deploy-local"):
        _install(tmp_path, layout=RuntimeLayout(tmp_path / "empty"))
    assert not (tmp_path / "LaunchAgents").exists()


def test_install_refuses_the_development_folder(tmp_path, no_subprocess):
    dev = tmp_path / "dev_repo"
    with pytest.raises(ConfigError, match="開發資料夾"):
        _install(tmp_path, layout=_fake_runtime(dev / "runtime"), dev_root=dev)


def test_protected_locations():
    home = Path.home()
    assert launchd.protected_location(home / "Desktop" / "x" / "house-watch") is not None
    assert launchd.protected_location(Path("/Volumes/disk/house-watch")) is not None
    assert launchd.protected_location(home / "Library" / "Application Support" / "x") is None


def test_remove_deletes_the_plist_and_prints_bootout(tmp_path, no_subprocess):
    result = _install(tmp_path)
    existed, command = launchd.remove_schedule(agents_dir=tmp_path / "LaunchAgents", uid=501)
    assert existed and not result.plist.exists()
    assert command == "launchctl bootout gui/501/com.house-watch.tick"
    assert launchd.remove_schedule(agents_dir=tmp_path / "LaunchAgents", uid=501)[0] is False


def test_schedule_command_is_mac_only(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert launchd.run_schedule(action="install") == 2
    assert "macOS" in capsys.readouterr().err


def test_schedule_command_end_to_end(monkeypatch, tmp_path, capsys, no_subprocess):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(launchd.ENV_RUNTIME_DIR, str(tmp_path / "runtime"))
    monkeypatch.setenv("HOUSE_WATCH_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(launchd, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "TCC_HOME_DIRS", ())  # pytest 的暫存資料夾可能在 ~/Desktop
    monkeypatch.setattr(launchd, "source_checkout", lambda: None)
    _fake_runtime(tmp_path / "runtime")
    assert launchd.run_schedule(action="install", every_hours=None) == 0
    out = capsys.readouterr().out
    assert "launchctl bootstrap gui/" in out and "沒有載入或啟動" in out
    assert launchd.run_schedule(action="remove") == 0
    assert "launchctl bootout gui/" in capsys.readouterr().out


# ---------------------------------------------------------------- 排程沿用安裝當下的路徑

SCHEDULE_KEYS = (
    "HOUSE_WATCH_CONFIG",
    "HOUSE_WATCH_DATA_DIR",
    "HOUSE_WATCH_LOG_DIR",
    "HOUSE_WATCH_RUNTIME_DIR",
)


def test_schedule_command_carries_the_installer_paths_into_the_plist(
    monkeypatch, tmp_path, capsys, no_subprocess
):
    """排程跑 tick 時沒有安裝當下的環境變數。四個路徑沒寫進 plist 的話，tick 會改用預設位置：
    讀另一份設定、寫另一個資料夾，等於換了一套資料（11/21 上線時手動補過）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(launchd.ENV_RUNTIME_DIR, str(tmp_path / "runtime"))
    monkeypatch.setenv("HOUSE_WATCH_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(launchd, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "TCC_HOME_DIRS", ())  # pytest 的暫存資料夾可能在 ~/Desktop
    monkeypatch.setattr(launchd, "source_checkout", lambda: None)
    _fake_runtime(tmp_path / "runtime")
    assert launchd.run_schedule(action="install", every_hours=None) == 0
    capsys.readouterr()
    written = plistlib.loads(launchd.plist_path(tmp_path / "LaunchAgents").read_bytes())
    env = written["EnvironmentVariables"]
    assert {key: env.get(key) for key in SCHEDULE_KEYS} == {
        key: os.environ[key] for key in SCHEDULE_KEYS
    }
    assert env["PATH"] == launchd.LAUNCHD_PATH and env["LANG"] == "en_US.UTF-8"


def test_schedule_command_runs_every_hour_by_default(monkeypatch, tmp_path, capsys, no_subprocess):
    """r4 §3.5：時段內每小時觸發一次（08–23 點共 16 次）。探測每 3 小時是 pipeline.scan 的事，
    不是排程的頻率；沒加 --every-hours 重裝時不能悄悄降成每 3 小時。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(launchd.ENV_RUNTIME_DIR, str(tmp_path / "runtime"))
    monkeypatch.setenv("HOUSE_WATCH_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(launchd, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "TCC_HOME_DIRS", ())
    monkeypatch.setattr(launchd, "source_checkout", lambda: None)
    _fake_runtime(tmp_path / "runtime")
    assert launchd.run_schedule(action="install", every_hours=None) == 0
    capsys.readouterr()
    written = plistlib.loads(launchd.plist_path(tmp_path / "LaunchAgents").read_bytes())
    times = written["StartCalendarInterval"]
    assert len(times) == 16 and len({(t["Hour"], t["Minute"]) for t in times}) == 16


def test_schedule_environment_writes_set_paths_as_absolute(tmp_path):
    """相對路徑以安裝當下的工作目錄為準（排程的工作目錄是執行區）；~ 展開；
    沒設、空字串的不寫（排程端會用和安裝端一樣的預設）；其他變數一律不帶。"""
    environ = {
        "HOUSE_WATCH_CONFIG": "conf/settings.toml",
        "HOUSE_WATCH_DATA_DIR": str(tmp_path / "data"),
        "HOUSE_WATCH_LOG_DIR": "~/hw-logs",
        "HOUSE_WATCH_RUNTIME_DIR": "",
        "HOUSE_WATCH_OTHER": "x",
        "PATH": "/opt/other/bin",
    }
    assert launchd.schedule_environment(environ, cwd=tmp_path / "work") == {
        "HOUSE_WATCH_CONFIG": str(tmp_path / "work" / "conf" / "settings.toml"),
        "HOUSE_WATCH_DATA_DIR": str(tmp_path / "data"),
        "HOUSE_WATCH_LOG_DIR": str(Path("~/hw-logs").expanduser()),
    }


def test_install_writes_the_given_paths_into_the_plist(tmp_path, no_subprocess):
    env = {
        "HOUSE_WATCH_CONFIG": str(tmp_path / "conf" / "settings.toml"),
        "HOUSE_WATCH_DATA_DIR": str(tmp_path / "data"),
    }
    result = _install(tmp_path, env=env)
    written = plistlib.loads(result.plist.read_bytes())["EnvironmentVariables"]
    assert written == {"PATH": launchd.LAUNCHD_PATH, "LANG": "en_US.UTF-8", **env}


def test_plist_only_takes_the_house_watch_paths(tmp_path):
    with pytest.raises(ValueError, match="PATH"):
        launchd.build_plist(
            executable=tmp_path / "house-watch",
            working_dir=tmp_path,
            logs=tmp_path,
            times=[(8, 0)],
            env={"PATH": "/opt/other/bin"},
        )


def test_install_refuses_config_or_data_in_protected_locations(tmp_path, no_subprocess):
    """設定檔、資料夾也是 tick 要讀寫的；放在隱私保護的位置，排程會跑不起來。"""
    cases = [
        ("config_path", "home/Desktop/settings.toml", "^設定檔在 ~/Desktop"),
        ("data_dir", "home/Documents/data", "^資料夾在 ~/Documents"),
        ("data_dir", "/Volumes/disk/data", "^資料夾在 /Volumes"),
    ]
    for index, (param, where, pattern) in enumerate(cases):
        base = tmp_path / str(index)  # _install 的 home 是 base/home
        with pytest.raises(ConfigError, match=pattern):
            _install(base, **{param: base / where})
        assert not (base / "LaunchAgents").exists()


def test_install_and_logging_do_not_block_the_first_tick(monkeypatch, tmp_path):
    """資料夾在第一次 init 之前只能是空的：log、執行區、plist 都不能放進資料夾。"""
    data = Path(os.environ["HOUSE_WATCH_DATA_DIR"])
    _install(tmp_path)
    assert not data.exists()
    clock = FakeClock(dt.datetime(2026, 11, 21, 0, 0, tzinfo=dt.UTC))
    for name in ("doorindex", "lvr", "sources"):
        monkeypatch.setitem(tick.cli.STEP_FUNCTIONS, name, lambda ctx: tick.cli.StepOutcome())
    code = tick.run_tick(
        clock=clock,
        log_directory=tmp_path / "logs",
        console=False,
        export_step=lambda ctx: tick.cli.StepOutcome(),
    )
    assert code == 0 and (data / "house_watch.sqlite").is_file()


# ---------------------------------------------------------------- deploy-local


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "code"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (root / ".python-version").write_text("3.13\n", encoding="utf-8")
    (root / "uv.lock").write_text("# 示意\n", encoding="utf-8")
    (root / "marker.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "x", cwd=root)
    _git("tag", "v0.0.1", cwd=root)
    (root / "marker.txt").write_text("改到一半\n", encoding="utf-8")  # 不能被裝進去
    return root


class FakeUv:
    """假的 uv 與 house-watch 執行檔；git 用真的。"""

    def __init__(self, layout: RuntimeLayout) -> None:
        self.layout = layout
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, argv, *, cwd=None, env=None):
        argv = list(argv)
        if Path(argv[0]).name == "git":
            return launchd.run_command(argv, cwd=cwd, env=env)
        self.calls.append((argv, dict(env) if env is not None else None))
        if argv[0] == str(self.layout.executable):
            return subprocess.CompletedProcess(argv, 0, b"house-watch 0.0.1\n", b"")
        if argv[1] == "export":
            Path(argv[argv.index("--output-file") + 1]).write_text("pkg==1\n", encoding="utf-8")
        elif argv[1] == "build":
            out = Path(argv[argv.index("--out-dir") + 1])
            (out / "house_watch-0.0.1-py3-none-any.whl").write_bytes(b"wheel")
        elif argv[1:3] == ["tool", "install"]:
            self.layout.bin_dir.mkdir(parents=True, exist_ok=True)
            self.layout.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_deploy_local_builds_the_ref_into_the_runtime(tmp_path, repo):
    layout = RuntimeLayout(tmp_path / "runtime")
    fake = FakeUv(layout)
    result = launchd.deploy_local("v0.0.1", repo=repo, layout=layout, run=fake, uv="uv")
    assert result.tagged and result.version == "0.0.1"
    assert result.commit == _git("rev-parse", "v0.0.1^{commit}", cwd=repo)
    build = layout.builds_dir / result.commit[:12]
    assert (build / "marker.txt").read_text(encoding="utf-8") == "committed\n"  # 從 ref，不是工作區
    install = next(argv for argv, _ in fake.calls if argv[1:3] == ["tool", "install"])
    assert "--force" in install and install[install.index("--python") + 1] == "3.13"
    assert install[install.index("--constraints") + 1] == str(build / "constraints.txt")
    assert install[-1].endswith(".whl")
    env = next(env for argv, env in fake.calls if argv[1:3] == ["tool", "install"])
    assert env["UV_TOOL_DIR"] == str(layout.tools_dir)
    assert env["UV_TOOL_BIN_DIR"] == str(layout.bin_dir)
    assert env["UV_PYTHON_INSTALL_DIR"] == str(layout.python_dir)
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    record = layout.deployed.read_text(encoding="utf-8")
    assert '"ref": "v0.0.1"' in record and result.commit in record


def test_deploy_local_rejects_unknown_refs(tmp_path, repo):
    layout = RuntimeLayout(tmp_path / "runtime")
    with pytest.raises(launchd.DeployError):
        launchd.deploy_local("no-such-tag", repo=repo, layout=layout, run=FakeUv(layout), uv="uv")
    with pytest.raises(launchd.DeployError):
        launchd.deploy_local("--all", repo=repo, layout=layout, run=FakeUv(layout), uv="uv")


def test_deploy_local_waits_for_a_running_tick(monkeypatch, tmp_path, capsys):
    """事後審查 SA-6：換版（uv tool install --force）和正在跑的 tick 重疊時，同一個程序會新舊
    模組混用，甚至把「模組不見」當成「資料還沒做好」而發布空網站。deploy-local 要拿同一把
    執行鎖，拿不到就不換。"""
    from filelock import FileLock

    from house_watch.paths import DataPaths

    data = DataPaths(Path(os.environ["HOUSE_WATCH_DATA_DIR"]))
    data.root.mkdir(parents=True)
    monkeypatch.setenv(launchd.ENV_RUNTIME_DIR, str(tmp_path / "runtime"))
    monkeypatch.setattr(launchd, "TCC_HOME_DIRS", ())  # pytest 的暫存資料夾可能在 ~/Desktop
    calls = []
    monkeypatch.setattr(launchd, "deploy_local", lambda *a, **k: calls.append(a))
    held = FileLock(data.lock)
    held.acquire()
    try:
        code = launchd.run_deploy_local(ref="v9", repo=str(tmp_path))
    finally:
        held.release()
    assert code != 0 and calls == []
    assert "執行鎖" in capsys.readouterr().err


def test_install_refuses_publish_files_in_protected_locations(tmp_path, no_subprocess):
    """事後審查 SA-2：發布用的網站 repo、deploy key、known_hosts 也是排程要讀寫的；
    放在隱私保護的位置，排程每小時都會發布失敗。"""
    base = tmp_path / "x"
    with pytest.raises(ConfigError, match="^網站 repo在 ~/Desktop"):
        _install(base, other_paths=[("網站 repo", base / "home" / "Desktop" / "site")])
    assert not (base / "LaunchAgents").exists()


def test_schedule_install_refuses_without_a_settings_file(monkeypatch, tmp_path, capsys, no_subprocess):
    """修完再審 F1-8：找不到本機設定檔時，裝出來的排程每小時都會以結束碼 2 停止（tick 的檢查）。
    所以安裝時就拒絕，不寫 plist。"""
    from house_watch import paths

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("HOUSE_WATCH_CONFIG")
    monkeypatch.setattr(paths, "default_config_dir", lambda: tmp_path / "no_config")
    monkeypatch.setenv(launchd.ENV_RUNTIME_DIR, str(tmp_path / "runtime"))
    monkeypatch.setenv("HOUSE_WATCH_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(launchd, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "TCC_HOME_DIRS", ())
    monkeypatch.setattr(launchd, "source_checkout", lambda: None)
    _fake_runtime(tmp_path / "runtime")
    assert launchd.run_schedule(action="install", every_hours=None) == 2
    assert "設定檔" in capsys.readouterr().err
    assert not (tmp_path / "LaunchAgents").exists()
