"""所有資料夾共用的抓取帳本（事後審查 SA-1、P-12）。

每日請求上限、同一主機的請求間隔、被擋後的暫停，原本都只記在各自資料夾的 DB 或程序記憶體。
第二個資料夾（DB 副本的試跑、設定遺失時自動建的新資料夾）因此成了第二個獨立爬蟲：合計會
超過每日上限，兩個程序也會一起打同一主機，一邊被擋時另一邊也不知道。

帳本是這台電腦、這個使用者共用的一份小 JSON：
- requests：{來源: {"date": 台北日期, "n": 次數}}
- host_last：{主機: 最後一個請求結束的時間（UNIX 秒，牆上時鐘，跨程序可以比）}
- paused：{來源: 暫停到何時（ISO）}

位置：環境變數 HOUSE_WATCH_LEDGER（測試用）＞ /tmp/house_watch-<uid>/fetch_ledger.json
（Windows 用系統暫存資料夾）。刻意不放在資料夾或執行區：那兩個會因環境變數不同而分開，帳本
就失去共用的意義。/tmp 重開機會清掉；每日計數另有各 DB 自己的一份（取兩者較大值），所以只會
失去「跨資料夾」的那部分。

讀改寫用 FileLock 保護。帳本資料夾要是自己的、不是 symlink、別人不能寫，否則拒絕使用
（避免別的使用者在 /tmp 預先放好 symlink）。檔案壞掉時當成空的。
"""

import datetime as dt
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from house_watch.clock import from_iso, to_iso
from house_watch.errors import ConfigError

ENV_LEDGER = "HOUSE_WATCH_LEDGER"
LEDGER_FILENAME = "fetch_ledger.json"
LOCK_TIMEOUT_S = 30


def ledger_path() -> Path:
    """帳本檔的位置（見模組說明）。"""
    override = os.environ.get(ENV_LEDGER)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(tempfile.gettempdir()) / "house_watch" / LEDGER_FILENAME
    return Path("/tmp") / f"house_watch-{os.getuid()}" / LEDGER_FILENAME


def _check_folder(folder: Path) -> None:
    """帳本資料夾：不存在就建（700）；存在時要是自己的一般資料夾、別人不能寫。"""
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"建不了抓取帳本資料夾：{folder}（{type(exc).__name__}）") from None
    if sys.platform == "win32":
        return
    info = os.lstat(folder)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"抓取帳本資料夾不是一般資料夾：{folder}")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConfigError(f"抓取帳本資料夾不是你的，或別人可以寫：{folder}")


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    return value


def _count_for(entry: Any, day: dt.date) -> int:
    if not isinstance(entry, dict) or entry.get("date") != day.isoformat():
        return 0
    count = entry.get("n")
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0


class FetchLedger:
    """共用帳本的讀寫。每個方法自己拿鎖、讀檔、（需要時）寫回。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else ledger_path()
        self._checked = False

    @contextmanager
    def _open(self, *, write: bool) -> Iterator[dict[str, Any]]:
        if not self._checked:
            _check_folder(self.path.parent)
            self._checked = True
        try:
            with FileLock(f"{self.path}.lock", timeout=LOCK_TIMEOUT_S):
                data = self._read()
                yield data
                if write:
                    tmp = self.path.with_name(f".{self.path.name}.tmp")
                    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
                    tmp.write_text(text, encoding="utf-8")
                    os.replace(tmp, self.path)
        except Timeout:
            raise ConfigError(f"抓取帳本被鎖住超過 {LOCK_TIMEOUT_S} 秒：{self.path}") from None

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def host_lock(self, host: str) -> FileLock:
        """同一主機的跨程序鎖（修完再審 F1-1）。從「讀最後請求時間 → 等間隔 → 送出 → 讀完 →
        記下時間」整段都拿著，兩個資料夾同時跑時才守得住 4 秒間隔。"""
        if not self._checked:
            _check_folder(self.path.parent)
            self._checked = True
        safe = re.sub(r"[^a-z0-9.-]", "_", host.lower())
        return FileLock(str(self.path.parent / f"host-{safe}.lock"))

    def requests_on(self, source: str, day: dt.date) -> int:
        with self._open(write=False) as data:
            return _count_for(_section(data, "requests").get(source), day)

    def add_request(self, source: str, day: dt.date) -> int:
        with self._open(write=True) as data:
            requests = _section(data, "requests")
            count = _count_for(requests.get(source), day) + 1
            requests[source] = {"date": day.isoformat(), "n": count}
            return count

    def host_last(self, host: str) -> float | None:
        with self._open(write=False) as data:
            value = _section(data, "host_last").get(host)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def mark_host(self, host: str, when: float) -> None:
        with self._open(write=True) as data:
            hosts = _section(data, "host_last")
            old = hosts.get(host)
            if isinstance(old, bool) or not isinstance(old, (int, float)) or when > old:
                hosts[host] = when

    def paused_until(self, source: str) -> dt.datetime | None:
        with self._open(write=False) as data:
            return self._parse(_section(data, "paused").get(source))

    def set_paused(self, source: str, until: dt.datetime) -> None:
        with self._open(write=True) as data:
            paused = _section(data, "paused")
            old = self._parse(paused.get(source))
            if old is None or until > old:
                paused[source] = to_iso(until)

    @staticmethod
    def _parse(raw: Any) -> dt.datetime | None:
        if not isinstance(raw, str):
            return None
        try:
            return from_iso(raw)
        except ValueError:
            return None
