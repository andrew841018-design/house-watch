"""排程的 log（WP7b；R2-15）。

- 位置：platformdirs 的 log 資料夾（Mac 是 ~/Library/Logs/house_watch），環境變數
  `HOUSE_WATCH_LOG_DIR` 可以改（測試、隔離的 dry-run）。
  不放在資料夾（DataPaths）裡：資料夾在第一次 init 之前只能是空的（DataPaths.is_empty），
  而 launchd 的輸出檔在第一次執行前就要有資料夾可寫。
- tick 用 Python 的輪替 handler：每個檔 MAX_BYTES（5 MB），保留 BACKUP_COUNT（5）份舊檔。
- launchd 的標準輸出／錯誤檔（LAUNCHD_STDOUT、LAUNCHD_STDERR）只接 Python 還沒啟動就失敗的
  錯誤；tick 本身不往那裡寫東西，所以這兩個檔不會一直變大。
- 每一行先經過遮罩（cli.Ui.mask）：家目錄換成 ~，發布設定與聯絡方式換成 ***。
- 時間一律是 UTC（結尾 Z），和資料庫一致。
"""

import logging
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs

from house_watch.paths import APP_NAME

ENV_LOG_DIR = "HOUSE_WATCH_LOG_DIR"
LOGGER_NAME = "house_watch.tick"
LOG_FILENAME = "tick.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
LAUNCHD_STDOUT = "launchd.out.log"
LAUNCHD_STDERR = "launchd.err.log"
_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def log_dir() -> Path:
    """log 資料夾：環境變數 > platformdirs 的使用者 log 資料夾。一律是絕對路徑。"""
    override = os.environ.get(ENV_LOG_DIR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


class MaskFilter(logging.Filter):
    """把訊息先組好再遮罩，之後的 handler 拿到的都是遮過的文字。"""

    def __init__(self, mask: Callable[[str], str]) -> None:
        super().__init__()
        self._mask = mask

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if record.exc_info:  # traceback 也要遮（路徑裡有家目錄）
            text += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            record.exc_info = None
            record.exc_text = None
        record.msg = self._mask(text)
        record.args = None
        return True


def _formatter() -> logging.Formatter:
    formatter = logging.Formatter(_FORMAT, _DATEFMT)
    formatter.converter = time.gmtime
    return formatter


@contextmanager
def tick_logging(
    directory: Path | None = None,
    *,
    mask: Callable[[str], str] | None = None,
    console: bool | None = None,
) -> Iterator[logging.Logger]:
    """在 with 區塊裡把 tick 的 log 寫到 `<directory>/tick.log`（輪替），結束時關掉檔案。

    console 是 None 時：標準錯誤是終端機（有人手動執行）才同時印到畫面；
    launchd 底下不印，launchd 的輸出檔只留給 Python 啟動前的錯誤。
    """
    directory = directory if directory is not None else log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            directory / LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    ]
    if console is None:
        console = bool(getattr(sys.stderr, "isatty", lambda: False)())
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    mask_filter = MaskFilter(mask) if mask is not None else None
    if mask_filter is not None:
        logger.addFilter(mask_filter)  # 掛在 logger 上：任何 handler 拿到的都是遮過的文字
    for handler in handlers:
        handler.setFormatter(_formatter())
        logger.addHandler(handler)
    try:
        yield logger
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        if mask_filter is not None:
            logger.removeFilter(mask_filter)
