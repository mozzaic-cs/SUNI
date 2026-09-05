"""
Recording a meeting SUNI is not in.

SUNI does not join Teams, Meet or Zoom. It listens to what this machine already
plays and hears, alongside whoever is actually in the call. That decision is the
whole design, and it buys three things a bot cannot:

  * it works identically on every platform, including a phone on speaker,
    because it never touches the platform;
  * it never appears in a participant list, never breaks when a vendor ships a
    UI change, and never violates a terms-of-service clause about automation;
  * the audio never leaves the machine — no third-party meeting-bot service,
    which is the opposite of what a self-hosted assistant is for.

WHAT IT WRITES, AND WHERE
Everything lands in the recording user's OWN output directory, through
resolve_output_dir(), so a meeting is covered by the same isolation as any other
generated file. Files sitting outside it would re-open the leak that was closed
the same week this was written.

    <user output dir>/meetings/<id>/
        audio.wav        16 kHz mono — what whisper wants, and small
        meeting.json     title, who started it, when, how long, state
        transcript.txt   written by the transcription pass
        summary.md       written by the summarisation pass

CONSENT IS A GATE, NOT A SETTING
start_recording() refuses unless the caller passes participants_informed=True.
There is no configuration that makes that default to true, and no code path that
supplies it on the caller's behalf. This is deliberate: `output_guard` is this
project's cautionary tale about a control that ships disabled — a protection
that can be switched off quietly is not a protection.

The software cannot verify that anyone was actually told. What it can do is
refuse to record unless a human states it, record WHO stated it, and make the
recording visible while it runs. announcement_text() exists so telling people is
one sentence rather than a thing to compose under time pressure.

Every start and stop is written to the audit trail. For a feature that records
people, "who started this, when, and for how long" is the record that matters,
and the table was already there.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import audit as _audit
from . import config as _cfg
from .logger import get_logger
from .user_settings import resolve_output_dir

log = get_logger("suni.meetings")

# One active recording per user. Keyed by user_id, holds the ffmpeg process and
# the metadata needed to close the file out.
_active: dict[str, dict] = {}

_FFMPEG_TIMEOUT_S = 15          # grace for ffmpeg to finalise the file on stop


class MeetingError(RuntimeError):
    """Something the caller should be told plainly, not a crash."""


# ── where things live ────────────────────────────────────────────────────────

def meetings_dir(user_id: str) -> Path:
    """This user's meetings folder, inside their own output directory."""
    d = Path(resolve_output_dir(user_id)) / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meeting_dir(user_id: str, meeting_id: str) -> Path:
    d = meetings_dir(user_id) / meeting_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_meta(path: Path) -> dict:
    try:
        return json.loads((path / "meeting.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(path: Path, meta: dict) -> None:
    (path / "meeting.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ── devices ──────────────────────────────────────────────────────────────────

def list_audio_devices() -> list[str]:
    """Audio inputs ffmpeg can actually open, as it names them.

    Windows lists devices that are present but DISABLED — "Stereo Mix" is the
    usual one — so a name appearing here is not proof it will open. The caller
    finds that out at start; this is for showing the operator their options.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        raise MeetingError("ffmpeg is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise MeetingError("Listing audio devices timed out.")
    # ffmpeg writes the device list to stderr and exits non-zero. That is normal.
    out = (r.stderr or "") + (r.stdout or "")
    return re.findall(r'"([^"]+)"\s*\(audio\)', out)


def _configured_devices() -> list[str]:
    """Which inputs to record. Both halves of the conversation need capturing:
    the loopback carries the people on the call, the microphone carries the
    people in the room. Recording only one produces half a meeting."""
    raw = _cfg.get("meeting_devices") or []
    if isinstance(raw, str):
        raw = [d.strip() for d in raw.split(",") if d.strip()]
    return [str(d) for d in raw if str(d).strip()]


def capture_args(devices: list[str], wav: Path) -> list[str]:
    """The ffmpeg command line for a capture.

    Split out so the recording lifecycle — spawn, liveness check, graceful stop,
    file finalisation — can be exercised against a SYNTHESISED source. Testing
    it against a real microphone would mean recording whoever is in the room to
    find out whether recording works.

    One input per device, mixed to a single 16 kHz mono track: what whisper
    wants, and roughly 2 MB per minute instead of 10.
    """
    args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for d in devices:
        args += ["-f", "dshow", "-i", f"audio={d}"]
    if len(devices) > 1:
        args += ["-filter_complex",
                 f"amix=inputs={len(devices)}:duration=longest:normalize=0"]
    args += ["-ac", "1", "-ar", "16000", str(wav)]
    return args


# ── the consent text ─────────────────────────────────────────────────────────

def announcement_text(lang: str = "en") -> str:
    """A sentence to say before starting, so telling people is not a thing to
    compose under time pressure. Names the AI explicitly — under the AI Act's
    Article 50 people are entitled to know they are dealing with one, and a
    recording that feeds an assistant is squarely that."""
    if str(lang).lower().startswith("pt"):
        return ("Antes de começarmos: vou gravar esta reunião e um assistente de "
                "IA vai gerar um resumo. A gravação fica no nosso próprio "
                "servidor. Alguém se opõe?")
    return ("Before we start: I'm recording this meeting and an AI assistant "
            "will produce a summary. The recording stays on our own server. "
            "Any objections?")


# ── recording ────────────────────────────────────────────────────────────────

def active_recording(user_id: str) -> dict | None:
    """What this user is currently recording, if anything."""
    entry = _active.get(user_id)
    if not entry:
        return None
    return {
        "meeting_id": entry["meeting_id"],
        "title":      entry["title"],
        "started_at": entry["started_at"],
        "seconds":    round(
            (datetime.now(timezone.utc)
             - datetime.fromisoformat(entry["started_at"])).total_seconds()),
    }


async def start_recording(
    user_id: str,
    username: str,
    title: str = "",
    *,
    participants_informed: bool,
    devices: list[str] | None = None,
) -> dict:
    """Begin capturing. Refuses unless a human states participants were told.

    `participants_informed` is keyword-only and has NO default on purpose: a
    caller has to type it, so nothing acquires the ability to record by
    forgetting an argument.
    """
    if not bool(_cfg.get("meetings_enabled", False)):
        raise MeetingError(
            "Meeting recording is turned off. An admin enables it in "
            "Configuration (meetings_enabled).")

    if participants_informed is not True:
        raise MeetingError(
            "Recording refused: nobody has confirmed the participants were told. "
            "Say the announcement first, then start the recording and confirm it.")

    if user_id in _active:
        cur = active_recording(user_id)
        raise MeetingError(
            f"Already recording '{cur['title']}' ({cur['seconds']}s). Stop that first.")

    devs = devices or _configured_devices()
    if not devs:
        raise MeetingError(
            "No audio devices configured. Set meeting_devices — usually the "
            "system loopback ('Stereo Mix …') plus your microphone.")

    meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    mdir  = _meeting_dir(user_id, meeting_id)
    wav   = mdir / "audio.wav"

    args = capture_args(devs, wav)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise MeetingError("ffmpeg is not installed or not on PATH.")

    # ffmpeg fails fast on a device it cannot open — a disabled "Stereo Mix" is
    # the common one. Give it a moment and report that now, rather than handing
    # back a meeting id for a recording that never started.
    await asyncio.sleep(1.5)
    if proc.returncode is not None:
        err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
        raise MeetingError(
            f"Could not open the audio device(s) {devs}: {err.splitlines()[-1] if err else 'unknown error'}")

    started = datetime.now(timezone.utc).isoformat()
    meta = {
        "meeting_id":  meeting_id,
        "title":       title or "Untitled meeting",
        "started_at":  started,
        "started_by":  username or user_id,
        "user_id":     user_id,
        "devices":     devs,
        "participants_informed_by": username or user_id,
        "state":       "recording",
    }
    _write_meta(mdir, meta)
    _active[user_id] = {**meta, "proc": proc, "dir": str(mdir)}

    # If it cannot be audited, it does not run. Recording people without a
    # record of who started it is the one outcome this feature must not
    # produce — and ffmpeg is already live by this point, so a failure here
    # would otherwise leave a recording going while the caller is told it
    # failed. Found by a test whose audit table did not exist.
    try:
        _audit.log_event(user_id, username, "meeting.recording.started",
                         detail=f"{meta['title']} · devices={len(devs)}",
                         target_id=meeting_id)
    except Exception as exc:                     # noqa: BLE001
        _active.pop(user_id, None)
        try:
            proc.terminate()
        except Exception:                        # noqa: BLE001
            pass
        log.error("[MEETING] audit write failed; recording aborted: %s", exc)
        raise MeetingError(
            f"Could not write the audit record, so the recording was stopped: {exc}")
    log.info("[MEETING] recording started %s (%s) by %s", meeting_id, meta["title"], username)
    return {"meeting_id": meeting_id, "title": meta["title"],
            "path": str(wav), "devices": devs}


async def stop_recording(user_id: str, username: str = "") -> dict:
    """Finish the recording and close the file cleanly.

    ffmpeg is asked to quit via stdin rather than killed: a terminated ffmpeg
    can leave a WAV whose header never got its final length, and the first thing
    anyone does with a meeting recording is open it.
    """
    entry = _active.pop(user_id, None)
    if not entry:
        raise MeetingError("Nothing is being recorded.")

    proc: asyncio.subprocess.Process = entry["proc"]
    try:
        if proc.returncode is None:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), timeout=_FFMPEG_TIMEOUT_S)
    except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
        log.warning("[MEETING] ffmpeg did not quit on request; terminating")
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:                            # noqa: BLE001
            pass

    mdir  = Path(entry["dir"])
    wav   = mdir / "audio.wav"
    ended = datetime.now(timezone.utc)
    secs  = round((ended - datetime.fromisoformat(entry["started_at"])).total_seconds())

    meta = _read_meta(mdir)
    meta.update({
        "ended_at": ended.isoformat(),
        "seconds":  secs,
        "state":    "recorded",
        "audio_bytes": wav.stat().st_size if wav.exists() else 0,
    })
    _write_meta(mdir, meta)

    _audit.log_event(user_id, username, "meeting.recording.stopped",
                     detail=f"{meta.get('title','')} · {secs}s",
                     target_id=entry["meeting_id"])
    log.info("[MEETING] recording stopped %s after %ss", entry["meeting_id"], secs)

    if not wav.exists() or wav.stat().st_size < 1000:
        raise MeetingError(
            "The recording stopped but produced no usable audio — check that the "
            "configured device is enabled in Windows sound settings.")

    return {"meeting_id": entry["meeting_id"], "title": meta.get("title", ""),
            "seconds": secs, "path": str(wav),
            "size_mb": round(meta["audio_bytes"] / 1_048_576, 1)}


# ── reading them back ────────────────────────────────────────────────────────

def list_meetings(user_id: str, limit: int = 20) -> list[dict]:
    """Most recent first. Only this user's own meetings — the folder is inside
    their output directory, so there is nothing else here to see."""
    root = meetings_dir(user_id)
    out: list[dict] = []
    for d in sorted(root.iterdir(), reverse=True) if root.exists() else []:
        if not d.is_dir():
            continue
        meta = _read_meta(d)
        if meta:
            meta.pop("user_id", None)
            meta["has_transcript"] = (d / "transcript.txt").exists()
            meta["has_summary"]    = (d / "summary.md").exists()
            out.append(meta)
        if len(out) >= limit:
            break
    return out


def get_meeting(user_id: str, meeting_id: str) -> tuple[Path, dict]:
    """Directory and metadata, or a clear error. The id is checked against the
    filesystem rather than trusted — it arrives from a model and a URL."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(meeting_id))
    if not safe:
        raise MeetingError("Invalid meeting id.")
    d = meetings_dir(user_id) / safe
    if not d.is_dir():
        raise MeetingError(f"No meeting '{safe}' for this user.")
    return d, _read_meta(d)
