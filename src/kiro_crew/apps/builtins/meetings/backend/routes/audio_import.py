"""Meetings — import an existing recording into a live meeting.

``POST …/{id}/import`` takes a host path to an audio file, transcribes it with the
gateway's own batch speech-to-text, and feeds the result into the meeting exactly as
if it had been spoken.

**Why "as if it had been spoken" rather than a transcript record.** This app keeps no
transcript file — speech is dispatched to agents and the durable artifacts are their
outputs (the minutes, the sketch, ``tasks.json``). So an import that wrote a
transcript somewhere would produce a file nothing reads. Routing the text through
:meth:`MeetingSession.broadcast` instead means the import gets the whole pipeline for
free and, more importantly, gets the SAME one: domain-dictionary correction, the noise
gate, per-agent batching, the muted-agent list, and live translation all behave as
they do for a microphone. There is no second code path to keep in step.

The consequence, and it is the honest way round: an import needs a LIVE meeting. That
is not a limitation to work around — the agents are what turn transcript into minutes,
and they only exist while a meeting is running.

Security posture:

* The client-supplied path goes through :func:`kiro_crew.hooks.validate_file_path`,
  the shared dashboard file gate, which canonicalizes (following symlinks) and
  enforces ``is_sensitive_path``. The predicate is never called directly here — using
  the gate is what keeps this route's answer identical to every other file read in the
  product. ``transcribe_audio`` re-checks the path itself, so a refusal is enforced
  twice by two owners.
* Rejections are SEL-audited, mirroring ``handle_post_note_image``.
* The transcript is already redacted by ``transcribe_audio`` before it is returned,
  and hallucination-filtered for whisper-family providers. **This module calls no
  redactor of its own**, deliberately: a second pass over text that has already been
  scrubbed adds no coverage and can only mangle a substituted span. (That absence is
  also why it is neither a registered redaction sink nor an allowlisted non-egress
  module in ``security_posture`` — there is no call site to classify.)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import audio
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    audit,
    field_str,
    json_body,
    live_session,
)
from kiro_crew.hooks import validate_file_path

logger = logging.getLogger("kirocrew.app.meetings")


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _vet_audio_file(raw_path: str) -> tuple[str, str]:
    """Return ``(canonical_path, "")`` or ``("", reason)``. BLOCKING.

    One helper for the whole check because all three steps touch the filesystem and
    they belong in the same thread hop: the gate canonicalizes (a ``realpath``), and
    the existence and suffix tests must apply to the CANONICAL path rather than to
    what the client sent — otherwise a symlink with an ``.mp3`` name could point at
    something else entirely.
    """
    canonical = validate_file_path(raw_path)
    if canonical is None:
        return "", "denied"
    path = Path(canonical)
    if not path.is_file():
        return "", "not_a_file"
    if path.suffix.lower() not in k.IMPORT_AUDIO_EXTENSIONS:
        return "", "unsupported_format"
    return canonical, ""


def _transcription_ready() -> bool:
    """Whether batch speech-to-text is usable at all. BLOCKING (reads config)."""
    from kiro_crew.transcribe import is_available

    try:
        return bool(is_available())
    except Exception:  # pragma: no cover — a broken config must not 500 the route
        logger.warning("meetings: could not determine STT availability", exc_info=True)
        return False


async def handle_import_audio(request: web.Request) -> web.Response:
    """Transcribe a recording from disk and dispatch it into the live meeting."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    raw_path = field_str(body, "audio_path", required=True, max_len=k.MAX_AUDIO_PATH_CHARS)

    def _reject(reason: str) -> None:
        audit("meetings.import_audio", f"{meeting_id} reason:{reason}", outcome="rejected")

    # The live session FIRST, before the expensive steps. Transcribing an hour of
    # audio and only then discovering there is nothing to dispatch into would waste
    # minutes of the user's time to reach an error we can give immediately.
    session = await live_session(request, meeting_id)

    canonical, reason = await asyncio.to_thread(_vet_audio_file, raw_path)
    if reason == "denied":
        _reject(reason)
        raise BadRequest(
            "that path cannot be read", status=403, code="audio_path_denied"
        )
    if reason == "not_a_file":
        _reject(reason)
        raise BadRequest("no such audio file", status=404, code="audio_file_not_found")
    if reason:
        _reject(reason)
        raise BadRequest(
            "unsupported audio format", status=400, code="audio_format_unsupported"
        )

    if not await asyncio.to_thread(_transcription_ready):
        # 503, not 400: the request is fine and will work once speech-to-text is
        # configured, which is a Settings action rather than a different request.
        raise BadRequest(
            "speech-to-text is not available",
            status=503,
            code="transcription_unavailable",
        )

    from kiro_crew.transcribe import transcribe_audio

    transcript = await transcribe_audio(canonical)
    # None covers three different endings — provider failure, a disabled provider,
    # and a transcript the hallucination filter emptied — and the client's move is
    # the same for all of them, so they share one code.
    if not transcript:
        audit("meetings.import_audio", f"{meeting_id} path:{canonical}", outcome="failed")
        raise BadRequest(
            "could not transcribe that recording", status=502, code="transcription_failed"
        )

    lines = audio.split_transcript(
        transcript, max_chars=k.MAX_TRANSCRIPT_CHARS, max_lines=k.MAX_IMPORT_LINES
    )

    # Enqueued, not awaited. `broadcast` hands each line to the per-agent batcher and
    # returns, so the whole import is queued in microseconds and the agents work
    # through it on their own timers — the same as a live meeting, and the reason a
    # long recording does not hold the request open.
    dispatched = 0
    for line in lines:
        if session.broadcast(line):
            dispatched += 1

    audit("meetings.import_audio", f"{meeting_id} lines:{len(lines)}", outcome="ok")
    return _response(canonical, lines, dispatched)


def _response(canonical: str, lines: list[str], dispatched: int) -> web.Response:
    """The success body.

    ``lines`` and ``dispatched`` are reported separately on purpose: the gap between
    them is what the noise gate dropped, and a recording that yields 400 lines of
    which 0 were dispatched is a real outcome the user needs to be able to see (an
    empty room, a filtered hallucination) rather than a silent success.
    """
    payload: dict[str, Any] = {
        "ok": True,
        "path": canonical,
        "lines": len(lines),
        "dispatched": dispatched,
    }
    return web.json_response(payload)
