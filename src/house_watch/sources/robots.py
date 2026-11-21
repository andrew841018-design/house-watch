"""robots.txt：每天最多讀一次，內容雜湊一變就停用該站，等人確認（R2-09）。

- 不解析規則（r4 §5 D6）：只比內容雜湊。
- 比對對象是公開設定的 `[robots] accepted_hash.<來源>`。
  設定裡沒有這個來源時，第一次讀到的雜湊當基準（記在 kv），之後一變就停用。
- 停用的方式：`source_state.last_error = robots_changed`，抓取層對該來源的請求一律回
  `Blocked(robots_changed)`，直到設定裡的雜湊（或基準）等於最新讀到的雜湊。
- 讀不到（5xx、逾時）就不算檢查過，本輪該來源停止，下一輪再試；404／410 當成空檔案。
- 這個模組不依賴抓取層的實作，由 http.py 在每個來源每天第一次請求前呼叫。
"""

import datetime as dt
import hashlib
from collections.abc import Sequence
from typing import Protocol

from house_watch.errors import ErrorCode
from house_watch.sources.base import Blocked, Fetch, PageInvalid, RequestSpec

CHECKED_KEY = "robots.checked.{source}"  # 最後一次成功檢查的台北日期
BASELINE_KEY = "robots.baseline.{source}"  # 設定沒有雜湊時，第一次讀到的雜湊


class RobotsStore(Protocol):
    """robots 檢查需要的狀態存取（http.StateStore 實作）。"""

    def kv_get(self, key: str) -> str | None: ...

    def kv_set(self, key: str, value: str) -> None: ...

    def robots_hash(self, source: str) -> str | None: ...

    def set_robots_hash(self, source: str, digest: str) -> None: ...

    def last_error(self, source: str) -> str | None: ...

    def set_last_error(self, source: str, code: str | None) -> None: ...


def normalize(body: bytes) -> bytes:
    """換行統一成 LF、去掉頭尾空白，避免只差換行就被判成改版。"""
    text = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip()
    return text + b"\n" if text else b""


def robots_digest(bodies: Sequence[bytes]) -> str:
    """一個來源所有 robots.txt 的內容雜湊（sha256 hex）。只有一個檔時就是那個檔的雜湊。"""
    digest = hashlib.sha256()
    for index, body in enumerate(bodies):
        if index:
            digest.update(b"\n\x00\n")
        digest.update(normalize(body))
    return digest.hexdigest()


def accepted_digest(store: RobotsStore, source: str, configured: str | None) -> str | None:
    """目前認可的雜湊：設定優先，否則用第一次讀到的基準。"""
    return configured or store.kv_get(BASELINE_KEY.format(source=source))


def is_changed(store: RobotsStore, source: str, configured: str | None) -> bool:
    """最新讀到的雜湊和認可的不同（任一個還不知道就不算）。"""
    observed = store.robots_hash(source)
    accepted = accepted_digest(store, source, configured)
    return observed is not None and accepted is not None and observed != accepted


def checked_on(store: RobotsStore, source: str) -> dt.date | None:
    value = store.kv_get(CHECKED_KEY.format(source=source))
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def ensure_robots(
    fetch: Fetch,
    store: RobotsStore,
    *,
    source: str,
    urls: Sequence[str],
    configured: str | None,
    today: dt.date,
) -> None:
    """今天還沒檢查就讀一次；內容和認可的不同就停用該來源並丟出 Blocked(robots_changed)。"""
    if not urls:
        return
    if checked_on(store, source) != today:
        bodies: list[bytes] = []
        for url in urls:
            resp = fetch(RequestSpec.get(url, headers={"Accept": "text/plain"}, kind="robots"))
            if resp.status == 200:
                bodies.append(resp.body)
            elif resp.status in (404, 410):
                bodies.append(b"")
            else:
                raise PageInvalid(ErrorCode.PAGE_INVALID, f"robots.txt HTTP {resp.status}")
        digest = robots_digest(bodies)
        store.set_robots_hash(source, digest)
        if configured is None and store.kv_get(BASELINE_KEY.format(source=source)) is None:
            store.kv_set(BASELINE_KEY.format(source=source), digest)
        store.kv_set(CHECKED_KEY.format(source=source), today.isoformat())
    if is_changed(store, source, configured):
        if store.last_error(source) != ErrorCode.ROBOTS_CHANGED:
            store.set_last_error(source, ErrorCode.ROBOTS_CHANGED.value)
        raise Blocked(ErrorCode.ROBOTS_CHANGED)
    if store.last_error(source) == ErrorCode.ROBOTS_CHANGED:
        store.set_last_error(source, None)
