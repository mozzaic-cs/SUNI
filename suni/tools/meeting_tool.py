"""
Meeting recording, as tools the model can call.

The one that matters is start_meeting_recording. It takes
`participants_informed`, which the model cannot know and must not guess — it is
a statement about something that happened in a room. The tool is also
consequential, so it goes through the approval gate: the human sees a card and
clicks Allow before anything records. Those two together are the consent
mechanism, and neither has a default that lets it be skipped.

The handlers return prose rather than JSON because the model reads them back to
the user, and "Recording started" is a better answer than a dict.
"""
from __future__ import annotations

import asyncio

from .. import meetings as _m
from .. import transcription as _tx
from ..logger import get_logger

log = get_logger("suni.tools.meeting")

# The orchestrator already binds the signed-in user for the whole tool loop.
# A second contextvar of our own would be one more thing to remember to set, and
# the first time it was forgotten a meeting would be filed under nobody.
from .registry import USER_ID_CTX


def _who() -> tuple[str, str]:
    """(user_id, username) for the caller of this tool."""
    uid = USER_ID_CTX.get("") or ""
    name = ""
    try:
        from .. import auth as _auth
        u = _auth.get_user(uid) if uid else None
        if u:
            name = str(u.get("username") or "")
    except Exception:                    # noqa: BLE001 — audit gets the id at least
        pass
    return uid, name


START_SCHEMA = {
    "name": "start_meeting_recording",
    "description": (
        "Start recording the audio of a meeting happening on this machine "
        "(Teams, Meet, Zoom, or a phone on speaker). SUNI does not join the "
        "meeting; it records what this computer plays and hears. "
        "REQUIRES that the participants have been told they are being recorded "
        "— ask the user to say the announcement first and confirm they did. "
        "Never pass participants_informed=true unless the user has actually "
        "said so."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short name for the meeting, e.g. 'Weekly with Maria'.",
            },
            "participants_informed": {
                "type": "boolean",
                "description": (
                    "True ONLY if the user has confirmed everyone in the meeting "
                    "was told it is being recorded and an AI will summarise it."
                ),
            },
        },
        "required": ["participants_informed"],
    },
}


async def start_handler(participants_informed: bool = False, title: str = "") -> str:
    try:
        r = await _m.start_recording(
            *_who(),
            title=title, participants_informed=bool(participants_informed),
        )
        sources = ", ".join(r.get("sources") or r["devices"])
        if r.get("auto_stop"):
            end = (f"It stops by itself when the call window closes "
                   f"({'; '.join(r['auto_stop'])}), and the transcript is made "
                   f"straight after. Or say when to stop.")
        else:
            end = "No call window was found, so say when to stop."
        return f"Recording '{r['title']}' (id {r['meeting_id']}) from: {sources}. {end}"
    except _m.MeetingError as e:
        return f"Could not start recording: {e}"


STOP_SCHEMA = {
    "name": "stop_meeting_recording",
    "description": "Stop the meeting recording that is currently running and save the audio.",
    "parameters": {"type": "object", "properties": {}},
}


async def stop_handler() -> str:
    try:
        r = await _m.stop_recording(*_who())
        mins = r["seconds"] // 60
        return (f"Stopped '{r['title']}' after {mins} min {r['seconds'] % 60}s "
                f"({r['size_mb']} MB). Meeting id {r['meeting_id']}. "
                f"Ask me to transcribe it when you want the summary.")
    except _m.MeetingError as e:
        return f"Could not stop recording: {e}"


LIST_SCHEMA = {
    "name": "list_meetings",
    "description": "List this user's recorded meetings, most recent first.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many to list (default 10)."},
        },
    },
}


def list_handler(limit: int = 10) -> str:
    rows = _m.list_meetings(_who()[0], limit=int(limit or 10))
    if not rows:
        return "No recorded meetings."
    out = ["Recorded meetings:"]
    for r in rows:
        secs = r.get("seconds") or 0
        state = []
        if r.get("has_transcript"): state.append("transcribed")
        if r.get("has_summary"):    state.append("summarised")
        out.append(f"  • {r.get('title','?')} — {r.get('started_at','')[:16]} "
                   f"({secs // 60}m{secs % 60:02d}s){' · ' + ', '.join(state) if state else ''} "
                   f"[{r.get('meeting_id','')}]")
    return "\n".join(out)


