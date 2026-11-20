"""update：步驟各自隔離、--only／--source／--area／--accept-scan、結束碼、交易回滾。"""

import datetime as dt
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from hw_cli_support import data_paths, fake_module, isolate, use_entries, write_config

from house_watch import cli, export
from house_watch.errors import ConfigError
from house_watch.export import site_dir
from house_watch.models import Meta

FAKE = "hw_fake_steps"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    record: list[str] = []

    class Fetcher:
        @classmethod
        def from_settings(cls, conn, settings, adapters, *, clock):
            record.append("fetcher")
            return cls()

        def close(self):
            record.append("fetcher closed")

    geo = SimpleNamespace(name="示意 GeoContext")

    def load_geo(*, config):
        record.append("geo")
        return geo

    def refresh(fetcher, data_dir, now, *, ctx):
        assert isinstance(fetcher, httpx.Client) and not fetcher.trust_env
        assert data_dir == data_paths().root and now.tzinfo is not None
        assert ctx is geo  # 範圍指標在建索引時一起算
        record.append("doorindex")
        return SimpleNamespace(version="示意版 2026-05")

    def run_lvr(ctx):
        ctx.fetcher(adapters=())  # C6 的 Fetcher，執行結束時要被關閉
        record.append("lvr")
        return SimpleNamespace(ok=True, summary="匯入示意資料", as_of=dt.date(2026, 5, 21))

    def run_sources(ctx):
        record.append(f"sources:{','.join(ctx.sources)}@{','.join(a.id for a in ctx.areas)}")

    def accept_scans(ctx):
        record.append(f"accept:{ctx.accept_scans}")

    fake_module(
        monkeypatch,
        FAKE,
        Fetcher=Fetcher,
        refresh=refresh,
        run_lvr=run_lvr,
        run_sources=run_sources,
        accept_scans=accept_scans,
        load_geo=load_geo,
    )
    monkeypatch.setattr(export, "GEO_LOAD", f"{FAKE}:load_geo")
    use_entries(
        monkeypatch,
        fetcher=f"{FAKE}:Fetcher",
        doorindex=f"{FAKE}:refresh",
        lvr=f"{FAKE}:run_lvr",
        sources=f"{FAKE}:run_sources",
        accept=f"{FAKE}:accept_scans",
    )
    return record


def _kv(key):
    conn = sqlite3.connect(data_paths().db)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def test_all_steps_succeed(calls, capsys):
    assert cli.main(["update"]) == 0
    assert calls == [
        "geo",
        "doorindex",
        "fetcher",
        "lvr",
        "sources:century21,cthouse,etwarm,hbhousing,rakuya@taoyuan_core",
        "fetcher closed",
    ]
    out = capsys.readouterr().out
    assert "lvr：完成（匯入示意資料）" in out and "export：完成" in out
    assert _kv("doorindex.version") == "示意版 2026-05"
    assert _kv("lvr.as_of") == "2026-05-21"
    meta = Meta.model_validate_json((site_dir(data_paths()) / "data" / "meta.json").read_bytes())
    assert meta.datasets.lvr.as_of == dt.date(2026, 5, 21)


def test_failing_step_does_not_stop_others(monkeypatch, calls, capsys):
    def broken_refresh(fetcher, data_dir, now, *, ctx):
        calls.append("doorindex")
        raise RuntimeError("門牌下載失敗")

    fake_module(monkeypatch, "hw_fake_broken", refresh=broken_refresh)
    use_entries(monkeypatch, doorindex="hw_fake_broken:refresh", sources="hw_not_there_yet:run")
    assert cli.main(["update"]) == 1
    out = capsys.readouterr().out
    assert "doorindex：失敗（RuntimeError: 門牌下載失敗）" in out
    assert "lvr：完成" in out
    assert "sources：沒有執行（not yet available" in out
    assert "export：完成" in out
    assert calls == ["geo", "doorindex", "fetcher", "lvr", "fetcher closed"]
    assert _kv("doorindex.last_check") is None


def test_open_transaction_is_rolled_back(monkeypatch, calls):
    def leaky(ctx):
        ctx.conn.execute("BEGIN IMMEDIATE")
        ctx.conn.execute("INSERT INTO kv (key, value) VALUES ('leaked', 'x')")
        raise RuntimeError("半途失敗")

    fake_module(monkeypatch, "hw_fake_leaky", run=leaky)
    use_entries(monkeypatch, lvr="hw_fake_leaky:run")
    assert cli.main(["update"]) == 1
    assert _kv("leaked") is None
    assert any(call.startswith("sources:") for call in calls)  # 下一步照樣能用資料庫


def test_config_error_inside_step_exits_2_but_others_run(monkeypatch, calls, capsys):
    def misconfigured(ctx):
        raise ConfigError("示意的設定錯誤")

    fake_module(monkeypatch, "hw_fake_config", run=misconfigured)
    use_entries(monkeypatch, lvr="hw_fake_config:run")
    assert cli.main(["update"]) == 2
    out = capsys.readouterr().out
    assert "lvr：失敗（示意的設定錯誤）" in out and "export：完成" in out


def test_partial_outcome_is_exit_1(monkeypatch, calls, capsys):
    fake_module(
        monkeypatch,
        "hw_fake_partial",
        run=lambda ctx: SimpleNamespace(ok=False, summary="兩站被擋"),
    )
    use_entries(monkeypatch, sources="hw_fake_partial:run")
    assert cli.main(["update"]) == 1
    assert "sources：部分完成（兩站被擋）" in capsys.readouterr().out


def test_only_selects_steps(calls, capsys):
    assert cli.main(["update", "--only", "lvr"]) == 0
    assert calls == ["fetcher", "lvr", "fetcher closed"]
    out = capsys.readouterr().out
    assert "doorindex" not in out and "export：完成" in out
    calls.clear()
    assert cli.main(["update", "--only", "export"]) == 0
    assert calls == []


def test_source_and_area_filters(calls):
    write_config('enabled_areas = ["taoyuan_core", "zhongli_ic"]\n')
    assert (
        cli.main(
            ["update", "--only", "sources", "--source", "rakuya,cthouse", "--area", "zhongli_ic"]
        )
        == 0
    )
    assert calls == ["sources:cthouse,rakuya@zhongli_ic"]


@pytest.mark.parametrize(
    "argv",
    [
        ["update", "--only", "bogus"],
        ["update", "--area", "zhongli_ic"],  # 沒有在 enabled_areas
        ["update", "--source", "nosuch"],
        ["update", "--accept-scan", "abc"],
        ["update", "--accept-scan", "0"],
    ],
)
def test_bad_selection_exits_2(calls, argv):
    assert cli.main(argv) == 2
    assert calls == []
    assert not data_paths().db.exists()


def test_disabled_source_cannot_be_selected(calls):
    write_config("[sources.etwarm]\nenabled = false\n")
    assert cli.main(["update", "--source", "etwarm"]) == 2


def test_accept_scan_runs_first(calls):
    assert (
        cli.main(["update", "--accept-scan", "5,7", "--accept-scan", "5", "--only", "export"]) == 0
    )
    assert calls == ["accept:(5, 7)"]


def test_accept_scan_not_available(monkeypatch, calls, capsys):
    use_entries(monkeypatch, accept="hw_not_there_yet:accept")
    assert cli.main(["update", "--accept-scan", "5", "--only", "export"]) == 1
    assert "accept：沒有執行（not yet available" in capsys.readouterr().out
