"""R2-12：轉接器 fixtures 不得含電話、證照號碼、email、LINE、cookie、token 或聯絡資料欄位。

逐欄位掃描：JSON 看每個鍵和每個字串值；HTML 看每個文字節點、每個屬性值，
以及 `__NUXT_DATA__` 這類內嵌 JSON 的鍵和值；Markdown 逐行看。
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sources"

PATTERNS = {
    "mobile": re.compile(r"(?<!\d)09\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"),
    "landline": re.compile(r"\(0\d{1,2}\)\s*\d{3,4}-?\d{4}|(?<![\d.])0\d{1,2}-\d{3,4}-?\d{4}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "licence": re.compile(r"(?:登字|營字|證字)第?\s*\d+\s*號|經紀人證|證照字號|營業員證"),
    "line_id": re.compile(r"(?i)\bline\s*(?:id|：|:)"),
    "cookie": re.compile(
        r"(?i)set-cookie|laravel_session|xsrf-token=|asp\.net_sessionid|routeid=|cf_clearance|__cf_bm"
    ),
}
# 40 字以上、像 base64 的字串：同時有大寫、小寫和數字。網址路徑（全小寫）與
# 十六進位雜湊（例如 robots.txt 的 sha256）不是秘密；Placeholder 開頭的是 fixtures 的佔位字。
TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])(?!Placeholder)[A-Za-z0-9+/_=-]{40,}")


def _has_token(value: str) -> bool:
    for match in TOKEN.finditer(value):
        text = match.group(0)
        if re.search(r"[A-Z]", text) and re.search(r"[a-z]", text) and re.search(r"\d", text):
            return True
    return False


FORBIDDEN_KEYS = {
    "brokercard",
    "email",
    "account_id",
    "franchise_id",
    "franchise_name",
    "employeeid",
    "phone",
    "mobile",
    "tel",
    "agent",
    "lineid",
    "contact",
    "contractno",
    "set-cookie",
    "cookie",
}


def _walk_json(node, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{where}.{key}#key", str(key)
            yield from _walk_json(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_json(value, f"{where}[{index}]")
    elif isinstance(node, str):
        yield where, node


def _fields(path: Path) -> Iterator[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        yield from _walk_json(json.loads(text), path.name)
    elif path.suffix == ".html":
        soup = BeautifulSoup(text, "html.parser")
        for number, string in enumerate(soup.find_all(string=True)):
            yield f"{path.name}#text{number}", str(string)
            stripped = str(string).strip()
            if stripped.startswith(("[", "{")):
                try:
                    yield from _walk_json(json.loads(stripped), f"{path.name}#json{number}")
                except ValueError:
                    pass
        for tag in soup.find_all(True):
            for name, value in tag.attrs.items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    yield f"{path.name}<{tag.name} {name}>", str(item)
    else:
        for number, line in enumerate(text.splitlines(), 1):
            yield f"{path.name}:{number}", line


def _files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_there_are_fixtures_for_each_site():
    sites = {p.parent.name for p in _files()}
    assert {"cthouse", "hbhousing", "etwarm"} <= sites


@pytest.mark.parametrize("path", _files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixture_has_no_personal_or_secret_fields(path):
    hits = []
    for where, value in _fields(path):
        if where.endswith("#key") and value.lower() in FORBIDDEN_KEYS:
            hits.append(f"{where}: 聯絡或個資欄位")
        for name, pattern in PATTERNS.items():
            if pattern.search(value):
                hits.append(f"{where}: {name}")
        if _has_token(value):
            hits.append(f"{where}: token")
    assert hits == []


def test_the_scanner_catches_what_it_should():
    samples = {
        "mobile": "聯絡 0912-345-678",
        "landline": "(03)3565-5080",
        "email": "someone@example.com",
        "licence": "(105)登字第012345號",
        "line_id": "LINE ID：abc",
        "cookie": "laravel_session=eyJpdiI6",
    }
    for name, sample in samples.items():
        assert PATTERNS[name].search(sample), name
    assert _has_token("eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9abcdefghijkl")
    assert not _has_token("PlaceholderCsrfToken000000000000000000000")
    assert not _has_token("16ceb5ee3e0dc13aa9adf31a3ebbe45a1d965b8c2b9f72eaf84e5911e140ed95")
    assert not PATTERNS["landline"].search("2026-11-21")
