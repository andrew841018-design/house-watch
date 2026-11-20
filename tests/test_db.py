"""C8 資料庫：結構、PRAGMA、開檔規則與「建新檔再搬資料」的遷移。"""

import sqlite3

import pytest

from house_watch import db
from house_watch.errors import DataDirError
from house_watch.paths import DataPaths

# 契約 C8 列出的主要欄位與鍵。
C8_COLUMNS = {
    "scans": {
        "scan_id",
        "source",
        "area",
        "mode",
        "started_at",
        "finished_at",
        "status",
        "pages",
        "parsed",
        "reported_total",
        "scope_hash",
        "error_code",
    },
    "listings": {
        "source",
        "source_id",
        "area",
        "first_seen",
        "last_seen",
        "last_full_seen_scan",
        "status",
        "gone_at",
        "baseline",
        "group_id",
        "detail_fetched_at",
    },
    "listing_versions": {
        "source",
        "source_id",
        "valid_from",
        "valid_to",
        "facts_hash",
        "facts_json",
        "price_yuan",
        "parser_version",
        "scan_id",
    },
    "groups": {"group_id", "first_seen", "baseline", "created_reason"},
    "community_points": {"source", "community_key", "lat", "lon", "method", "precision"},
    "transactions": {
        "serial",
        "release_id",
        "trade_date",
        "district",
        "facts_json",
        "lat",
        "lon",
        "precision",
        "doorindex_version",
    },
    "lvr_releases": {"release_id", "kind", "imported_at", "sha256"},
    "parking_medians": {
        "lvr_version",
        "level",
        "district",
        "ptype",
        "price_n",
        "price_p25_yuan",
        "price_p50_yuan",
        "price_p75_yuan",
        "area_n",
        "area_p25_cm2",
        "area_p50_cm2",
        "area_p75_cm2",
    },
    "source_state": {"source", "paused_until", "backoff_level", "robots_hash", "last_error"},
    "kv": {"key", "value"},
}
C8_KEYS = {
    "scans": ["scan_id"],
    "listings": ["source", "source_id"],
    "listing_versions": ["source", "source_id", "valid_from"],
    "groups": ["group_id"],
    "community_points": ["source", "community_key"],
    "transactions": ["serial"],
    "lvr_releases": ["release_id"],
    "parking_medians": ["lvr_version", "level", "district", "ptype"],
    "source_state": ["source"],
    "kv": ["key"],
}


@pytest.fixture
def conn(tmp_path):
    connection = db.open_or_init(DataPaths(tmp_path / "data"))
    yield connection
    connection.close()


def _seed_rows(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO scans (source, area, mode, started_at, status, scope_hash)"
            " VALUES ('cthouse', 'taoyuan_core', 'full', '2026-06-01T01:00:00Z', 'ok', 'h1')"
        )
        conn.execute(
            "INSERT INTO \"groups\" (first_seen, created_reason) VALUES ('2026-06-01T01:00:00Z', 'new')"
        )
        conn.execute(
            "INSERT INTO listings (source, source_id, area, first_seen, last_seen, group_id)"
            " VALUES ('cthouse', 'A1', 'taoyuan_core', '2026-06-01T01:00:00Z',"
            " '2026-06-01T01:00:00Z', 1)"
        )
        conn.execute(
            "INSERT INTO listing_versions (source, source_id, valid_from, facts_hash, facts_json,"
            " price_yuan, parser_version, scan_id) VALUES ('cthouse', 'A1', '2026-06-01T01:00:00Z',"
            " 'h', '{}', 11110000, 'cthouse-1', 1)"
        )
        conn.execute("INSERT INTO kv (key, value) VALUES ('k', 'v')")


def test_init_creates_all_c8_tables(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(C8_COLUMNS) == set(db.TABLES)
    for table, columns in C8_COLUMNS.items():
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        assert columns <= {row[1] for row in info}, table
        key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5] > 0]
        assert key == C8_KEYS[table], table


def test_constraints_and_foreign_keys(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listing_versions (source, source_id, valid_from, facts_hash, facts_json,"
            " parser_version) VALUES ('cthouse', 'missing', 't', 'h', '{}', 'p')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scans (source, area, mode, started_at, status, scope_hash)"
            " VALUES ('cthouse', 'taoyuan_core', 'partial', 't', 'ok', 'h')"
        )


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES ('a', 'b')")
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


def test_connect_refuses_other_versions(tmp_path):
    path = tmp_path / "x.sqlite"
    db.init_db(path).close()
    for version in (2, 0):
        raw = sqlite3.connect(path)
        raw.execute(f"PRAGMA user_version = {version}")
        raw.close()
        with pytest.raises(DataDirError):
            db.connect(path)


def test_connect_never_creates_a_file(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(DataDirError):
        db.connect(path)
    assert not path.exists()


def test_init_refuses_existing_file(tmp_path):
    path = tmp_path / "x.sqlite"
    db.init_db(path).close()
    with pytest.raises(DataDirError):
        db.init_db(path)


def test_open_or_init_only_initialises_empty_folders(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "run.lock").write_text("", encoding="utf-8")
    (root / "settings.toml").write_text("", encoding="utf-8")
    db.open_or_init(DataPaths(root)).close()
    assert (root / "instance.json").is_file()
    db.open_or_init(DataPaths(root)).close()

    (root / "house_watch.sqlite").rename(root / "moved.sqlite")
    with pytest.raises(DataDirError):
        db.open_or_init(DataPaths(root))

    other = tmp_path / "other"
    other.mkdir()
    (other / "something.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DataDirError):
        db.open_or_init(DataPaths(other))


def test_rebuild_copies_everything_and_keeps_backup(tmp_path):
    paths = DataPaths(tmp_path / "data")
    conn = db.open_or_init(paths)
    _seed_rows(conn)
    conn.close()

    backup = db.rebuild(paths.db)

    assert backup.is_file() and backup.name == "house_watch.sqlite.v1.bak"
    assert not paths.db.with_name("house_watch.sqlite.rebuild").exists()
    rebuilt = db.connect(paths.db)
    try:
        for table, expected in (
            ("scans", 1),
            ("groups", 1),
            ("listings", 1),
            ("listing_versions", 1),
            ("kv", 1),
        ):
            assert rebuilt.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == expected
        assert rebuilt.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        rebuilt.close()


def test_rebuild_refuses_newer_database(tmp_path):
    path = tmp_path / "x.sqlite"
    db.init_db(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 9")
    raw.close()
    with pytest.raises(DataDirError):
        db.rebuild(path)
