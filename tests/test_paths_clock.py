"""paths（platformdirs＋環境變數）與 clock（台北日期、可注入時鐘）。"""

import datetime as dt

import pytest

from house_watch import paths
from house_watch.clock import FakeClock, SystemClock, from_iso, taipei_date, to_iso


def test_taipei_date_switches_at_utc_1600():
    assert taipei_date(dt.datetime(2026, 1, 1, 15, 59, 59, tzinfo=dt.UTC)) == dt.date(2026, 1, 1)
    assert taipei_date(dt.datetime(2026, 1, 1, 16, 0, 0, tzinfo=dt.UTC)) == dt.date(2026, 1, 2)


def test_iso_round_trip_and_naive_rejected():
    when = dt.datetime(2026, 3, 4, 13, 14, 15, 999, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    text = to_iso(when)
    assert text == "2026-03-04T05:14:15Z"
    assert from_iso(text) == when.replace(microsecond=0)
    naive = dt.datetime(2026, 1, 1)  # noqa: DTZ001 -- 故意不帶時區
    with pytest.raises(ValueError):
        to_iso(naive)
    with pytest.raises(ValueError):
        from_iso("2026-01-01T00:00:00")


def test_fake_clock_models_sleep():
    clock = FakeClock(dt.datetime(2026, 1, 1, tzinfo=dt.UTC), monotonic=100.0)
    clock.advance(30)
    clock.advance(3600, asleep=True)
    assert clock.now() == dt.datetime(2026, 1, 1, 1, 0, 30, tzinfo=dt.UTC)
    assert clock.monotonic() == 130.0
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_system_clock_is_utc_seconds():
    now = SystemClock().now()
    assert now.tzinfo is dt.UTC and now.microsecond == 0


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_CONFIG, str(tmp_path / "conf" / "mine.toml"))
    assert paths.settings_file() == (tmp_path / "conf" / "mine.toml", True)
    monkeypatch.delenv(paths.ENV_CONFIG)
    assert paths.settings_file() == (paths.default_config_dir() / "settings.toml", False)
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "d"))
    assert paths.resolve_data_dir(tmp_path / "ignored") == (tmp_path / "d").resolve()


def test_default_dirs_use_app_name_without_author():
    assert paths.default_data_dir().name == "house_watch"
    assert paths.default_config_dir().name == "house_watch"


def test_data_paths_and_emptiness(tmp_path):
    data = paths.DataPaths(tmp_path / "d")
    assert data.is_empty()
    data.root.mkdir()
    for name in (".DS_Store", "run.lock", "settings.toml"):
        (data.root / name).write_text("", encoding="utf-8")
    assert data.is_empty()
    data.logs_dir.mkdir()
    assert not data.is_empty()
    assert data.db.name == "house_watch.sqlite" and data.lock.name == "run.lock"
