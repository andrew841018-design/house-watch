"""C10 指令：init、export、尚未實作的指令、參數錯誤、執行鎖、錯誤訊息的遮罩。"""

from pathlib import Path

import pytest
from filelock import FileLock
from hw_cli_support import data_paths, fake_module, isolate, use_entries, write_config

from house_watch import cli
from house_watch.export import load_manifest, manifest_path, site_dir
from house_watch.models import Meta


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)


def test_init_only_on_empty_dir(capsys):
    data = data_paths()
    assert cli.main(["init"]) == 0
    assert data.db.is_file() and data.instance_marker.is_file()
    assert "已建立" in capsys.readouterr().out
    assert cli.main(["init"]) == 0
    assert "已經初始化過" in capsys.readouterr().out


def test_init_refuses_foreign_dir(capsys):
    data = data_paths()
    data.root.mkdir(parents=True)
    (data.root / "someone_elses_file.txt").write_text("x", encoding="utf-8")
    assert cli.main(["init"]) == 2
    assert not data.db.exists()
    assert "instance 標記" in capsys.readouterr().err


def test_marker_without_database_is_a_data_dir_error():
    data = data_paths()
    assert cli.main(["init"]) == 0
    data.db.unlink()
    assert cli.main(["export"]) == 2
    assert cli.main(["update", "--only", "export"]) == 2


def test_export_on_empty_dir_twice(capsys):
    """自動初始化要在寫任何其他東西之前；第二次執行不能被自己的輸出擋住。"""
    data = data_paths()
    assert cli.main(["export"]) == 0
    assert cli.main(["export"]) == 0
    meta = Meta.model_validate_json((site_dir(data) / "data" / "meta.json").read_bytes())
    assert meta.status == "building"
    manifest = load_manifest(manifest_path(data))
    assert "index.html" in {f.path for f in manifest.files}
    assert not any(f.path.startswith("test/") for f in manifest.files)
    assert "build" in capsys.readouterr().out


def test_export_reports_missing_parts(monkeypatch, tmp_path, capsys):
    isolate(monkeypatch, tmp_path / "no_index", index=False)
    assert cli.main(["export"]) == 1
    assert "index.html not yet available" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["tick"],
        ["publish"],
        ["publish", "--dry-run"],
        ["schedule", "install"],
        ["schedule", "remove"],
    ],
)
def test_later_commands_are_stubs(argv, capsys):
    assert cli.main(argv) == 1
    assert "not yet available" in capsys.readouterr().err


def test_later_commands_delegate_when_available(monkeypatch):
    received = {}

    def run_publish(*, dry_run):
        received["dry_run"] = dry_run
        return 0

    use_entries(
        monkeypatch,
        publish=f"{fake_module(monkeypatch, 'hw_fake_publish', run_publish=run_publish)}:run_publish",
    )
    assert cli.main(["publish", "--dry-run"]) == 0
    assert received == {"dry_run": True}


@pytest.mark.parametrize(
    "argv",
    [[], ["nosuch"], ["serve", "--port", "x"], ["serve", "--port", "70000"], ["schedule", "start"]],
)
def test_bad_arguments_exit_2(argv):
    assert cli.main(argv) == 2


def test_version_and_help_exit_0(capsys):
    assert cli.main(["--version"]) == 0
    assert cli.main(["--help"]) == 0
    assert "house-watch" in capsys.readouterr().out


def test_config_error_exits_2_without_leaking_home(capsys):
    write_config("[profiles]\nname = '示意'\n")
    assert cli.main(["export"]) == 2
    err = capsys.readouterr().err
    assert "個人條件" in err
    assert str(Path.home()) not in err


@pytest.mark.parametrize("argv", [["init"], ["export"], ["update", "--only", "export"]])
def test_busy_lock_skips_with_exit_1(argv, capsys):
    data = data_paths()
    data.root.mkdir(parents=True)
    with FileLock(data.lock):  # 模擬排程正在跑
        assert cli.main(argv) == 1
    assert "正在執行" in capsys.readouterr().err
    assert not data.db.exists()


def test_unexpected_error_is_masked_and_exit_1(monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise OSError(f"寫不進去：{Path.home() / 'hw-not-real' / 'file'}")

    monkeypatch.setattr(cli, "export_site", explode)
    assert cli.main(["export"]) == 1
    err = capsys.readouterr().err
    assert "OSError" in err and "Traceback" in err
    assert str(Path.home()) not in err and "~/hw-not-real/file" in err