TRANSCRIBE_SCHEMA = {
    "name": "transcribe_meeting",
    "description": (
        "Transcribe a recorded meeting to text. Slow on CPU — minutes for a long "
        "meeting. Returns the transcript so it can be summarised."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": "Meeting id from list_meetings. Omit for the most recent.",
            },
        },
    },
}


async def transcribe_handler(meeting_id: str = "") -> str:
    return await transcribe_for(_who()[0], meeting_id)


# One transcription per meeting at a time. After an automatic stop one starts
# by itself; asking for the transcript meanwhile waits for that one instead of
# running a second on the same CPU. Shielded: a Claude Code turn that times out
# cancels its tool call, and that must not throw away ten minutes of work —
# the next ask finds the finished transcript.
_jobs: dict[tuple[str, str], asyncio.Future] = {}


async def transcribe_for(uid: str, meeting_id: str = "") -> str:
    if not meeting_id:
        rows = _m.list_meetings(uid, limit=1)
        if not rows:
            return "No recorded meetings to transcribe."
        meeting_id = rows[0]["meeting_id"]
    key = (uid, meeting_id)
    job = _jobs.get(key)
    if job is None or job.done():
        job = asyncio.ensure_future(_transcribe(uid, meeting_id))
        _jobs[key] = job
        job.add_done_callback(lambda j, k=key: _jobs.pop(k, None) if _jobs.get(k) is j else None)
    return await asyncio.shield(job)


async def _transcribe(uid: str, meeting_id: str) -> str:
    try:
        mdir, meta = _m.get_meeting(uid, meeting_id)
    except _m.MeetingError as e:
        return str(e)
    cached = mdir / "transcript.txt"
    if cached.exists():
        return (f"Transcript of '{meta.get('title','')}' (already transcribed):\n\n"
                + cached.read_text(encoding="utf-8"))
    if not _tx.available():
        return ("Local transcription is not installed. Install it with:\n"
                "    pip install -r requirements-meetings.txt\n"
                "The recording is safe — it can be transcribed later.")
    wav = mdir / "audio.wav"
    if not wav.exists():
        return f"Meeting '{meeting_id}' has no audio file."
    try:
        # The language of the person whose meeting this is. Without it,
        # transcribe_file falls back to the INSTANCE default — which on this
        # machine is en-GB while the speaker is pt-PT, so a Portuguese meeting
        # came back as confident English nonsense. The same mistake was made on
        # the live speech path; this is the other place it was waiting.
        try:
            from .. import user_settings as _us
            _lang = str((_us.get(uid) or {}).get("stt_language", "") or "")
        except Exception:      # noqa: BLE001 — a missing preference is not fatal
            _lang = ""
        segments = await _tx.transcribe_file(wav, language=_lang)
    except _tx.TranscriptionError as e:
        return str(e)
    if not segments:
        return ("The recording transcribed to nothing — it is probably silent. "
                "Check the configured audio device is enabled in Windows sound settings.")

    text = _tx.to_text(segments)
    cached.write_text(text, encoding="utf-8")
    meta["state"] = "transcribed"
    meta["segments"] = len(segments)
    (mdir / "meeting.json").write_text(__import__("json").dumps(meta, indent=2),
                                       encoding="utf-8")
    log.info("[MEETING] transcribed %s — %d segments", meeting_id, len(segments))
    return (f"Transcript of '{meta.get('title','')}' ({len(segments)} segments):\n\n"
            + text)


async def _after_auto_stop(uid: str, stopped: dict) -> None:
    """The meeting ended by itself: have the transcript ready for the ask."""
    try:
        await transcribe_for(uid, stopped["meeting_id"])
    except Exception as exc:      # noqa: BLE001 — it can still be asked for
        log.warning("[MEETING] transcription after auto-stop failed: %s", exc)


_m.on_auto_stop = _after_auto_stop
