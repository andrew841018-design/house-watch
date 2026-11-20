"""匯出測試用的合成資料（照契約 C2）。數字都是示意值，跟任何人的條件無關。

provider 刻意用 set 產生成員、旗標與衝突值，讓輸出順序跟 PYTHONHASHSEED 有關；
匯出必須自己排好順序，兩次輸出才會相同。
"""

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

from house_watch import db
from house_watch.clock import FakeClock
from house_watch.export import ExportContext, Providers, export_site
from house_watch.paths import DataPaths
from house_watch.settings import AreaConfig, load_settings

NOW = dt.datetime(2026, 6, 2, 3, 4, 5, tzinfo=dt.UTC)
BOTH_AREAS = 'enabled_areas = ["taoyuan_core", "zhongli_ic"]\n'
SECRET_PATH = "/Users/someone-else/private/runtime"  # 假的本機路徑，不能出現在輸出

WEB_FILES = {
    "index.html": "<!doctype html><title>示意</title>\n",
    "app.js": "export const app = 1;\n",
    "rules.js": "export const ENUMS = {};\n",
    "style.css": "body { margin: 0; }\n",
    "vendor/leaflet/leaflet.js": "/* 示意 */\n",
    "vendor/leaflet/LICENSE": "示意授權\n",
    # 下面這些不能進網站
    "test/rules.test.mjs": "// 測試\n",
    "test/rules_cases.json": "{}\n",
    "package.json": '{"private": true}\n',
    ".DS_Store": "x",
    "notes.txt~": "暫存\n",
}
WEB_PUBLISHED = {
    "index.html",
    "app.js",
    "rules.js",
    "style.css",
    "vendor/leaflet/leaflet.js",
    "vendor/leaflet/LICENSE",
}


def make_web(root: Path) -> Path:
    for rel, text in WEB_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root


