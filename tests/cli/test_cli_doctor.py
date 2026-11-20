"""doctor：遮掉本機值、不建立任何東西、資料庫與來源狀態、結束碼。"""

from collections import namedtuple
from pathlib import Path

import pytest
from hw_cli_support import data_paths, isolate, write_config

from house_watch import cli, db

OWNER_VALUES = {
    "contact": "someone-else@example.invalid",
    "remote": "git@example.invalid:someone-else/site.git",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)


def _doctor(capsys) -> tuple[int, str]:
    code = cli.main(["doctor"])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_missing_data_dir_creates_nothing(capsys):
    data = data_paths()
    code, out = _doctor(capsys)
    assert code == 0
    assert "資料夾還不存在" in out
    assert not data.root.exists()


def test_empty_data_dir_creates_nothing(capsys):
    data = data_paths()
    data.root.mkdir(parents=True)
    code, out = _doctor(capsys)
    assert code == 0 and "資料夾是空的" in out
    assert list(data.root.iterdir()) == []


def test_owner_values_are_masked(capsys):
    home = Path.home()
    write_config(
        f'[http]\ncontact = "{OWNER_VALUES["contact"]}"\n'
        f'[publish]\nremote = "{OWNER_VALUES["remote"]}"\n'
        f'deploy_key = "{home / "hw-not-real" / "deploy_key"}"\n'
        f'known_hosts = "{home / "hw-not-real" / "known_hosts"}"\n'
        f'site_repo_dir = "{home / "hw-not-real" / "site"}"\n'
    )
    assert cli.main(["init"]) == 0
    code, out = _doctor(capsys)
    assert code == 0
    assert str(home) not in out
    assert "hw-not-real" not in out
    for value in OWNER_VALUES.values():
        assert value not in out
    assert "deploy_key 已設定" in out and "聯絡方式：已設定" in out
    assert "~" in out  # 資料夾路徑在家目錄底下，換成 ~


def test_reports_database_sources_and_runs(capsys):
    assert cli.main(["init"]) == 0
    conn = db.connect(data_paths().db)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO scans (scan_id, source, area, mode, started_at, finished_at, status,"
                " scope_hash, error_code) VALUES (41, 'hbhousing', 'taoyuan_core', 'full',"
                " '2026-06-01T00:00:00Z', '2026-06-01T01:00:00Z', 'invalid', 'h', 'count_short')"
            )
            conn.execute(
                "INSERT INTO source_state (source, paused_until, backoff_level, last_error)"
                " VALUES ('etwarm', NULL, 1, 'Traceback at /Users/someone-else/x.py')"
            )
    finally:
        conn.close()
    code, out = _doctor(capsys)
    assert code == 0
    assert "integrity_check：ok" in out and "foreign_key_check：ok" in out
    assert "#41 hbhousing taoyuan_core full invalid" in out
    assert "--accept-scan 41" in out
    assert "不是公開代碼，已隱藏" in out
    assert "Traceback" not in out and "someone-else" not in out


@pytest.mark.parametrize("damage", ["foreign_file", "missing_db", "garbage_db"])
def test_broken_data_dir_exits_2(capsys, damage):
    data = data_paths()
    if damage == "foreign_file":
        data.root.mkdir(parents=True)
        (data.root / "someone_elses_file.txt").write_text("x", encoding="utf-8")
    else:
        assert cli.main(["init"]) == 0
        data.db.unlink()
        if damage == "garbage_db":
            data.db.write_bytes(b"this is not sqlite" * 100)
    code, _ = _doctor(capsys)
    assert code == 2


def test_config_error_exits_2(capsys):
    write_config("price_max_wan = 1\n")
    code, out = _doctor(capsys)
    assert code == 2 and "個人條件" in out
    assert str(Path.home()) not in out


def test_low_disk_is_a_problem(monkeypatch, capsys):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: usage(10 << 30, 10 << 30, 1 << 20))
    code, out = _doctor(capsys)
    assert code == 1 and "空間不足" in out
