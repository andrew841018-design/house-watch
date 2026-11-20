"""WP7a 匯出：網站結構、欄位白名單、allow_nan、排序、大小上限、錯誤代碼、provider 缺席。"""

import hashlib
import json
import sys
import types

import pytest
from hw_export_samples import (
    EMPTY_PROVIDERS,
    NOW,
    SAMPLE_PROVIDERS,
    SECRET_PATH,
    WEB_PUBLISHED,
    fill_db,
    make_web,
    new_data,
    sample_groups,
    settings_for,
    tree_bytes,
    write_settings,
)

from house_watch import export
from house_watch.clock import FakeClock
from house_watch.export import (
    ExportError,
    NotAvailable,
    Providers,
    SizeLimits,
    export_dir,
    export_site,
    load_entry,
    load_manifest,
    manifest_path,
    site_dir,
)
from house_watch.models import ListingGroup, ListingsFile, Meta, Metrics, TxFile


@pytest.fixture
def env(tmp_path):
    data, conn = new_data(tmp_path / "data")
    settings = settings_for(write_settings(tmp_path / "settings.toml"))
    web = make_web(tmp_path / "web")
    yield data, conn, settings, web
    conn.close()


def _export(env, providers=SAMPLE_PROVIDERS, **kwargs):
    data, conn, settings, web = env
    return export_site(
        conn,
        kwargs.pop("settings", settings),
        data=data,
        clock=FakeClock(NOW),
        providers=providers,
        web_root=web,
        **kwargs,
    )


def _meta(data) -> Meta:
    return Meta.model_validate_json((site_dir(data) / "data" / "meta.json").read_bytes())


def test_empty_data_produces_valid_site(env):
    data = env[0]
    result = _export(env, EMPTY_PROVIDERS)
    site = site_dir(data)
    assert result.site_dir == site and result.complete
    meta = _meta(data)
    assert meta.status == "building"  # 還沒有成交，也沒有完整掃描（R3-15）
    assert [a.id for a in meta.areas] == ["taoyuan_core", "zhongli_ic"]
    for area in meta.areas:
        assert area.files == [f"data/listings_{area.id}.json", f"data/tx_{area.id}.json"]
        listings = ListingsFile.model_validate_json((site / area.files[0]).read_bytes())
        txs = TxFile.model_validate_json((site / area.files[1]).read_bytes())
        assert listings.groups == [] and txs.tx == [] and txs.as_of is None
    published = set(tree_bytes(site))
    assert published == WEB_PUBLISHED | {
        ".nojekyll",
        "data/meta.json",
        "data/listings_taoyuan_core.json",
        "data/tx_taoyuan_core.json",
        "data/listings_zhongli_ic.json",
        "data/tx_zhongli_ic.json",
    }
    assert {e.path for e in result.manifest.files} == published


def test_manifest_lists_every_file_with_hash(env):
    data = env[0]
    result = _export(env)
    manifest = load_manifest(manifest_path(data))
    assert manifest == result.manifest
    files = tree_bytes(site_dir(data))
    assert [e.path for e in manifest.files] == sorted(files)
    for entry in manifest.files:
        assert entry.bytes == len(files[entry.path])
        assert entry.sha256 == hashlib.sha256(files[entry.path]).hexdigest()
    assert manifest.build_id == _meta(data).build_id
    # manifest 不在網站裡；暫存資料夾也不留
    assert not (site_dir(data) / "manifest.json").exists()
    assert sorted(p.name for p in data.root.iterdir() if p.name.startswith(".")) == []


def test_sample_export_meta(env):
    data, conn, *_ = env
    fill_db(conn)
    result = _export(env)
    assert result.complete
    meta = _meta(data)
    assert meta.status == "ready"
    assert meta.generated_at == NOW
    sources = {s.id: s for s in meta.sources}
    assert list(sources) == sorted(sources)
    assert sources["cthouse"].last_ok.isoformat() == "2026-06-01T04:10:00+00:00"
    assert sources["cthouse"].last_full_ok.isoformat() == "2026-06-01T01:00:00+00:00"
    assert sources["cthouse"].error_code is None
    assert sources["hbhousing"].last_full_ok.isoformat() == "2026-05-30T02:00:00+00:00"
    assert sources["hbhousing"].error_code == "count_short"
    assert sources["rakuya"].error_code == "blocked"
    assert sources["rakuya"].paused_until.isoformat() == "2026-06-09T00:00:00+00:00"
    assert sources["etwarm"].error_code is None  # 不是公開代碼的文字一律不輸出
    assert sources["century21"].paused_until is None  # 暫停已過期
    assert sources["century21"].last_ok is None  # 沒啟用的區域不算
    assert meta.datasets.lvr.as_of.isoformat() == "2026-05-21"
    assert meta.datasets.doorindex.version == "示意版 2026-05"
    assert set(meta.freshness_hours) == set(sources) | {"lvr", "doorindex"}
    core = meta.areas[0]
    assert core.options == env[2].public.areas[0].options.as_export()  # 公開的選項清單原樣輸出
    assert set(core.options) == {"near_m", "rca_m"}
    assert core.files == [
        "data/listings_taoyuan_core.json",
        "data/tx_taoyuan_core.json",
        "geo/taoyuan_core.geojson",
    ]


