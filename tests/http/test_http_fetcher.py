"""C6 抓取層：User-Agent、限速、timeout、大小上限、轉址、被擋偵測、退避、每日上限。全部離線。"""

import datetime as dt
import gzip
import re
import ssl
import types

import httpx
import pytest

import house_watch
from house_watch import db
from house_watch.errors import ErrorCode
from house_watch.http import (
    USER_AGENT,
    BudgetExhausted,
    DeadlineReached,
    ResponseRejected,
    SiteError,
    SourcePolicy,
    StateStore,
    blocked_code,
    daily_cap_of,
    parse_retry_after,
    policies_from_settings,
    prune_failed_responses,
    save_failed_response,
)
from house_watch.settings import load_settings
from house_watch.sources.base import FETCHED_AT_HEADER, Blocked, RequestSpec, Resp
from house_watch.sources.cthouse import CthouseAdapter
from house_watch.sources.etwarm import EtwarmAdapter
from house_watch.sources.hbhousing import HbhousingAdapter

HOST = "a.example"
URL = f"https://{HOST}/page"
FAR = 1e9
HOUR = 3600.0


def get(url: str = URL, **kwargs) -> RequestSpec:
    return RequestSpec.get(url, **kwargs)


class Recorder:
    """MockTransport 的 handler：依序回傳設定的回應，並記下每個請求與當時的單調時間。"""

    def __init__(self, clock, *responses):
        self.clock = clock
        self.responses = list(responses) or [httpx.Response(200, text="ok")]
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock.monotonic())
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if callable(item):
            return item(request)
        # 每次都給新的 Response 物件，不重複使用同一個串流
        return httpx.Response(item.status_code, headers=item.headers, content=item.content)


# ---------------------------------------------------------------- User-Agent、cookie、標頭


def test_user_agent_is_exactly_tool_and_version(make_fetcher, clock):
    handler = Recorder(clock)
    fetcher = make_fetcher(handler)
    spec = get(headers={"User-Agent": "Mozilla/5.0 (fake)", "Cookie": "sid=forged"})
    fetcher.fetch(spec, source="x", deadline=FAR)
    assert USER_AGENT == f"house-watch/{house_watch.__version__}"
    assert re.fullmatch(r"house-watch/[0-9][0-9A-Za-z.+-]*", USER_AGENT)
    sent = handler.requests[0].headers
    assert sent["user-agent"] == USER_AGENT
    assert "cookie" not in sent


def test_cookies_stay_in_memory_and_are_not_returned(make_fetcher, clock, conn):
    handler = Recorder(
        clock,
        httpx.Response(200, headers={"set-cookie": "sid=memory-only; Path=/"}, text="a"),
        httpx.Response(200, text="b"),
    )
    fetcher = make_fetcher(handler)
    first = fetcher.fetch(get(), source="x", deadline=FAR)
    fetcher.fetch(get(), source="x", deadline=FAR)
    assert "set-cookie" not in first.headers
    assert handler.requests[1].headers.get("cookie") == "sid=memory-only"
    dumped = "\n".join(conn.iterdump())
    assert "memory-only" not in dumped


def test_response_carries_fetch_time(make_fetcher, clock):
    resp = make_fetcher(Recorder(clock)).fetch(get(), source="x", deadline=FAR)
    assert isinstance(resp, Resp)
    assert resp.headers[FETCHED_AT_HEADER] == "2026-11-21T01:00:00Z"
    assert resp.status == 200 and resp.body == b"ok" and resp.url == URL


# ---------------------------------------------------------------- 限速與截止時間


def test_same_host_waits_at_least_four_seconds(make_fetcher, clock):
    handler = Recorder(clock)
    hosts = frozenset({HOST, "b.example"})
    fetcher = make_fetcher(handler, policies={"x": SourcePolicy(hosts)})
    fetcher.fetch(get(), source="x", deadline=FAR)
    fetcher.fetch(get(), source="x", deadline=FAR)
    fetcher.fetch(get("https://b.example/"), source="x", deadline=FAR)
    assert handler.times == [0.0, 4.0, 4.0]


