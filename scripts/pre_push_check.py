"""推送前的隱私檢查（r4 §3.1；事後審查 P-15）。

用法（在程式 repo 裡執行）：

    python3 scripts/pre_push_check.py <敏感值清單檔> [<git 範圍，例如 origin/main..HEAD>]

- 清單檔放在私人工作區，由擁有者自己維護：一行一個值，只寫有辨識度的寫法
  （例如條件的「上限值＋單位」）；空行與 # 開頭的行略過。清單本身不能放進 repo。
- 掃描 git 追蹤的每個文字檔；給了範圍時，也掃描那些 commit 的 message。
- 命中時只印出「檔名:行號」或「commit <短 hash>」，不印出值；有命中結束碼 1，沒有是 0。
"""

import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024


def load_values(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def scan_files(repo: Path, values: list[str]) -> list[str]:
    hits = []
    for raw in _git("ls-files", "-z", cwd=repo).split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        path = repo / rel
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二進位檔
        for number, line in enumerate(text.splitlines(), start=1):
            if any(value in line for value in values):
                hits.append(f"{rel}:{number}")
    return hits


def scan_messages(repo: Path, values: list[str], revisions: str) -> list[str]:
    hits = []
    log = _git("log", "--format=%h%x00%B%x01", revisions, cwd=repo).decode("utf-8", "replace")
    for entry in log.split("\x01"):
        short, _, body = entry.strip("\n").partition("\x00")
        if short and any(value in body for value in values):
            hits.append(f"commit {short}")
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
    hits = scan_files(root, values)
    if len(argv) == 2:
        hits += scan_messages(root, values, argv[1])
    for hit in hits:
        print(hit)
    print(f"共 {len(hits)} 處命中（只列位置，不列值）", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
