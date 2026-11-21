"""2026-11-21 契約合併（STATUS 第 1–13 項）的結構測試。數字都是示意值。"""

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from house_watch import db, export
from house_watch.http import daily_cap_of
from house_watch.models import (
    GEO_FEATURE_ROLES,
    HINT_KINDS,
    ListingFacts,
    TxFacts,
    UnitPrice,
    UpBasis,
)
from house_watch.paths import DataPaths
from house_watch.settings import AreaConfig, HttpConfig, SourceConfig, load_public

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 第 1、2 項：每日上限與 robots 雜湊


def test_every_source_has_a_daily_cap_from_the_owner_decision():
    public = load_public()
    caps = {source: config.max_requests_day for source, config in public.sources.items()}
    assert caps["rakuya"] == 650 and caps["cthouse"] == 300  # PLAN_v2 §4 第 1、6 項
    assert all(caps[s] <= 150 for s in caps if s not in ("rakuya", "cthouse"))
    for config in public.sources.values():
        assert config.max_requests_full <= config.max_requests_day
        assert daily_cap_of(config) == config.max_requests_day
        assert config.min_total_ratio == 0.9


def test_full_scan_budget_cannot_exceed_the_daily_cap():
    base = {
        "enabled": True,
        "publish": True,
        "min_interval_s": 4,
        "max_requests_day": 20,
        "max_requests_full": 21,
        "probe_pages": 2,
        "detail_cap": 1,
    }
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(base)
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({**base, "max_requests_full": 5, "min_total_ratio": 1.5})


def test_robots_hashes_from_the_live_check_are_configured():
    accepted = load_public().robots.accepted_hash
    assert set(accepted) == set(load_public().sources)
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in accepted.values())
    live = (ROOT / "tests" / "fixtures" / "sources" / "LIVE_CHECK.md").read_text(encoding="utf-8")
    for source, digest in accepted.items():
        assert digest in live, f"{source} 的雜湊要有實際連線的紀錄"


# ---------------------------------------------------------------- 第 4、9 項：事實模型


def test_district_hint_kinds_are_fixed():
    assert HINT_KINDS == ("district", "zip", "address")
    base = {"source": "x", "source_id": "1", "area": "a"}
    ListingFacts(**base, district_hints=[("district", "甲區"), ("zip", "999"), ("address", "乙區")])
    with pytest.raises(ValidationError):
        ListingFacts(**base, district_hints=[("road", "示意路")])


def test_tx_facts_keep_row_level_parking_totals():
    tx = TxFacts(
        serial="TESTSERIAL1",
        release_id="b20260101",
        trade_date=dt.date(2026, 1, 2),
        district="甲區",
        total_yuan=12_340_000,
        parking_total_yuan=1_230_000,
        parking_total_cm2=3_456,
    )
    assert (tx.parking_total_yuan, tx.parking_total_cm2) == (1_230_000, 3_456)
    assert TxFacts.model_validate_json(tx.model_dump_json()) == tx


def test_unit_price_levels_only_for_estimates():
    values = {"q_lo": 1, "q_hi": 2, "k_lo": 3, "k_hi": 4}
    up = UnitPrice(
        basis=UpBasis.ESTIMATED, price_level="district_flat", area_level="city", **values
    )
    assert up.price_level == "district_flat"
    with pytest.raises(ValidationError):
        UnitPrice(basis=UpBasis.OFFICIAL, price_level="district", **values)
    with pytest.raises(ValidationError):
        UnitPrice(basis=UpBasis.ESTIMATED, price_level="village", **values)


# ---------------------------------------------------------------- 第 5、8 項：設定


def test_user_agent_cannot_be_configured():
    http = load_public().http
    assert http.user_agent is None
    data = http.model_dump() | {"user_agent": "anything/1.0"}
    with pytest.raises(ValidationError):
        HttpConfig.model_validate(data)


def test_buffer_area_may_declare_one_district():
    core = load_public().areas[0].model_dump(mode="json")
    district = {"file": "district_boundary.geojson", "role": "district"}
    with_one = {**core, "geo": {**core["geo"], "district": district}}
    assert AreaConfig.model_validate(with_one).geo["district"].role == "district"
    with_two = {**with_one, "geo": {**with_one["geo"], "district2": district}}
    with pytest.raises(ValidationError):
        AreaConfig.model_validate(with_two)


# ---------------------------------------------------------------- 第 11 項：geo 屬性


def test_web_fixture_geo_uses_the_contract_roles():
    for path in sorted((ROOT / "web" / "test" / "fixtures" / "geo").glob("*.geojson")):
        for feature in json.loads(path.read_text(encoding="utf-8"))["features"]:
            props = feature["properties"]
            assert props["role"] in GEO_FEATURE_ROLES, path.name
            for key in GEO_FEATURE_ROLES[props["role"]]:
                if key != "name":
                    assert isinstance(props.get(key), int), (path.name, props["role"], key)


# ---------------------------------------------------------------- 第 12 項：匯出位置、kv 鍵、掃描狀態


def test_export_paths_kv_keys_and_scan_statuses_match_wp7a(tmp_path):
    data = DataPaths(tmp_path)
    assert data.export_dir == export.export_dir(data)
    assert data.export_site_dir == export.site_dir(data)
    assert data.export_manifest == export.manifest_path(data)
    assert db.KV_LVR_LAST_CHECK == export.KV_LVR_LAST_CHECK
    assert db.KV_LVR_AS_OF == export.KV_LVR_AS_OF
    assert db.KV_DOORINDEX_LAST_CHECK == export.KV_DOORINDEX_LAST_CHECK
    assert db.KV_DOORINDEX_VERSION == export.KV_DOORINDEX_VERSION
    assert db.SCAN_OK_STATUSES == export.OK_SCAN_STATUSES
    assert set(db.SCAN_OK_STATUSES) < set(db.SCAN_STATUSES)


# ---------------------------------------------------------------- 第 13 項


def test_vendor_files_are_kept_byte_for_byte():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^web/vendor/\*\* -text$", text, re.M)
