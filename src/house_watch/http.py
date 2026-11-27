"""禮貌的抓取層（契約 C6）。

`Fetcher.fetch(spec, *, source, deadline) -> Resp`。一個 Fetcher 就是一次執行（一個 tick）：
cookie 只放在記憶體、執行結束就丟，不寫進任何檔案。

規則（PLAN_v2 §4 擁有者決定、R2-09、R2-10）：
- User-Agent 照實寫 `house-watch/<版本>`，不放帳號或聯絡方式；轉接器不能改它。
- 同一主機兩次請求之間至少 4 秒（設定的 min_interval_s 更大就用設定的），從上一個回應讀完起算。
- 連線與讀取都有 timeout；每個回應有大小上限（解壓縮後計算）；只跟隨 GET 的轉址，
  而且只能轉到該來源白名單上的 https 主機。
- 被擋的判斷只看狀態碼與標頭：403、429、`cf-mitigated`、`x-amzn-waf-action`。
  不看頁面裡的「captcha」「challenge」字樣（正常頁也會有）。資料錨點消失由轉接器的
  parse_* 丟 PageInvalid。
- 被擋：整個來源暫停並長時間退避：24 小時起，每次再被擋加倍，上限 7 天；
  有 Retry-After 而且更長就照它（最多 30 天）。最後一次暫停結束後滿 7 天都沒再被擋，
  倍數才歸零。
- 5xx、逾時、連線或 TLS 錯誤：只停本輪的這個來源（之後的請求直接失敗、不連線）；
  連續 3 輪都這樣才進入長時間退避。
- 每日請求上限：讀設定（`max_requests_day`，見 `policies_from_settings`），不寫死。
- 憑證驗證永遠開著。設定 `[tls] relax_strict_hosts` 列出的主機只清掉
  VERIFY_X509_STRICT 這一個旗標（TWCA 憑證鏈），主機名稱與憑證鏈的檢查都保留。
- 退避狀態寫在 `source_state`；每日請求數、連續失敗輪數、robots 檢查日寫在 `kv`。

資料庫：抓取層用呼叫端給的連線，每次寫入都是自己的短交易（BEGIN IMMEDIATE）。
所以呼叫 fetch() 時，這條連線不能正在寫入交易中（否則丟 RuntimeError）：
先抓完、再開交易寫結果。
"""

import datetime as dt
import hashlib
import json
import logging
import os
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from filelock import Timeout

from house_watch import __version__
from house_watch.clock import Clock, SystemClock, from_iso, taipei_date, to_iso
from house_watch.db import transaction
from house_watch.errors import ConfigError, ErrorCode, HouseWatchError
from house_watch.ledger import ENV_LEDGER, FetchLedger
from house_watch.settings import HttpConfig, Settings, SourceConfig, TlsConfig
from house_watch.sources import robots
from house_watch.sources.base import FETCHED_AT_HEADER, Adapter, Blocked, Fetch, RequestSpec, Resp

USER_AGENT = f"house-watch/{__version__}"

MIN_HOST_INTERVAL_S = 4.0  # 擁有者決定：同一主機兩次請求至少間隔 4 秒
# 一個回應從送出到讀完的總時間上限（事後審查 SA-3）：read timeout 只限單次讀取，
# 伺服器慢慢送時可以讀很久，tick 會一直拿著執行鎖。下載（實價登錄季檔）給長一點。
REQUEST_TOTAL_MAX_S = 120.0
DOWNLOAD_TOTAL_MAX_S = 900.0
MAX_REDIRECTS = 5
BACKOFF_BASE = dt.timedelta(hours=24)
BACKOFF_MAX = dt.timedelta(days=7)
BACKOFF_QUIET_RESET = dt.timedelta(days=7)
RETRY_AFTER_MAX = dt.timedelta(days=30)
SOFT_FAILURE_LIMIT = 3  # 5xx／逾時連續幾輪才長時間退避

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_STRIPPED_REQUEST_HEADERS = frozenset({"user-agent", "cookie", "host"})
_DROPPED_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})

