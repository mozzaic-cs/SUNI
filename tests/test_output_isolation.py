"""
Generated files belong to the person who generated them.

SUNI writes files on the SERVER — the machine it runs on — and hands them to a
remote user through /api/files/serve. That is fine while SUNI is one person on
their own laptop. It stops being fine the moment colleagues can reach it over
the network, which is the deployment this was written for.

Two things were wrong, and both were verified before being fixed rather than
argued about:

  * `/api/files/serve` allowed `~/Desktop`, `~/Downloads` and `~/Documents` — the
    personal folders of whoever runs SUNI. A READ-ONLY account fetched a canary
    file out of ~/Documents that it had never created and was never shown. The
    first test here is that same probe, now expected to fail.

  * Everyone shared ONE output directory, so each person's documents sat beside
    everybody else's, and the serve endpoint would hand any of them to any
    account that asked.

The negative cases are the point. A test suite that only proves a user can read
their OWN files would have passed just as happily before the fix.
"""
from __future__ import annotations

import pathlib
import uuid

import pytest

from suni import user_settings


# ── the leak that was real ──────────────────────────────────────────────────

def test_a_read_only_user_can_no_longer_read_the_operators_documents(client, ro_headers):
    """The exact probe that returned 200 and the file's contents before."""
    probe = pathlib.Path.home() / "Documents" / f"suni_probe_{uuid.uuid4().hex[:8]}.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("PRIVATE-CANARY", encoding="utf-8")
    try:
        r = client.get("/api/files/serve", params={"path": str(probe)},
                       headers=ro_headers)
        assert r.status_code == 403, (
            f"a read-only account read the operator's private file "
            f"(HTTP {r.status_code})")
        assert b"PRIVATE-CANARY" not in r.content
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.parametrize("folder", ["Desktop", "Downloads", "Documents"])
def test_the_operators_personal_folders_are_not_download_roots(folder):
    """Asserted on the list itself as well as through the endpoint: someone
    re-adding one 'for convenience' should fail the build."""
    from suni.web import server
    src = (pathlib.Path(server.__file__)).read_text(encoding="utf-8-sig")
    block = src.split("_ALLOWED_DL_ROOTS = [")[1].split("]")[0]
    assert folder not in block, f"~/{folder} is a download root again"


def test_email_cannot_attach_from_those_folders_either(monkeypatch, tmp_path):
    """The same leak had a second route out of the building."""
    from suni.notifications import email_notify
    monkeypatch.setattr("suni.user_settings.resolve_output_dir",
                        lambda user_id="": str(tmp_path))
    roots = {str(p) for p in email_notify._safe_attach_roots("someone")}
    # Check the three personal folders by name, not by home prefix: pytest's
    # tmp_path lives under the home directory too, so a prefix test flags the
    # legitimate output directory and proves nothing.
    home = pathlib.Path.home()
    personal = {str(home / n) for n in ("Desktop", "Downloads", "Documents")}
    assert not (personal & roots), \
        f"an attachment can still be pulled from a personal folder: {personal & roots}"


# ── isolation between users ─────────────────────────────────────────────────

def test_each_user_gets_their_own_directory(test_users):
    a = user_settings.resolve_output_dir(test_users["admin"]["id"])
    b = user_settings.resolve_output_dir(test_users["standard"]["id"])
    assert a != b, "two users share one output directory"


def test_a_user_cannot_download_another_users_file(client, std_headers, test_users):
    """The whole point of per-user folders."""
    other = pathlib.Path(user_settings.resolve_output_dir(test_users["readonly"]["id"]))
    other.mkdir(parents=True, exist_ok=True)
    victim = other / "their_private_report.txt"
    victim.write_text("SOMEONE-ELSES-WORK", encoding="utf-8")
    try:
        r = client.get("/api/files/serve", params={"path": str(victim)},
                       headers=std_headers)
        assert r.status_code == 403, \
            f"one user downloaded another's file (HTTP {r.status_code})"
        assert b"SOMEONE-ELSES-WORK" not in r.content
    finally:
        victim.unlink(missing_ok=True)


def test_a_user_can_still_download_their_own_file(client, std_headers, test_users):
    """Isolation that also blocks the owner is just breakage."""
    mine = pathlib.Path(user_settings.resolve_output_dir(test_users["standard"]["id"]))
    mine.mkdir(parents=True, exist_ok=True)
    f = mine / "my_report.txt"
    f.write_text("MY-OWN-WORK", encoding="utf-8")
    try:
        r = client.get("/api/files/serve", params={"path": str(f)},
                       headers=std_headers)
        assert r.status_code == 200, f"a user cannot read their own file ({r.status_code})"
        assert b"MY-OWN-WORK" in r.content
    finally:
        f.unlink(missing_ok=True)


def test_the_shared_root_itself_is_not_servable(client, std_headers, tmp_path):
    """Allowing the root would let anyone walk into a sibling's folder."""
    root = user_settings.output_root().resolve()
    stray = root / "loose_file.txt"
    root.mkdir(parents=True, exist_ok=True)
    stray.write_text("IN-THE-ROOT", encoding="utf-8")
    try:
        r = client.get("/api/files/serve", params={"path": str(stray)},
                       headers=std_headers)
        assert r.status_code == 403, "the shared root is served to any account"
    finally:
        stray.unlink(missing_ok=True)


# ── where files go by default ───────────────────────────────────────────────

def test_the_default_root_is_inside_the_install_not_a_personal_folder():
    """The Desktop default was written for a single-user tool. On a networked
    instance it puts every user's output in the operator's own folder."""
    root = user_settings.output_root()
    assert "Desktop" not in str(root), "output still defaults to the Desktop"
    assert "files" in str(root).replace("\\", "/"), f"unexpected default: {root}"


def test_a_custom_output_dir_outside_the_root_is_ignored(test_users, tmp_path):
    """Otherwise setting output_dir would be a way to point the serve endpoint
    at any path on the machine."""
    uid = test_users["standard"]["id"]
    escape = tmp_path / "somewhere_else"
    escape.mkdir(parents=True, exist_ok=True)
    user_settings.save(uid, {"output_dir": str(escape)})
    try:
        got = pathlib.Path(user_settings.resolve_output_dir(uid)).resolve()
        assert got != escape.resolve(), "output_dir escaped the root"
        assert got.is_relative_to(user_settings.output_root().resolve())
    finally:
        user_settings.save(uid, {"output_dir": ""})


def test_work_with_no_signed_in_user_is_kept_out_of_everyones_folder():
    """Scheduled runs and channel gateways have no user. Dropping their output
    in the root would put it where the isolation rules do not reach."""
    d = pathlib.Path(user_settings.resolve_output_dir(""))
    assert d.name == "_system", f"unattributed output goes to {d}"


def test_folder_names_are_readable_and_unique(test_users):
    """Readable so an operator can find their files by looking; unique because
    two usernames can sanitise to the same string and silently share a folder."""
    name = user_settings.user_folder_name(test_users["admin"]["id"])
    assert "admin_test" in name or "admin" in name, f"unreadable folder: {name}"
    assert name != user_settings.user_folder_name(test_users["standard"]["id"])
