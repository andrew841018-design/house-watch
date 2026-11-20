"""錯誤代碼、結束碼與例外（契約 C10，只由 WP0 修改）。

`ErrorCode` 是公開的代碼：會寫進 `data/meta.json` 的 `sources[].error_code`，
所以只放代碼，不放任何訊息或設定值（R1-31）。
"""

from enum import IntEnum, StrEnum


class ErrorCode(StrEnum):
    """公開錯誤代碼（C10）。新增代碼只由 WP0 合併。"""

    # 抓取
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    CHALLENGE = "challenge"
    PAGE_INVALID = "page_invalid"
    PARSE_RATE_LOW = "parse_rate_low"
    COUNT_SHORT = "count_short"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMEOUT = "timeout"
    HTTP_5XX = "http_5xx"
    ROBOTS_CHANGED = "robots_changed"
    PAUSED = "paused"
    TLS_ERROR = "tls_error"
    # 資料
    DOORINDEX_INVALID = "doorindex_invalid"
    LVR_INVALID = "lvr_invalid"
    # 發布
    PUBLISH_FAILED = "publish_failed"
    PUSH_REJECTED = "push_rejected"


class ExitCode(IntEnum):
    """指令的結束碼（C10）。"""

    OK = 0  # 成功，或取不到鎖而略過
    PARTIAL = 1  # 部分失敗
    CONFIG = 2  # 設定或資料夾錯誤


class HouseWatchError(Exception):
    """本專案例外的基底。訊息只描述哪裡錯，不印出設定值或個人資料。"""

    exit_code: ExitCode = ExitCode.PARTIAL


class ConfigError(HouseWatchError):
    """公開預設設定或本機設定檔不合法。"""

    exit_code = ExitCode.CONFIG


class DataDirError(HouseWatchError):
    """資料夾或資料庫狀態不對，例如有 instance 標記卻沒有資料庫、資料庫版本比程式新。"""

    exit_code = ExitCode.CONFIG
