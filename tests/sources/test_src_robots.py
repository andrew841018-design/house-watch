"""R2-09：robots.txt 每天最多讀一次；內容雜湊一變就停用該站，等人確認。不解析規則。"""

import httpx
import pytest

from house_watch.errors import ErrorCode
from house_watch.http import SiteError, SourcePolicy, StateStore
from house_watch.sources.base import Blocked, PageInvalid, RequestSpec
from house_watch.sources.robots import BASELINE_KEY, CHECKED_KEY, robots_digest

HOST = "a.example"
ROBOTS = f"https://{HOST}/robots.txt"
PAGE = f"https://{HOST}/list"
ALLOW = "User-agent: *\nAllow: /\n"
DAY = 24 * 3600.0


def policies():
    return {"x": SourcePolicy(frozenset({HOST}), robots_urls=(ROBOTS,))}


class Site:
    """假網站：robots.txt 內容與狀態可以改，記下每個請求的路徑。"""

    def __init__(self, robots_text: str = ALLOW, robots_status: int = 200):
        self.robots_text = robots_text
        self.robots_status = robots_status
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(self.robots_status, text=self.robots_text)
        return httpx.Response(200, text="page")

    def count(self, path: str) -> int:
        return self.paths.count(path)


def fetch(fetcher):
    return fetcher.fetch(RequestSpec.get(PAGE), source="x", deadline=1e9)


def test_digest_ignores_line_endings_and_outer_whitespace():
    assert robots_digest([b"User-agent: *\r\nAllow: /\r\n"]) == robots_digest([b"  User-agent: *\nAllow: /"])
    assert robots_digest([b"a"]) != robots_digest([b"b"])
    assert robots_digest([b"a", b"b"]) != robots_digest([b"ab"])
    # 和 LIVE_CHECK.md 記錄的雜湊一致（內容是 User-agent: * / Allow: /）
    assert robots_digest([ALLOW.encode()]) == (
        "16ceb5ee3e0dc13aa9adf31a3ebbe45a1d965b8c2b9f72eaf84e5911e140ed95"
    )


def test_robots_is_read_at_most_once_per_taipei_day(make_fetcher, clock):
    site = Site()
    fetcher = make_fetcher(site, policies=policies())
    fetch(fetcher)
    fetch(fetcher)
    fetch(make_fetcher(site, policies=policies()))
    assert site.count("/robots.txt") == 1
    clock.advance(DAY)
    fetch(make_fetcher(site, policies=policies()))
    assert site.count("/robots.txt") == 2
    assert site.paths[0] == "/robots.txt", "robots.txt 要在第一個請求之前讀"


def test_a_changed_robots_pauses_the_source_until_accepted(make_fetcher, clock, conn):
    store = StateStore(conn)
    site = Site()
    fetch(make_fetcher(site, policies=policies()))
    assert store.kv_get(BASELINE_KEY.format(source="x")) == robots_digest([ALLOW.encode()])

    site.robots_text = ALLOW + "Disallow: /list\n"
    clock.advance(DAY)
    with pytest.raises(Blocked) as caught:
        fetch(make_fetcher(site, policies=policies()))
    assert caught.value.code is ErrorCode.ROBOTS_CHANGED
    assert store.state("x").last_error == "robots_changed"
    pages_before = site.count("/list")

    clock.advance(3600)
    with pytest.raises(Blocked):
        fetch(make_fetcher(site, policies=policies()))
    assert site.count("/list") == pages_before, "停用後不能再抓頁面"
    assert site.count("/robots.txt") == 2, "同一天不重讀 robots.txt"

    accepted = {"x": robots_digest([site.robots_text.encode()])}
    fetch(make_fetcher(site, policies=policies(), robots_accepted=accepted))
    assert store.state("x").last_error is None


def test_a_configured_hash_that_differs_pauses_immediately(make_fetcher):
    site = Site()
    fetcher = make_fetcher(site, policies=policies(), robots_accepted={"x": "0" * 64})
    with pytest.raises(Blocked) as caught:
        fetch(fetcher)
    assert caught.value.code is ErrorCode.ROBOTS_CHANGED
    assert site.count("/list") == 0


def test_missing_robots_counts_as_empty(make_fetcher, conn):
    site = Site(robots_status=404)
    fetch(make_fetcher(site, policies=policies()))
    assert StateStore(conn).state("x").robots_hash == robots_digest([b""])


def test_unreachable_robots_stops_the_run_and_is_retried(make_fetcher, clock, conn):
    store = StateStore(conn)
    site = Site(robots_status=503)
    with pytest.raises(SiteError):
        fetch(make_fetcher(site, policies=policies()))
    assert store.kv_get(CHECKED_KEY.format(source="x")) is None
    assert site.count("/list") == 0
    site.robots_status = 200
    clock.advance(3 * 3600)
    fetch(make_fetcher(site, policies=policies()))
    assert site.count("/list") == 1


def test_unexpected_robots_status_is_invalid(make_fetcher):
    with pytest.raises(PageInvalid):
        fetch(make_fetcher(Site(robots_status=401), policies=policies()))


def test_blocked_robots_is_a_block(make_fetcher, conn):
    with pytest.raises(Blocked) as caught:
        fetch(make_fetcher(Site(robots_status=403), policies=policies()))
    assert caught.value.code is ErrorCode.BLOCKED
    assert StateStore(conn).state("x").paused_until is not None
