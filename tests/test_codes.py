"""C10：公開錯誤代碼與結束碼。"""

from house_watch.errors import ConfigError, DataDirError, ErrorCode, ExitCode, HouseWatchError


def test_public_error_codes_are_exactly_c10():
    assert [code.value for code in ErrorCode] == [
        "blocked",
        "rate_limited",
        "challenge",
        "page_invalid",
        "parse_rate_low",
        "count_short",
        "budget_exhausted",
        "timeout",
        "http_5xx",
        "robots_changed",
        "paused",
        "tls_error",
        "doorindex_invalid",
        "lvr_invalid",
        "publish_failed",
        "push_rejected",
    ]


def test_exit_codes():
    assert (ExitCode.OK, ExitCode.PARTIAL, ExitCode.CONFIG) == (0, 1, 2)
    assert ConfigError.exit_code == DataDirError.exit_code == ExitCode.CONFIG
    assert HouseWatchError.exit_code == ExitCode.PARTIAL
