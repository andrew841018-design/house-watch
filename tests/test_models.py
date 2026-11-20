"""C1 事實模型與 C2 匯出模型的結構測試。數字都是示意值。"""

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from house_watch.errors import ErrorCode
from house_watch.models import (
    BType,
    DateRange,
    Elevator,
    ListingFacts,
    ListingGroup,
    ListingsFile,
    Location,
    Meta,
    Parking,
    Precision,
    TxFacts,
    TxFile,
    TxRecord,
    export_json,
)

UTC = dt.UTC


def test_enums_are_exactly_c1():
    assert [p.value for p in Precision] == ["site", "door", "community", "road", "none"]
    assert [p.value for p in Parking] == [
        "ramp_flat",
        "ground_flat",
        "lift_flat",
        "flat_unknown_access",
        "mechanical",
        "mixed_with_flat",
        "mixed_no_flat",
        "none",
        "unknown",
    ]
    assert [e.value for e in Elevator] == ["yes", "no", "inferred", "unknown"]
    assert [b.value for b in BType] == ["大樓", "華廈", "公寓", "透天", "其他"]


def _listing(**overrides) -> ListingFacts:
    data = {
        "source": "cthouse",
        "source_id": "A123",
        "area": "taoyuan_core",
        "community_name": "示意社區",
        "community_key": "c-77",
        "road_text": "示意路一段",
        "district_hints": [("zip", "330")],
        "site_lat": 25.01,
        "site_lon": 121.31,
        "price_yuan": 11_110_000,
        "price_excl_parking": False,
        "parking_price_yuan": None,
        "site_original_price_yuan": 11_990_000,
        "main_cp": 2711,
        "main_kind": "main",
        "title_area_cp": 4422,
        "floor_lo": 5,
        "floor_hi": 6,
        "floors_total": 14,
        "built": {"lo": "2015-02-01", "hi": "2015-02-28"},
        "btype": "大樓",
        "elevator": "yes",
        "parking": "ramp_flat",
        "parking_public": False,
        "layout": "2房1廳1衛",
        "brand_origin_id": "9990001",
    }
    data.update(overrides)
    return ListingFacts.model_validate(data)


def test_listing_facts_round_trip():
    facts = _listing()
    assert ListingFacts.model_validate_json(facts.model_dump_json()) == facts
    assert ListingFacts.model_validate(facts.model_dump(mode="json")) == facts


def test_listing_facts_defaults_are_unknown():
    facts = ListingFacts(source="rakuya", source_id="9", area="zhongli_ic")
    assert facts.elevator is Elevator.UNKNOWN
    assert facts.parking is Parking.UNKNOWN
    assert facts.built == DateRange()


@pytest.mark.parametrize("field", ["title", "description", "contact_phone", "updated_at", "views"])
def test_listing_facts_reject_non_fact_fields(field):
    with pytest.raises(ValidationError):
        _listing(**{field: "x"})


def test_nan_infinity_and_blank_become_null():
    facts = _listing(site_lat=float("nan"), site_lon=float("inf"), layout="  ", price_yuan="")
    assert (facts.site_lat, facts.site_lon, facts.layout, facts.price_yuan) == (None,) * 4


def test_half_missing_coordinates_rejected():
    with pytest.raises(ValidationError):
        _listing(site_lon=None)


def test_facts_hash_tracks_facts_only():
    assert _listing().facts_hash() == _listing().facts_hash()
    assert _listing().facts_hash() != _listing(price_yuan=11_100_000).facts_hash()


def test_date_range_order():
    with pytest.raises(ValidationError):
        DateRange(lo=dt.date(2020, 2, 1), hi=dt.date(2020, 1, 1))


def test_tx_facts_and_location_round_trip():
    tx = TxFacts.model_validate(
        {
            "serial": "RPQXEXAMPLE0001",
            "release_id": "lvr-current-1",
            "trade_date": "2026-01-05",
            "district": "桃園區",
            "address_text": "桃園區示意路一段1~30號",
            "btype": "華廈",
            "floor_lo": 4,
            "floor_hi": 4,
            "floors_total": 7,
            "main_cm2": 7_321,
            "bldg_cm2": 11_234,
            "aux_cm2": 512,
            "balcony_cm2": 640,
            "built": {"lo": "2012-03-01", "hi": "2012-03-01"},
            "elevator": "yes",
            "parkings": [{"ptype": "lift_flat", "price_yuan": None, "area_cm2": 2_800}],
            "parking_n": 1,
            "total_yuan": 8_880_000,
            "flags": ["balcony_enclosed"],
            "exclusion": None,
        }
    )
    assert TxFacts.model_validate_json(tx.model_dump_json()) == tx
    loc = Location(lat=25.0, lon=121.3, precision="door", district="桃園區", door_id="d1")
    assert Location.model_validate_json(loc.model_dump_json()) == loc
    with pytest.raises(ValidationError):
        Location(lat=25.0, lon=121.3, precision="none")


