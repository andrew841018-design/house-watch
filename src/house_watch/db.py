"""SQLite 資料庫（契約 C8，只由 WP0 修改）。

- 一個檔案、WAL、`PRAGMA user_version = 1`、每個連線都開外鍵檢查。
- 結構是「小的狀態表＋append-only 的版本表」（r4 §3.8）：事實存成 JSON，
  只有查詢會用到的價格提成獨立欄位；價格歷史是版本表的檢視。
- 時間欄位一律存 UTC ISO 字串（clock.to_iso），日期存 YYYY-MM-DD。
- 資料庫版本和程式不同就拒絕開啟；比程式新的尤其不能動（R3-13）。
- 遷移不就地 ALTER：建新檔 → INSERT…SELECT → 檢查 → 換檔，舊檔留作備份（rebuild）。
- 門牌索引是另一個檔案（WP2），不在這裡。
- 抓取層（http.Fetcher）每次寫 source_state、kv 都開自己的短交易，所以呼叫 fetch() 時
  這條連線不能正在寫入交易中（否則 RuntimeError）。流程一律是：先抓、解析，再開交易寫結果。

`scans.status`（SCAN_STATUSES）：
- running：開始了還沒結束；下一輪開始時，留下來的 running 一律改成 failed（執行中斷）。
- ok：有效的完整掃描，或成功的探測。
- invalid：完整掃描讀完了但沒通過有效性檢查；error_code 寫原因（count_short、parse_rate_low…）。
- failed：抓取失敗或被擋而中斷；error_code 寫原因（blocked、http_5xx、budget_exhausted…）。
- accepted：擁有者用 `update --accept-scan` 手動接受的 invalid（只限 count_short）。
只有 ok 與 accepted 算成功（SCAN_OK_STATUSES）：下架計數、基準、最後成功時間都只看這兩種。

`kv` 的公開鍵（值都是字串）：KV_LVR_LAST_CHECK、KV_LVR_AS_OF、KV_DOORINDEX_LAST_CHECK、
KV_DOORINDEX_VERSION；其他鍵由各工作包自己命名，加上自己的前綴（例如 fetch.、robots.）。
"""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from house_watch.errors import DataDirError
from house_watch.models import Precision
from house_watch.paths import DataPaths

SCHEMA_VERSION = 1

SCAN_RUNNING = "running"
SCAN_OK = "ok"
SCAN_INVALID = "invalid"
SCAN_FAILED = "failed"
SCAN_ACCEPTED = "accepted"
SCAN_STATUSES = (SCAN_RUNNING, SCAN_OK, SCAN_INVALID, SCAN_FAILED, SCAN_ACCEPTED)
SCAN_OK_STATUSES = (SCAN_OK, SCAN_ACCEPTED)

KV_LVR_LAST_CHECK = "lvr.last_check"  # UTC ISO 時間
KV_LVR_AS_OF = "lvr.as_of"  # YYYY-MM-DD，實價登錄資料截至的日期
KV_DOORINDEX_LAST_CHECK = "doorindex.last_check"  # UTC ISO 時間
KV_DOORINDEX_VERSION = "doorindex.version"  # 門牌資料的版本文字

_PRECISIONS = ", ".join(f"'{p.value}'" for p in Precision)

