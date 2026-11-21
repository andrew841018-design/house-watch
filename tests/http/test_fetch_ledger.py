"""所有資料夾共用的抓取帳本（事後審查 SA-1、P-12）：第二個資料夾不能變成第二個獨立爬蟲。

兩個 Fetcher 用兩個不同的資料庫（等於兩個資料夾），共用 conftest 指定的帳本
（HOUSE_WATCH_LEDGER）。舊版每個資料夾各算各的，這幾個測試在舊版會失敗。
"""

import datetime as dt
import os

import httpx
import pytest

from house_watch import db
from house_watch.errors import ConfigError, ErrorCode
from house_watch.http import MIN_HOST_INTERVAL_S, BudgetExhausted, Fetcher, SourcePolicy
from house_watch.ledger import FetchLedger, ledger_path
from house_watch.settings import load_public
from house_watch.sources.base import Blocked, RequestSpec

HOST = "a.example"
SOURCE = "x"
FAR = 1e9


class Recorder:
    def __init__(self, clock, status: int = 200):
        self.clock = clock
        self.status = status
        self.times: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.times.append(self.clock.monotonic())
        return httpx.Response(self.status, text="ok")


@pytest.fixture
def two_folders(tmp_path, clock):
    """兩個資料夾（兩個 DB），同一個假時鐘；回傳 make(conn_index, handler, **policy)。"""
    conns = [db.init_db(tmp_path / "a.sqlite"), db.init_db(tmp_path / "b.sqlite")]
    made: list[Fetcher] = []

    def make(index: int, handler, **policy):
        fetcher = Fetcher(
            conns[index],
            http=load_public().http,
            policies={SOURCE: SourcePolicy(frozenset({HOST}), **policy)},
            clock=clock,
            transport=httpx.MockTransport(handler),
            sleep=clock.advance,
        )
        made.append(fetcher)
        return fetcher

    yield make
    for fetcher in made:
        fetcher.close()
    for conn in conns:
        conn.close()


def get() -> RequestSpec:
    return RequestSpec(method="GET", url=f"https://{HOST}/page")


def test_two_folders_share_the_daily_cap(two_folders, clock):
    first = two_folders(0, Recorder(clock), daily_cap=3)
    first.fetch(get(), source=SOURCE, deadline=FAR)
    first.fetch(get(), source=SOURCE, deadline=FAR)
    second = two_folders(1, Recorder(clock), daily_cap=3)
    second.fetch(get(), source=SOURCE, deadline=FAR)  # 合計第 3 個
    with pytest.raises(BudgetExhausted):
        second.fetch(get(), source=SOURCE, deadline=FAR)
    assert second.store.db_requests_on(SOURCE, dt.date(2026, 11, 21)) == 1, "各 DB 仍記自己的"


def test_a_block_in_one_folder_pauses_the_other(two_folders, clock):
    with pytest.raises(Blocked):
        two_folders(0, Recorder(clock, status=403)).fetch(get(), source=SOURCE, deadline=FAR)
    handler = Recorder(clock)
    with pytest.raises(Blocked) as caught:
        two_folders(1, handler).fetch(get(), source=SOURCE, deadline=FAR)
    assert caught.value.code is ErrorCode.PAUSED
    assert handler.times == [], "另一個資料夾不能再打剛擋我們的網站"


def test_host_interval_is_kept_across_folders(two_folders, clock):
    first = Recorder(clock)
    two_folders(0, first).fetch(get(), source=SOURCE, deadline=FAR)
    second = Recorder(clock)
    two_folders(1, second).fetch(get(), source=SOURCE, deadline=FAR)
    assert second.times[0] - first.times[0] >= MIN_HOST_INTERVAL_S


def test_counts_start_over_on_a_new_taipei_day(tmp_path):
    ledger = FetchLedger(tmp_path / "l" / "fetch_ledger.json")
    day = dt.date(2026, 11, 21)
    assert ledger.add_request(SOURCE, day) == 1
    assert ledger.add_request(SOURCE, day) == 2
    assert ledger.requests_on(SOURCE, day) == 2
    assert ledger.requests_on(SOURCE, day + dt.timedelta(days=1)) == 0


def test_a_broken_ledger_file_counts_as_empty(tmp_path):
    path = tmp_path / "l" / "fetch_ledger.json"
    path.parent.mkdir(mode=0o700)
    path.write_text("{not json", encoding="utf-8")
    ledger = FetchLedger(path)
    assert ledger.requests_on(SOURCE, dt.date(2026, 11, 21)) == 0
    assert ledger.paused_until(SOURCE) is None
    assert ledger.add_request(SOURCE, dt.date(2026, 11, 21)) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows 沒有 Unix 權限位元")
def test_a_folder_others_can_write_is_refused(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    folder.chmod(0o777)
    with pytest.raises(ConfigError, match="別人可以寫"):
        FetchLedger(folder / "fetch_ledger.json").requests_on(SOURCE, dt.date(2026, 11, 21))


def test_default_location_is_per_user_and_outside_data_folders(monkeypatch):
    monkeypatch.delenv("HOUSE_WATCH_LEDGER")
    path = ledger_path()
    assert path.name == "fetch_ledger.json"
    if os.name != "nt":
        assert path.parent == type(path)("/tmp") / f"house_watch-{os.getuid()}"


def test_a_request_in_flight_holds_the_other_folder(two_folders, clock, tmp_path):
    """修完再審 F1-1：一邊的請求還沒讀完時，另一個資料夾不能同時打同一主機；
    要等它讀完、再隔 4 秒才送（同一主機一把跨程序鎖，從等待、送出到讀完都拿著）。"""
    import threading
    import time

    second_rec = Recorder(clock)
    first_times: list[float] = []
    errors: list[BaseException] = []

    def other_folder():  # 另一個資料夾：自己的 DB、自己的執行緒（SQLite 連線不能跨執行緒）
        conn = db.init_db(tmp_path / "other.sqlite")
        try:
            fetcher = Fetcher(
                conn,
                http=load_public().http,
                policies={SOURCE: SourcePolicy(frozenset({HOST}))},
                clock=clock,
                transport=httpx.MockTransport(second_rec),
                sleep=clock.advance,
            )
            with fetcher:
                fetcher.fetch(get(), source=SOURCE, deadline=FAR)
        except BaseException as exc:  # 測試裡收集起來，主執行緒再判斷
            errors.append(exc)
        finally:
            conn.close()

    worker = threading.Thread(target=other_folder)

    def first_handler(request):
        first_times.append(clock.monotonic())
        worker.start()
        time.sleep(0.3)  # 另一個資料夾在這段時間想送出
        return httpx.Response(200, text="ok")

    two_folders(0, first_handler).fetch(get(), source=SOURCE, deadline=FAR)
    worker.join(timeout=10)
    assert not errors and second_rec.times, "另一個資料夾的請求最後要送得出去"
    assert second_rec.times[0] - first_times[0] >= MIN_HOST_INTERVAL_S
