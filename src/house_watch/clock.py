"""可注入的時鐘（R1-25、R3-18）。

- 時間一律存 UTC，格式 `YYYY-MM-DDTHH:MM:SSZ`，到秒。
- 「今天」一律用台北日期（Asia/Taipei，zoneinfo＋tzdata，Windows 也能用）。
- 時鐘同時提供牆上時間 `now()` 與單調時間 `monotonic()`：
  截止時間用單調時間；牆上時間經過太久代表中途睡著（R3-18）。
"""

import datetime as dt
import time
from typing import Protocol
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """目前時間，帶 UTC 時區，到秒。"""
        ...

    def monotonic(self) -> float:
        """單調遞增的秒數，只用來算經過時間。"""
        ...


class SystemClock:
    """真正的時鐘。"""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC).replace(microsecond=0)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """測試用的時鐘。`advance(..., asleep=True)` 只推進牆上時間，模擬電腦睡著。"""

    def __init__(self, start: dt.datetime, monotonic: float = 0.0) -> None:
        self._now = _require_aware(start).astimezone(dt.UTC).replace(microsecond=0)
        self._monotonic = monotonic

    def now(self) -> dt.datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float, *, asleep: bool = False) -> None:
        if seconds < 0:
            raise ValueError("時鐘不能倒退")
        self._now = (self._now + dt.timedelta(seconds=seconds)).replace(microsecond=0)
        if not asleep:
            self._monotonic += seconds


def _require_aware(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("時間必須帶時區")
    return when


def taipei_date(when: dt.datetime) -> dt.date:
    """這個時間點在台北是哪一天。"""
    return _require_aware(when).astimezone(TAIPEI).date()


def to_iso(when: dt.datetime) -> str:
    """存進資料庫或匯出的時間字串：UTC，到秒，結尾 Z。"""
    utc = _require_aware(when).astimezone(dt.UTC).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> dt.datetime:
    """讀回 to_iso 寫的字串（也接受其他帶時區的 ISO 寫法），一律轉成 UTC。"""
    return _require_aware(dt.datetime.fromisoformat(text)).astimezone(dt.UTC)
