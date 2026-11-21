"""下架判定（R1-10、R3-08；r4 §3.4 下架與重新上架）。

- 一切以（來源, 區域, 範圍版本）為單位：範圍版本就是掃描的 scope_hash。
  範圍一改（例如公開上限放寬、轉接器改了伺服器端條件），新的版本要重新累積有效掃描，
  舊版本的掃描不算數（R1-10 的「範圍一改，計數就歸零」）。
- 下架：最後一次看到之後（任何一種掃描看到都算，包括探測，以及後來判成無效的完整掃描裡
  讀到的頁），同一（來源, 區域, 範圍版本）已經有兩次有效的完整掃描開始。
  實作上就是：last_seen 早於「倒數第二次有效完整掃描」的開始時間。
- 有效＝scans.status 是 ok 或 accepted（db.SCAN_OK_STATUSES）。失敗或無效的掃描不計入，
  也不會讓計數歸零：它們根本不在這張清單裡。
- 重新上架只認同站同編號，由 versions.record_sighting 處理；跨編號的重新上架延後（R1-09）。
- 一戶要所有刊登都下架才算下架：群組的狀態在匯出時由成員決定（dedupe.export_groups）。
- 時間都是 clock.to_iso 的 UTC 字串（到秒），字串比較就是時間比較。
"""

import datetime as dt
import sqlite3
from dataclasses import dataclass

from house_watch.clock import from_iso, to_iso
from house_watch.db import SCAN_OK_STATUSES

GONE_AFTER_VALID_SCANS = 2  # 擁有者同意的規則：連續兩次有效完整掃描都沒看到

_OK = ", ".join(f"'{status}'" for status in SCAN_OK_STATUSES)


@dataclass(frozen=True)
class ScanRef:
    scan_id: int
    started_at: dt.datetime
    pages: int
    parsed: int


def valid_full_scans(
    conn: sqlite3.Connection, source: str, area: str, scope_hash: str, limit: int | None = None
) -> list[ScanRef]:
    """這個（來源, 區域, 範圍版本）的有效完整掃描，新的在前。"""
    sql = (
        "SELECT scan_id, started_at, pages, parsed FROM scans WHERE source = ? AND area = ?"
        f" AND scope_hash = ? AND mode = 'full' AND status IN ({_OK})"
        " ORDER BY started_at DESC, scan_id DESC"
    )
    params: tuple = (source, area, scope_hash)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [
        ScanRef(int(row[0]), from_iso(row[1]), int(row[2]), int(row[3]))
        for row in conn.execute(sql, params).fetchall()
    ]


def has_valid_full_scan(conn: sqlite3.Connection, source: str, area: str, scope_hash: str) -> bool:
    return bool(valid_full_scans(conn, source, area, scope_hash, limit=1))


def gone_cutoff(
    conn: sqlite3.Connection, source: str, area: str, scope_hash: str
) -> dt.datetime | None:
    """last_seen 早於這個時間的刊登就算下架；有效完整掃描不到兩次時是 None。"""
    scans = valid_full_scans(conn, source, area, scope_hash, limit=GONE_AFTER_VALID_SCANS)
    if len(scans) < GONE_AFTER_VALID_SCANS:
        return None
    return scans[-1].started_at


def mark_gone(
    conn: sqlite3.Connection, source: str, area: str, scope_hash: str, now: dt.datetime
) -> list[str]:
    """把該下架的刊登標成 gone，回傳它們的編號。呼叫端要在寫入交易裡。"""
    cutoff = gone_cutoff(conn, source, area, scope_hash)
    if cutoff is None:
        return []
    rows = conn.execute(
        "SELECT source_id FROM listings WHERE source = ? AND area = ? AND status = 'active'"
        " AND last_seen < ? ORDER BY source_id",
        (source, area, to_iso(cutoff)),
    ).fetchall()
    ids = [row[0] for row in rows]
    if ids:
        conn.executemany(
            "UPDATE listings SET status = 'gone', gone_at = ? WHERE source = ? AND source_id = ?",
            [(to_iso(now), source, source_id) for source_id in ids],
        )
    return ids
