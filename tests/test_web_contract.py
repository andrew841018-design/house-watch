"""Python 端的契約測試（r4 §3.3）：rules.js 的列舉值、原因代碼與案例表格式。

- ENUMS 從 rules.js 的標記區塊讀出，必須和 models.RULE_ENUMS 完全相同。
- 案例表的 rec 只檢查「鍵都是匯出欄位」，不檢查值：R3-02 的缺值案例
  （null、缺欄位、空字串）必須合法。種子案例另外整筆驗證成匯出模型。
"""

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from house_watch.models import (
    ConflictValue,
    ExportLoc,
    GroupFacts,
    ListingGroup,
    ListingMember,
    Metrics,
    TxParking,
    TxRecord,
    UnitPrice,
    rule_enum_values,
)

ROOT = Path(__file__).resolve().parents[1]
RULES_JS = ROOT / "web" / "rules.js"
CASES = ROOT / "web" / "test" / "rules_cases.json"

SEED_CASES = {
    "seed_tx_all_criteria_match",
    "seed_listing_price_missing_pending",
    "seed_listing_age_boundary_pending",
}
# 契約 C3 的條件欄位（只用來檢查案例表的鍵，Python 不讀任何人的條件）。
PROFILE_KEYS = {
    "name",
    "area",
    "price_max_wan",
    "built_years",
    "floor",
    "main_min_ping",
    "elevator",
    "parking_ok",
    "lift_flat_ok",
    "near_m",
    "rca_m",
    "drive_min",
    "unit_price_max_wan",
    "tx_months",
}
RECORD_MODELS: dict[str, type[BaseModel]] = {"listing": ListingGroup, "tx": TxRecord}
# 巢狀欄位：(紀錄種類, 欄位) → (模型, 是不是清單)
NESTED: dict[tuple[str, str], tuple[type[BaseModel], bool]] = {
    ("listing", "facts"): (GroupFacts, False),
    ("listing", "loc"): (ExportLoc, False),
    ("listing", "metrics"): (Metrics, False),
    ("listing", "members"): (ListingMember, True),
    ("tx", "parking"): (TxParking, True),
    ("tx", "up"): (UnitPrice, False),
    ("tx", "metrics"): (Metrics, False),
}


def _contract_block(name: str):
    text = RULES_JS.read_text(encoding="utf-8")
    found = re.search(rf"/\*<contract:{name}>\*/(.*?)/\*</contract:{name}>\*/", text, re.S)
    assert found, f"rules.js 缺少 {name} 的標記區塊"
    return json.loads(found.group(1))


def _cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def test_rules_js_enums_equal_models():
    assert _contract_block("ENUMS") == rule_enum_values()


def test_reasons_are_unique_codes():
    reasons = _contract_block("REASONS")
    assert len(reasons) == len(set(reasons))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", reason) for reason in reasons)


def test_case_table_envelope():
    table = json.loads(CASES.read_text(encoding="utf-8"))
    assert set(table) == {"version", "cases"} and table["version"] == 1
    names = [case["name"] for case in table["cases"]]
    assert len(names) == len(set(names))
    assert SEED_CASES <= set(names)


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_case_format(case):
    assert set(case) == {"name", "kind", "rec", "profile", "today", "expect"}
    kind = case["kind"]
    assert kind in RECORD_MODELS
    dt.date.fromisoformat(case["today"])
    assert set(case["expect"]) == {"status", "reasons"}
    assert case["expect"]["status"] in {"match", "pending", "fail"}
    assert set(case["expect"]["reasons"]) <= set(_contract_block("REASONS"))

    profile = case["profile"]
    assert isinstance(profile, dict) and isinstance(profile.get("area"), str)
    assert set(profile) <= PROFILE_KEYS

    rec = case["rec"]
    assert isinstance(rec.get("area"), str), "rec 要帶 area（所在資料檔的區域 id）"
    assert set(rec) - {"area"} <= set(RECORD_MODELS[kind].model_fields)
    for (record_kind, field), (model, many) in NESTED.items():
        value = rec.get(field) if record_kind == kind else None
        items = value if many and isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                assert set(item) <= set(model.model_fields), f"{field} 有不認得的鍵"
    if kind == "listing" and isinstance(rec.get("conflicts"), dict):
        for values in rec["conflicts"].values():
            for item in values:
                assert set(item) <= set(ConflictValue.model_fields)


@pytest.mark.parametrize(
    "case", [c for c in _cases() if c["name"] in SEED_CASES], ids=lambda case: case["name"]
)
def test_seed_records_are_valid_exports(case):
    rec = {key: value for key, value in case["rec"].items() if key != "area"}
    RECORD_MODELS[case["kind"]].model_validate(rec)
    assert case["rec"]["area"] == case["profile"]["area"]


def _rule_constant(name: str) -> float:
    found = re.search(rf"^\s*{name}:\s*([0-9.]+),", RULES_JS.read_text(encoding="utf-8"), re.M)
    assert found, f"rules.js 的 RULE_CONSTANTS 缺少 {name}"
    return float(found.group(1))


def test_price_drop_step_matches_python():
    """事後審查 CE-1：網頁的降價徽章和 Python 的 price_summary 用同一個「1 萬」門檻（R3-22）。"""
    from house_watch.pipeline.versions import PRICE_MIN_STEP_YUAN

    assert _rule_constant("PRICE_MIN_STEP_YUAN") == PRICE_MIN_STEP_YUAN


def test_drive_options_are_spaced_by_the_near_band():
    """事後審查 AT-10：「接近車程門檻」寫成固定寬度，選項間距必須等於它，否則下一檔對不上。"""
    from house_watch.settings import load_public

    band = _rule_constant("DRIVE_NEAR_BAND_MIN")
    for area in load_public().areas:
        options = area.options.drive_min_options if area.options else None
        if options:
            assert [b - a for a, b in zip(options, options[1:], strict=False)] == [band] * (
                len(options) - 1
            ), area.id
