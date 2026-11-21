"""屋齡與完工日（WP4）：各種寫法 → 完工日區間 DateRange [lo, hi]，兩端都含（r4 §3.10）。

瀏覽器再用今天的日期與日曆年判斷屋齡條件；Python 不知道任何屋齡門檻。

| 寫法 | 區間（抓取日或成交日 R） |
|---|---|
| 完工年月日 d | [d, d] |
| 只有年月 | [該月 1 日, 該月最後一天] |
| 只有年（月為 00） | [1/1, 12/31] |
| 整數年 n，無條件捨去（integer_mode="floor"，例如中信） | (R − (n+1) 年, R − n 年] |
| 顯示值 x，不知道是四捨五入還是截斷（integer_mode="display"） | 屋齡 ∈ [x − h, x + 2h)，h 是最後一位的一半 |
| N 個月、N 年 M 個月 | 以月計、無條件捨去：(R − (月數+1) 個月, R − 月數 個月] |
| 0、--、空白 | 未知；有「新成屋」佐證時當成未滿 1 年 |
| 約 | 兩端各再放寬 1 年，加註 age_approx |
| 以上／以下／以內／未滿 | 一端不設限，加註 age_open |
| 數值大到像民國年 | 未知，加註 age_maybe_roc_year（R3-11） |

說明：
- 顯示值的區間同時涵蓋「四捨五入」[x − h, x + h) 與「截斷」[x, x + 2h)，
  比 r4 §3.10 表上的 ±h 寬，因為 R1-06 第 3 點指出兩種都可能。
- 小數年以天數近似（每年 365.2425 天），並且往外取整，區間只會更寬。
- 年份加減用日曆計算。2 月 29 日加減年數後落在平年時，一律改成 2 月 28 日。
"""

import calendar
import datetime as dt
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from house_watch.models import DateRange
from house_watch.parse.text import compact

ROC_OFFSET = 1911
DAYS_PER_YEAR = Decimal("365.2425")
APPROX_WIDEN_YEARS = 1
IMPLAUSIBLE_AGE_YEARS = 60  # 屋齡寫到這麼大，多半是民國完工年（R3-11）

IntegerMode = Literal["floor", "display"]


