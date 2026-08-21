"""
Shipping logs to a remote collector, without shipping the credential with them.

Three transports rather than a list of vendor names: syslog covers rsyslog,
syslog-ng and every SIEM that ingests RFC 5424; HTTP/JSON covers Splunk HEC,
Loki, Elastic, Datadog and any webhook, which differ only in URL, headers and
body shape; SFTP/FTP covers estates that collect by file drop.

The tests that matter most are not about delivery. They are:

  the credential never reaches a log line — a handler that reports
  "cannot connect to sftp://user:hunter2@host" writes the password into the
  file it is about to upload;

  nothing here can block or raise — a logging call that blocks hangs the event
  loop, and one that raises takes down whatever was logging;

  local logging survives a broken collector — otherwise a remote outage becomes
  a logging outage, destroying the evidence needed to diagnose it.
"""
from __future__ import annotations

import inspect
import logging
import queue

import pytest

from suni import log_ship as ship


# ── the credential must never appear in anything we log ──────────────────────
def test_a_password_is_scrubbed_from_an_error():
    msg = "Authentication failed for sftp://bob:hunter2@logs.example.com:22"
    out = ship._safe(msg, "hunter2")
    assert "hunter2" not in out
    assert "logs.example.com" in out, "scrubbing destroyed the useful part too"


def test_a_token_is_scrubbed_even_when_the_pattern_does_not_match():
    """Libraries embed credentials in shapes no regex anticipates, so the known
    secrets are passed in explicitly as well."""
    weird = "HEC rejected request [key=abc123XYZ] after 3 retries"
    assert "abc123XYZ" not in ship._safe(weird, "abc123XYZ")


def test_url_credentials_are_masked_without_the_secret_being_known():
    out = ship.redact_url("ftp://svc:s3cr3t@files.corp.local/logs")
    assert "s3cr3t" not in out and "svc" in out and "files.corp.local" in out


def test_describe_never_includes_a_credential():
    for cfg in (
        {"type": "sftp", "username": "bob", "password": "hunter2",
         "host": "h", "port": 22, "remote_dir": "/drop"},
        {"type": "http", "url": "https://x/y", "token": "abc123XYZ"},
        {"type": "syslog", "host": "h", "port": 514, "protocol": "tls"},
    ):
        d = ship.describe(cfg)
        assert "hunter2" not in d and "abc123XYZ" not in d


def test_a_failing_target_reports_without_leaking(caplog):
    """The end-to-end version of the rule: make delivery fail and read the log."""
    class Boom:
        def emit(self, record):
            raise RuntimeError("login failed for sftp://bob:hunter2@h:22")
        def close(self):
            pass

    h = ship._ShippingHandler(Boom(), secrets=("hunter2",))
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    with caplog.at_level(logging.WARNING):
        h.emit(rec)
    text = caplog.text
    assert "hunter2" not in text, "the password was written to the log"
    assert "delivery failed" in text


# ── it must not block, raise, or replace local logging ───────────────────────
def test_emit_never_raises():
    class Boom:
        def emit(self, record):
            raise OSError("network unreachable")
        def close(self):
            pass
    h = ship._ShippingHandler(Boom())
    h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None))


def test_repeated_failures_do_not_flood_the_log(caplog):
    """A broken collector logging once per record is the same outage from the
    other end."""
    class Boom:
        def emit(self, record):
            raise OSError("down")
        def close(self):
            pass
    h = ship._ShippingHandler(Boom())
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None))
    assert caplog.text.count("delivery failed") == 1


def test_the_queue_drops_instead_of_blocking():
    """The stock QueueHandler blocks when full, which would put the stall this
    module exists to avoid into every log call."""
    q = queue.Queue(maxsize=2)
    qh = ship._DroppingQueueHandler(q)
    for i in range(10):
        qh.enqueue(logging.LogRecord("t", logging.INFO, __file__, i, str(i), (), None))
    assert q.qsize() == 2, "the queue grew past its bound or blocked"


def test_shipping_is_additive_not_a_replacement():
    """start() adds a handler; it must never remove the local file handler."""
    src = inspect.getsource(ship.start)
    assert "addHandler" in src
    assert "removeHandler" not in src, "shipping detaches local logging"


def test_a_bad_config_does_not_stop_logging():
    assert ship.start({"enabled": True, "type": "nonsense"}) == {
        "enabled": False, "type": "", "level": "", "detail": ""}


def test_disabled_is_the_default_path():
    assert ship.start({})["enabled"] is False
    assert ship.start({"enabled": False, "type": "syslog"})["enabled"] is False


# ── the privacy floor ────────────────────────────────────────────────────────
def test_debug_is_floored_to_info():
    """DEBUG ships the document scanner's output — tens of thousands of lines
    naming local file paths — to a third party."""
    src = inspect.getsource(ship.start)
    assert "logging.INFO" in src and "level < logging.INFO" in src


def test_shipping_is_off_by_default_in_config():
    from suni import config
    assert config.DEFAULTS["logship_enabled"] is False
    assert config.DEFAULTS["logship_level"] == "INFO"


def test_the_secrets_follow_the_existing_redaction_convention():
    """Blank-on-POST preserves the stored value; GET never echoes it. Using the
    same path as smtp_pass rather than inventing a second one."""
    import pathlib
    srv = (pathlib.Path(__file__).resolve().parent.parent
           / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert '"logship_token", "logship_password"' in srv
    i = srv.index('cfg["smtp_pass"] = ""')
    assert "logship_token" in srv[i:i + 600], "GET does not redact the shipping secrets"


def test_the_test_endpoint_is_admin_only():
    import pathlib
    srv = (pathlib.Path(__file__).resolve().parent.parent
           / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/logship/test"')
    assert "require_admin" in srv[i:i + 400]


def test_sftp_refuses_an_unknown_host_key():
    """This connection carries the log contents; an unverified host is an
    unknown recipient."""
    src = inspect.getsource(ship._FileDropTarget.upload)
    assert "RejectPolicy" in src
    assert "AutoAddPolicy" not in src