SCHEMA = f"""
CREATE TABLE scans (
    scan_id        INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    area           TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('full', 'probe')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    pages          INTEGER NOT NULL DEFAULT 0,
    parsed         INTEGER NOT NULL DEFAULT 0,
    reported_total INTEGER,
    scope_hash     TEXT NOT NULL,
    error_code     TEXT
);
CREATE INDEX scans_by_source_area ON scans (source, area, mode, started_at);

CREATE TABLE "groups" (
    group_id       INTEGER PRIMARY KEY,
    first_seen     TEXT NOT NULL,
    baseline       INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
    created_reason TEXT NOT NULL
);

CREATE TABLE listings (
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    area                TEXT NOT NULL,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    last_full_seen_scan INTEGER REFERENCES scans (scan_id),
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'gone')),
    gone_at             TEXT,
    relisted_at         TEXT,
    baseline            INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
    group_id            INTEGER REFERENCES "groups" (group_id),
    detail_fetched_at   TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX listings_by_area ON listings (area, status);
CREATE INDEX listings_by_group ON listings (group_id);

CREATE TABLE listing_versions (
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    facts_hash     TEXT NOT NULL,
    facts_json     TEXT NOT NULL,
    price_yuan     INTEGER,
    parser_version TEXT NOT NULL,
    scan_id        INTEGER REFERENCES scans (scan_id),
    PRIMARY KEY (source, source_id, valid_from),
    FOREIGN KEY (source, source_id) REFERENCES listings (source, source_id)
);
CREATE INDEX listing_versions_by_parser ON listing_versions (parser_version);

CREATE TABLE community_points (
    source        TEXT NOT NULL,
    community_key TEXT NOT NULL,
    lat           REAL,
    lon           REAL,
    method        TEXT NOT NULL,
    precision     TEXT NOT NULL CHECK (precision IN ({_PRECISIONS})),
    PRIMARY KEY (source, community_key)
);

CREATE TABLE lvr_releases (
    release_id  TEXT NOT NULL PRIMARY KEY,
    kind        TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    sha256      TEXT NOT NULL
);

CREATE TABLE transactions (
    serial            TEXT NOT NULL PRIMARY KEY,
    release_id        TEXT NOT NULL REFERENCES lvr_releases (release_id),
    trade_date        TEXT NOT NULL,
    district          TEXT NOT NULL,
    facts_json        TEXT NOT NULL,
    lat               REAL,
    lon               REAL,
    precision         TEXT NOT NULL CHECK (precision IN ({_PRECISIONS})),
    doorindex_version TEXT
);
CREATE INDEX transactions_by_district_date ON transactions (district, trade_date);

CREATE TABLE parking_medians (
    lvr_version    TEXT NOT NULL,
    level          TEXT NOT NULL,
    district       TEXT NOT NULL,
    ptype          TEXT NOT NULL,
    price_n        INTEGER NOT NULL,
    price_p25_yuan INTEGER,
    price_p50_yuan INTEGER,
    price_p75_yuan INTEGER,
    area_n         INTEGER NOT NULL,
    area_p25_cm2   INTEGER,
    area_p50_cm2   INTEGER,
    area_p75_cm2   INTEGER,
    PRIMARY KEY (lvr_version, level, district, ptype)
);

CREATE TABLE source_state (
    source        TEXT NOT NULL PRIMARY KEY,
    paused_until  TEXT,
    backoff_level INTEGER NOT NULL DEFAULT 0,
    robots_hash   TEXT,
    last_error    TEXT
);

CREATE TABLE kv (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TABLES = (
    "scans",
    "groups",
    "listings",
    "listing_versions",
    "community_points",
    "lvr_releases",
    "transactions",
    "parking_medians",
    "source_state",
    "kv",
)

_INSTANCE_MARKER = {"app": "house_watch"}


def _sidecars(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]


def _remove(path: Path) -> None:
    for candidate in (path, *_sidecars(path)):
        candidate.unlink(missing_ok=True)


def _build(target: Path) -> None:
    """在 target 建一個只有結構的新資料庫（rollback journal 模式，關閉後不留附屬檔）。"""
    conn = sqlite3.connect(target, isolation_level=None)
    try:
        conn.executescript(f"BEGIN;{SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;")
    finally:
        conn.close()


def _open_existing(path: Path) -> sqlite3.Connection:
    """開啟既有檔案；檔案不存在時失敗，不會建立新檔。"""
    if not path.is_file():
        raise DataDirError(f"找不到資料庫：{path.name}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError:
        raise DataDirError("資料庫檔案損壞或不是 SQLite 檔") from None


def connect(path: Path) -> sqlite3.Connection:
    """開啟既有的資料庫，設定 WAL、外鍵與等待時間。版本不是 SCHEMA_VERSION 就拒絕。

    連線是 autocommit；要寫入時用 `transaction(conn)`。
    """
    conn = _open_existing(path)
    try:
        version = _user_version(conn)
        if version > SCHEMA_VERSION:
            raise DataDirError(
                f"資料庫版本 {version} 比程式支援的 {SCHEMA_VERSION} 新，拒絕執行；請升級程式"
            )
        if version != SCHEMA_VERSION:
            raise DataDirError(f"資料庫版本 {version} 和程式的 {SCHEMA_VERSION} 不同，需要先遷移")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise DataDirError("資料庫無法切換成 WAL 模式")
        conn.execute("PRAGMA synchronous = NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一個寫入交易（BEGIN IMMEDIATE）。正常結束就 COMMIT，有例外就 ROLLBACK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(path: Path) -> sqlite3.Connection:
    """建立新的資料庫並開啟。檔案已經存在就失敗。先寫暫存檔，完成後才換名。"""
    if path.exists():
        raise DataDirError(f"資料庫已經存在：{path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    _remove(tmp)
    _build(tmp)
    os.replace(tmp, path)
    return connect(path)


def open_or_init(data: DataPaths) -> sqlite3.Connection:
    """資料夾完全是空的才自動建立；否則一定要有 instance 標記和資料庫（R3 §1.1 (h)）。"""
    if data.is_empty():
        data.root.mkdir(parents=True, exist_ok=True)
        conn = init_db(data.db)
        data.instance_marker.write_text(
            json.dumps(_INSTANCE_MARKER) + "\n", encoding="utf-8", newline="\n"
        )
        return conn
    if not data.instance_marker.is_file():
        raise DataDirError("資料夾不是空的，也沒有 house_watch 的 instance 標記；不自動初始化")
    if not data.db.is_file():
        raise DataDirError("有 instance 標記卻沒有資料庫")
    return connect(data.db)


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return int(conn.execute(f'SELECT count(*) FROM {schema}."{table}"').fetchone()[0])


def _free_name(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{n}")
        n += 1
    return candidate


def rebuild(path: Path) -> Path:
    """遷移：照目前的 SCHEMA 建新檔，把舊檔的資料搬過去，檢查通過才換上。

    - 呼叫前要關掉這個資料庫的所有連線，並持有執行鎖。
    - 欄位只搬新舊兩邊都有的；新欄位用預設值。
    - 檢查：每張表筆數相同、integrity_check、foreign_key_check。
    - 舊檔（連同 -wal、-shm）改名成 `<檔名>.v<舊版本>.bak` 留作備份，回傳它的位置。
    """
    old = _open_existing(path)
    try:
        old_version = _user_version(old)
        if old_version > SCHEMA_VERSION:
            raise DataDirError("資料庫版本比程式新，不能遷移；請升級程式")
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        old.close()
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise DataDirError("資料庫還有其他連線開著，不能遷移")

    new = path.with_name(f"{path.name}.rebuild")
    _remove(new)
    _build(new)
    conn = sqlite3.connect(new, isolation_level=None)
    try:
        conn.execute("ATTACH DATABASE ? AS old", (str(path),))
        old_tables = {
            row[0] for row in conn.execute("SELECT name FROM old.sqlite_master WHERE type='table'")
        }
        copied = [table for table in TABLES if table in old_tables]
        conn.execute("BEGIN")
        for table in copied:
            old_columns = set(_table_columns(conn, "old", table))
            columns = [c for c in _table_columns(conn, "main", table) if c in old_columns]
            names = ", ".join(f'"{c}"' for c in columns)
            conn.execute(f'INSERT INTO main."{table}" ({names}) SELECT {names} FROM old."{table}"')
        conn.execute("COMMIT")
        short = [t for t in copied if _count(conn, "main", t) != _count(conn, "old", t)]
        integrity = conn.execute("PRAGMA main.integrity_check").fetchone()[0]
        dangling = conn.execute("PRAGMA main.foreign_key_check").fetchall()
        conn.execute("DETACH DATABASE old")
        if short or integrity != "ok" or dangling:
            raise DataDirError("遷移後的資料庫檢查沒有通過，舊檔沒有更動")
    except BaseException:
        conn.close()
        _remove(new)
        raise
    conn.close()

    backup = _free_name(path.with_name(f"{path.name}.v{old_version}.bak"))
    os.replace(path, backup)
    for side in _sidecars(path):
        if side.exists():
            os.replace(side, Path(f"{backup}{side.name[len(path.name) :]}"))
    os.replace(new, path)
    return backup