def test_configured_interval_can_only_be_longer(make_fetcher, clock):
    handler = Recorder(clock)
    slow = make_fetcher(handler, policies={"x": SourcePolicy(frozenset({HOST}), min_interval_s=6)})
    slow.fetch(get(), source="x", deadline=FAR)
    slow.fetch(get(), source="x", deadline=FAR)
    assert handler.times == [0.0, 6.0]
    handler = Recorder(clock)
    fast = make_fetcher(handler, policies={"x": SourcePolicy(frozenset({HOST}), min_interval_s=1)})
    fast.fetch(get(), source="x", deadline=FAR)
    fast.fetch(get(), source="x", deadline=FAR)
    assert handler.times[1] - handler.times[0] == 4.0


def test_deadline_stops_before_waiting_and_is_not_a_site_failure(make_fetcher, clock, conn):
    handler = Recorder(clock)
    fetcher = make_fetcher(handler)
    fetcher.fetch(get(), source="x", deadline=FAR)
    with pytest.raises(DeadlineReached) as caught:
        fetcher.fetch(get(), source="x", deadline=clock.monotonic() + 2)
    assert caught.value.code is ErrorCode.TIMEOUT
    assert len(handler.requests) == 1
    store = StateStore(conn)
    assert store.kv_get("fetch.soft_failures.x") is None
    assert store.state("x").last_error is None


def test_timeouts_are_explicit_and_capped_by_deadline(make_fetcher, clock):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200)

    fetcher = make_fetcher(handler)
    fetcher.fetch(get(), source="x", deadline=FAR)
    assert seen[0] == {"connect": 10.0, "read": 30.0, "write": 30.0, "pool": 10.0}
    clock.advance(10)
    fetcher.fetch(get(), source="x", deadline=clock.monotonic() + 5)
    assert seen[1]["read"] == 5.0 and seen[1]["connect"] == 5.0


# ---------------------------------------------------------------- 5xx、逾時、TLS


def test_timeout_ends_the_run_for_that_source(make_fetcher, clock, conn):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    fetcher = make_fetcher(handler)
    with pytest.raises(SiteError) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.TIMEOUT
    with pytest.raises(SiteError):
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert len(calls) == 1, "本輪停止後不能再連線"
    state = StateStore(conn).state("x")
    assert state.last_error == "timeout" and state.paused_until is None


def test_three_consecutive_runs_of_5xx_start_long_backoff(make_fetcher, clock, conn):
    store = StateStore(conn)
    for run in range(3):
        fetcher = make_fetcher(lambda request: httpx.Response(503, text="busy"))
        with pytest.raises(SiteError) as caught:
            fetcher.fetch(get(), source="x", deadline=FAR)
        assert caught.value.code is ErrorCode.HTTP_5XX
        # 同一輪第二個請求不再連線，也不重複計數
        with pytest.raises(SiteError):
            fetcher.fetch(get(), source="x", deadline=FAR)
        state = store.state("x")
        if run < 2:
            assert state.paused_until is None
            assert store.kv_get("fetch.soft_failures.x") == str(run + 1)
            clock.advance(3 * HOUR)
    state = store.state("x")
    assert state.paused_until == clock.now() + dt.timedelta(hours=24)
    assert state.backoff_level == 1 and state.last_error == "http_5xx"
    assert store.kv_get("fetch.soft_failures.x") is None
    with pytest.raises(Blocked) as caught:
        make_fetcher(Recorder(clock)).fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.PAUSED


def test_success_resets_consecutive_failures(make_fetcher, clock, conn):
    store = StateStore(conn)
    with pytest.raises(SiteError):
        make_fetcher(lambda r: httpx.Response(500)).fetch(get(), source="x", deadline=FAR)
    clock.advance(HOUR)
    make_fetcher(lambda r: httpx.Response(404)).fetch(get(), source="x", deadline=FAR)
    assert store.kv_get("fetch.soft_failures.x") is None
    assert store.state("x").last_error is None


def test_tls_errors_are_reported_as_tls_error(make_fetcher, clock):
    def handler(request):
        raise httpx.ConnectError("handshake failed", request=request) from ssl.SSLCertVerificationError(
            1, "certificate verify failed"
        )

    with pytest.raises(SiteError) as caught:
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.TLS_ERROR


def test_other_connection_errors_count_as_timeout(make_fetcher):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(SiteError) as caught:
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.TIMEOUT