def _group(gid: int, **overrides) -> dict:
    group = {
        "gid": gid,
        "first_seen": "2026-05-01T02:00:00Z",
        "baseline": False,
        "status": "active",
        "price_min": 11_110_000,
        "price_max": 11_220_000,
        "facts": {"floor_lo": 5, "floor_hi": 5, "elevator": "yes", "built_lo": "2015-01-01"},
        "loc": {"lat": 25.0, "lon": 121.3, "precision": "road", "district": "桃園區"},
        "metrics": {"road_frac": {"n300_r500": 0.25, "n1000_r0": 1.0}},
        "members": [
            {
                "src": "cthouse",
                "id": "A1",
                "price": 11_110_000,
                "first_seen": "2026-05-01T02:00:00Z",
                "status": "active",
            },
        ],
    }
    group.update(overrides)
    return group


def _tx(serial: str) -> dict:
    return {
        "serial": serial,
        "date": "2026-02-03",
        "district": "中壢區",
        "precision": "door",
        "lat": 24.95,
        "lon": 121.22,
        "elevator": "yes",
        "parking": [{"ptype": "ramp_flat", "price": 1_230_000, "area_cm2": 3_100}],
        "total": 9_870_000,
        "up": {
            "basis": "estimated",
            "q_lo": 8_500_000,
            "q_hi": 8_700_000,
            "k_lo": 9_000,
            "k_hi": 9_300,
        },
        "metrics": {"in_district": True, "drive_min": 10},
    }


def _meta() -> Meta:
    return Meta.model_validate(
        {
            "build_id": "20260601T010203Z-abc123",
            "generated_at": dt.datetime(
                2026, 6, 1, 9, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=8))
            ),
            "status": "ready",
            "sources": [{"id": "cthouse", "publish": True, "error_code": "count_short"}],
            "datasets": {"lvr": {"as_of": "2026-05-31"}, "doorindex": {"version": "示意版"}},
            "areas": [
                {
                    "id": "zhongli_ic",
                    "name": "示意區域",
                    "kind": "drive",
                    "options": {"drive_min": [5, 15]},
                    "files": ["data/listings_zhongli_ic.json", "geo/zhongli_ic.geojson"],
                },
            ],
            "freshness_hours": {"cthouse": 11, "lvr": 33},
        }
    )


def test_export_models_round_trip_through_export_json():
    meta = _meta()
    listings = ListingsFile.model_validate(
        {"area": "taoyuan_core", "groups": [_group(1), _group(2)]}
    )
    txs = TxFile.model_validate(
        {"area": "zhongli_ic", "as_of": "2026-05-31", "tx": [_tx("A"), _tx("B")]}
    )
    for model in (meta, listings, txs):
        text = export_json(model)
        assert text.endswith("\n")
        assert type(model).model_validate_json(text) == model
        data = json.loads(text)
        assert list(data) == sorted(data)
        assert data["schema_version"] == 1
    assert json.loads(export_json(meta))["generated_at"] == "2026-06-01T01:02:03Z"
    assert json.loads(export_json(meta))["sources"][0]["error_code"] == ErrorCode.COUNT_SHORT


def test_export_json_is_deterministic():
    first = ListingsFile.model_validate({"area": "taoyuan_core", "groups": [_group(1)]})
    reordered = dict(reversed(list(_group(1).items())))
    second = ListingsFile.model_validate({"groups": [reordered], "area": "taoyuan_core"})
    assert export_json(first) == export_json(second)


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        naive = dt.datetime(2026, 5, 1, 2, 0)  # noqa: DTZ001 -- 故意不帶時區
        ListingGroup.model_validate(_group(1, first_seen=naive))


def test_group_conflicts_must_match_facts():
    facts = {"floor_lo": "conflict", "floor_hi": 5}
    with pytest.raises(ValidationError):
        ListingGroup.model_validate(_group(1, facts=facts))
    conflicts = {"floor_lo": [{"src": "cthouse", "value": 4}, {"src": "rakuya", "value": 5}]}
    group = ListingGroup.model_validate(_group(1, facts=facts, conflicts=conflicts))
    assert group.facts.floor_lo == "conflict"
    with pytest.raises(ValidationError):
        ListingGroup.model_validate(
            _group(1, facts=facts, conflicts={"floor_lo": conflicts["floor_lo"][:1]})
        )


def test_records_must_be_sorted_and_unique():
    with pytest.raises(ValidationError):
        ListingsFile.model_validate({"area": "taoyuan_core", "groups": [_group(2), _group(1)]})
    with pytest.raises(ValidationError):
        TxFile.model_validate({"area": "zhongli_ic", "tx": [_tx("A"), _tx("A")]})


def test_unit_price_basis_rules():
    record = _tx("A")
    record["up"] = {"basis": "none", "q_lo": 1, "q_hi": 1, "k_lo": 1, "k_hi": 1}
    with pytest.raises(ValidationError):
        TxRecord.model_validate(record)
    record["up"] = {"basis": "official", "q_lo": 1, "q_hi": None, "k_lo": 1, "k_hi": 1}
    with pytest.raises(ValidationError):
        TxRecord.model_validate(record)


def test_export_rejects_nan_ratio_and_unknown_keys():
    with pytest.raises(ValidationError):
        ListingGroup.model_validate(_group(1, metrics={"road_frac": {"n300_r500": float("nan")}}))
    with pytest.raises(ValidationError):
        ListingGroup.model_validate(_group(1, title="不放標題"))