def add_years(day: dt.date, years: int) -> dt.date:
    """日曆年加減；2 月 29 日落在平年時改成 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def add_months(day: dt.date, months: int) -> dt.date:
    """日曆月加減；日期超過該月天數時改成該月最後一天。"""
    index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(index, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return dt.date(year, month0 + 1, min(day.day, last))


def date_range(year: int, month: int = 0, day: int = 0) -> DateRange:
    """西元年月日 → 區間。日為 0 → 整個月；月為 0 → 整年；年為 0 或不合法 → 未知。"""
    if not 1 <= year <= 9999:
        return DateRange()
    try:
        if month == 0:
            return DateRange(lo=dt.date(year, 1, 1), hi=dt.date(year, 12, 31))
        if day == 0:
            last = calendar.monthrange(year, month)[1]
            return DateRange(lo=dt.date(year, month, 1), hi=dt.date(year, month, last))
        exact = dt.date(year, month, day)
    except (ValueError, calendar.IllegalMonthError):
        return DateRange()
    return DateRange(lo=exact, hi=exact)


def _year(value: int) -> int:
    """小於 1911 的年份當成民國年。"""
    return value + ROC_OFFSET if 0 < value < ROC_OFFSET else value


_DATE_CJK = re.compile(r"^(?:民國)?(?P<y>\d{1,4})年(?:(?P<m>\d{1,2})月(?:(?P<d>\d{1,2})日)?)?$")
_DATE_SEP = re.compile(r"^(?:民國)?(?P<y>\d{1,4})[/.\-](?P<m>\d{1,2})(?:[/.\-](?P<d>\d{1,2}))?$")


def built_from_date(text: str | None) -> DateRange:
    """完工日期的文字 → 區間。

    支援：`2014-05-12`、`2014/5`、`2014年5月`、`民國103年5月12日`、`103/05/12`、`103.05`、
    七碼民國 `1030512`、`0840419`、八碼西元 `20140512`、只有年 `2014`、`103年`。
    小於 1911 的年份當成民國年。日為 00 → 整個月；月為 00 → 整年；看不懂 → 未知。
    """
    s = compact(text)
    if not s:
        return DateRange()
    m = _DATE_CJK.match(s) or _DATE_SEP.match(s)
    if m is not None:
        return date_range(
            _year(int(m.group("y"))),
            int(m.group("m") or 0),
            int(m.group("d") or 0),
        )
    if s.isdigit():
        if len(s) == 8:
            return date_range(int(s[:4]), int(s[4:6]), int(s[6:]))
        if len(s) == 6 and ROC_OFFSET <= int(s[:4]) <= 2100 and 1 <= int(s[4:]) <= 12:
            return date_range(int(s[:4]), int(s[4:]))
        if len(s) in (6, 7):
            roc = int(s[:-4])
            if roc == 0:
                return DateRange()
            return date_range(roc + ROC_OFFSET, int(s[-4:-2]), int(s[-2:]))
        if len(s) == 4:
            return date_range(int(s))
    return DateRange()


@dataclass(frozen=True)
class AgeParse:
    """屋齡換算的結果：完工日區間與附註代碼。"""

    built: DateRange
    flags: tuple[str, ...] = ()


# 年數後面一定要接「年」或結尾（含以上、以下）；否則「11個月」會被拆成 1 年 1 個月。
_AGE = re.compile(
    r"^(?P<pre>約|未滿)?"
    r"(?:(?P<y>\d+(?:\.\d+)?)(?:年|(?=以上|以下|以內|內|$)))?"
    r"(?:(?P<mo>\d+)個?月)?"
    r"(?P<post>以上|以下|以內|內)?$"
)
_NEW_HOUSE = re.compile(r"新成屋|全新|新屋")
_BRACKETS = re.compile(r"[()\[\]]")


def _unknown(*flags: str) -> AgeParse:
    return AgeParse(built=DateRange(), flags=tuple(sorted({"age_unknown", *flags})))


def _floor_years(as_of: dt.date, years: int) -> tuple[dt.date, dt.date]:
    """屋齡 ∈ [years, years+1)：完工日 ∈ (R − (years+1) 年, R − years 年]。"""
    return add_years(as_of, -(years + 1)) + dt.timedelta(days=1), add_years(as_of, -years)


def _floor_months(as_of: dt.date, months: int) -> tuple[dt.date, dt.date]:
    return add_months(as_of, -(months + 1)) + dt.timedelta(days=1), add_months(as_of, -months)


def _display(as_of: dt.date, value: Decimal) -> tuple[dt.date, dt.date]:
    """顯示值 x：屋齡 ∈ [x − h, x + 2h)，換成天數後往外取整。"""
    exponent = value.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    half = Decimal(5) * Decimal(10) ** -(decimals + 1)
    low_days = max(math.floor((value - half) * DAYS_PER_YEAR), 0)
    high_days = math.ceil((value + 2 * half) * DAYS_PER_YEAR)
    return as_of - dt.timedelta(days=high_days), as_of - dt.timedelta(days=low_days)


def built_from_age(
    value: str | int | float | Decimal | None,
    *,
    as_of: dt.date,
    integer_mode: IntegerMode = "display",
    new_house: bool = False,
    implausible_years: int = IMPLAUSIBLE_AGE_YEARS,
) -> AgeParse:
    """屋齡 → 完工日區間（見模組說明）。

    - as_of：屋齡的基準日（開價物件是抓取日，成交是交易日）。
    - integer_mode：整數屋齡的語意。"floor" 是無條件捨去（中信），"display" 是不知道怎麼進位的顯示值。
    - new_house：同一筆資料另有「新成屋」這類佐證，屋齡 0 才可信。
    """
    if value is None:
        return _unknown()
    if isinstance(value, float):
        value = repr(value)
    s = compact(str(value))
    if _NEW_HOUSE.search(s):
        new_house = True
        s = _BRACKETS.sub("", _NEW_HOUSE.sub("", s))
    if s in ("", "-", "--", "—", "不詳", "未知"):
        if new_house:
            lo, hi = _floor_years(as_of, 0)
            return AgeParse(built=DateRange(lo=lo, hi=hi), flags=("age_new_house",))
        return _unknown()
    m = _AGE.match(s)
    if m is None or (m.group("y") is None and m.group("mo") is None):
        return _unknown()
    try:
        years = Decimal(m.group("y")) if m.group("y") is not None else Decimal(0)
    except InvalidOperation:
        return _unknown()
    months = int(m.group("mo")) if m.group("mo") is not None else None
    flags: list[str] = []
    if years >= implausible_years:
        return _unknown("age_maybe_roc_year")
    if months is None and years == 0:
        if not new_house:
            return _unknown()
        flags.append("age_new_house")
        lo, hi = _floor_years(as_of, 0)
    elif months is not None:
        if years != years.to_integral_value():
            return _unknown()
        lo, hi = _floor_months(as_of, int(years) * 12 + months)
    elif integer_mode == "floor" and years == years.to_integral_value() and "." not in s:
        lo, hi = _floor_years(as_of, int(years))
    else:
        lo, hi = _display(as_of, years)

    pre, post = m.group("pre"), m.group("post")
    if pre == "未滿":
        if months is not None or post is not None:
            return _unknown()
        whole_years = math.ceil(years)
        lo = add_years(as_of, -whole_years) + dt.timedelta(days=1)
        hi = as_of
        flags.append("age_open")
    elif pre == "約":
        lo = add_years(lo, -APPROX_WIDEN_YEARS)
        hi = min(add_years(hi, APPROX_WIDEN_YEARS), as_of)
        flags.append("age_approx")
    if post == "以上":
        lo = None
        flags.append("age_open")
    elif post in ("以下", "以內", "內"):
        hi = as_of
        flags.append("age_open")
    return AgeParse(built=DateRange(lo=lo, hi=hi), flags=tuple(sorted(set(flags))))
