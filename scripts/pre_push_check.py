"""推送前的隱私檢查（r4 §3.1；事後審查 P-15、修完再審 F1-10）。

用法（在程式 repo 裡執行）：

    python3 scripts/pre_push_check.py <敏感值清單檔> [<git 範圍，例如 origin/main..HEAD>]

- 清單檔放在私人工作區，由擁有者自己維護：一行一個值，只寫有辨識度的寫法
  （例如條件的「上限值＋單位」）；空行與 # 開頭的行略過。清單本身不能放進 repo。
- 掃描 HEAD 裡的每個文字檔（不是工作區：要推出去的是 commit）。
- 給了範圍時，另外掃描範圍內每個 commit「新增的行」與 commit message：中間 commit 加了、
  後來又刪掉的值，照樣會跟著推上去。
- 命中時只印出「檔名:行號」「commit <短 hash>：檔名」或「commit <短 hash>：message」，不印出值；
  有命中結束碼 1，沒有是 0。
"""

import subprocess
import sys
from pathlib import Path


def load_values(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def scan_head(repo: Path, values: list[str]) -> list[str]:
    """HEAD 裡每個文字檔的每一行。"""
    hits = []
    for raw in _git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=repo).split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        blob = _git("show", f"HEAD:{rel}", cwd=repo)
        if b"\0" in blob[:8000]:
            continue  # 二進位檔
        text = blob.decode("utf-8", "replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if any(value in line for value in values):
                hits.append(f"{rel}:{number}")
    return hits


def scan_commits(repo: Path, values: list[str], revisions: str) -> list[str]:
    """範圍內每個 commit 新增的行，以及 commit message。"""
    hits: list[str] = []
    log = _git(
        "log", "--format=%x01%h%x00%B%x02", "-p", "--unified=0", "--no-color", revisions, cwd=repo
    ).decode("utf-8", "replace")
    for chunk in log.split("\x01")[1:]:
        short, _, rest = chunk.partition("\x00")
        message, _, diff = rest.partition("\x02")
        if any(value in message for value in values):
            hits.append(f"commit {short}：message")
        path = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("+") and any(value in line[1:] for value in values):
                hit = f"commit {short}：{path}"
                if hit not in hits:
                    hits.append(hit)
    return hits


def main(argv: list[str], *, repo: Path | None = None) -> int:
    if not argv or len(argv) > 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    values = load_values(Path(argv[0]).expanduser())
    if not values:
        print("敏感值清單是空的", file=sys.stderr)
        return 2
    root = repo or Path.cwd()
    hits = scan_head(root, values)
    if len(argv) == 2:
        hits += scan_commits(root, values, argv[1])
    for hit in hits:
        print(hit)
    print(f"共 {len(hits)} 處命中（只列位置，不列值）", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
