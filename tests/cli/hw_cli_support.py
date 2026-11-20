"""CLI 測試共用：固定 provider、網頁檔與其他工作包的入口，結果不受其他工作包進度影響。"""

import os
import sys
import types
from pathlib import Path

from house_watch import cli, export
from house_watch.export import Providers
from house_watch.paths import DataPaths

ABSENT = "house_watch._absent_for_tests"
EMPTY_PROVIDERS = Providers(
    listing_groups=lambda ctx, area: [],
    tx_records=lambda ctx, area: [],
    area_geojson=lambda ctx, area: None,
)


def config_path() -> Path:
    return Path(os.environ["HOUSE_WATCH_CONFIG"])


def data_paths() -> DataPaths:
    return DataPaths(Path(os.environ["HOUSE_WATCH_DATA_DIR"]))


def write_config(text: str) -> None:
    config_path().write_text(text, encoding="utf-8")


def make_web(root: Path, *, index: bool = True) -> Path:
    files = {"app.js": "export const app = 1;\n", "test/x.test.mjs": "// 測試\n"}
    if index:
        files["index.html"] = "<!doctype html><title>示意</title>\n"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def isolate(monkeypatch, tmp_path: Path, *, providers=EMPTY_PROVIDERS, index: bool = True) -> None:
    """網頁檔用合成的；provider 固定；其他工作包的入口一律指到不存在的模組。"""
    web = make_web(tmp_path / "web", index=index)
    monkeypatch.setattr(export, "default_web_root", lambda: web)
    monkeypatch.setattr(export, "PROVIDERS", providers)
    absent = {name: f"{ABSENT}.{name}:entry" for name in cli.ENTRY_POINTS}
    monkeypatch.setattr(cli, "ENTRY_POINTS", absent)
    monkeypatch.setattr(export, "GEO_LOAD", f"{ABSENT}.geo:load")


def use_entries(monkeypatch, **specs: str) -> None:
    monkeypatch.setattr(cli, "ENTRY_POINTS", {**cli.ENTRY_POINTS, **specs})


def fake_module(monkeypatch, name: str, **attrs) -> str:
    """登記一個假的模組，讓 `name:屬性` 的入口拿得到。"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return name
