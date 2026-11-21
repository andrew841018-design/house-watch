"""本機資料夾與檔案位置（R2-03、R2-07）。

- 預設位置由 platformdirs 決定：Mac 是 ~/Library/Application Support/house_watch，
  Windows 是 %LOCALAPPDATA%，Linux 依 XDG。程式裡不寫死任何本機路徑。
- 環境變數可以覆寫：
  - `HOUSE_WATCH_DATA_DIR`：資料夾。優先序：環境變數 > settings.toml 的 data_dir > 預設。
    相對路徑以目前的工作目錄為準。
  - `HOUSE_WATCH_CONFIG`：本機設定檔的完整檔案路徑（不是資料夾）。設了就一定要存在。
"""

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "house_watch"
ENV_DATA_DIR = "HOUSE_WATCH_DATA_DIR"
ENV_CONFIG = "HOUSE_WATCH_CONFIG"
SETTINGS_FILENAME = "settings.toml"

# 判斷資料夾「完全是空的」時不算數的檔案：鎖檔（tick 先拿鎖才檢查資料夾）、
# 本機設定檔（Mac 上設定資料夾和資料夾是同一個）、Finder 的 .DS_Store。
IGNORED_WHEN_EMPTY = frozenset({".DS_Store", "run.lock", SETTINGS_FILENAME})


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def default_config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def settings_file() -> tuple[Path, bool]:
    """本機設定檔的位置，以及它是不是由環境變數明確指定的。"""
    override = os.environ.get(ENV_CONFIG)
    if override:
        return Path(override).expanduser(), True
    return default_config_dir() / SETTINGS_FILENAME, False


def resolve_data_dir(configured: Path | None = None) -> Path:
    """資料夾：環境變數 > settings.toml 的 data_dir > platformdirs 預設。"""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    if configured is not None:
        return configured.expanduser()
    return default_data_dir()


@dataclass(frozen=True)
class DataPaths:
    """資料夾裡各檔案的位置。"""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "house_watch.sqlite"

    @property
    def doorindex(self) -> Path:
        return self.root / "doorindex.sqlite"

    @property
    def lock(self) -> Path:
        return self.root / "run.lock"

    @property
    def instance_marker(self) -> Path:
        return self.root / "instance.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def failed_dir(self) -> Path:
        """解析失敗時留下的原始回應，7 天後刪除（R2-12）。"""
        return self.root / "failed"

    @property
    def site_repo_dir(self) -> Path:
        """網站 repo 的本機 clone（r4 §3.6）。"""
        return self.root / "site"

    @property
    def export_dir(self) -> Path:
        """匯出成果：`export/site/`（網站本體）與 `export/manifest.json`（WP7a）。"""
        return self.root / "export"

    @property
    def export_site_dir(self) -> Path:
        """匯出的網站本體；`serve` 只提供這裡，publish 只發布 manifest 列出的檔。"""
        return self.export_dir / "site"

    @property
    def export_manifest(self) -> Path:
        """site/ 每個檔的路徑、位元組數、sha256；不在網站裡。"""
        return self.export_dir / "manifest.json"

    def is_empty(self) -> bool:
        """資料夾不存在，或除了 IGNORED_WHEN_EMPTY 以外沒有任何東西。"""
        if not self.root.exists():
            return True
        return all(entry.name in IGNORED_WHEN_EMPTY for entry in self.root.iterdir())
