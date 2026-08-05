"""Meeting lifecycle routes — init, start, pause/resume, stop, list, outputs.

``POST …/{id}/init``          create the meeting folder + seed files (idempotent)
``POST …/{id}/start``         activate: seed outputs, spawn agent sessions
``POST …/{id}/status``        move between active / paused / reviewing
``POST …/{id}/stop``          flush agents, mark ended
``GET  …/meetings``           list every meeting with metadata on disk
``GET  …/{id}``               one meeting's metadata
``GET  …/{id}/outputs``       batch-read every agent output + tasks.json
``POST …/{id}/attachments``   add/remove context attachments
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from aiohttp import BodyPartReader, web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import images
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.domain import translate
from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    ACTIVE,
    START_LOCK,
    BadRequest,
    audit,
    data_root,
    field_str,
    field_str_list,
    hooks_of,
    json_body,
    query_int,
    sessions_of,
)
from kiro_crew.security import redact

logger = logging.getLogger("kirocrew.app.meetings")

# An attachment is a small, fixed-shape record; anything else is dropped rather
# than stored, so a malformed entry can never reach an agent prompt.
_ATTACHMENT_TYPES = ("file", "url")


def _meeting_id(request: web.Request) -> str:
    """The validated, filesystem-safe meeting id from the URL."""
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _clean_attachment(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if kind not in _ATTACHMENT_TYPES:
        return None
    label = redact(str(raw.get("label") or "").strip())[:200]
    if kind == "url":
        url = str(raw.get("url") or "").strip()
        # Only http(s) — a file:// or javascript: "attachment" would be handed to
        # an agent as something to open.
        if not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
            return None
        return {"type": "url", "url": redact(url), "label": label or url[:80]}
    path = str(raw.get("path") or "").strip()
    if not path or len(path) > 1000:
        return None
    return {"type": "file", "path": redact(path), "label": label or path.rsplit("/", 1)[-1]}


def _init_meeting(
    meeting_id: str, title: str, body: dict[str, Any], root: Any
) -> dict[str, Any]:
    """Create the meeting folder, metadata, tasks file, and agent outputs. BLOCKING.

    Runs on a worker thread, never the event loop: this is half a dozen filesystem
    operations (two directory creations, a JSON read, up to two atomic writes, and
    one seeded output file per enabled agent), and it is the first call the
    dashboard makes when a user opens a meeting. Inline, each of those syscalls
    stalls the gateway's single loop — the user's chat turn and the liveness
    heartbeat included.

    Grouped into ONE hop rather than a ``to_thread`` per store call so the
    metadata read and the writes derived from it cannot have another request
    interleaved between them.

    ``agents_enabled`` is validated HERE, after the folder work, because that is
    where the handler validated it inline — a malformed value must still 400 at the
    same point in the sequence rather than before the meeting folder exists.
    """
    store.ensure_data_dirs(root)
    mdir = store.meeting_dir(meeting_id, root)
    mdir.mkdir(parents=True, exist_ok=True)

    # Under the metadata lock: read-modify-write, on a worker thread, so a
    # concurrent request would otherwise interleave (see `store.meta_transaction`).
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            meta = store.new_meeting_meta(meeting_id, redact(title))
            store.write_meeting_meta(meeting_id, meta, root)

    if not store.tasks_path(meeting_id, root).is_file():
        store.write_tasks(meeting_id, [], root)

    config = store.read_config(root)
    agents_enabled = field_str_list(body, "agents_enabled") or meta.get("agents_enabled")
    enabled = sess.get_enabled_agents(config, agents_enabled)
    store.ensure_agent_files(meeting_id, enabled, meta.get("title", "Meeting Notes"), root)
    return meta


async def handle_meeting_init(request: web.Request) -> web.Response:
    """Create the meeting folder, metadata, tasks file, and agent outputs."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    title = field_str(body, "title", default="Meeting", max_len=k.MAX_TITLE_LEN)

    meta = await asyncio.to_thread(
        _init_meeting, meeting_id, title, body, data_root(request)
    )
    return web.json_response({"ok": True, "meeting_id": meeting_id, "meta": meta})


async def handle_get_meeting(request: web.Request) -> web.Response:
    """One meeting's metadata (the frontend's poll target)."""
    meeting_id = _meeting_id(request)
    meta = await asyncio.to_thread(store.read_meeting_meta, meeting_id, data_root(request))
    if meta is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)
    live = ACTIVE.get(meeting_id)
    return web.json_response(
        {
            "meta": meta,
            "live": live.status() if live is not None else None,
        }
    )


