"""Golden 案例：21 筆實價登錄真實成交（公開資料），驗證修正後的扣車位單價（R1-05、R1-19）。

- 其中 10 筆依各類別的車位結構條件，從桃園市全市季檔以固定 seed 隨機抽出，不依任何價格、屋齡、
  樓層、坪數或區域挑選（seed 記在 golden_expected.json 的 note）。

- fixtures 只保留單價計算會讀的欄位；地址、樓層、完工日、備註換成佔位字或空白。
- 涵蓋：官方拆價、車位價含在總價（面積已登記／未登記）、有價無面積、多車位、
  明細價格是 0 但主檔有總價、沒有明細、機械車位、「其他」車位、沒有車位、
  估扣面積碰到上限、備註寫了車位價但官方價是 0。
- 中位數是示意值；預期值由另一支獨立的計算產生。
- 另外逐筆用政府算的「單價元平方公尺」交叉檢查解析出來的總價、面積與車位：
  官方公式是「車位價與車位面積都 > 0 才扣車位，否則總價 ÷ 總面積」（R1 §6.1）。
"""

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from house_watch.lvr.parse import MAIN_COLUMNS, parse_release, read_table
from house_watch.lvr.unitprice import MedianGroup, MedianTable, Quartiles, unit_price
from house_watch.models import UpBasis

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lvr"
DOC = json.loads((FIXTURES / "golden_expected.json").read_text(encoding="utf-8"))
CASES = DOC["cases"]
IDS = [f"{case['category']}-{case['serial']}" for case in CASES]


@pytest.fixture(scope="module")
def parsed():
    main = (FIXTURES / "golden_main.csv").read_bytes()
    result = parse_release(
        main,
        (FIXTURES / "golden_build.csv").read_bytes(),
        (FIXTURES / "golden_park.csv").read_bytes(),
        release_id="s115S3",
    )
    rows = {row["編號"]: row for row in read_table(main, MAIN_COLUMNS, what="golden").rows}
    return result, {facts.serial: facts for facts in result.facts}, rows


@pytest.fixture(scope="module")
def medians():
    groups = {}
    for m in DOC["medians"]:
        key = (m["level"], m["district"], m["ptype"])
        groups[key] = MedianGroup(*key, Quartiles(**m["price"]), Quartiles(**m["area"]))
    return MedianTable(version="golden", groups=groups)


def test_all_golden_rows_parse(parsed):
    result, facts, _ = parsed
    assert result.skipped_total == 0
    assert len(facts) == len(CASES) >= 20
    assert all(f.exclusion is None for f in facts.values())
    assert {case["basis"] for case in CASES} == {"official", "estimated", "none"}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_parsing_matches_official_unit_price(parsed, case):
    _, facts, rows = parsed
    tx = facts[case["serial"]]
    official = Decimal(rows[case["serial"]]["單價元平方公尺"])
    price = sum(space.price_yuan or 0 for space in tx.parkings)
    area = sum(space.area_cm2 or 0 for space in tx.parkings)
    if price > 0 and area > 0:
        ours = Fraction(tx.total_yuan - price) * 100 / (tx.bldg_cm2 - area)
    else:
        ours = Fraction(tx.total_yuan * 100, tx.bldg_cm2)
    assert abs(ours - Fraction(official)) <= max(2, Fraction(official) / 1000)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_golden_unit_price(parsed, medians, case):
    _, facts, _ = parsed
    result = unit_price(facts[case["serial"]], medians, min_n=DOC["min_samples"])
    up = result.up
    assert up.basis == case["basis"]
    assert (up.q_lo, up.q_hi, up.k_lo, up.k_hi) == (
        case["q_lo"],
        case["q_hi"],
        case["k_lo"],
        case["k_hi"],
    )
    assert sorted(result.flags) == case["flags"]


def _case(category: str) -> dict:
    return next(case for case in CASES if case["category"] == category)


def test_corrected_logic_against_known_prototype_bugs(parsed, medians):
    _, facts, _ = parsed

    # 1. 車位價與車位面積都沒拆：原型初版只估車位價、沒扣面積 → 這裡兩個都扣
    both = facts[_case("both_bundled")["serial"]]
    up = unit_price(both, medians).up
    assert up.basis == UpBasis.ESTIMATED
    assert up.q_hi < both.total_yuan and up.k_hi < both.bldg_cm2

    # 2. 有車位價、沒有車位面積：官方公式完全不扣（等於含車位單價）；這裡扣官方價、估扣面積
    split = facts[_case("price_split_area_none")["serial"]]
    up = unit_price(split, medians).up
    official_price = split.parkings[0].price_yuan
    assert up.q_lo == up.q_hi == split.total_yuan - official_price
    assert up.k_lo < up.k_hi < split.bldg_cm2

    # 3. 備註寫了車位價、官方車位價是 0：用中位數估，不讀備註（r4 F4 延後）
    remark = facts[_case("remark_price_official_zero")["serial"]]
    up = unit_price(remark, medians).up
    assert up.q_hi == remark.total_yuan - 1_450_000  # 中壢區坡道平面的示意 p25
    assert up.q_hi != remark.total_yuan - 1_400_000  # 不是備註寫的 140 萬

    # 4. 估扣面積不能讓分母小於主建物＋附屬＋陽台（R1-05 第 3 點）
    cap = facts[_case("cap")["serial"]]
    result = unit_price(cap, medians)
    floor = cap.main_cm2 + cap.aux_cm2 + cap.balcony_cm2
    assert result.up.k_lo == floor
    assert "up_area_capped" in result.flags

    # 5. 多車位逐位看明細：4 個車位都有官方價與面積 → 官方拆價，不用中位數
    multi = facts[_case("multi_official")["serial"]]
    assert multi.parking_n == 4
    assert unit_price(multi, None).up.basis == UpBasis.OFFICIAL

    # 6. 明細價格都是 0、主檔有總價：平均分給各車位，總和不變，算官方拆價
    spread = facts[_case("multi_split_total")["serial"]]
    assert "parking_price_split" in spread.flags
    assert unit_price(spread, None).up.basis == UpBasis.OFFICIAL

    # 7. 「其他」車位價格沒拆：不猜，basis 是 none
    other = facts[_case("other_type")["serial"]]
    assert unit_price(other, medians).up.basis == UpBasis.NONE
