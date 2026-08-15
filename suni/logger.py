"""
Central logging for SUNI.

Usage:
    from suni.logger import get_logger
    log = get_logger(__name__)
    log.info("[REQUEST] query=%r route=%s", query, route)
    log.error("[TOOL_FAIL] %s: %s", tool, exc, exc_info=True)

Files:
    logs/suni_YYYY-MM-DD.log  — one file per day, kept 30 days
"""
from __future__ import annotations
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path("logs")
_FMT     = "%(asctime)s | %(levelname)-7s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_SETUP   = False


class _DailyFileHandler(logging.handlers.BaseRotatingHandler):
    """
    Writes to logs/suni_YYYY-MM-DD.log. Rotates at midnight, keeping the
    last backupCount files. Unlike TimedRotatingFileHandler, the current
    file is always named with today's date — no awkward .log vs .log.date split.
    """

    def __init__(self, log_dir: Path, backup_count: int = 30, encoding: str = "utf-8"):
        self.log_dir      = log_dir
        self.backup_count = backup_count
        self._current_date = self._today()
        filename = str(log_dir / self._filename(self._current_date))
        super().__init__(filename, mode="a", encoding=encoding, delay=False)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _filename(date: str) -> str:
        return f"suni_{date}.log"

    def shouldRollover(self, record) -> bool:
        return self._today() != self._current_date

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]

        self._current_date = self._today()
        self.baseFilename = str(self.log_dir / self._filename(self._current_date))
        self.stream = self._open()
        self._cleanup()

    def _cleanup(self) -> None:
        """Remove log files older than backup_count days."""
        files = sorted(self.log_dir.glob("suni_*.log"))
        for old in files[: max(0, len(files) - self.backup_count)]:
            try:
                old.unlink()
            except OSError:
                pass


def setup(log_dir: Path = _LOG_DIR) -> None:
    global _SETUP
    if _SETUP:
        return
    _SETUP = True

    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    fh = _DailyFileHandler(log_dir, backup_count=30)
    fh.setFormatter(fmt)

    root = logging.getLogger("suni")
    root.setLevel(logging.DEBUG)
    if not root.handlers:
        root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "ollama", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"suni.{name}" if not name.startswith("suni") else name)


def current_log_path() -> Path:
    """Return the path of today's log file."""
    return _LOG_DIR / f"suni_{datetime.now().strftime('%Y-%m-%d')}.log"
