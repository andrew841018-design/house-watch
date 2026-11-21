"""C9 設定：公開預設可以載入，本機設定拒絕條件類的鍵，錯誤訊息不印出值。數字都是示意值。"""

import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from house_watch import paths
from house_watch.errors import ConfigError, ExitCode
from house_watch.models import AreaKind
from house_watch.settings import (
    CRITERIA_KEYS,
    find_criteria_keys,
    load_local,
    load_public,
    load_settings,
)

DEFAULT_TEXT = files("house_watch").joinpath("default.toml").read_text(encoding="utf-8")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_toml_loads_with_both_areas():
    public = load_public()
    assert [(a.id, a.kind) for a in public.areas] == [
        ("taoyuan_core", AreaKind.BUFFER),
        ("zhongli_ic", AreaKind.DRIVE),
    ]
    core, ic = public.areas
    assert set(core.options.as_export()) == {"near_m", "rca_m"}
    assert set(ic.options.as_export()) == {"drive_min"}
    assert core.scope.max_near_m >= max(core.options.near_m_options)
    assert ic.scope.max_drive_min >= max(ic.options.drive_min_options)
    assert core.geo["yiwen"].match, "藝文特區要依名稱挑 feature，不能用順序"
    assert ic.geo["drive"].band_property == "minutes"
    assert set(public.freshness_hours) == set(public.sources) | {"lvr", "doorindex"}
    assert all(source.min_interval_s >= 3 for source in public.sources.values())
    assert public.tls.relax_strict_hosts == []
    assert public.http.user_agent is None  # 擁有者已決定（PLAN_v2 §4 第 2 項）：固定 house-watch/<版本>，不能設定


def test_default_toml_has_no_criteria_keys():
    assert find_criteria_keys(tomllib.loads(DEFAULT_TEXT)) == []


@pytest.mark.parametrize(
    ("snippet", "path"),
    [
        ("price_max_wan = 1111\n", "price_max_wan"),
        ("[[profiles]]\nname = 'x'\n", "profiles"),
        ("[sources.cthouse]\nnear_m = 333\n", "sources.cthouse.near_m"),
        ("[publish]\nUnit-Price-Max-Wan = 22\n", "publish.Unit-Price-Max-Wan"),
        ("[http]\ncontact = 'x'\n[http.extra]\nbuilt_years = [1, 2]\n", "http.extra.built_years"),
    ],
)
def test_local_settings_reject_criteria_keys(tmp_path, snippet, path):
    with pytest.raises(ConfigError) as caught:
        load_local(_write(tmp_path, snippet))
    message = str(caught.value)
    assert path in message
    for value in ("1111", "333", "22"):
        assert value not in message
    assert caught.value.exit_code == ExitCode.CONFIG


def test_public_config_also_rejects_criteria_keys():
    with pytest.raises(ConfigError):
        load_public(DEFAULT_TEXT + "\n[profiles]\nname = 'x'\n")


def test_validation_errors_do_not_echo_values(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_local(
            _write(tmp_path, 'mystery = "secret-7741"\n[sources.cthouse]\nprobe_pages = "x9x"\n')
        )
    message = str(caught.value)
    assert "mystery" in message
    assert "secret-7741" not in message and "x9x" not in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_empty_local_settings_use_public_defaults():
    settings = load_settings()
    assert [area.id for area in settings.enabled_areas] == ["taoyuan_core"]
    assert settings.sources == settings.public.sources


def test_source_overrides_merge(tmp_path):
    path = _write(tmp_path, "enabled_areas = ['zhongli_ic']\n[sources.rakuya]\nprobe_pages = 4\n")
    settings = load_settings(local_path=path)
    assert [area.id for area in settings.enabled_areas] == ["zhongli_ic"]
    assert settings.sources["rakuya"].probe_pages == 4
    assert settings.sources["cthouse"] == settings.public.sources["cthouse"]


@pytest.mark.parametrize(
    "snippet",
    [
        "enabled_areas = ['nowhere']\n",
        "enabled_areas = []\n",
        "[sources.unknown_site]\nenabled = false\n",
        "[sources.cthouse]\nmin_interval_s = 1\n",
        "data_dir = 'relative/dir'\n",
        "not toml at all = = =\n",
    ],
)
def test_invalid_local_settings(tmp_path, snippet):
    with pytest.raises(ConfigError):
        load_settings(local_path=_write(tmp_path, snippet))


def test_explicit_settings_file_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_CONFIG, str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError):
        load_local()


def test_missing_default_settings_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_CONFIG)
    monkeypatch.setattr(paths, "default_config_dir", lambda: tmp_path / "nothing-here")
    assert load_local().enabled_areas is None


def test_data_dir_precedence(tmp_path, monkeypatch):
    configured = tmp_path / "from_settings"
    path = _write(tmp_path, f"data_dir = '{configured.as_posix()}'\n")
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "from_env"))
    assert load_settings(local_path=path).data_dir == (tmp_path / "from_env").resolve()
    monkeypatch.delenv(paths.ENV_DATA_DIR)
    assert load_settings(local_path=path).data_dir == configured
    assert load_settings().data_dir == paths.default_data_dir()


def test_criteria_key_list_covers_profile_fields():
    profile_fields = {
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
    assert profile_fields <= CRITERIA_KEYS
    assert "profiles" in CRITERIA_KEYS


def test_source_overrides_are_validated_after_merging(tmp_path):
    """事後審查 SA-7：本機覆寫合併後要重新檢查；每日上限只能調低（擁有者定的禮貌上限）。"""
    path = tmp_path / "settings.toml"
    path.write_text("[sources.rakuya]\nmax_requests_full = 9000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_requests_full"):
        load_settings(local_path=path)
    path.write_text("[sources.cthouse]\nmax_requests_day = 10\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_requests_full"):  # 單次 220 大於每日 10
        load_settings(local_path=path)
    path.write_text("[sources.cthouse]\nmax_requests_day = 5000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="只能調低"):
        load_settings(local_path=path)
    path.write_text("[sources.cthouse]\nmax_requests_full = 200\n", encoding="utf-8")
    assert load_settings(local_path=path).sources["cthouse"].max_requests_full == 200