def test_records_are_sorted_and_canonical(env):
    data, conn, *_ = env
    fill_db(conn)
    _export(env)
    site = site_dir(data)
    listings = json.loads((site / "data" / "listings_taoyuan_core.json").read_text("utf-8"))
    assert [g["gid"] for g in listings["groups"]] == [3, 7]
    members = listings["groups"][1]["members"]
    assert [(m["src"], m["id"]) for m in members] == sorted((m["src"], m["id"]) for m in members)
    conflicts = listings["groups"][1]["conflicts"]["floor_lo"]
    assert [c["src"] for c in conflicts] == ["cthouse", "rakuya"]
    txs = json.loads((site / "data" / "tx_zhongli_ic.json").read_text("utf-8"))
    assert [t["serial"] for t in txs["tx"]] == ["RPSAMPLE0001", "RPSAMPLE0002", "RPSAMPLE0003"]
    assert txs["tx"][0]["flags"] == ["balcony_enclosed", "mixed_use", "small_addition"]
    assert txs["as_of"] == "2026-05-21"
    for path in site.joinpath("data").iterdir():
        text = path.read_text("utf-8")
        assert text.endswith("\n") and "\r" not in text
        assert (
            text
            == json.dumps(
                json.loads(text), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )


def _core_area():
    class _Area:
        id = "taoyuan_core"

    return _Area()


def _with_group(extra: dict | None = None, model=None):
    def groups(ctx, area):
        if model is not None:
            return [model]
        group = sample_groups(ctx, area)[0]
        group.update(extra or {})
        return [group]

    return Providers(listing_groups=groups, tx_records=lambda c, a: [], area_geojson=None)


def test_whitelist_rejects_unknown_field_and_keeps_old_export(env):
    data = env[0]
    _export(env)
    before = tree_bytes(export_dir(data))
    with pytest.raises(ExportError, match="白名單"):
        _export(env, _with_group({"title": "不能公開的標題"}))
    assert tree_bytes(export_dir(data)) == before
    assert sorted(p.name for p in data.root.iterdir() if p.name.startswith(".")) == []


def test_whitelist_rejects_subclass_with_extra_field(env):
    class Leaky(ListingGroup):
        model_config = ListingGroup.model_config | {"extra": "allow"}

    group = sample_groups(None, _core_area())[1]
    leaky = Leaky.model_validate({**group, "agent_phone": "0900-000-000"})
    with pytest.raises(ExportError, match="白名單"):
        _export(env, _with_group(model=leaky))


def test_nan_never_reaches_output(env):
    group = sample_groups(None, _core_area())[1]
    bad = ListingGroup.model_validate(group).model_copy(
        update={"metrics": Metrics.model_construct(road_frac={"n300_r0": float("nan")})}
    )
    with pytest.raises(ExportError):
        _export(env, _with_group(model=bad))


def test_member_from_unpublished_source_is_rejected(env, tmp_path):
    path = write_settings(
        tmp_path / "local.toml",
        'enabled_areas = ["taoyuan_core"]\n[sources.rakuya]\npublish = false\n',
    )
    settings = settings_for(path)
    with pytest.raises(ExportError, match="rakuya"):
        _export(env, _with_group(), settings=settings)
    # 沒有 rakuya 成員時可以匯出，meta 照實標 publish=false
    _export(env, EMPTY_PROVIDERS, settings=settings)
    sources = {s.id: s for s in _meta(env[0]).sources}
    assert sources["rakuya"].publish is False and sources["cthouse"].publish is True


def test_duplicate_gid_is_rejected(env):
    def groups(ctx, area):
        first = sample_groups(ctx, area)[1]
        return [first, dict(first)]

    with pytest.raises(ExportError, match="gid"):
        _export(env, Providers(groups, lambda c, a: [], None))


def test_error_text_and_paths_never_exported(env):
    data, conn, *_ = env
    fill_db(conn)
    _export(env)
    for content in tree_bytes(export_dir(data)).values():
        text = content.decode("utf-8", "replace")
        assert SECRET_PATH not in text and "Traceback" not in text and "someone-else" not in text


def test_size_limits(env):
    data = env[0]
    _export(env)
    before = tree_bytes(export_dir(data))
    with pytest.raises(ExportError, match="超過上限"):
        _export(env, limits=SizeLimits(data_file=200))
    with pytest.raises(ExportError, match="超過上限"):
        _export(env, limits=SizeLimits(site=1_000))
    with pytest.raises(ExportError, match="超過上限"):
        _export(env, limits=SizeLimits(meta=100))
    assert tree_bytes(export_dir(data)) == before


def test_missing_providers_give_empty_data_and_building(env):
    data, conn, *_ = env
    fill_db(conn)
    absent = Providers(
        listing_groups="house_watch._absent_for_tests.pipeline:export_groups",
        tx_records="house_watch.clock:no_such_function",
        area_geojson=None,
    )
    result = _export(env, absent)
    assert not result.complete
    text = "\n".join(result.problems)
    assert "listings：not yet available" in text
    assert "tx：interface not available" in text
    assert "geo：not yet available" in text
    meta = _meta(data)
    assert meta.status == "building"
    for area in meta.areas:
        assert not any(f.startswith("geo/") for f in area.files)
    txs = TxFile.model_validate_json(
        (site_dir(data) / "data" / "tx_taoyuan_core.json").read_bytes()
    )
    assert txs.tx == [] and txs.as_of is None  # 不自己補值


def test_load_entry_distinguishes_missing_from_broken(tmp_path, monkeypatch):
    with pytest.raises(NotAvailable, match="not yet available"):
        load_entry("house_watch._absent_for_tests:fn")
    with pytest.raises(NotAvailable, match="interface not available"):
        load_entry("house_watch.clock:no_such_function")
    package = tmp_path / "hw_probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "broken.py").write_text("import hw_probe_missing_dependency\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):  # 模組在、但它自己壞了：是真的錯誤
        load_entry("hw_probe_pkg.broken:fn")


