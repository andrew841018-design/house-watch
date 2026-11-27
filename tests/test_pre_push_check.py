"""推送前的隱私檢查（scripts/pre_push_check.py；事後審查 P-15）。全部用合成的值。"""

import importlib.util
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("pre_push_check", ROOT / "scripts" / "pre_push_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str, cwd: Path) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


def test_hits_are_listed_by_position_never_by_value(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("ok\n示意上限 9876萬\n", encoding="utf-8")
    (repo / "b.txt").write_text("沒有東西\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "提到 9876萬", cwd=repo)
    values = tmp_path / "private" / "sensitive.txt"
    values.parent.mkdir()
    values.write_text("# 示意清單\n9876萬\n", encoding="utf-8")
    module = _load()
    code = module.main([str(values), "HEAD"], repo=repo)
    out = capsys.readouterr()
    assert code == 1
    assert "a.txt:2" in out.out and "：message" in out.out and "：a.txt" in out.out  # HEAD、message、新增的行
    assert "9876" not in out.out + out.err


def test_clean_repo_exits_zero(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("合成資料\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    values = tmp_path / "sensitive.txt"
    values.write_text("9876萬\n", encoding="utf-8")
    assert _load().main([str(values), "HEAD"], repo=repo) == 0


def test_a_value_added_then_removed_in_the_range_is_still_caught(tmp_path, capsys):
    """修完再審 F1-10：commit A 加了值、commit B 刪掉；工作區乾淨，但 A 會跟著推上去。
    要掃範圍內每個 commit 新增的行，不是工作區。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("開始\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    (repo / "a.txt").write_text("開始\n示意 9876萬\n", encoding="utf-8")
    _git("commit", "-qam", "add", cwd=repo)
    (repo / "a.txt").write_text("開始\n", encoding="utf-8")
    _git("commit", "-qam", "remove", cwd=repo)
    values = tmp_path / "sensitive.txt"
    values.write_text("9876萬\n", encoding="utf-8")
    code = _load().main([str(values), "HEAD~2..HEAD"], repo=repo)
    out = capsys.readouterr()
    assert code == 1 and "a.txt" in out.out and "9876" not in out.out + out.err
