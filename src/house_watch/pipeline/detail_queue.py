"""詳細頁佇列（R1-21；r4 §3.4 的詳細頁佇列）。

- 依首次出現的先後排隊（FIFO：first_seen、再依編號），每個來源每一輪最多 detail_cap 個。
- 只排「還沒抓過詳細頁、轉接器說需要（needs_detail）、而且沒有已知事實落在公開範圍外」
  的在架刊登。判斷只用公開抓取範圍（default.toml [scope.listing]）與事實；Python 沒有、
  也不能有任何個人條件，所以佇列的內容不會洩漏條件（R1-21）。缺值一律照排（R1-11）。
- 抓完、解析成功：把結果寫成新版本（versions.append），並記 detail_fetched_at。
- 解析失敗（頁面不對、已下架、被轉到別頁）：原始回應存到資料夾外的 failed/（7 天後刪除，
  R2-12），一樣記 detail_fetched_at，不會每一輪重抓同一頁。
- 抓取失敗（被擋、5xx、逾時、今天的上限用完、截止時間到了）：這個來源這一輪不再抓，
  不記 detail_fetched_at，下一輪再排。
- 先抓、再開短交易寫（抓取層自己開交易，不能包在寫入交易裡）。
"""

import datetime as dt
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from house_watch.clock import taipei_date
from house_watch.db import transaction
from house_watch.errors import ErrorCode, HouseWatchError
from house_watch.http import FetchFailed, ResponseRejected, save_failed_response
from house_watch.models import ListingFacts
from house_watch.parse.age import add_years
from house_watch.pipeline import versions
from house_watch.settings import ListingScope
from house_watch.sources.base import Adapter, Blocked, PageInvalid, RequestSpec, Resp

YUAN_PER_WAN = 10_000


def outside_public_scope(facts: ListingFacts, scope: ListingScope, today: dt.date) -> bool:
    """已知的事實落在公開抓取範圍外（缺值一律不算在範圍外，R1-11）。"""
    if facts.price_yuan is not None and facts.price_yuan > scope.max_price_wan * YUAN_PER_WAN:
        return True
    if facts.btype is not None and facts.btype not in scope.btypes:
        return True
    if facts.main_cp is not None and facts.main_cp < scope.min_main_ping * 100:
        return True  # main_plus_aux 比只有主建物大，小於門檻時主建物一定也小於
    lo, hi = scope.floors
    if facts.floor_hi is not None and facts.floor_hi < lo:
        return True
    if facts.floor_lo is not None and facts.floor_lo > hi:
        return True
    oldest = add_years(today, -scope.max_age_years)
    return facts.built.hi is not None and facts.built.hi < oldest


def pending(
    conn: sqlite3.Connection,
    adapter: Adapter,
    *,
    scope: ListingScope,
    today: dt.date,
    limit: int,
) -> list[ListingFacts]:
    """這個來源這一輪要抓的詳細頁（依首次出現的先後）。"""
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT source_id FROM listings WHERE source = ? AND status = 'active'"
        " AND detail_fetched_at IS NULL ORDER BY first_seen, source_id",
        (adapter.source,),
    ).fetchall()
    queue: list[ListingFacts] = []
    for (source_id,) in rows:
        current = versions.latest(conn, adapter.source, source_id)
        if current is None:
            continue
        facts = current.facts
        if outside_public_scope(facts, scope, today) or not adapter.needs_detail(facts):
            continue
        queue.append(facts)
        if len(queue) >= limit:
            break
    return queue


def detail_spec(adapter: Adapter, base: ListingFacts) -> RequestSpec:
    """詳細頁的請求：轉接器有 detail_request_for（要用到列表其他欄位）就用它。"""
    by_facts = getattr(adapter, "detail_request_for", None)
    if callable(by_facts):
        return by_facts(base)
    return adapter.detail_request(base.source_id)


@dataclass
class DetailReport:
    """一個來源這一輪的詳細頁結果。error 是讓這個來源停下來的公開錯誤代碼。"""

    fetched: int = 0
    updated: int = 0
    invalid: int = 0
    error: ErrorCode | None = None
    stop_source: bool = False
    ids: list[str] = field(default_factory=list)


def run(
    conn: sqlite3.Connection,
    fetch: Callable[[RequestSpec], Resp],
    adapter: Adapter,
    *,
    scope: ListingScope,
    now: Callable[[], dt.datetime],
    limit: int,
    failed_dir: Path | None = None,
    in_time: Callable[[], bool] = lambda: True,
) -> DetailReport:
    """抓這個來源這一輪的詳細頁。fetch 是綁好來源與截止時間的抓取函式。"""
    report = DetailReport()
    queue = pending(conn, adapter, scope=scope, today=taipei_date(now()), limit=limit)
    for base in queue:
        if not in_time():
            report.error, report.stop_source = ErrorCode.TIMEOUT, True
            break
        resp: Resp | None = None
        try:
            resp = fetch(detail_spec(adapter, base))
            facts = adapter.parse_detail(resp, base)
        except PageInvalid:
            _save(resp, failed_dir, now())
            facts = None
        except ResponseRejected:
            facts = None
        except Blocked as exc:
            report.error, report.stop_source = exc.code, True
            break
        except FetchFailed as exc:
            report.error, report.stop_source = exc.code, True
            break
        except Exception as exc:
            # 解析詳細頁遇到沒預料到的資料（事後審查 P-2）：這一筆算無效、留下回應，繼續下一筆。
            # 設定類的錯誤照樣往外丟。
            if isinstance(exc, HouseWatchError):
                raise
            logging.getLogger("house_watch.tick").warning(
                "%s 詳細頁：轉接器沒預料到的例外 %s，這一筆算無效", adapter.source, type(exc).__name__
            )
            _save(resp, failed_dir, now())
            facts = None
        report.fetched += 1
        report.ids.append(base.source_id)
        stamp = now()
        with transaction(conn):
            if facts is None:
                report.invalid += 1
            elif versions.append(
                conn, facts, now=stamp, parser_version=adapter.parser_version, scan_id=None
            ):
                report.updated += 1
            versions.mark_detail_fetched(conn, base.source, base.source_id, stamp)
    return report


def _save(resp: Resp | None, failed_dir: Path | None, now: dt.datetime) -> None:
    if resp is not None and failed_dir is not None:
        save_failed_response(resp, failed_dir, now)