def test_provider_exception_fails_whole_export(env):
    data = env[0]
    _export(env)
    before = tree_bytes(export_dir(data))

    def boom(ctx, area):
        raise RuntimeError("provider 壞了")

    with pytest.raises(ExportError, match="provider 失敗"):
        _export(env, Providers(SAMPLE_PROVIDERS.listing_groups, boom, None))
    assert tree_bytes(export_dir(data)) == before


POINT = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [121.3, 25.0]},
    "properties": {"layer": "marker", "name": ""},
}


def _with_geo(geo):
    return Providers(lambda c, a: [], lambda c, a: [], lambda c, a: geo)


@pytest.mark.parametrize(
    "geo",
    [
        {"type": "FeatureCollection", "features": [POINT], "extra": 1},
        {
            "type": "FeatureCollection",
            "features": [
                {**POINT, "geometry": {"type": "Point", "coordinates": [float("nan"), 1]}}
            ],
        },
        {"type": "FeatureCollection", "features": [{**POINT, "properties": {"role": {"x": 1}}}]},
        {"type": "FeatureCollection", "area": "zhongli_ic", "features": [POINT]},  # 別的區域
        ["not", "geojson"],
    ],
)
def test_bad_geojson_is_skipped_and_reported(env, geo):
    data = env[0]
    result = _export(env, _with_geo(geo))
    assert any(p.startswith("geo/taoyuan_core.geojson：略過") for p in result.problems)
    assert not (site_dir(data) / "geo" / "taoyuan_core.geojson").exists()
    core = _meta(data).areas[0]
    assert core.id == "taoyuan_core" and not any(f.startswith("geo/") for f in core.files)


def test_geo_glue_loads_geometry_once(env, monkeypatch):
    loads = []

    class FakeGeo:
        def site_geojson(self, area_id):
            return {"type": "FeatureCollection", "area": area_id, "features": [POINT]}

    def load(*, config):
        loads.append(config)
        return FakeGeo()

    module = types.ModuleType("hw_fake_geo_areas")
    module.load = load
    monkeypatch.setitem(sys.modules, "hw_fake_geo_areas", module)
    monkeypatch.setattr(export, "GEO_LOAD", "hw_fake_geo_areas:load")
    data = env[0]
    result = _export(env, Providers(lambda c, a: [], lambda c, a: [], export.geo_site_geojson))
    assert result.complete and len(loads) == 1
    for area in _meta(data).areas:
        assert f"geo/{area.id}.geojson" in area.files
        text = (site_dir(data) / "geo" / f"{area.id}.geojson").read_text("utf-8")
        assert f'"area":"{area.id}"' in text

    module.load = lambda *, config: object()  # 沒有 site_geojson
    result = _export(env, Providers(lambda c, a: [], lambda c, a: [], export.geo_site_geojson))
    assert any("interface not available" in p for p in result.problems)


def test_web_dir_cannot_shadow_export_dirs(env):
    web = env[3]
    (web / "data").mkdir()
    (web / "data" / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ExportError, match="撞名"):
        _export(env)


def test_missing_index_is_reported(env):
    (env[3] / "index.html").unlink()
    result = _export(env)
    assert "web：index.html not yet available" in result.problems


def test_build_id_follows_content_only(env):
    data = env[0]
    first = _export(env).manifest.build_id
    later = export_site(
        env[1],
        env[2],
        data=data,
        clock=FakeClock(NOW.replace(hour=9)),
        providers=SAMPLE_PROVIDERS,
        web_root=env[3],
    )
    assert later.manifest.build_id == first  # 只有時間變，資料沒變
    (env[3] / "app.js").write_text("export const app = 2;\n", encoding="utf-8")
    assert _export(env).manifest.build_id != first