async def handle_list_meetings(request: web.Request) -> web.Response:
    # `list_meetings` globs `*/session.json` under the meetings root and JSON-parses
    # every hit, so it grows with the user's meeting history — off the loop.
    meetings = await asyncio.to_thread(store.list_meetings, data_root(request))
    return web.json_response({"meetings": meetings})


def _begin_meeting(
    meeting_id: str,
    agents_enabled: list[str] | None,
    title: str,
    preset: str,
    muted: list[str],
    root: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark a meeting active on disk and read the config back. BLOCKING.

    Runs on a worker thread, never the event loop: ``start_meeting_meta`` alone is
    a config read, a metadata read, a metadata write and one seeded output file per
    enabled agent, and this handler then rewrites the metadata and re-reads the
    config.

    Grouped into ONE hop so the read-modify-write of the metadata (status,
    ``preset``, ``muted_agents``) happens without another request interleaving, and
    so the config the live session is built from is the one that was on disk at
    that moment.
    """
    with store.meta_transaction():
        meta = sess.start_meeting_meta(meeting_id, agents_enabled, title, root)
        if preset:
            meta["preset"] = preset
        meta["muted_agents"] = muted
        store.write_meeting_meta(meeting_id, meta, root)
    return meta, store.read_config(root)


async def handle_start_meeting(request: web.Request) -> web.Response:
    """Activate a meeting: seed outputs, build the live session, init agents."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    root = data_root(request)
    title = field_str(body, "title", default="", max_len=k.MAX_TITLE_LEN)
    preset = field_str(body, "preset", default="", max_len=120)
    agents_enabled = field_str_list(body, "agents_enabled")
    muted = field_str_list(body, "muted_agents") or []
    is_restart = bool(body.get("restart") is True)

    # One critical section from the "is another meeting active?" read through to the
    # install. Both the metadata IO and the drain below are awaits, so two starts
    # interleaving in that gap would BOTH pass the check and the second would replace
    # the first — whose transcript then fails to dispatch with a confusing 409.
    async with START_LOCK:
        existing = ACTIVE.get()
        if existing is not None and existing.meeting_id != meeting_id and not existing.expired:
            audit("meetings.start", meeting_id, outcome="denied", error="another meeting is active")
            return web.json_response(
                {"error": "another meeting is already active", "code": "meeting_already_active"}, status=409
            )

        meta, config = await asyncio.to_thread(
            _begin_meeting, meeting_id, agents_enabled, redact(title), preset, muted, root
        )
        session = sess.MeetingSession(
            meeting_id=meeting_id,
            sessions=sessions_of(request),
            hooks=hooks_of(request),
            agents_enabled=agents_enabled,
            config=config,
            # Threaded through for the translation worker's writes, which are the
            # only ones a live session makes on its own rather than via a handler.
            root=root,
        )
        session.muted_agents = set(muted)
        # Drain the OUTGOING session before this one replaces it. `set()` cancels the
        # previous session's queues, so starting a second meeting while an earlier
        # (typically expired) one still held a half-batch discarded that transcript —
        # the same loss as the teardown paths, reached by a different route. Awaiting is
        # possible here because this is an `async def`; the previous justification for
        # the non-awaiting `set()` did not survive checking.
        outgoing = await ACTIVE.drain_and_clear()
        ACTIVE.set(session)

        # A replacement of a DIFFERENT meeting is a teardown of that meeting, so its
        # metadata needs the same terminal status every other teardown writes.
        #
        # Only an EXPIRED one can be here — the guard above 409s otherwise — and it
        # is gone for good: its session was just dropped, and reopening it would show
        # `active` with nothing installed, so its transcript dispatches would 409 into
        # the void. Two meetings persisting as `active` at once also breaks the
        # single-active-meeting invariant the list view reads.
        if outgoing is not None and outgoing.meeting_id != meeting_id:
            await asyncio.to_thread(sess.end_meeting_meta, outgoing.meeting_id, root)

        # ALWAYS initialize, restart or not, THEN send the restart notice.
        #
        # The restart branch used to skip `init_agents` entirely, on the assumption
        # that a restarted meeting's agents still remember their instructions. They
        # may not: the slots are ordinary kiro sessions and can have been reclaimed
        # (session cleanup, a gateway restart, an idle sweep) between stop and
        # restart. A fresh session then received only "continue appending to your
        # output" — an instruction that names no output — so it had no `OUTPUT_FILE`
        # and the notes and tasks silently stopped updating for the rest of the
        # meeting.
        #
        # Re-initializing a session that DOES remember is harmless: the init message
        # is idempotent by construction (it re-states the path and says "the file
        # already exists — overwrite it directly"), and `init_agents` writes no
        # files, so nothing already captured is lost. Ordering the notice last means
        # "disregard the previous 'Meeting ended' message" arrives after the
        # instructions it qualifies.
        #
        # INSIDE `START_LOCK`, which now also covers `handle_stop_meeting`. Agent
        # initialization is a long sequence of awaited dispatches, and it ran
        # unlocked: a stale Close in another tab could tear the session down midway,
        # so the remaining agents were initialized into a session no longer installed
        # while this request still answered `active` — a meeting the UI showed as
        # running, with no live session and an `ended` status on disk.
        #
        # The lock is what makes stop WAIT for a start to finish rather than
        # interleave with it. The cost is that a stop arriving during initialization
        # is delayed until the agents are ready, which is the correct order anyway:
        # the finalize notice stop broadcasts is only meaningful to agents that have
        # been told what they are doing.
        await sess.init_agents(session, meta, root)
        if is_restart:
            await sess.broadcast_system(session, k.SYSTEM_MEETING_RESTARTED)

    audit("meetings.start", meeting_id, outcome="ok")
    return web.json_response(
        {
            "ok": True,
            "status": k.STATUS_ACTIVE,
            "agents": sorted(meta.get("outputs", {}).keys()),
            "meta": meta,
        }
    )


def _apply_status(meeting_id: str, status: str, root: Any) -> dict[str, Any] | None:
    """Set a meeting's status on disk, or return None when it does not exist. BLOCKING.

    Runs on a worker thread, never the event loop: a metadata read plus an atomic
    metadata write.

    Grouped so the read-modify-write is a single hop — splitting it would let a
    concurrent status change land between the read and the write and be discarded.

    The TRANSITION is validated here, inside the lock, against the status actually
    on disk — not at the handler against a value read earlier. Checking outside
    would leave the same race the lock exists to close: two requests could each see
    a legal transition from the same starting status and the second would apply an
    illegal one.

    Raises :class:`BadRequest` for a transition the lifecycle does not allow. The
    dashboard greys out those buttons, but that is an affordance and not
    enforcement — the endpoint accepted any valid status name, so an authenticated
    ``POST status=idle`` against an ACTIVE meeting persisted "idle" while the live
    session stayed installed: transcription stopped feeding a meeting the UI still
    showed as running, and starting another answered 409 because ``ACTIVE`` was
    still held.
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return None
        current = str(meta.get("status") or k.STATUS_IDLE)
        # Same-status is always allowed: an idempotent retry of a request whose
        # response was lost must not fail.
        if status != current and status not in k.ALLOWED_TRANSITIONS.get(current, ()):
            raise BadRequest(
                f"a {current} meeting cannot move to {status}",
                status=HTTPStatus.CONFLICT,
                code="invalid_transition",
            )
        meta["status"] = status
        if status == k.STATUS_ENDED:
            meta["ended_at"] = store.utc_now_iso()
        store.write_meeting_meta(meeting_id, meta, root)
    return meta


async def handle_meeting_status(request: web.Request) -> web.Response:
    """Move a meeting between active / paused / reviewing."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    root = data_root(request)
    status = field_str(body, "status", default="", max_len=32)
    if status not in k.VALID_STATUSES:
        raise BadRequest(f"status must be one of {', '.join(k.VALID_STATUSES)}")

    meta = await asyncio.to_thread(_apply_status, meeting_id, status, root)
    if meta is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)

    session = ACTIVE.get(meeting_id)
    if session is not None and status in (k.STATUS_PAUSED, k.STATUS_REVIEWING, k.STATUS_ENDED):
        # A paused/reviewing meeting stops receiving transcription, so flush what
        # is queued rather than leaving a half-batch to expire with the session.
        await session.flush_all()
    if session is not None and status == k.STATUS_ENDED:
        # Already flushed above for this status, but use the draining teardown so a
        # future edit to the branch above cannot silently reintroduce the loss.
        await ACTIVE.drain_and_clear()

    return web.json_response({"ok": True, "status": status})


