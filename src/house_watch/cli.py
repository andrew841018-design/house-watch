"""指令列入口。

這個檔案由 WP7a 擁有（init、update、export、serve、doctor）。
G0 只放佔位，讓 `house-watch` 這個 console script 可以執行並回報尚未實作。
"""

import sys

from house_watch.errors import ExitCode


def main(argv: list[str] | None = None) -> int:
    del argv
    print("house-watch：指令尚未實作（由 WP7a 完成）。", file=sys.stderr)
    return int(ExitCode.CONFIG)


if __name__ == "__main__":
    raise SystemExit(main())
