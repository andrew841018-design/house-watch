"""`update --only lvr --area …` 端到端：下載頁 → 匯入 → kv → 匯出 tx_<區域>.json（MockTransport）。

實價登錄的合成檔用 tests/lvr/lvr_testkit.py 產生，全部是示意值。
"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from house_watch import cli, db
from house_watch.clock import FakeClock
from house_watch.export import site_dir
from house_watch.http import Fetcher, SourcePolicy
from house_watch.lvr import importer
from house_watch.lvr.fetch import (
    ACTIVE_URL,
    ALLOWED_HOSTS,
    BASE_URL,
    HISTORY_LIST_URL,
    SEASON_LIST_URL,
    batch_window,
    release_window,
)
from house_watch.models import TxFile, UpBasis
from house_watch.paths import DataPaths
from house_watch.settings import load_settings

NOW = dt.datetime(2026, 11, 21, 2, 0, tzinfo=dt.UTC)


def _kit():
    name = "lvr_testkit"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "lvr" / "lvr_testkit.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


kit = _kit()


def rows():
    tao = {"鄉鎮市區": "桃園區", "土地位置建物門牌": "桃園市桃園區示意路１號五樓"}
    mains = [
        kit.main_row(1, **tao),
        kit.main_row(2, **tao, 主建物面積=""),  # 主建物缺值：照樣保留（R1-11）
        kit.main_row(3, **tao, 主建物面積="30.00"),  # 主建物太小：公開範圍外
        kit.main_row(4, **tao, 備註="其他增建；"),  # 特殊交易：排除
        kit.main_row(5, 鄉鎮市區="中壢區", 土地位置建物門牌="桃園市中壢區示意路２號三樓"),
    ]
    parks = [kit.park_row(n) for n in range(1, 6)]
    return kit.release_files(mains, parks)


def pages():
    main, build, park = rows()
    season = f"{BASE_URL}/DownloadSeason?season=115S3&fileName="
    zipped = kit.make_zip(
        {
            "h_lvr_land_a.csv": main,
            "h_lvr_land_a_build.csv": build,
            "h_lvr_land_a_park.csv": park,
            "build_time.xml": kit.build_time_xml(*batch_window(dt.date(2026, 10, 1))),
        }
    )
    return {
        SEASON_LIST_URL: kit.season_html("115S3"),
        HISTORY_LIST_URL: kit.history_html(("20261001", batch_window(dt.date(2026, 10, 1)))),
        ACTIVE_URL: kit.active_html(*batch_window(dt.date(2026, 11, 11))),
        season + "H_lvr_land_A.csv": main,
        season + "H_lvr_land_A_build.csv": build,
        season + "H_lvr_land_A_park.csv": park,
        season + "build_time.xml": kit.build_time_xml(*release_window("s115S3")),
        f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001": zipped,
        f"{BASE_URL}/Download?fileName=h_lvr_land_a.csv": main,
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_build.csv": build,
        f"{BASE_URL}/Download?fileName=h_lvr_land_a_park.csv": park,
    }


@pytest.fixture
def served(monkeypatch):
    table = pages()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        assert request.headers["user-agent"].startswith("house-watch/")
        if url not in table:
            return httpx.Response(404)
        return httpx.Response(200, content=table[url])

    def factory(ctx):
        public = ctx.settings.public
        policy = SourcePolicy(ALLOWED_HOSTS, max_bytes=public.http.download_max_bytes)
        return Fetcher(
            ctx.conn,
            http=public.http,
            policies={importer.LVR_SOURCE: policy},
            clock=ctx.clock,
            transport=httpx.MockTransport(handler),
            sleep=ctx.clock.advance,
        )

    monkeypatch.setattr(importer, "FETCHER_FACTORY", factory)
    return seen


def run_lvr(area: str, clock: FakeClock):
    settings = load_settings()
    data = DataPaths(settings.data_dir)
    conn = db.open_or_init(data)
    try:
        plan = cli.plan_update(settings, only=["lvr"], areas=[area])
        report = cli.run_update(settings, conn, data=data, clock=clock, plan=plan)
        as_of = conn.execute("SELECT value FROM kv WHERE key = 'lvr.as_of'").fetchone()
    finally:
        conn.close()
    return report, as_of[0] if as_of else None, data


def read_tx(data: DataPaths, area: str) -> TxFile:
    return TxFile.model_validate_json((site_dir(data) / "data" / f"tx_{area}.json").read_bytes())


def test_update_only_lvr_end_to_end(served):
    clock = FakeClock(NOW)
    report, as_of, data = run_lvr("taoyuan_core", clock)
    assert [s.name for s in report.steps] == ["lvr", "export"]
    assert report.steps[0].status == "ok", report.steps[0].describe()
    assert as_of == "2026-10-31"
    assert all(url.startswith(f"{BASE_URL}/") for url in served)
    tx = read_tx(data, "taoyuan_core")
    assert tx.as_of == dt.date(2026, 10, 31)
    serials = [record.serial for record in tx.tx]
    assert serials == [kit.serial(1), kit.serial(2)], "範圍外與特殊交易不輸出，缺值保留"
    first = tx.tx[0]
    assert first.district == "桃園區" and first.up.basis == UpBasis.OFFICIAL
    assert first.metrics.in_district is True and first.total == 15_600_000
    assert first.parking[0].price == 1_500_000
    # 第二次：沒有新的發布，只讀三個下載頁
    served.clear()
    clock.advance(3600)
    report, _, _ = run_lvr("taoyuan_core", clock)
    assert report.steps[0].status == "ok"
    assert served == [SEASON_LIST_URL, HISTORY_LIST_URL, ACTIVE_URL]


def test_other_area_gets_its_own_transactions(served, monkeypatch, tmp_path):
    import os

    Path(os.environ["HOUSE_WATCH_CONFIG"]).write_text(
        'enabled_areas = ["taoyuan_core", "zhongli_ic"]\n', encoding="utf-8"
    )
    _, _, data = run_lvr("zhongli_ic", FakeClock(NOW))
    zhongli = read_tx(data, "zhongli_ic")
    assert [r.serial for r in zhongli.tx] == [kit.serial(5)]
    assert zhongli.tx[0].district == "中壢區"


def test_download_failure_is_reported_and_keeps_imports(served, monkeypatch):
    table = pages()
    del table[f"{BASE_URL}/DownloadHistory?type=history&fileName=20261001"]

    def factory(ctx):
        def handler(request):
            url = str(request.url)
            return httpx.Response(200, content=table[url]) if url in table else httpx.Response(404)

        return Fetcher(
            ctx.conn,
            http=ctx.settings.public.http,
            policies={importer.LVR_SOURCE: SourcePolicy(ALLOWED_HOSTS)},
            clock=ctx.clock,
            transport=httpx.MockTransport(handler),
            sleep=ctx.clock.advance,
        )

    monkeypatch.setattr(importer, "FETCHER_FACTORY", factory)
    report, as_of, _ = run_lvr("taoyuan_core", FakeClock(NOW))
    step = report.steps[0]
    assert step.status == "partial" and "lvr_invalid" in step.message
    assert as_of == "2026-09-10", "已匯入的季檔保留，資料截至日照實寫"