_SOFT_KEY = "fetch.soft_failures.{source}"
_COUNT_KEY = "fetch.requests.{source}"
_RESET_KEY = "fetch.backoff_reset_at.{source}"


# ---------------------------------------------------------------- 例外


class FetchFailed(HouseWatchError):
    """這次沒抓到，但不是被擋。code 是公開錯誤代碼。"""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail


class SiteError(FetchFailed):
    """5xx、逾時、連線或 TLS 錯誤：這個來源本輪停止；連續 3 輪才長時間退避。"""


class ResponseRejected(FetchFailed):
    """回應超過大小上限、轉址到白名單外、無法解壓縮：code 是 page_invalid。"""


class BudgetExhausted(FetchFailed):
    """這個來源今天（台北日期）的請求上限用完了。"""


class DeadlineReached(FetchFailed):
    """本次執行的截止時間到了（不是網站的錯，不計入連續失敗）。"""


# ---------------------------------------------------------------- 設定


@dataclass(frozen=True)
class SourcePolicy:
    """一個來源的抓取規則。

    - allowed_hosts：只能連到、也只能轉址到這些主機（https）。
    - daily_cap：每天（台北日期）最多幾個請求，含 robots.txt 與轉址；None 表示不限。
    - max_bytes：回應大小上限；None 用 `[http] page_max_bytes`。
    - robots_urls：每天第一次請求前要檢查的 robots.txt；空的表示不檢查（例如政府開放資料）。
    """

    allowed_hosts: frozenset[str]
    min_interval_s: float = MIN_HOST_INTERVAL_S
    daily_cap: int | None = None
    max_bytes: int | None = None
    robots_urls: tuple[str, ...] = ()


