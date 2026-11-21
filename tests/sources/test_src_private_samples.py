"""私人樣本測試（r4 §3.12）：設了 HOUSE_WATCH_PRIVATE_SAMPLES 才跑，否則略過。

環境變數指到第一批原始回應的資料夾（私人工作區，不在 repo 裡）。只檢查解析器
不出錯與基本不變式，不印出、不保存任何內容。
"""

import json
import os
from pathlib import Path

import pytest

from house_watch.models import ListingFacts
from house_watch.sources.base import FETCHED_AT_HEADER, AreaQuery, DistrictRef, PageInvalid, Resp
from house_watch.sources.cthouse import CthouseAdapter
from house_watch.sources.etwarm import EtwarmAdapter
from house_watch.sources.hbhousing import HbhousingAdapter

ROOT = os.environ.get("HOUSE_WATCH_PRIVATE_SAMPLES")
pytestmark = pytest.mark.skipif(not ROOT, reason="沒有設定 HOUSE_WATCH_PRIVATE_SAMPLES")

QUERY = AreaQuery(area="sample", districts=(DistrictRef("桃園區", "330"),), max_price_wan=9999)
HEADERS = {FETCHED_AT_HEADER: "2026-11-20T14:00:00Z"}


def _root() -> Path:
    return Path(ROOT or ".")


def _list_bodies(site: str, patterns: tuple[str, ...]) -> list[tuple[Path, bytes]]:
    found = []
    for pattern in patterns:
        for path in sorted(_root().glob(f"*/{site}/{pattern}")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and "response" in doc:
                doc = doc["response"]
            found.append((path, json.dumps(doc, ensure_ascii=False).encode("utf-8")))
    return found


def _check(adapter, bodies, *, allow_invalid=()):
    assert bodies, "找不到樣本"
    total = 0
    for path, body in bodies:
        resp = Resp(200, HEADERS, body, "https://example.invalid/")
        try:
            page = adapter.parse_list(resp, QUERY)
        except PageInvalid:
            assert path.name.startswith(allow_invalid), path.name
            continue
        assert page.skipped == 0, path.name
        ids = [f.source_id for f in page.items]
        assert len(ids) == len(set(ids)), path.name
        for facts in page.items:
            assert isinstance(facts, ListingFacts)
            assert facts.price_yuan is None or facts.price_yuan >= 100_000
        total += len(page.items)
    assert total > 0


def test_cthouse_samples_parse():
    _check(CthouseAdapter(), _list_bodies("cthouse", ("list_*.json", "empty_*.json")), allow_invalid=("empty_",))


def test_hbhousing_samples_parse():
    # list_api_style_* 是送錯型態代碼得到的 422 錯誤回應，應該判成 PageInvalid
    bodies = _list_bodies("hbhousing", ("list_api_*.json", "list_ALL_*.json"))
    _check(HbhousingAdapter(), bodies, allow_invalid=("list_api_style",))


def test_etwarm_samples_parse():
    _check(EtwarmAdapter(), _list_bodies("etwarm", ("list_*.json",)))


@pytest.mark.parametrize(
    ("adapter", "name", "source_id", "url"),
    [
        (CthouseAdapter(), "cthouse/detail_*.html", None, "https://buy.cthouse.com.tw/house/{id}.html"),
        (HbhousingAdapter(), "hbhousing/detail_*.html", None, "https://www.hbhousing.com.tw/detail?sn={id}"),
        (EtwarmAdapter(), "etwarm/detail_*.html", None, "https://www.etwarm.com.tw/houses/buy/{id}"),
    ],
)
def test_raw_detail_pages_parse(adapter, name, source_id, url):
    paths = sorted(_root().glob(f"*/{name}"))
    assert paths, "找不到詳細頁樣本"
    for path in paths:
        sid = path.stem.removeprefix("detail_")
        base = ListingFacts(source=adapter.source, source_id=sid, area="sample")
        resp = Resp(200, HEADERS, path.read_bytes(), url.format(id=sid))
        detailed = adapter.parse_detail(resp, base)
        assert detailed.source_id == sid
