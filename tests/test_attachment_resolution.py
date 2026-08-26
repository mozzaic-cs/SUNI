"""
The model names the file correctly and invents the directory.

From live use, 2026-08-26. One request — "make a PDF about Coimbra and email it
to me" — produced these two log lines, seconds apart:

    [TOOL] create_pdf   PDF created: C:\\Users\\<the real user>\\Desktop\\coimbra_info.pdf (33.0 KB)
    [TOOL] send_email   Attachment(s) not found: \\home\\user\\Desktop\\coimbra_info.pdf

A POSIX path, on Windows, for a file the model had just been told the real
location of. The PDF was written correctly and sat on the Desktop the whole
time. Only the second tool call was wrong, and only in its directory.

Telling the model harder is not the fix — a 7B blends the shape of a path it
has seen a thousand times in training with the one in front of it, and this is
the third distinct invented path this one prompt has produced. The filename it
gets right, and SUNI itself chose where the file went, so the lookup is
recoverable in code. Same reasoning as _strip_unknown_args in the registry: one
place absorbs the small-model failure, rather than every tool growing a
workaround.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

import pytest

from suni.tools.safe_path import resolve_attachment_path


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Point resolve_output_dir at a scratch directory."""
    out = tmp_path / "generated"
    out.mkdir()
    monkeypatch.setattr("suni.user_settings.resolve_output_dir",
                        lambda user_id="": str(out))
    return out


# ── the reported failure ─────────────────────────────────────────────────────
def test_the_reported_invented_path_is_recovered(output_dir):
    (output_dir / "coimbra_info.pdf").write_bytes(b"%PDF-1.4 fake")
    got = resolve_attachment_path(r"\home\user\Desktop\coimbra_info.pdf")
    assert Path(got) == output_dir / "coimbra_info.pdf"


def test_a_posix_invention_is_recovered_too(output_dir):
    (output_dir / "coimbra_info.pdf").write_bytes(b"%PDF-1.4 fake")
    got = resolve_attachment_path("/home/user/Desktop/coimbra_info.pdf")
    assert Path(got) == output_dir / "coimbra_info.pdf"


def test_the_earlier_windows_placeholder_is_recovered(output_dir):
    """The same prompt previously produced C:/Users/yourusername/Desktop —
    the literal placeholder from the tool description."""
    (output_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    got = resolve_attachment_path(r"C:\Users\yourusername\Desktop\report.pdf")
    assert Path(got) == output_dir / "report.pdf"


# ── what must NOT change ─────────────────────────────────────────────────────
def test_a_path_that_exists_is_left_alone(tmp_path, output_dir):
    """A user who says "attach D:/reports/q3.pdf" means that file, even if a
    file of the same name sits in the output directory."""
    real = tmp_path / "elsewhere" / "q3.pdf"
    real.parent.mkdir()
    real.write_bytes(b"the one the user meant")
    (output_dir / "q3.pdf").write_bytes(b"a different file entirely")

    assert resolve_attachment_path(str(real)) == str(real)


def test_a_file_that_exists_nowhere_still_fails(output_dir):
    """Masking a genuine missing file would turn a clear error into a silent
    send with nothing attached."""
    given = "/home/user/Desktop/never_created.pdf"
    assert resolve_attachment_path(given) == given


def test_an_empty_path_stays_empty(output_dir):
    """send_email's attachment_path defaults to "" and means "no attachment".
    Resolving that must not conjure one."""
    assert resolve_attachment_path("") == ""
    assert resolve_attachment_path(None) == ""


def test_the_search_does_not_wander(tmp_path, output_dir, monkeypatch):
    """Only the directories SUNI writes to are searched. A wider hunt would let
    a hallucinated filename attach a document nobody asked to send."""
    secret = tmp_path / "private"
    secret.mkdir()
    (secret / "salaries.pdf").write_bytes(b"not for sending")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    given = "/home/user/Desktop/salaries.pdf"
    assert resolve_attachment_path(given) == given, \
        "found a file outside the output directory and Desktop"


def test_desktop_is_searched_when_the_output_dir_misses(tmp_path, monkeypatch):
    """The live instance has no global_output_dir, so generated files land on
    the Desktop — which is exactly where the reported PDF was."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "coimbra_info.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("suni.user_settings.resolve_output_dir", lambda user_id="": "")

    got = resolve_attachment_path(r"\home\user\Desktop\coimbra_info.pdf")
    assert Path(got) == desktop / "coimbra_info.pdf"


# ── both arguments, through the real tool ────────────────────────────────────
def _run(**kwargs):
    from suni.tools import email_tool
    sent = {}

    def fake_send(to, subject, body, attachment_path, attachment_paths, user_id=""):
        sent["single"] = attachment_path
        sent["many"] = attachment_paths
        return "ok"

    email_tool._send = fake_send
    try:
        asyncio.run(email_tool.handler(to="a@example.com", subject="s",
                                       body="b", **kwargs))
    finally:
        del email_tool._send
        from suni.notifications.email_notify import send_email as real
        email_tool._send = real
    return sent


def test_the_single_attachment_argument_is_resolved(output_dir):
    (output_dir / "coimbra_info.pdf").write_bytes(b"%PDF-1.4 fake")
    sent = _run(attachment_path=r"\home\user\Desktop\coimbra_info.pdf")
    assert Path(sent["single"]) == output_dir / "coimbra_info.pdf"


def test_the_list_argument_is_resolved_too(output_dir):
    """Two arguments do the same job; fixing one and shipping the other broken
    is the easy mistake here."""
    (output_dir / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (output_dir / "b.png").write_bytes(b"\x89PNG b")
    sent = _run(attachment_paths=["/home/user/Desktop/a.pdf",
                                  r"\home\user\Desktop\b.png"])
    assert [Path(p) for p in sent["many"]] == [output_dir / "a.pdf",
                                               output_dir / "b.png"]


def test_no_attachment_stays_no_attachment(output_dir):
    sent = _run()
    assert sent["single"] == ""
    assert sent["many"] == []


# ── the roots must include where SUNI actually writes ────────────────────────
def test_the_configured_output_dir_is_an_allowed_root(tmp_path, monkeypatch):
    """A module-level roots list excluded global_output_dir entirely: SUNI
    would write a PDF there and then refuse to attach its own output, saying
    "not in allowed directories". It passed unnoticed only because the
    directory falls back to Desktop on this instance."""
    from suni.notifications import email_notify

    out = tmp_path / "suni_out"
    out.mkdir()
    generated = out / "report.pdf"
    generated.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("suni.user_settings.resolve_output_dir",
                        lambda user_id="": str(out))
    assert email_notify._validate_attachment(generated) is True


def test_a_blocked_extension_is_still_blocked(tmp_path, monkeypatch):
    """Widening the roots must not widen what may be sent."""
    from suni.notifications import email_notify

    out = tmp_path / "suni_out"
    out.mkdir()
    payload = out / "installer.exe"
    payload.write_bytes(b"MZ")

    monkeypatch.setattr("suni.user_settings.resolve_output_dir",
                        lambda user_id="": str(out))
    assert email_notify._validate_attachment(payload) is False


def test_an_arbitrary_directory_is_still_refused(tmp_path, monkeypatch):
    from suni.notifications import email_notify

    monkeypatch.setattr("suni.user_settings.resolve_output_dir",
                        lambda user_id="": str(tmp_path / "suni_out"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    stray = tmp_path / "elsewhere" / "x.pdf"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"%PDF-1.4")
    assert email_notify._validate_attachment(stray) is False
