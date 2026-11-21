"""parse/age.py：屋齡與完工日 → 完工日區間（r4 §3.10、R1-06、R3-11）。屋齡數字都是示意值。"""

import datetime as dt

import pytest

from house_watch.models import DateRange
from house_watch.parse.age import (
    add_months,
    add_years,
    built_from_age,
    built_from_date,
    date_range,
)

D = dt.date
R = D(2026, 11, 21)  # 抓取日（示意）


def contains(rng: DateRange, day: dt.date) -> bool:
    return (rng.lo is None or rng.lo <= day) and (rng.hi is None or day <= rng.hi)


# ------------------------------------------------------------------ 完工日文字


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("2014-05-12", D(2014, 5, 12), D(2014, 5, 12)),
        ("2014/05/12", D(2014, 5, 12), D(2014, 5, 12)),
        ("20140512", D(2014, 5, 12), D(2014, 5, 12)),
        ("民國103年5月12日", D(2014, 5, 12), D(2014, 5, 12)),
        ("103/05/12", D(2014, 5, 12), D(2014, 5, 12)),
        ("1030512", D(2014, 5, 12), D(2014, 5, 12)),  # 實價登錄的七碼民國
        ("0840419", D(1995, 4, 19), D(1995, 4, 19)),
        ("2014/5", D(2014, 5, 1), D(2014, 5, 31)),  # 只有年月 → 整個月
        ("2014年2月", D(2014, 2, 1), D(2014, 2, 28)),
        ("103.02", D(2014, 2, 1), D(2014, 2, 28)),
        ("201405", D(2014, 5, 1), D(2014, 5, 31)),
        ("0630400", D(1974, 4, 1), D(1974, 4, 30)),  # 日為 00 → 整個月（不是 1 日）
        ("0150000", D(1926, 1, 1), D(1926, 12, 31)),  # 月為 00 → 整年
        ("2014", D(2014, 1, 1), D(2014, 12, 31)),
        ("103年", D(2014, 1, 1), D(2014, 12, 31)),
    ],
)
def test_built_from_date(text, lo, hi):
    assert built_from_date(text) == DateRange(lo=lo, hi=hi)


@pytest.mark.parametrize(
    "text", ["", None, "0000000", "-250701", "012", "025", "abc", "2014/13", "1031332", "0-10701"]
)
def test_built_from_date_unknown(text):
    assert built_from_date(text) == DateRange()


def test_date_range_rules():
    assert date_range(2024, 2) == DateRange(lo=D(2024, 2, 1), hi=D(2024, 2, 29))
    assert date_range(0) == DateRange()
    assert date_range(2024, 2, 30) == DateRange()


# ------------------------------------------------------------------ 日曆計算


def test_feb_29_rule_is_fixed():
    # 2 月 29 日加減年數落在平年時，一律改成 2 月 28 日。
    assert add_years(D(2028, 2, 29), -1) == D(2027, 2, 28)
    assert add_years(D(2028, 2, 29), 4) == D(2032, 2, 29)
    assert add_years(D(2026, 3, 1), -2) == D(2024, 3, 1)
    assert add_months(D(2026, 3, 31), -1) == D(2026, 2, 28)
    assert add_months(D(2026, 1, 15), -13) == D(2024, 12, 15)


def test_floor_years_boundaries_use_calendar_not_365_days():
    # 剛好滿 10 年（日曆）一定在「整數 10 年」的區間裡；差一天就是 9 年。
    as_of = D(2026, 11, 20)
    ten = built_from_age(10, as_of=as_of, integer_mode="floor").built
    assert ten == DateRange(lo=D(2015, 11, 21), hi=D(2016, 11, 20))
    assert contains(ten, D(2016, 11, 20))
    assert not contains(ten, D(2016, 11, 21))
    leap = built_from_age(1, as_of=D(2028, 2, 29), integer_mode="floor").built
    assert leap == DateRange(lo=D(2026, 3, 1), hi=D(2027, 2, 28))


# ------------------------------------------------------------------ 中信的整數年：無條件捨去（R1-06 第 2 點的回歸測試）


