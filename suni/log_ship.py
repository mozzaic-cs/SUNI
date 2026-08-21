"""
Ship logs to a remote collector — syslog, HTTP/JSON, or SFTP/FTP.

Three transports rather than a list of vendor names, because the vendors differ
only in the details these already carry:

  syslog  — rsyslog, syslog-ng, and every SIEM that ingests RFC 5424
  http    — Splunk HEC, Grafana Loki, Elasticsearch, Datadog, any webhook;
            they differ by URL, headers and body shape, all configurable
  sftp/ftp — periodic upload of the rotated daily file, for estates that
            collect by file drop rather than by listener

Three rules, and the first two are not negotiable:

**The local file handler always stays.** Shipping is additive. If it replaced
local logging, a remote outage would become a logging outage, and the evidence
you need to diagnose the outage would be the evidence you just lost.

**Nothing here may block.** Handlers hand off to a bounded queue and a
background thread does the network. A blocking socket inside a log call would
hang the event loop, which is a failure this project has already seen.

**The credential must never reach a log line.** A handler that reports its own
failure as "cannot connect to sftp://user:hunter2@host" writes the password into
the file it then uploads. Every error path here goes through _safe(), and there
is a test that misconfigures a target on purpose and greps for the secret.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import re
import socket
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("suni.logship")

# Bounded on purpose. If the collector is slow or gone, the newest lines matter
# more than a backlog that grows until memory does.
_QUEUE_MAX = 5000

_listener: logging.handlers.QueueListener | None = None
_queue: queue.Queue | None = None

# Anything that looks like a credential, in any string we are about to log.
_CRED_IN_URL = re.compile(r"(?<=://)([^/@\s:]+):([^/@\s]+)(?=@)")
_SECRETISH = re.compile(
    r"(?i)\b(pass(word)?|token|secret|api[_-]?key|authorization)\b\s*[=:]\s*\S+")


def _safe(text: Any, *secrets: str) -> str:
    """A message with credentials removed, for use in error paths.

    Takes the known secrets explicitly as well as pattern-matching, because a
    password can appear in an exception string in a shape no regex anticipated —
    paramiko and ftplib both embed connection details in their errors.
    """
    s = str(text)
    for sec in secrets:
        if sec and len(sec) >= 3:
            s = s.replace(sec, "***")
    s = _CRED_IN_URL.sub(r"\1:***", s)
    s = _SECRETISH.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=***", s)
    return s


def redact_url(url: str) -> str:
    """A URL safe to show in the UI or a log line."""
    try:
        p = urlsplit(url)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}" + (f":{p.port}" if p.port else "")
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:      # noqa: BLE001
        pass
    return _CRED_IN_URL.sub(r"\1:***", str(url))


# ── transports ───────────────────────────────────────────────────────────────
class _SyslogTarget:
    """RFC 5424 over UDP, TCP, or TCP+TLS."""

    def __init__(self, cfg: dict):
        self.host = str(cfg.get("host") or "")
        self.port = int(cfg.get("port") or 514)
        self.proto = str(cfg.get("protocol") or "udp").lower()
        self.app = str(cfg.get("app_name") or "suni")
        self._h: logging.Handler | None = None

    def _handler(self) -> logging.Handler:
        if self._h is None:
            if self.proto == "udp":
                sock = socket.SOCK_DGRAM
            else:
                sock = socket.SOCK_STREAM
            h = logging.handlers.SysLogHandler(address=(self.host, self.port),
                                               socktype=sock)
            if self.proto == "tls":
                # SysLogHandler has no TLS of its own; wrap its socket once
                # connected. Certificate verification stays on — a log stream
                # sent to an unverified host is a log stream sent anywhere.
                import ssl
                h.socket = ssl.create_default_context().wrap_socket(
                    h.socket, server_hostname=self.host)
            h.setFormatter(logging.Formatter(f"{self.app}: %(message)s"))
            self._h = h
        return self._h

    def emit(self, record: logging.LogRecord) -> None:
        self._handler().emit(record)

    def close(self) -> None:
        if self._h:
            try:
                self._h.close()
            finally:
                self._h = None


class _HttpTarget:
    """POST one JSON object per record.

    Deliberately generic: Splunk HEC, Loki, Elastic and Datadog all accept a
    POST with a token in a header, and differ only in URL and body key names.
    Extra headers are configurable so a bespoke collector does not need code.
    """

    def __init__(self, cfg: dict):
        self.url = str(cfg.get("url") or "")
        self.token = str(cfg.get("token") or "")
        self.header = str(cfg.get("auth_header") or "Authorization")
        self.prefix = str(cfg.get("auth_prefix") or "Bearer")
        self.extra = dict(cfg.get("headers") or {})
        self.timeout = float(cfg.get("timeout") or 5)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", **self.extra}
        if self.token:
            h[self.header] = f"{self.prefix} {self.token}".strip()
        return h

    def emit(self, record: logging.LogRecord) -> None:
        import requests
        body = {
            "time": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "host": socket.gethostname(),
            "source": "suni",
        }
        r = requests.post(self.url, json=body, headers=self._headers(),
                          timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    def close(self) -> None:
        pass


class _FileDropTarget:
    """Upload the rotated daily log over SFTP or FTP.

    Batched by nature: this uploads a FILE, so it runs on a timer rather than
    per record. Records are counted, not sent — the file on disk is the payload.
    """

    def __init__(self, cfg: dict):
        self.kind = str(cfg.get("kind") or "sftp").lower()
        self.host = str(cfg.get("host") or "")
        self.port = int(cfg.get("port") or (22 if self.kind == "sftp" else 21))
        self.user = str(cfg.get("username") or "")
        self.password = str(cfg.get("password") or "")
        self.remote_dir = str(cfg.get("remote_dir") or ".")

    def upload(self, local_path) -> str:
        """Send one file. Returns the remote name written."""
        from pathlib import Path
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        remote = f"{self.remote_dir.rstrip('/')}/{p.name}"
        if self.kind == "sftp":
            try:
                import paramiko
            except ImportError as exc:
                raise RuntimeError(
                    "SFTP needs the 'paramiko' package: pip install paramiko"
                ) from exc
            cli = paramiko.SSHClient()
            cli.load_system_host_keys()
            # Reject unknown hosts rather than trusting on first use: this
            # connection carries the contents of the log, and an unverified
            # host is an unknown recipient.
            cli.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                cli.connect(self.host, port=self.port, username=self.user,
                            password=self.password, timeout=10)
                sftp = cli.open_sftp()
                try:
                    sftp.put(str(p), remote)
                finally:
                    sftp.close()
            finally:
                cli.close()
        else:
            from ftplib import FTP, FTP_TLS
            cls = FTP_TLS if self.kind == "ftps" else FTP
            with cls() as ftp:
                ftp.connect(self.host, self.port, timeout=10)
                ftp.login(self.user, self.password)
                if isinstance(ftp, FTP_TLS):
                    ftp.prot_p()
                with p.open("rb") as fh:
                    ftp.storbinary(f"STOR {remote}", fh)
        return remote

    def emit(self, record: logging.LogRecord) -> None:
        # Nothing per-record: this target ships whole files on a timer.
        return

    def close(self) -> None:
        pass


def _build(cfg: dict):
    kind = str(cfg.get("type") or "").lower()
    if kind == "syslog":
        return _SyslogTarget(cfg)
    if kind == "http":
        return _HttpTarget(cfg)
    if kind in ("sftp", "ftp", "ftps"):
        return _FileDropTarget({**cfg, "kind": kind})
    raise ValueError(f"unknown log target type {kind!r}")


class _ShippingHandler(logging.Handler):
    """Sends each record to the configured target, and never lets that matter.

    A logging handler that raises takes down the call that logged. One that
    blocks takes down the loop. So every failure is swallowed after being
    recorded once — repeating it per record would turn a broken collector into
    a log flood, which is the same outage from the other end.
    """

    def __init__(self, target, secrets: tuple[str, ...] = ()):
        super().__init__()
        self.target = target
        self._secrets = tuple(s for s in secrets if s)
        self._failed_once = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.emit(record)
            self._failed_once = False
        except Exception as exc:      # noqa: BLE001 — logging must not raise
            if not self._failed_once:
                self._failed_once = True
                log.warning("[LOGSHIP] delivery failed, continuing locally: %s",
                            _safe(exc, *self._secrets))


def stop() -> None:
    """Detach shipping. Safe to call when nothing is running."""
    global _listener, _queue
    if _listener is not None:
        try:
            _listener.stop()
        except Exception:      # noqa: BLE001
            pass
    _listener = None
    _queue = None


def start(cfg: dict, root: logging.Logger | None = None) -> dict:
    """Attach shipping to the root logger from a config block.

    Returns a description of what was attached, for the caller to log or show.
    Never raises: a bad shipping config must not stop SUNI from starting.
    """
    global _listener, _queue
    stop()
    out = {"enabled": False, "type": "", "level": "", "detail": ""}
    if not cfg or not cfg.get("enabled"):
        return out

    try:
        target = _build(cfg)
    except Exception as exc:      # noqa: BLE001
        log.warning("[LOGSHIP] not started: %s", _safe(exc, str(cfg.get("password") or ""),
                                                      str(cfg.get("token") or "")))
        return out

    # Floor the level. Shipping DEBUG by default would send document-scan spam
    # — tens of thousands of lines naming local file paths — to a third party.
    level_name = str(cfg.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if level < logging.INFO:
        level = logging.INFO
        level_name = "INFO"

    secrets = (str(cfg.get("password") or ""), str(cfg.get("token") or ""))
    handler = _ShippingHandler(target, secrets)
    handler.setLevel(level)

    _queue = queue.Queue(maxsize=_QUEUE_MAX)
    qh = _DroppingQueueHandler(_queue)
    qh.setLevel(level)
    _listener = logging.handlers.QueueListener(_queue, handler,
                                               respect_handler_level=True)
    _listener.daemon = True
    _listener.start()
    (root or logging.getLogger()).addHandler(qh)

    out.update(enabled=True, type=str(cfg.get("type") or ""), level=level_name,
               detail=describe(cfg))
    return out


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """Drops the oldest record when the queue is full.

    The default QueueHandler blocks on a full queue, which would put the very
    stall this module exists to avoid back into every log call.
    """

    def enqueue(self, record) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            try:
                self.queue.get_nowait()      # discard oldest
                self.queue.put_nowait(record)
            except Exception:      # noqa: BLE001
                pass


def describe(cfg: dict) -> str:
    """A one-line, credential-free description for the UI."""
    kind = str(cfg.get("type") or "").lower()
    if kind == "syslog":
        return f"syslog {cfg.get('protocol', 'udp')}://{cfg.get('host', '')}:{cfg.get('port', 514)}"
    if kind == "http":
        return redact_url(str(cfg.get("url") or ""))
    if kind in ("sftp", "ftp", "ftps"):
        return (f"{kind}://{cfg.get('username', '')}@{cfg.get('host', '')}:"
                f"{cfg.get('port', '')}{cfg.get('remote_dir', '')}")
    return kind or "(not configured)"


def test(cfg: dict) -> dict:
    """Send one line and report what actually happened.

    A remote endpoint that can only be validated by waiting for silence is
    configuration nobody will trust, so the error is returned verbatim —
    scrubbed of credentials.
    """
    secrets = (str(cfg.get("password") or ""), str(cfg.get("token") or ""))
    try:
        target = _build(cfg)
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "error": _safe(exc, *secrets)}

    try:
        if isinstance(target, _FileDropTarget):
            from . import logger as _logger
            path = _logger.current_log_path()
            remote = target.upload(path)
            return {"ok": True, "detail": f"uploaded {path.name} → {remote}"}
        rec = logging.LogRecord(
            name="suni.logship", level=logging.INFO, pathname=__file__, lineno=0,
            msg="SUNI log shipping test — if you can read this, delivery works.",
            args=(), exc_info=None)
        target.emit(rec)
        return {"ok": True, "detail": f"delivered to {describe(cfg)}"}
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "error": _safe(exc, *secrets)}
    finally:
        try:
            target.close()
        except Exception:      # noqa: BLE001
            pass
