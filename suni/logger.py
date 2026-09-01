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


def start_shipping() -> dict:
    """Attach remote log shipping from config, if enabled.

    Called after config is available. Kept separate from setup() because setup()
    runs before config loads, and because a remote target must never be a
    precondition for local logging working.
    """
    try:
        from . import config as _cfg, log_ship as _ship
        block = {
            "enabled":     _cfg.get("logship_enabled", False),
            "type":        _cfg.get("logship_type", ""),
            "level":       _cfg.get("logship_level", "INFO"),
            "host":        _cfg.get("logship_host", ""),
            "port":        _cfg.get("logship_port", 0),
            "protocol":    _cfg.get("logship_protocol", "udp"),
            "app_name":    _cfg.get("logship_app_name", "suni"),
            "url":         _cfg.get("logship_url", ""),
            "auth_header": _cfg.get("logship_auth_header", "Authorization"),
            "auth_prefix": _cfg.get("logship_auth_prefix", "Bearer"),
            "username":    _cfg.get("logship_username", ""),
            "remote_dir":  _cfg.get("logship_remote_dir", "/"),
            "token":       _cfg.get("logship_token", ""),
            "password":    _cfg.get("logship_password", ""),
        }
        return _ship.start(block)
    except Exception:      # noqa: BLE001 — logging must survive a bad target
        return {"enabled": False, "type": "", "level": "", "detail": ""}


# ── asyncio client-disconnect noise ──────────────────────────────────────────
# On Windows, a client that drops a connection abruptly makes the proactor event
# loop raise ConnectionResetError from _ProactorBasePipeTransport._call_connection_lost,
# and asyncio's default handler prints a six-line traceback for every one. This
# is a known CPython wart (bpo-39010), not a fault in SUNI, and nothing can be
# done about the disconnect — the peer is already gone.
#
# It is filtered because of what it COST, not because it is untidy. A browser tab
# left open on the Face page holds a few keep-alive sockets, and the browser
# recycles them every few minutes; that is roughly 19 tracebacks an hour from an
# IDLE tab, doing nothing. logs/startup.log reached 19,328 of them — 97% of the
# file — and when SUNI stopped on 28 Aug 2026 the shutdown left no trace anyone
# could find in the noise. A log nobody can read is not a log.
#
# Suppressed, never discarded: the count is kept and reported periodically, so
# an unusual RATE (a client in a reconnect loop, a proxy flapping) is still
# visible — which the flood of individual tracebacks actually obscured.
_disconnect_noise = 0
_NOISE_REPORT_EVERY = 500


def suppressed_disconnect_count() -> int:
    """How many client-disconnect tracebacks have been filtered this run."""
    return _disconnect_noise


def _is_client_disconnect(context: dict) -> bool:
    """Match the known-benign shape ONLY.

    Deliberately narrow: the exception type alone is not enough, because a
    ConnectionResetError raised anywhere else in the loop is a real event that
    must still be reported. It is the pairing with _call_connection_lost — the
    teardown of a socket whose peer has already gone — that makes it noise.
    """
    exc = context.get("exception")
    if not isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return False
    where = f"{context.get('handle', '')}{context.get('transport', '')}"
    return "_call_connection_lost" in where


def install_disconnect_noise_filter(loop=None) -> bool:
    """Filter client-disconnect tracebacks out of the event loop's handler.

    Chains to whatever handler was already installed rather than replacing it,
    so anything else that wanted to see loop exceptions still does. Returns
    False if there is no running loop to attach to.
    """
    import asyncio
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        return False

    previous = loop.get_exception_handler()
    log = get_logger("suni.asyncio")

    def _handler(loop_, context):
        global _disconnect_noise
        if _is_client_disconnect(context):
            _disconnect_noise += 1
            if _disconnect_noise % _NOISE_REPORT_EVERY == 0:
                log.info("[NET] %d client disconnects filtered so far "
                         "(browser tabs recycling idle connections)",
                         _disconnect_noise)
            return
        if previous is not None:
            previous(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    return True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"suni.{name}" if not name.startswith("suni") else name)


def current_log_path() -> Path:
    """Return the path of today's log file."""
    return _LOG_DIR / f"suni_{datetime.now().strftime('%Y-%m-%d')}.log"