def daily_cap_of(config: SourceConfig) -> int:
    """設定的每日請求上限（PLAN_v2 §4：擁有者定的禮貌上限，SourceConfig 必填）。

    讀不到或不是正整數就是設定錯誤，丟 ValueError；不能當成「不設上限」（事後審查 AT-7）。
    """
    value = getattr(config, "max_requests_day", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("來源設定缺少每日請求上限（max_requests_day）")
    return value


def policies_from_settings(settings: Settings, adapters: Iterable[Adapter]) -> dict[str, SourcePolicy]:
    """依設定與轉接器建每個來源的抓取規則。設定裡沒有、或停用的來源不建。"""
    sources = settings.sources
    policies: dict[str, SourcePolicy] = {}
    for adapter in adapters:
        config = sources.get(adapter.source)
        if config is None or not config.enabled:
            continue
        policies[adapter.source] = SourcePolicy(
            allowed_hosts=frozenset(adapter.hosts),
            min_interval_s=max(MIN_HOST_INTERVAL_S, float(config.min_interval_s)),
            daily_cap=daily_cap_of(config),
            robots_urls=tuple(adapter.robots_urls),
        )
    return policies


def make_ssl_context(*, relax_strict: bool = False) -> ssl.SSLContext:
    """憑證驗證一律開著。relax_strict 只清掉 VERIFY_X509_STRICT（R2-10）。"""
    context = httpx.create_ssl_context(trust_env=False)
    if relax_strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("TLS 憑證驗證必須開著")
    return context


# ---------------------------------------------------------------- 狀態（source_state、kv）


@dataclass(frozen=True)
class SourceState:
    paused_until: dt.datetime | None
    backoff_level: int
    robots_hash: str | None
    last_error: str | None


_STATE_COLUMNS = frozenset({"paused_until", "backoff_level", "robots_hash", "last_error"})


class StateStore:
    """source_state 與 kv 的讀寫。每次寫入都是自己的短交易。

    每日請求數另外記在所有資料夾共用的帳本（ledger，SA-1），requests_on 取兩者較大值。
    """

    def __init__(self, conn: Any, ledger: FetchLedger | None = None) -> None:
        self.conn = conn
        self.ledger = ledger if ledger is not None else FetchLedger()

    @contextmanager
    def _write(self) -> Iterator[Any]:
        if self.conn.in_transaction:
            raise RuntimeError("抓取層要自己的交易：不要在寫入交易中呼叫 fetch()")
        with transaction(self.conn) as conn:
            yield conn

    def state(self, source: str) -> SourceState:
        row = self.conn.execute(
            "SELECT paused_until, backoff_level, robots_hash, last_error"
            " FROM source_state WHERE source = ?",
            (source,),
        ).fetchone()
        if row is None:
            return SourceState(None, 0, None, None)
        paused = from_iso(row[0]) if row[0] else None
        return SourceState(paused, int(row[1] or 0), row[2], row[3])

    def update_state(self, source: str, **values: Any) -> None:
        unknown = set(values) - _STATE_COLUMNS
        if unknown:
            raise ValueError(f"source_state 沒有這些欄位：{sorted(unknown)}")
        if not values:
            return
        names = sorted(values)
        params = [to_iso(v) if isinstance(v, dt.datetime) else v for v in (values[n] for n in names)]
        assignments = ", ".join(f"{name} = ?" for name in names)
        with self._write() as conn:
            conn.execute("INSERT INTO source_state (source) VALUES (?) ON CONFLICT DO NOTHING", (source,))
            conn.execute(f"UPDATE source_state SET {assignments} WHERE source = ?", (*params, source))

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def kv_set(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def kv_delete(self, key: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # robots.RobotsStore
    def robots_hash(self, source: str) -> str | None:
        return self.state(source).robots_hash

    def set_robots_hash(self, source: str, digest: str) -> None:
        self.update_state(source, robots_hash=digest)

    def last_error(self, source: str) -> str | None:
        return self.state(source).last_error

    def set_last_error(self, source: str, code: str | None) -> None:
        self.update_state(source, last_error=code)

    def requests_on(self, source: str, day: dt.date) -> int:
        """這個來源在這個台北日期已送出的請求數：本資料夾的紀錄和共用帳本取較大值（SA-1）。"""
        return max(self.db_requests_on(source, day), self.ledger.requests_on(source, day))

    def db_requests_on(self, source: str, day: dt.date) -> int:
        """只看本資料夾 DB 的紀錄。"""
        raw = self.kv_get(_COUNT_KEY.format(source=source))
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return 0
        if not isinstance(data, dict) or data.get("date") != day.isoformat():
            return 0
        count = data.get("n")
        return count if isinstance(count, int) and count >= 0 else 0


# ---------------------------------------------------------------- 小工具


def parse_retry_after(value: str | None, now: dt.datetime) -> float | None:
    """Retry-After：秒數或 HTTP 日期。看不懂就是 None。"""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max((when - now).total_seconds(), 0.0)


def blocked_code(status: int, headers: Mapping[str, str]) -> ErrorCode | None:
    """只看狀態碼與標頭判斷被擋（R2-09）。"""
    if headers.get("cf-mitigated", "").strip():
        return ErrorCode.CHALLENGE
    waf = headers.get("x-amzn-waf-action", "").strip().lower()
    if waf in ("challenge", "captcha"):
        return ErrorCode.CHALLENGE
    if waf == "block" or status == 403:
        return ErrorCode.BLOCKED
    if status == 429:
        return ErrorCode.RATE_LIMITED
    return None


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _host_of(url: str) -> str | None:
    """網址的主機（https、沒有帳密、連接埠 443）；不合法的網址回 None（事後審查 P-2）。"""
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        if parts.port not in (None, 443):  # 連接埠不合法時 .port 會丟 ValueError
            return None
    except ValueError:
        return None
    return parts.hostname.lower()


def _backoff(level: int) -> dt.timedelta:
    return min(BACKOFF_BASE * (2 ** min(max(level - 1, 0), 8)), BACKOFF_MAX)


# ---------------------------------------------------------------- 抓取器


class Fetcher:
    """一次執行用的抓取器。用完要 close()（或用 with）。"""

    def __init__(
        self,
        conn: Any,
        *,
        http: HttpConfig,
        policies: Mapping[str, SourcePolicy],
        tls: TlsConfig | None = None,
        robots_accepted: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        ledger: FetchLedger | None = None,
    ) -> None:
        self.store = StateStore(conn, ledger)
        self.ledger = self.store.ledger
        if transport is None and os.environ.get(ENV_LEDGER):
            # 修完再審 F1-3：帳本換成副本的連網試跑，會變成獨立的爬蟲（每日上限、間隔、暫停都分開）。
            raise ConfigError("抓取帳本不在預設位置（HOUSE_WATCH_LEDGER）時不能連網；連網一定要共用同一份帳本")
        self._http = http
        self._policies = dict(policies)
        self._relax = frozenset(h.lower() for h in (tls.relax_strict_hosts if tls else ()))
        self._accepted = dict(robots_accepted or {})
        self._clock = clock or SystemClock()
        self._transport = transport
        self._sleep = sleep
        self._clients: dict[tuple[str, bool], httpx.Client] = {}
        self._last_done: dict[str, float] = {}
        self._ended: dict[str, ErrorCode] = {}
        self._soft_counted: set[str] = set()
        self.requests_sent: dict[str, int] = {}

    @classmethod
    def from_settings(
        cls, conn: Any, settings: Settings, adapters: Iterable[Adapter], **kwargs: Any
    ) -> "Fetcher":
        public = settings.public
        return cls(
            conn,
            http=public.http,
            tls=public.tls,
            robots_accepted=public.robots.accepted_hash,
            policies=policies_from_settings(settings, adapters),
            **kwargs,
        )

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def bind(self, source: str, deadline: float) -> Fetch:
        """給轉接器的 prepare(fetch) 用：綁定來源與截止時間。"""
        return lambda spec: self.fetch(spec, source=source, deadline=deadline)

    # ------------------------------------------------------------ 主流程

    def fetch(self, spec: RequestSpec, *, source: str, deadline: float) -> Resp:
        """送出一個請求（含轉址），回傳讀完的回應。

        例外：Blocked（被擋或暫停中）、SiteError（5xx／逾時／TLS，本輪停止）、
        ResponseRejected（太大、轉址到白名單外）、BudgetExhausted、DeadlineReached。
        4xx（403、429 以外）照常回傳，由轉接器判斷。
        """
        policy = self._policies.get(source)
        if policy is None:
            raise ValueError(f"沒有這個來源的抓取規則：{source}")
        if spec.method not in ("GET", "POST"):
            raise ValueError(f"不支援的方法：{spec.method}")
        if _host_of(spec.url) not in policy.allowed_hosts:
            raise ValueError(f"{source} 不能連到這個網址：{spec.url}")
        if source in self._ended:
            raise SiteError(self._ended[source], "這個來源本輪已停止")
        self._gate_pause(source)
        if spec.kind != "robots":
            self._ensure_robots(source, policy, deadline)
        return self._follow(spec, source, policy, deadline)

    def _gate_pause(self, source: str) -> None:
        """本資料夾或共用帳本（別的資料夾被擋過）任一邊還在暫停，就不送。"""
        now = self._clock.now()
        until = self.store.state(source).paused_until
        shared = self.ledger.paused_until(source)
        if shared is not None and shared > now + RETRY_AFTER_MAX:
            shared = None  # 不合理的未來時間（時鐘回撥、壞資料）不採用（F1-11）
        if until is not None and until > now and (shared is None or until > shared):
            self._ledger_write(self.ledger.set_paused, source, until)  # 帳本被清掉後補回（F1-2）
        if shared is not None and (until is None or shared > until):
            until = shared
        if until is not None and until > now:
            raise Blocked(ErrorCode.PAUSED, (until - now).total_seconds())

    def _ensure_robots(self, source: str, policy: SourcePolicy, deadline: float) -> None:
        configured = self._accepted.get(source)
        if policy.robots_urls:
            robots.ensure_robots(
                self.bind(source, deadline),
                self.store,
                source=source,
                urls=policy.robots_urls,
                configured=configured,
                today=taipei_date(self._clock.now()),
            )
        elif robots.is_changed(self.store, source, configured):
            raise Blocked(ErrorCode.ROBOTS_CHANGED)

    def _follow(
        self, spec: RequestSpec, source: str, policy: SourcePolicy, deadline: float
    ) -> Resp:
        url = spec.url
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._send(spec, url, source, policy, deadline)
            location = resp.header("location")
            if resp.status not in _REDIRECTS or not location:
                return resp
            if spec.method != "GET":
                raise ResponseRejected(ErrorCode.PAGE_INVALID, "POST 不跟隨轉址")
            try:
                target = urljoin(url, location)
            except ValueError:  # 例如 "https://[x/"（事後審查 P-2）
                raise ResponseRejected(ErrorCode.PAGE_INVALID, "轉址網址不合法") from None
            if _host_of(target) not in policy.allowed_hosts:
                raise ResponseRejected(ErrorCode.PAGE_INVALID, "轉址到白名單外的主機")
            url = target
        raise ResponseRejected(ErrorCode.PAGE_INVALID, "轉址次數太多")

    def _send(
        self, spec: RequestSpec, url: str, source: str, policy: SourcePolicy, deadline: float
    ) -> Resp:
        host = _host_of(url)
        assert host is not None
        # 同一主機的跨程序鎖（修完再審 F1-1）：另一個資料夾的請求還在進行時，要等它讀完。
        lock = self.ledger.host_lock(host)
        try:
            lock.acquire(timeout=max(0.0, deadline - self._clock.monotonic()))
        except Timeout:
            raise DeadlineReached(ErrorCode.TIMEOUT, "等同一主機的另一個請求逾時") from None
        try:
            return self._send_locked(spec, url, source, policy, deadline, host)
        finally:
            lock.release()

    def _send_locked(
        self,
        spec: RequestSpec,
        url: str,
        source: str,
        policy: SourcePolicy,
        deadline: float,
        host: str,
    ) -> Resp:
        self._check_budget(source, policy)
        self._wait_for_host(host, policy, deadline)
        self._count_request(source)
        limit = spec.max_bytes or policy.max_bytes or self._http.page_max_bytes
        headers = {
            name: value
            for name, value in spec.headers
            if name.lower() not in _STRIPPED_REQUEST_HEADERS
        }
        headers["User-Agent"] = USER_AGENT
        method = spec.method if url == spec.url else "GET"
        body = spec.body if method == "POST" else None
        client = self._client(source, host)
        total_max = DOWNLOAD_TOTAL_MAX_S if spec.kind == "download" else REQUEST_TOTAL_MAX_S
        stop_at = min(deadline, self._clock.monotonic() + total_max)
        try:
            with client.stream(
                method, url, headers=headers, content=body, timeout=self._timeout(deadline)
            ) as response:
                status = response.status_code
                resp_headers = {
                    k.lower(): v
                    for k, v in response.headers.items()
                    if k.lower() not in _DROPPED_RESPONSE_HEADERS
                }
                code = blocked_code(status, resp_headers)
                if code is not None:
                    retry_after = parse_retry_after(resp_headers.get("retry-after"), self._clock.now())
                    self._on_blocked(source, code, retry_after)
                if status >= 500:
                    self._on_soft_failure(source, ErrorCode.HTTP_5XX, f"HTTP {status}")
                content = self._read_capped(response, limit, stop_at)
                final_url = str(response.url)
        except httpx.TimeoutException:
            self._on_soft_failure(source, ErrorCode.TIMEOUT, "逾時")
        except httpx.DecodingError:
            raise ResponseRejected(ErrorCode.PAGE_INVALID, "回應無法解壓縮") from None
        except httpx.InvalidURL:
            raise ResponseRejected(ErrorCode.PAGE_INVALID, "網址不合法") from None
        except (httpx.TransportError, ssl.SSLError) as exc:
            if _is_tls_error(exc):
                self._on_soft_failure(source, ErrorCode.TLS_ERROR, "TLS 錯誤")
            self._on_soft_failure(source, ErrorCode.TIMEOUT, "連線錯誤")
        finally:
            self._last_done[host] = self._clock.monotonic()
            # 帳本寫不進去不能蓋掉原本的例外（F1-12）；這次請求有每主機鎖保護。
            self._ledger_write(self.ledger.mark_host, host, self._clock.now().timestamp())
        self._on_success(source)
        resp_headers[FETCHED_AT_HEADER] = to_iso(self._clock.now())
        return Resp(status=status, headers=resp_headers, body=content, url=final_url)

    # ------------------------------------------------------------ 連線

    def _client(self, source: str, host: str) -> httpx.Client:
        relax = host in self._relax
        key = (source, relax)
        client = self._clients.get(key)
        if client is None:
            options: dict[str, Any] = {
                "headers": {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"},
                "follow_redirects": False,
                "trust_env": False,
                "verify": make_ssl_context(relax_strict=relax),
            }
            if self._transport is not None:
                options["transport"] = self._transport
            client = httpx.Client(**options)
            self._clients[key] = client
        return client

    def _timeout(self, deadline: float) -> httpx.Timeout:
        remaining = deadline - self._clock.monotonic()
        read = max(1.0, min(self._http.read_timeout_s, remaining))
        connect = max(1.0, min(self._http.connect_timeout_s, remaining))
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)

    def _read_capped(self, response: httpx.Response, limit: int, stop_at: float) -> bytes:
        """讀完回應：超過大小上限 → ResponseRejected；超過總時間（stop_at，單調時間）→ 逾時。"""
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ResponseRejected(ErrorCode.PAGE_INVALID, "回應超過大小上限")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise ResponseRejected(ErrorCode.PAGE_INVALID, "回應超過大小上限")
            if self._clock.monotonic() > stop_at:
                raise httpx.ReadTimeout("回應的總時間超過上限")  # 照逾時處理（_send 接住）
            chunks.append(chunk)
        return b"".join(chunks)

    def _wait_for_host(self, host: str, policy: SourcePolicy, deadline: float) -> None:
        interval = max(MIN_HOST_INTERVAL_S, policy.min_interval_s)
        now = self._clock.monotonic()
        last = self._last_done.get(host)
        wait = 0.0 if last is None else max(0.0, last + interval - now)
        shared = self.ledger.host_last(host)  # 別的程序（別的資料夾）最後一次打這個主機
        if shared is not None:  # 最多等一個間隔：帳本的時間在未來時不採用（F1-11）
            wait = max(wait, min(shared + interval - self._clock.now().timestamp(), interval))
        if now + wait >= deadline:
            raise DeadlineReached(ErrorCode.TIMEOUT, "本次執行的截止時間到了")
        if wait > 0:
            self._sleep(wait)

    # ------------------------------------------------------------ 預算與退避

    def _check_budget(self, source: str, policy: SourcePolicy) -> None:
        if policy.daily_cap is None:
            return
        today = taipei_date(self._clock.now())
        if self.store.requests_on(source, today) >= policy.daily_cap:
            raise BudgetExhausted(ErrorCode.BUDGET_EXHAUSTED, "今天的請求上限用完了")

    def _count_request(self, source: str) -> None:
        today = taipei_date(self._clock.now())
        count = self.store.db_requests_on(source, today) + 1
        payload = json.dumps({"date": today.isoformat(), "n": count}, separators=(",", ":"))
        self.store.kv_set(_COUNT_KEY.format(source=source), payload)
        self.ledger.add_request(source, today, at_least=count)
        self.requests_sent[source] = self.requests_sent.get(source, 0) + 1

    def _ledger_write(self, write: Callable[..., Any], *args: Any) -> None:
        """寫帳本；失敗只記 log、不中斷（修完再審 F1-12）。讀帳本失敗才擋下不送。"""
        try:
            write(*args)
        except (ConfigError, OSError) as exc:
            logging.getLogger("house_watch.tick").warning("抓取帳本寫不進去：%s", type(exc).__name__)

    def _pause(self, source: str, code: ErrorCode, minimum: dt.timedelta) -> dt.timedelta:
        """長時間退避：倍數加一，暫停到 now + max(倍增的時間, minimum)。"""
        now = self._clock.now()
        level = self.store.state(source).backoff_level + 1
        pause = max(_backoff(level), minimum)
        until = now + pause
        # 先寫 DB，再寫帳本；帳本寫不進去也不能讓 DB 的暫停不見（F1-12）。
        self.store.update_state(source, paused_until=until, backoff_level=level, last_error=code.value)
        self._ledger_write(self.ledger.set_paused, source, until)  # 別的資料夾也要停
        self.store.kv_set(_RESET_KEY.format(source=source), to_iso(until + BACKOFF_QUIET_RESET))
        self.store.kv_delete(_SOFT_KEY.format(source=source))
        return pause

    def _on_blocked(self, source: str, code: ErrorCode, retry_after_s: float | None) -> None:
        minimum = dt.timedelta(0)
        if retry_after_s:
            minimum = min(dt.timedelta(seconds=retry_after_s), RETRY_AFTER_MAX)
        pause = self._pause(source, code, minimum)
        raise Blocked(code, pause.total_seconds())

    def _on_soft_failure(self, source: str, code: ErrorCode, detail: str) -> None:
        self._ended[source] = code
        if source not in self._soft_counted:
            self._soft_counted.add(source)
            key = _SOFT_KEY.format(source=source)
            count = int(self.store.kv_get(key) or 0) + 1
            if count >= SOFT_FAILURE_LIMIT:
                self._pause(source, code, dt.timedelta(0))
            else:
                self.store.kv_set(key, str(count))
                self.store.set_last_error(source, code.value)
        raise SiteError(code, detail)

    def _on_success(self, source: str) -> None:
        key = _SOFT_KEY.format(source=source)
        if self.store.kv_get(key) is not None:
            self.store.kv_delete(key)
        state = self.store.state(source)
        now = self._clock.now()
        changes: dict[str, Any] = {}
        if state.last_error and state.last_error != ErrorCode.ROBOTS_CHANGED:
            changes["last_error"] = None
        if state.paused_until is not None and state.paused_until <= now:
            changes["paused_until"] = None
        reset_at = self.store.kv_get(_RESET_KEY.format(source=source))
        if reset_at is not None and now >= from_iso(reset_at):
            changes["backoff_level"] = 0
            self.store.kv_delete(_RESET_KEY.format(source=source))
        if changes:
            self.store.update_state(source, **changes)


# ---------------------------------------------------------------- 解析失敗時留下的原始回應（R2-12、R3-20）


def save_failed_response(resp: Resp, failed_dir: Path, now: dt.datetime) -> Path:
    """只存 body（不存標頭與 cookie），檔名用時間＋雜湊，不用網站編號（R3-20）。

    放在 repo 外的資料夾（paths.DataPaths.failed_dir），7 天後由 prune_failed_responses 刪掉。
    """
    digest = hashlib.sha256(resp.body).hexdigest()[:16]
    stamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{stamp}-{digest}.bin"
    path.write_bytes(resp.body)
    return path


def prune_failed_responses(failed_dir: Path, now: dt.datetime, keep_days: int = 7) -> int:
    """刪掉超過 keep_days 天的失敗回應，回傳刪了幾個。只動符合命名格式的檔。"""
    if not failed_dir.is_dir():
        return 0
    cutoff = now.astimezone(dt.UTC) - dt.timedelta(days=keep_days)
    removed = 0
    for path in failed_dir.glob("*-*.bin"):
        try:
            stamp = dt.datetime.strptime(path.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if stamp < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