async def handle_stop_meeting(request: web.Request) -> web.Response:
    """End a meeting: flush every agent, send the finalize notice, mark ended.

    Takes ``START_LOCK``, so a stop cannot interleave with a start. Without it, a
    stale Close in one tab tore down a session another tab was still initializing:
    the remaining agents were initialized into a session no longer installed, and the
    start still answered `active` for a meeting with `ended` on disk and nothing live.

    Both directions matter, which is why the lock is shared rather than a second one:
    a stop landing mid-start waits for the agents to be ready (the finalize notice
    only means something to an initialized agent), and a start landing mid-stop waits
    for the teardown to complete rather than installing a session the stop then drops.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    async with START_LOCK:
        session = ACTIVE.get(meeting_id)
        if session is not None:
            await sess.broadcast_system(session, k.SYSTEM_MEETING_ENDED)
            # The finalize notice is itself enqueued, so the teardown MUST drain or
            # the very notice just broadcast would never reach the agents.
            await ACTIVE.drain_and_clear()
        # `end_meeting_meta` is itself a metadata read-modify-write; one hop keeps it
        # atomic with respect to the loop as well as off it.
        meta = await asyncio.to_thread(sess.end_meeting_meta, meeting_id, root)
    audit("meetings.stop", meeting_id, outcome="ok")
    return web.json_response({"ok": True, "status": k.STATUS_ENDED, "meta": meta})


def _collect_outputs(meeting_id: str, root: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Read every configured agent's output and the task list. BLOCKING.

    Runs on a worker thread, never the event loop: the note-taker is prompted to
    rewrite its WHOLE file after each transcription batch, so these reads are
    unbounded, and `redact()` over a megabyte of notes measures in the tens of
    milliseconds. The dashboard polls this every few seconds for the length of a
    meeting, so doing it inline would stall every other task on the loop —
    including the liveness heartbeat — on a repeating timer.

    Both halves are redacted. The outputs are model-generated prose; the tasks
    come from `tasks.json`, which an agent writes, so they go through the task
    module's own normalizer (which redacts every field and drops a malformed
    record) rather than being forwarded raw.
    """
    config = store.read_config(root)
    agents = config.get("meeting_agents") or []
    outputs = {
        agent_id: redact(content)
        for agent_id, content in store.read_agent_outputs(meeting_id, agents, root).items()
    }
    return outputs, task_routes.read_normalized(meeting_id, root)