@pytest.mark.parametrize(
    ("shown", "true_years", "true_months"),
    [(7, 7, 9), (11, 11, 6), (15, 15, 6)],
)
def test_cthouse_integer_years_are_floored(shown, true_years, true_months):
    built_true = add_months(R, -(true_years * 12 + true_months))
    result = built_from_age(shown, as_of=R, integer_mode="floor")
    assert result.flags == ()
    assert contains(result.built, built_true)
    # 四捨五入的讀法會得到下一個整數，那個區間不包含真值
    rounded = built_from_age(shown + 1, as_of=R, integer_mode="floor")
    assert not contains(rounded.built, built_true)


def test_display_value_covers_both_rounding_and_truncation():
    # 「10.9」：四捨五入 [10.85, 10.95)，截斷 [10.9, 11.0)，兩者聯集。
    result = built_from_age("10.9", as_of=R)
    assert result.built == DateRange(lo=D(2015, 11, 21), hi=D(2016, 1, 16))
    # 帶小數點的顯示值不套用「無條件捨去」，即使數字是整數（例如「10.0」）
    shown = built_from_age("10.0", as_of=R, integer_mode="floor").built
    assert shown == DateRange(lo=D(2016, 10, 15), hi=D(2016, 12, 9))


def test_months_forms():
    # 「11個月」是 11 個月，不是 1 年 1 個月
    assert built_from_age("11個月", as_of=R).built == DateRange(
        lo=D(2025, 11, 22), hi=D(2025, 12, 21)
    )
    assert built_from_age("7年9個月", as_of=R).built == DateRange(
        lo=D(2019, 1, 22), hi=D(2019, 2, 21)
    )


# ------------------------------------------------------------------ 0、空白、約、以上、民國年


@pytest.mark.parametrize("value", [0, "0", "0.0", "--", "", None, "不詳"])
def test_zero_and_blank_are_unknown(value):
    # R1-06 第 1 點：0 不等於新屋
    result = built_from_age(value, as_of=R, integer_mode="floor")
    assert result.built == DateRange()
    assert "age_unknown" in result.flags


def test_zero_with_new_house_evidence():
    expected = DateRange(lo=D(2025, 11, 22), hi=R)
    assert built_from_age(0, as_of=R, new_house=True).built == expected
    assert built_from_age("新成屋", as_of=R).built == expected
    assert built_from_age("0(新成屋)", as_of=R).flags == ("age_new_house",)


def test_approx_widens_both_ends():
    base = built_from_age(10, as_of=R, integer_mode="floor").built
    approx = built_from_age("約10", as_of=R, integer_mode="floor")
    assert approx.flags == ("age_approx",)
    assert approx.built.lo == add_years(base.lo, -1)
    assert approx.built.hi == add_years(base.hi, 1)


def test_open_ended_forms():
    above = built_from_age("30年以上", as_of=R, integer_mode="floor")
    assert above.built == DateRange(lo=None, hi=D(1996, 11, 21))  # 不會被當成剛好 30.0 年
    assert above.flags == ("age_open",)
    below = built_from_age("5年以下", as_of=R, integer_mode="floor")
    assert below.built == DateRange(lo=D(2020, 11, 22), hi=R)
    under = built_from_age("未滿8年", as_of=R)
    assert under.built == DateRange(lo=D(2018, 11, 22), hi=R)


@pytest.mark.parametrize("value", [103, "103", "98年", 60])
def test_roc_year_like_values_are_unknown(value):
    # R3-11：屋齡 103 多半是民國 103 年完工
    result = built_from_age(value, as_of=R)
    assert result.built == DateRange()
    assert "age_maybe_roc_year" in result.flags


def test_implausible_threshold_is_adjustable():
    # 公開範圍上限的兩倍以上也列為可疑（R3-11）；門檻由呼叫端給
    assert "age_maybe_roc_year" in built_from_age(51, as_of=R, implausible_years=51).flags
    assert built_from_age(51, as_of=R).flags == ()


def test_garbage_age_is_unknown():
    assert built_from_age("屋齡請洽", as_of=R).flags == ("age_unknown",)
    assert built_from_age("7.5年9個月", as_of=R).flags == ("age_unknown",)