# ---------------------------------------------------------------- 被擋偵測與退避


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (403, {}, ErrorCode.BLOCKED),
        (429, {}, ErrorCode.RATE_LIMITED),
        (403, {"cf-mitigated": "challenge"}, ErrorCode.CHALLENGE),
        (200, {"cf-mitigated": "challenge"}, ErrorCode.CHALLENGE),
        (202, {"x-amzn-waf-action": "challenge"}, ErrorCode.CHALLENGE),
        (405, {"x-amzn-waf-action": "captcha"}, ErrorCode.CHALLENGE),
        (403, {"x-amzn-waf-action": "block"}, ErrorCode.BLOCKED),
    ],
)
def test_blocked_by_status_or_header(make_fetcher, clock, conn, status, headers, code):
    fetcher = make_fetcher(lambda r: httpx.Response(status, headers=headers, text="blocked"))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is code
    assert caught.value.retry_after_s == 24 * HOUR
    state = StateStore(conn).state("x")
    assert state.last_error == code.value and state.backoff_level == 1


def test_challenge_words_in_a_normal_page_are_not_a_block(make_fetcher):
    body = (
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
        '<script>var routes = {"captcha": "captcha/image", "challenge": "x"};</script>'
    )
    resp = make_fetcher(lambda r: httpx.Response(200, text=body)).fetch(
        get(), source="x", deadline=FAR
    )
    assert resp.status == 200 and b"captcha" in resp.body


def test_block_backoff_doubles_up_to_seven_days(make_fetcher, clock, conn):
    store = StateStore(conn)
    for hours in (24, 48, 96, 168, 168):
        with pytest.raises(Blocked) as caught:
            make_fetcher(lambda r: httpx.Response(403)).fetch(get(), source="x", deadline=FAR)
        assert caught.value.retry_after_s == hours * HOUR
        assert store.state("x").paused_until == clock.now() + dt.timedelta(hours=hours)
        clock.advance(hours * HOUR + 1)


def test_retry_after_is_honoured_when_longer(make_fetcher, clock, conn):
    two_days = str(int(48 * HOUR))
    fetcher = make_fetcher(lambda r: httpx.Response(429, headers={"retry-after": two_days}))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_s == 48 * HOUR
    assert StateStore(conn).state("x").backoff_level == 1


def test_short_retry_after_does_not_shorten_the_backoff(make_fetcher):
    fetcher = make_fetcher(lambda r: httpx.Response(429, headers={"retry-after": "60"}))
    with pytest.raises(Blocked) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.retry_after_s == 24 * HOUR


def test_paused_source_is_refused_without_network(make_fetcher, clock, conn):
    StateStore(conn).update_state("x", paused_until=clock.now() + dt.timedelta(hours=1))
    handler = Recorder(clock)
    with pytest.raises(Blocked) as caught:
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.PAUSED
    assert caught.value.retry_after_s == HOUR
    assert handler.requests == []


def test_backoff_level_resets_only_after_a_quiet_week(make_fetcher, clock, conn):
    store = StateStore(conn)
    with pytest.raises(Blocked):
        make_fetcher(lambda r: httpx.Response(403)).fetch(get(), source="x", deadline=FAR)
    clock.advance(24 * HOUR + 1)
    make_fetcher(Recorder(clock)).fetch(get(), source="x", deadline=FAR)
    state = store.state("x")
    assert state.backoff_level == 1, "暫停一結束就成功，倍數還不歸零"
    assert state.paused_until is None and state.last_error is None
    clock.advance(7 * 24 * HOUR)
    make_fetcher(Recorder(clock)).fetch(get(), source="x", deadline=FAR)
    assert store.state("x").backoff_level == 0


def test_blocked_code_ignores_body_and_unrelated_headers():
    assert blocked_code(200, {"server": "cloudflare"}) is None
    assert blocked_code(503, {}) is None
    assert blocked_code(404, {"x-amzn-waf-action": ""}) is None


def test_parse_retry_after():
    now = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)
    assert parse_retry_after("120", now) == 120.0
    assert parse_retry_after("Sat, 21 Nov 2026 03:00:00 GMT", now) == 2 * HOUR
    assert parse_retry_after("Fri, 20 Nov 2026 03:00:00 GMT", now) == 0.0
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after(None, now) is None


