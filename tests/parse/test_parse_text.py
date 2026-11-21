"""parse/text.py：NFKC、中文數字、地址的標準寫法。"""

import pytest

from house_watch.parse.text import (
    cn_to_int,
    compact,
    int_to_cn,
    nfkc,
    normalize_address,
    unify_variants,
    zip3,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("十", 10),
        ("十五", 15),
        ("二十", 20),
        ("二十三", 23),
        ("四十七", 47),
        ("一百零五", 105),
        ("一百一十", 110),
        ("一百十", 110),
        ("一千零十", 1010),
        ("兩千", 2000),
        ("一〇五", 105),
        ("壹拾", 10),
        ("參", 3),
        ("零", 0),
        ("１２３", 123),
        ("45", 45),
    ],
)
def test_cn_to_int(text, expected):
    assert cn_to_int(text) == expected


@pytest.mark.parametrize("text", ["", None, "十百", "二三十", "百", "五層", "abc", "一點五"])
def test_cn_to_int_rejects_garbage(text):
    assert cn_to_int(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "一"), (3, "三"), (10, "十"), (11, "十一"), (20, "二十"), (23, "二十三"), (99, "九十九")],
)
def test_int_to_cn(value, expected):
    assert int_to_cn(value) == expected
    assert cn_to_int(expected) == value


def test_int_to_cn_range():
    with pytest.raises(ValueError):
        int_to_cn(0)
    with pytest.raises(ValueError):
        int_to_cn(100)


def test_nfkc_handles_full_width_and_ideographic_space():
    assert nfkc("　１２３ＡＢ　") == "123AB"
    assert nfkc(None) == ""
    assert compact("大興西路１ 段 ３１７號") == "大興西路1段317號"
    # ㎡ 經過 NFKC 會變成 m2（R3-11 的陷阱，面積解析要把它當單位）
    assert nfkc("88.45㎡") == "88.45m2"


def test_unify_variants_and_private_use_chars():
    assert unify_variants("臺峯厦塩双舘") == "台峰廈鹽雙館"
    assert unify_variants("雙路") == "雙峰路"  # 已確認的造字
    assert unify_variants("門街") == "廈門街"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 實價登錄原樣：全形數字、中文樓層、之幾、尾端 U+3000
        ("桃園市桃園區示意路１１７號二十三樓之１０　", "桃園市桃園區示意路117號23樓之10"),
        # 段與路名中的數字用國字
        ("示意西路3段317號", "示意西路三段317號"),
        ("示意西路１段", "示意西路一段"),
        ("示埔6街12巷五弄7號三樓", "示埔六街12巷5弄7號3樓"),
        # 舊資料的 24-4號 看成 24之4號
        ("示意路24-4號", "示意路24之4號"),
        # 路名裡本來就是國字的不動
        ("示國一路十號", "示國一路10號"),
        ("臺北市峯示路5號", "台北市峰示路5號"),
        ("", ""),
    ],
)
def test_normalize_address(raw, expected):
    assert normalize_address(raw) == expected


def test_normalize_address_keeps_three_digit_road_numbers():
    # 三位數以上的數字不會被當成段號或路名數字
    assert normalize_address("示意路123號") == "示意路123號"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("330", "330"),
        ("33012", "330"),  # 3＋2 碼
        ("330012", "330"),  # 3＋3 碼：整個拿去比 330 會失敗（R3-11）
        ("330-12", "330"),
        ("３２０", "320"),
        (" 320 ", "320"),
        ("3301", None),
        ("33", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_zip3(text, expected):
    assert zip3(text) == expected
