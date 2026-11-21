"""tick 的 log：位置、輪替（5 MB × 5）、遮罩、結束時關檔（WP7b；R2-15）。"""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from house_watch.ops import logging as ops_logging


def test_log_dir_env_override_and_default(monkeypatch, tmp_path):
    monkeypatch.setenv(ops_logging.ENV_LOG_DIR, str(tmp_path / "logs"))
    assert ops_logging.log_dir() == (tmp_path / "logs").resolve()
    monkeypatch.delenv(ops_logging.ENV_LOG_DIR)
    default = ops_logging.log_dir()
    assert default.is_absolute() and "house_watch" in default.parts[-2:] + (default.name,)


def test_rotating_handler_limits(tmp_path):
    with ops_logging.tick_logging(tmp_path, console=False) as log:
        handler = next(h for h in log.handlers if isinstance(h, RotatingFileHandler))
        assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 5
        assert log.propagate is False


def test_rotation_keeps_five_old_files(monkeypatch, tmp_path):
    monkeypatch.setattr(ops_logging, "MAX_BYTES", 300)
    with ops_logging.tick_logging(tmp_path, console=False) as log:
        for n in range(200):
            log.info("第 %d 行：示意的 log 內容", n)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["tick.log"] + [f"tick.log.{n}" for n in range(1, 6)]


def test_lines_are_masked_and_handlers_closed(tmp_path):
    secret = "git@example.invalid:someone-else/site.git"

    def mask(text: str) -> str:
        return text.replace(secret, "***").replace(str(Path.home()), "~")

    with ops_logging.tick_logging(tmp_path, mask=mask, console=False) as log:
        log.info("remote %s 在 %s", secret, Path.home() / "x")
        try:
            raise ValueError(f"{Path.home()}/y")
        except ValueError:
            log.exception("壞了")
    text = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert secret not in text and str(Path.home()) not in text
    assert "ValueError" in text and "~/y" in text
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO ", text)
    logger = logging.getLogger(ops_logging.LOGGER_NAME)
    assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    assert logger.filters == []