# ---------------------------------------------------------------- 大小上限與轉址


def test_size_cap_by_content_length(make_fetcher):
    policies = {"x": SourcePolicy(frozenset({HOST}), max_bytes=100)}
    fetcher = make_fetcher(lambda r: httpx.Response(200, content=b"x" * 101), policies=policies)
    with pytest.raises(ResponseRejected) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.PAGE_INVALID


def test_size_cap_while_streaming(make_fetcher):
    policies = {"x": SourcePolicy(frozenset({HOST}), max_bytes=100)}
    fetcher = make_fetcher(
        lambda r: httpx.Response(200, content=iter([b"x" * 60, b"x" * 60])), policies=policies
    )
    with pytest.raises(ResponseRejected):
        fetcher.fetch(get(), source="x", deadline=FAR)


def test_size_cap_counts_decompressed_bytes(make_fetcher):
    bomb = gzip.compress(b"\0" * 50_000)
    policies = {"x": SourcePolicy(frozenset({HOST}), max_bytes=10_000)}
    fetcher = make_fetcher(
        lambda r: httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}),
        policies=policies,
    )
    assert len(bomb) < 10_000
    with pytest.raises(ResponseRejected):
        fetcher.fetch(get(), source="x", deadline=FAR)


def test_request_can_set_its_own_size_cap(make_fetcher):
    fetcher = make_fetcher(lambda r: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(ResponseRejected):
        fetcher.fetch(get(max_bytes=10), source="x", deadline=FAR)


def test_redirect_within_allowed_hosts_is_followed(make_fetcher, clock):
    handler = Recorder(
        clock,
        httpx.Response(302, headers={"location": "/next"}),
        httpx.Response(200, text="done"),
    )
    resp = make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert resp.url == f"https://{HOST}/next" and resp.body == b"done"
    assert handler.times == [0.0, 4.0], "轉址的每一跳也要限速"


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/x", f"http://{HOST}/plain", "//evil.example/x", f"https://{HOST}:8443/x"],
)
def test_redirect_outside_allow_list_is_rejected(make_fetcher, clock, location):
    handler = Recorder(clock, httpx.Response(302, headers={"location": location}))
    with pytest.raises(ResponseRejected):
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert len(handler.requests) == 1


def test_post_is_never_redirected(make_fetcher, clock):
    handler = Recorder(clock, httpx.Response(307, headers={"location": "/other"}))
    spec = RequestSpec.post_json(URL, {"page": 1})
    with pytest.raises(ResponseRejected):
        make_fetcher(handler).fetch(spec, source="x", deadline=FAR)


def test_redirect_loop_is_rejected(make_fetcher):
    fetcher = make_fetcher(lambda r: httpx.Response(302, headers={"location": "/page"}))
    with pytest.raises(ResponseRejected):
        fetcher.fetch(get(), source="x", deadline=FAR)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/page",
        "https://evil.example/page",
        f"https://user:pw@{HOST}/page",
        f"https://{HOST}:8443/page",
    ],
)
def test_requests_only_to_allowed_https_hosts(make_fetcher, clock, url):
    handler = Recorder(clock)
    with pytest.raises(ValueError):
        make_fetcher(handler).fetch(get(url), source="x", deadline=FAR)
    assert handler.requests == []


def test_unknown_source_is_an_error(make_fetcher, clock):
    with pytest.raises(ValueError):
        make_fetcher(Recorder(clock)).fetch(get(), source="nope", deadline=FAR)


# ---------------------------------------------------------------- 每日上限、交易


def test_daily_cap_is_per_taipei_day_and_persisted(make_fetcher, clock):
    policies = {"x": SourcePolicy(frozenset({HOST}), daily_cap=2)}
    handler = Recorder(clock)
    fetcher = make_fetcher(handler, policies=policies)
    fetcher.fetch(get(), source="x", deadline=FAR)
    fetcher.fetch(get(), source="x", deadline=FAR)
    with pytest.raises(BudgetExhausted) as caught:
        fetcher.fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.BUDGET_EXHAUSTED
    with pytest.raises(BudgetExhausted):
        make_fetcher(handler, policies=policies).fetch(get(), source="x", deadline=FAR)
    assert len(handler.requests) == 2
    clock.advance(16 * HOUR)  # 台北時間隔天 01:00
    make_fetcher(handler, policies=policies).fetch(get(), source="x", deadline=FAR)
    assert len(handler.requests) == 3