def write_settings(path: Path, text: str = BOTH_AREAS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def settings_for(path: Path):
    return load_settings(local_path=path)


def fill_db(conn: sqlite3.Connection) -> None:
    """meta 會讀到的狀態：掃描紀錄、來源狀態、kv。"""
    scans = [
        # (source, area, mode, started, finished, status, error_code)
        (
            "cthouse",
            "taoyuan_core",
            "full",
            "2026-06-01T00:00:00Z",
            "2026-06-01T01:00:00Z",
            "ok",
            None,
        ),
        (
            "cthouse",
            "taoyuan_core",
            "probe",
            "2026-06-01T04:00:00Z",
            "2026-06-01T04:10:00Z",
            "ok",
            None,
        ),
        (
            "hbhousing",
            "zhongli_ic",
            "full",
            "2026-05-30T00:00:00Z",
            "2026-05-30T02:00:00Z",
            "ok",
            None,
        ),
        (
            "hbhousing",
            "zhongli_ic",
            "full",
            "2026-06-01T00:00:00Z",
            "2026-06-01T02:00:00Z",
            "invalid",
            "count_short",
        ),
        (
            "rakuya",
            "taoyuan_core",
            "full",
            "2026-06-01T05:00:00Z",
            "2026-06-01T05:01:00Z",
            "invalid",
            "blocked",
        ),
        (
            "etwarm",
            "taoyuan_core",
            "probe",
            "2026-06-01T06:00:00Z",
            "2026-06-01T06:01:00Z",
            "failed",
            "Traceback: /Users/someone-else/x.py",
        ),
        # 沒有啟用的區域：不算
        (
            "century21",
            "other_area",
            "full",
            "2026-06-01T00:00:00Z",
            "2026-06-01T00:30:00Z",
            "ok",
            None,
        ),
    ]
    with db.transaction(conn):
        for source, area, mode, started, finished, status, code in scans:
            conn.execute(
                "INSERT INTO scans (source, area, mode, started_at, finished_at, status,"
                " scope_hash, error_code) VALUES (?, ?, ?, ?, ?, ?, 'h', ?)",
                (source, area, mode, started, finished, status, code),
            )
        conn.executemany(
            "INSERT INTO source_state (source, paused_until, backoff_level, last_error)"
            " VALUES (?, ?, ?, ?)",
            [
                ("rakuya", "2026-06-09T00:00:00Z", 2, "blocked"),
                ("etwarm", None, 0, f"OSError at {SECRET_PATH}"),
                ("century21", "2026-05-01T00:00:00Z", 1, "rate_limited"),  # 暫停已過期
            ],
        )
        conn.executemany(
            "INSERT INTO kv (key, value) VALUES (?, ?)",
            [
                ("lvr.last_check", "2026-06-02T01:00:00Z"),
                ("lvr.as_of", "2026-05-21"),
                ("doorindex.last_check", "2026-06-01T02:00:00Z"),
                ("doorindex.version", "示意版 2026-05"),
            ],
        )


def _member(src: str, item_id: str, price: int) -> dict:
    return {
        "src": src,
        "id": item_id,
        "price": price,
        "price_first": price + 110_000,
        "price_drop_at": "2026-05-20T03:00:00Z",
        "site_original_price": None,
        "first_seen": "2026-05-01T02:00:00Z",
        "status": "active",
    }


def sample_groups(ctx: ExportContext, area: AreaConfig) -> list[dict]:
    if area.id == "taoyuan_core":
        members = [
            _member(src, item_id, price)
            for src, item_id, price in {
                ("cthouse", "A9001", 6_660_000),
                ("rakuya", "R0077", 6_770_000),
                ("hbhousing", "c4321", 6_660_000),
                ("etwarm", "E55", 6_880_000),
            }
        ]
        conflict_values = [{"src": src, "value": v} for src, v in {("rakuya", 7), ("cthouse", 6)}]
        first = {
            "gid": 7,
            "first_seen": "2026-05-01T02:00:00Z",
            "baseline": False,
            "status": "active",
            "price_min": 6_660_000,
            "price_max": 6_880_000,
            "facts": {
                "floor_lo": "conflict",
                "floor_hi": 9,
                "floors_total": 15,
                "elevator": "yes",
                "btype": "大樓",
                "main_cp": 3_456,
                "built_lo": "2007-03-01",
                "built_hi": "2007-03-31",
                "parking": "ramp_flat",
            },
            "conflicts": {"floor_lo": conflict_values},
            "loc": {"lat": 25.0123, "lon": 121.3012, "precision": "door", "district": "桃園區"},
            "metrics": {"d_zhonglu_m": 0, "d_yiwen_m": 777, "d_rca_m": 2_345},
            "members": members,
        }
        second = {
            "gid": 3,
            "first_seen": "2026-04-01T02:00:00Z",
            "baseline": True,
            "status": "gone",
            "gone_at": "2026-05-30T02:00:00Z",
            "facts": {"elevator": "unknown"},
            "loc": {"precision": "road", "district": "桃園區"},
            "metrics": {"road_frac": {"n300_r0": 0.25, "n0_r300": 1.0}},
            "members": [
                {
                    "src": "cthouse",
                    "id": "A0042",
                    "first_seen": "2026-04-01T02:00:00Z",
                    "status": "gone",
                }
            ],
        }
        return [first, second]
    return [
        {
            "gid": 11,
            "first_seen": "2026-05-11T02:00:00Z",
            "baseline": False,
            "status": "active",
            "price_min": 5_550_000,
            "price_max": 5_550_000,
            "facts": {"main_cp": 2_975, "elevator": "inferred"},
            "loc": {"lat": 24.9612, "lon": 121.2207, "precision": "site", "district": "中壢區"},
            "metrics": {"in_district": True, "drive_min": 10},
            "members": [_member("century21", "T1234", 5_550_000)],
        }
    ]


def sample_tx(ctx: ExportContext, area: AreaConfig) -> list[dict]:
    district = "桃園區" if area.id == "taoyuan_core" else "中壢區"
    metrics = (
        {"d_zhonglu_m": 120, "d_yiwen_m": 0, "d_rca_m": 3_210}
        if area.id == "taoyuan_core"
        else {"in_district": True, "drive_min": 6}
    )
    records = []
    for serial in {"RPSAMPLE0003", "RPSAMPLE0001", "RPSAMPLE0002"}:
        records.append(
            {
                "serial": serial,
                "date": "2026-03-04",
                "district": district,
                "addr": f"{district}示意路二段1~30號",
                "lat": 24.9555,
                "lon": 121.2222,
                "precision": "door",
                "btype": "華廈",
                "floor_lo": 6,
                "floor_hi": 6,
                "floors_total": 9,
                "main_cm2": 7_654,
                "built_lo": "2009-01-01",
                "built_hi": "2009-01-01",
                "elevator": "yes",
                "parking": [{"ptype": "ramp_flat", "price": 990_000, "area_cm2": 2_900}],
                "total": 7_770_000,
                "up": {
                    "basis": "estimated",
                    "q_lo": 6_700_000,
                    "q_hi": 6_900_000,
                    "k_lo": 7_000,
                    "k_hi": 7_300,
                },
                "flags": list({"balcony_enclosed", "small_addition", "mixed_use"}),
                "metrics": metrics,
            }
        )
    return records


def sample_geojson(ctx: ExportContext, area: AreaConfig) -> dict:
    square = [[121.30, 25.01], [121.31, 25.01], [121.31, 25.02], [121.30, 25.02], [121.30, 25.01]]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [square]},
                "properties": {"role": "near", "name": "示意範圍"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [121.305, 25.015]},
                "properties": {"role": "marker", "minutes": None},
            },
        ],
    }


SAMPLE_PROVIDERS = Providers(
    listing_groups=sample_groups, tx_records=sample_tx, area_geojson=sample_geojson
)
EMPTY_PROVIDERS = Providers(
    listing_groups=lambda ctx, area: [],
    tx_records=lambda ctx, area: [],
    area_geojson=lambda ctx, area: None,
)


def new_data(root: Path) -> tuple[DataPaths, sqlite3.Connection]:
    data = DataPaths(root)
    return data, db.open_or_init(data)


def tree_bytes(root: Path) -> dict[str, bytes]:
    """資料夾底下每個檔（相對路徑 → 內容）。"""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_sample_export(root: Path, settings_path: Path, web_root: Path) -> list[str]:
    """給確定性測試的子程序用：在 root 匯出一次，回傳 provider 原始的成員順序。"""
    data = DataPaths(root)
    conn = db.connect(data.db)
    try:
        settings = settings_for(settings_path)
        export_site(
            conn,
            settings,
            data=data,
            clock=FakeClock(NOW),
            providers=SAMPLE_PROVIDERS,
            web_root=web_root,
        )
        ctx = ExportContext(conn=conn, settings=settings, now=NOW, publish_sources=frozenset())
        area = settings.enabled_areas[0]
        return [m["src"] for m in sample_groups(ctx, area)[0]["members"]]
    finally:
        conn.close()


if __name__ == "__main__":
    order = run_sample_export(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    print(json.dumps(order))