async def handle_get_outputs(request: web.Request) -> web.Response:
    """Batch-read every configured agent's output plus the task list."""
    meeting_id = _meeting_id(request)
    root = data_root(request)
    outputs, tasks = await asyncio.to_thread(_collect_outputs, meeting_id, root)
    return web.json_response({"outputs": outputs, "tasks": tasks})


async def handle_get_note(request: web.Request) -> web.Response:
    """The user's own note for a meeting."""
    meeting_id = _meeting_id(request)
    root = data_root(request)
    note = await asyncio.to_thread(store.read_note, meeting_id, root)
    return web.json_response(note)


async def handle_put_note(request: web.Request) -> web.Response:
    """Replace the user's note for a meeting.

    Deliberately NOT redacted, unlike every other text this app accepts. The
    others are transcript or attachment metadata — untrusted input on its way to
    an agent's context — whereas this is the user's own writing on its way back to
    only themselves. Scrubbing what someone typed into their own memo would
    silently corrupt it, and the note is never fed to an agent (agents are told
    about their own output files and ``tasks.json``, not this one). It renders
    through the dashboard's shared markdown sanitizer, which is what makes the
    round-trip safe without altering the text.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    body = await json_body(request)

    # Validated by hand rather than with `field_str`, which would be the natural
    # choice here and is wrong for a note in two ways:
    #
    # 1. It treats a non-string as MISSING and returns the default — so a malformed
    #    request would come back 200 having silently erased the user's memo. A note
    #    is the one thing in this app the user cannot regenerate, so a bad body must
    #    be refused, not applied.
    # 2. It `strip()`s. Leading and trailing whitespace is part of what someone
    #    typed (a trailing blank line under a list, an indented block), and quietly
    #    rewriting it on every autosave would make the field feel broken.
    #
    # An EMPTY string is still accepted: deleting everything is a legitimate edit.
    content = body.get("content")
    if not isinstance(content, str):
        raise BadRequest("content must be a string")
    if len(content) > k.MAX_NOTE_CHARS:
        raise BadRequest(f"content must be at most {k.MAX_NOTE_CHARS} characters")

    note = await asyncio.to_thread(store.write_note, meeting_id, content, root)
    return web.json_response({"ok": True, **note})


def _note_image_alt(meeting_id: str, root: Any) -> str:
    """The meeting's elapsed time, as the alt text for a pasted image. BLOCKING.

    Empty when the meeting has not started or its metadata is unreadable — an
    honest ``![](images/…)`` beats inventing a timestamp, and the image is still
    useful without one.
    """
    meta = store.read_meeting_meta(meeting_id, root) or {}
    started = str(meta.get("started_at") or "")
    if not started:
        return ""
    try:
        begin = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return images.format_elapsed((datetime.now(timezone.utc) - begin).total_seconds())


def _note_image_count(meeting_id: str, root: Any) -> int:
    """How many images this note already has. BLOCKING."""
    directory = store.note_images_dir(meeting_id, root)
    try:
        return sum(1 for entry in directory.iterdir() if entry.is_file())
    except OSError:
        return 0


async def handle_post_note_image(request: web.Request) -> web.Response:
    """Store one image pasted into a meeting note.

    The app's first binary upload, so the posture is spelled out:

    * The client's **filename is never used**. The extension comes from sniffing
      the bytes (``domain/images.sniff_image_ext``) and the name is a fresh uuid4,
      so no client-supplied string reaches a path at all — a stronger position than
      sanitizing a name and then checking it, and the reason there is no traversal
      case to reason about.
    * An **unrecognised signature is refused**, which is what keeps SVG out: it has
      no binary signature, and it is a document that can carry script rather than
      an image.
    * The body is read in **bounded chunks** and abandoned the moment it exceeds the
      cap, so an oversized paste is never fully buffered.
    * Rejections are SEL-audited, mirroring ``api_upload_file``.

    Serving is deliberately NOT implemented here. The note references the image
    relatively and the dashboard's markdown renderer resolves that through
    ``/api/file-raw``, which already derives content type from magic bytes, refuses
    symlinks, and sets ``nosniff``. A second file-serving path is exactly what
    ``pptx_maker`` documents as the thing not to add.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)

    def _reject(reason: str) -> None:
        audit("meetings.note_image", f"{meeting_id} reason:{reason}", outcome="rejected")

    if not request.content_type.startswith("multipart/"):
        _reject("not_multipart")
        raise BadRequest("expected a multipart/form-data body")

    if await asyncio.to_thread(_note_image_count, meeting_id, root) >= k.MAX_NOTE_IMAGES:
        _reject("too_many_images")
        raise BadRequest(f"this note already has {k.MAX_NOTE_IMAGES} images")

    reader = await request.multipart()
    part = await reader.next()
    while part is not None and getattr(part, "name", None) != "file":
        part = await reader.next()
    if part is None or not isinstance(part, BodyPartReader):
        _reject("no_file_part")
        raise BadRequest("expected a form field named 'file'")

    data = bytearray()
    while True:
        chunk = await part.read_chunk(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > k.MAX_NOTE_IMAGE_BYTES:
            # Abandoned mid-stream rather than read to the end and then measured.
            _reject("too_large")
            # A literal 413, not `HTTPStatus.REQUEST_ENTITY_TOO_LARGE`: the
            # error-code contract scanner can only verify a `code` is present when
            # the status is a literal, and a computed status lands in the ratcheted
            # `dynamic_status` bucket instead. Spending ratchet budget to spell the
            # number differently would be a bad trade.
            return web.json_response(
                {
                    "error": "image too large",
                    "code": "image_too_large",
                    "max_bytes": k.MAX_NOTE_IMAGE_BYTES,
                },
                status=413,
            )

    ext = images.sniff_image_ext(bytes(data))
    if ext is None:
        _reject("unrecognised_format")
        raise BadRequest("not a PNG, JPEG, GIF or WebP image")

    filename = f"{uuid.uuid4().hex}{ext}"
    await asyncio.to_thread(store.write_note_image, meeting_id, filename, bytes(data), root)
    alt = await asyncio.to_thread(_note_image_alt, meeting_id, root)
    audit("meetings.note_image", f"{meeting_id} {filename}", outcome="ok")
    logger.info("meetings: stored a note image for %s (%d bytes)", meeting_id, len(data))

    # `src` is relative so the renderer resolves it against the note's own location;
    # `alt` is the meeting's elapsed time, which is what lets a reader line the image
    # up against the transcript later. Both are computed HERE rather than in the
    # client so there is one formatter and one clock.
    return web.json_response(
        {
            "ok": True,
            "filename": filename,
            "src": f"{k.NOTE_IMAGES_DIR}/{filename}",
            "alt": alt,
            "content_type": images.CONTENT_TYPES.get(ext, "application/octet-stream"),
        }
    )


def _read_translations_since(meeting_id: str, since: int, root: Any) -> dict[str, Any]:
    """Translated lines with ``n >= since``, plus the cursor to ask for next. BLOCKING."""
    doc = store.read_translations(meeting_id, root)
    lines = [
        line
        for line in doc.get("lines", [])
        if isinstance(line, dict) and int(line.get("n", -1)) >= since
    ]
    language = str(doc.get("language", "") or "")
    return {
        "language": language,
        # Resolved here rather than in the frontend: the accepted languages and
        # their endonyms are published by the backend (see GET /config), so a
        # second copy in the client would be the thing that drifts.
        "language_label": translate.language_label(language) if language else "",
        "lines": lines,
        "next_n": int(doc.get("next_n", 0)),
    }


async def handle_get_translations(request: web.Request) -> web.Response:
    """Live-translation lines for a meeting, newer than a client cursor.

    A cursor rather than the whole document: a long meeting accumulates hundreds
    of lines and the panel polls while it is open, so resending everything each
    time would grow linearly for no benefit. ``next_n`` is what the client sends
    back as ``since``.

    Separate from ``…/outputs`` on purpose. Outputs is polled for every meeting;
    this is polled only while the panel is open, and translation is off by default.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    since = query_int(request, "since", default=0, low=0, high=10_000_000)
    payload = await asyncio.to_thread(_read_translations_since, meeting_id, since, root)
    live = ACTIVE.get(meeting_id)
    queue = live.translations if live is not None else None
    payload["pending"] = queue.pending if queue is not None else 0
    payload["dropped"] = queue.dropped if queue is not None else 0
    return web.json_response(payload)


def _apply_attachments(
    meeting_id: str, body: dict[str, Any], root: Any
) -> list[dict[str, Any]] | None:
    """Add or remove attachments on a meeting's metadata. BLOCKING.

    Runs on a worker thread, never the event loop: a metadata read plus an atomic
    metadata write.

    Grouped so the read-modify-write is ONE hop — the new list is derived from the
    list that was just read, so splitting the read from the write would let two
    concurrent adds each drop the other's attachment.

    Body validation stays inside this helper, after the read, so a request naming a
    meeting that does not exist still answers 404 before a malformed body answers
    400 (the order the handler had inline). ``BadRequest`` raised here propagates
    out of the ``to_thread`` await into ``_common.guarded`` unchanged.
    """
    # The attachment list is derived from the list just read, so the read and the
    # write must be ONE critical section: two concurrent adds each appended to the
    # same snapshot and the second write dropped the first attachment, with both
    # requests reporting success. The `field_*` validation stays inside, after the
    # read, so a 404 still precedes a 400 as it did before.
    with store.meta_transaction():
        return _apply_attachments_locked(meeting_id, body, root)


def _apply_attachments_locked(
    meeting_id: str, body: dict[str, Any], root: Any
) -> list[dict[str, Any]] | None:
    """The read-modify-write itself. Caller holds ``store.meta_transaction()``."""
    meta = store.read_meeting_meta(meeting_id, root)
    if meta is None:
        return None

    action = field_str(body, "action", default="add", max_len=16)
    attachments: list[dict[str, Any]] = list(meta.get("attachments") or [])

    if action == "add":
        raw_items = body.get("attachments")
        if not isinstance(raw_items, list):
            raise BadRequest("attachments must be a list")
        for raw in raw_items[: k.MAX_ATTACHMENTS]:
            cleaned = _clean_attachment(raw)
            if cleaned is not None and len(attachments) < k.MAX_ATTACHMENTS:
                attachments.append(cleaned)
    elif action == "remove":
        index = body.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise BadRequest("index must be an integer")
        if 0 <= index < len(attachments):
            attachments.pop(index)
    else:
        raise BadRequest("action must be 'add' or 'remove'")

    meta["attachments"] = attachments
    store.write_meeting_meta(meeting_id, meta, root)
    return attachments


async def handle_attachments(request: web.Request) -> web.Response:
    """Add or remove meeting context attachments."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    attachments = await asyncio.to_thread(
        _apply_attachments, meeting_id, body, data_root(request)
    )
    if attachments is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)
    return web.json_response({"ok": True, "attachments": attachments})