def test_fetch_inside_a_write_transaction_is_refused(make_fetcher, clock, conn):
    fetcher = make_fetcher(Recorder(clock))
    with pytest.raises(RuntimeError), db.transaction(conn):
        fetcher.fetch(get(), source="x", deadline=FAR)


# ---------------------------------------------------------------- 設定


def test_policies_come_from_settings_and_adapters(tmp_path):
    local = tmp_path / "settings.toml"
    local.write_text("[sources.etwarm]\nenabled = false\n", encoding="utf-8")
    settings = load_settings(local_path=local)
    adapters = [CthouseAdapter(), HbhousingAdapter(), EtwarmAdapter()]
    policies = policies_from_settings(settings, adapters)
    assert set(policies) == {"cthouse", "hbhousing"}
    cthouse = policies["cthouse"]
    assert cthouse.allowed_hosts == frozenset({"buy.cthouse.com.tw"})
    assert cthouse.robots_urls == ("https://buy.cthouse.com.tw/robots.txt",)
    assert cthouse.min_interval_s >= 4
    assert cthouse.min_interval_s >= settings.sources["cthouse"].min_interval_s
    assert cthouse.daily_cap == 300  # 擁有者定的中信每日上限（PLAN_v2 §4）；不和被測函式自己比


def test_daily_cap_is_read_from_config_not_hard_coded():
    assert daily_cap_of(types.SimpleNamespace(max_requests_day=321)) == 321
    # 事後審查 AT-7：讀不到或不是正整數時是設定錯誤，不能當成「不設上限」。
    for bad in (types.SimpleNamespace(), types.SimpleNamespace(max_requests_day=0)):
        with pytest.raises(ValueError):
            daily_cap_of(bad)


# ---------------------------------------------------------------- 解析失敗時留下的回應（R2-12、R3-20）


def test_failed_responses_are_named_by_time_and_hash(tmp_path):
    now = dt.datetime(2026, 11, 21, 1, 2, 3, tzinfo=dt.UTC)
    resp = Resp(status=200, headers={"set-cookie": "no"}, body=b"<html>x</html>", url="https://a/CS123")
    path = save_failed_response(resp, tmp_path / "failed", now)
    assert re.fullmatch(r"20261121T010203Z-[0-9a-f]{16}\.bin", path.name)
    assert "CS123" not in path.name
    assert path.read_bytes() == b"<html>x</html>"
    old = save_failed_response(resp, tmp_path / "failed", now - dt.timedelta(days=8))
    assert prune_failed_responses(tmp_path / "failed", now) == 1
    assert path.exists() and not old.exists()


@pytest.mark.parametrize("location", ["https://a.example:99999/x", "https://[x/", "https://a.example:abc/"])
def test_a_malformed_redirect_is_rejected_not_a_crash(make_fetcher, clock, location):
    """事後審查 P-2：不合法的轉址網址要變成抓取層的錯誤（FetchFailed：ResponseRejected 或
    httpx 自己判的 SiteError），掃描接得住；不能丟 ValueError 讓整輪後面的來源與分群都跳過。"""
    from house_watch.http import FetchFailed

    handler = Recorder(clock, httpx.Response(302, headers={"location": location}))
    with pytest.raises(FetchFailed):
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)


def test_a_trickling_response_is_cut_at_the_total_time_limit(make_fetcher, clock):
    """事後審查 SA-3：伺服器每 25 秒送 1 個位元組時，每次讀取都在 read timeout 內，
    但整個回應要有總時間上限；否則 tick 會一直拿著執行鎖，之後的整點都不跑。"""

    def trickle():
        for _ in range(1000):
            clock.advance(25)
            yield b"x"

    handler = Recorder(clock, lambda request: httpx.Response(200, content=trickle()))
    with pytest.raises(SiteError) as caught:
        make_fetcher(handler).fetch(get(), source="x", deadline=FAR)
    assert caught.value.code is ErrorCode.TIMEOUT
