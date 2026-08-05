"""Shared constants for the Meetings builtin app.

Every hardcoded string/limit the app's business logic depends on lives here
(AGENTS.md "no hardcoded strings in business logic"). Nothing in this module
touches the network or the filesystem at import time, so it is safe to import
from a Windows gateway even though the app's live-transcription feature is
macOS/Linux only.
"""

from __future__ import annotations

APP_NAME = "meetings"

# HTTP surface. Handlers are registered directly on the gateway's aiohttp
# Application (see backend/routes/__init__.py:register_routes), so this is the
# same ``/api/apps/{name}`` convention issue-radar and code-review-sage use —
# NOT the ``/apps/{name}/api`` reverse-proxy prefix used by child-process apps.
API_BASE = f"/api/apps/{APP_NAME}"

# Safety caps.
MAX_SESSION_DURATION = 4 * 3600  # a single meeting may run at most 4 hours
MAX_CONCURRENT_MEETINGS = 1
MAX_TRANSCRIPT_CHARS = 4000  # per dispatched transcription line
MAX_BATCH_CHARS = 60_000  # per flushed agent batch
MAX_ATTACHMENTS = 25
MAX_DICTIONARY_TERMS = 500
MAX_CALENDAR_EVENTS = 500
MAX_MEETING_ID_LEN = 128
MAX_TITLE_LEN = 300
MAX_ICS_BYTES = 4 * 1024 * 1024  # refuse absurd .ics payloads
ICS_FETCH_TIMEOUT_SECS = 20
#: Redirects are followed MANUALLY so each hop is SSRF-validated, so the chain
#: needs its own bound (aiohttp's own `max_redirects` no longer applies).
ICS_MAX_REDIRECTS = 5
CALENDAR_SYNC_DAYS = 7

# Per-agent batching dispatcher.
BATCH_INTERVAL_SECS = 30.0
MAX_DISPATCH_FAILURES = 3
BACKOFF_STEP_SECS = 60.0
BACKOFF_CAP_SECS = 180.0

# Meeting lifecycle states. ``reviewing`` is the post-stop task-review gate.
STATUS_IDLE = "idle"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_REVIEWING = "reviewing"
STATUS_ENDED = "ended"
VALID_STATUSES = (STATUS_IDLE, STATUS_ACTIVE, STATUS_PAUSED, STATUS_REVIEWING, STATUS_ENDED)

#: Which status a meeting may move to, from each status. The SERVER's copy of the
#: rule the dashboard also applies (`ALLOWED_TRANSITIONS` in `useMeetingSession`).
#:
#: The client's copy is a UI affordance — it greys out buttons. It is not
#: enforcement: the endpoint accepted any member of `VALID_STATUSES`, so an
#: authenticated `POST status=idle` against an ACTIVE meeting persisted "idle"
#: while the live session stayed installed. Transcription then stopped feeding a
#: meeting the UI still showed as running, and starting another one answered 409
#: because `ACTIVE` was still held — a state reachable through the API that the UI
#: cannot produce or explain.
#:
#: A transition to the SAME status is allowed everywhere (an idempotent retry of a
#: request whose response was lost must not fail).
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_IDLE: (STATUS_ACTIVE,),
    # NOT `ended` from either of these: reaching `ended` goes through `reviewing`,
    # which is the action-item review gate the app is built around. Allowing a
    # direct active -> ended would let the API skip the review the UI requires,
    # which is the same class of "the client enforces it, the server does not" bug
    # this table exists to close. (`POST .../stop` is the separate, deliberate exit
    # that ends a meeting outright.)
    STATUS_ACTIVE: (STATUS_PAUSED, STATUS_REVIEWING),
    STATUS_PAUSED: (STATUS_ACTIVE, STATUS_REVIEWING),
    STATUS_REVIEWING: (STATUS_PAUSED, STATUS_ENDED),
    STATUS_ENDED: (STATUS_ACTIVE,),
}

# Agent output widget kinds → output-file extension. ``chat`` agents have no
# file (their output IS the chat transcript), hence None.
WIDGET_EXT_MAP: dict[str, str | None] = {"markdown": ".md", "html": ".html", "chat": None}
DEFAULT_WIDGET_TYPE = "markdown"

#: The one widget type whose output the user may edit (the editable minutes).
#:
#: ``html`` is a GENERATED artifact — the sketch artist's Mermaid document — not
#: prose, so a textarea over its source is not an affordance anyone wants, and
#: accepting hand-written HTML would open a document-authoring surface whose output
#: renders in an iframe for no gain (it is sandboxed and CSP-locked, so this is not a
#: new hole — just not a surface worth opening). ``chat`` agents have no output file
#: at all. Read by BOTH the edit-write gate and the read overlay, so the rule lives
#: in exactly one place and cannot come apart.
EDITABLE_WIDGET_TYPE = "markdown"

# On-disk layout under ``app_data_dir("meetings")``.
DATA_SUBDIRS = ("meetings", "notes", "widgets", "tasks", "configs")
CONFIG_FILE = "config.json"
DICTIONARY_FILE = "dictionary.toml"
CALENDAR_CACHE_FILE = "calendar-cache.json"
SESSION_META_FILE = "session.json"
TASKS_FILE = "tasks.json"
TRANSLATIONS_FILE = "translations.json"

#: The user's own note for a meeting, inside the meeting directory.
#:
#: **The leading underscore is load-bearing, not decoration.** Agent output files
#: share this directory and their names are derived as
#: ``safe_agent_id(id) + WIDGET_EXT_MAP[widget_type]``. ``_SAFE_AGENT_ID_RE`` is
#: ``^[a-z0-9][a-z0-9-]*$``, which cannot produce a leading underscore — so no
#: configured agent, whatever the user names it, can ever be handed this path as
#: its ``OUTPUT_FILE`` and overwrite what the user wrote. ``note.md`` and
#: ``notes.md`` are both reachable that way (``note`` and ``notes`` are legal agent
#: ids), which is why neither is used. Pinned by
#: ``test_note_filename_is_unreachable_by_any_agent``.
NOTE_FILE = "_note.md"

#: Subdirectory holding images pasted into a note. Referenced from the note as a
#: RELATIVE path (``![10:23](images/xxx.png)``), which is what lets the dashboard's
#: markdown renderer resolve it through the existing hardened file route.
NOTE_IMAGES_DIR = "images"

# The always-on system agent that maintains ``tasks.json``. Not a configurable
# entry in ``meeting_agents`` — it is a core feature of the app.
TASK_EXTRACTOR_ID = "task-extractor"

# Slot-name prefix for the per-agent background chat sessions this app drives.
SLOT_PREFIX = "meetings"

# System messages injected into agent sessions at lifecycle boundaries.
SYSTEM_MEETING_ENDED = "[system] Meeting ended. Finalize your output."
SYSTEM_MEETING_RESTARTED = (
    "[system] Meeting restarted. Disregard the previous 'Meeting ended' message. "
    "Continue listening for new transcription and appending to your output."
)
CHAT_PREFIX = "[chat]"

# Task provider ids (see backend/providers/tasks.py).
TASK_PROVIDER_LOCAL = "local"
DEFAULT_TASK_PROVIDER = TASK_PROVIDER_LOCAL

# Calendar provider ids (see backend/providers/calendar.py).
CALENDAR_PROVIDER_ICS = "ics"
CALENDAR_PROVIDER_NONE = "none"
DEFAULT_CALENDAR_PROVIDER = CALENDAR_PROVIDER_NONE

# STT provider ids. KiroCrew's own streaming endpoint is the only one.
STT_PROVIDER_KIROCREW = "kirocrew"
DEFAULT_STT_PROVIDER = STT_PROVIDER_KIROCREW

# Task review states.
REVIEW_PENDING = "pending"
REVIEW_ARCHIVED = "archived"
REVIEW_PUSHED = "pushed"
VALID_REVIEW_STATES = (REVIEW_PENDING, REVIEW_ARCHIVED, REVIEW_PUSHED)

TASK_PRIORITIES = ("high", "medium", "low")
DEFAULT_TASK_PRIORITY = "medium"
TASK_STATES = ("open", "done")

# ── live translation ────────────────────────────────────────────────────────

# Target languages for live transcript translation, as ``(code, label)``.
#
# The label is the language's own endonym and is deliberately NOT translated:
# a picker of target languages is the one place where every option should be
# readable to whoever wants that option. Same rationale as the dashboard's own
# UI-language picker.
#
# A curated list rather than every code a model might manage: each entry is a
# promise that the translation is worth reading, and it is the set MeetNote
# shipped. The empty string is not a member — see DEFAULT_TRANSLATION_LANG.
TRANSLATION_LANGS: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("zh", "中文 (简体)"),
    ("zh-TW", "中文 (繁體)"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("ru", "Русский"),
)

TRANSLATION_LANG_CODES: frozenset[str] = frozenset(code for code, _ in TRANSLATION_LANGS)

#: Off. Live translation costs one model call per spoken line, so it is opt-in —
#: a default-on feature would bill every meeting for something most do not need.
DEFAULT_TRANSLATION_LANG = ""

#: Lines waiting to be translated before the OLDEST are dropped.
#:
#: Translation runs one line at a time behind live speech, so a slow model builds
#: a backlog. Dropping is the right failure: the panel is a live aid, and a
#: translation that arrives ten minutes late is worth less than keeping up with
#: what is being said now. Transcription and the agents are never affected —
#: they do not wait on this queue.
MAX_TRANSLATION_BACKLOG = 40

#: Translated lines retained in ``translations.json``.
#:
#: Trimmed from the front when exceeded. Line numbers stay monotonic, so a client
#: polling with ``since`` is unaffected by trimming — it only loses scroll-back.
MAX_TRANSLATION_LINES = 2000

# ── notes ───────────────────────────────────────────────────────────────────

#: Ceiling on a meeting note. Generous — this is a human typing for at most the
#: four hours ``MAX_SESSION_DURATION`` allows — but bounded, because the note is
#: written by a request body and read back into a poll response.
MAX_NOTE_CHARS = 100_000

#: Ceiling on one pasted note image. A full-screen PNG screenshot on a retina
#: display is comfortably under this; anything larger is not a screenshot.
MAX_NOTE_IMAGE_BYTES = 8 * 1024 * 1024

#: Images one meeting's note may accumulate. Bounds the directory a single meeting
#: can create, since each paste writes a new file and nothing deletes them.
MAX_NOTE_IMAGES = 200

# ── editable minutes ────────────────────────────────────────────────────────

#: Subdirectory holding the user's edits of agent outputs, inside a meeting folder.
#:
#: A DIRECTORY rather than a name-mangled sibling, and that choice is the security
#: property: an agent's output path is always
#: ``meeting_dir / (safe_agent_id(id) + WIDGET_EXT_MAP[widget_type])`` — a FLAT
#: filename — and ``_SAFE_AGENT_ID_RE`` (``^[a-z0-9][a-z0-9-]*$``) cannot contain a
#: path separator. So no configured agent, whatever the user names it, can be handed
#: a path inside here and overwrite the very edit that exists to survive its next
#: write. Same argument as :data:`NOTE_IMAGES_DIR`, and it is why the sidecar needs
#: no leading-underscore trick like :data:`NOTE_FILE`. The name matches the
#: ``edits/`` layout MeetNote shipped, so the concept reads the same in both.
AGENT_EDITS_DIR = "edits"

#: Ceiling on one edited agent output.
#:
#: Twice :data:`MAX_NOTE_CHARS`, because this is a whole GENERATED document the user
#: is correcting rather than a memo they typed: the note-taker is prompted to rewrite
#: its entire file after every transcription batch, so a long meeting's minutes are
#: already larger than anything a person writes by hand.
MAX_MINUTES_CHARS = 200_000

#: Body cap for the minutes PUT specifically, replacing ``_common.MAX_BODY_BYTES``.
#:
#: **The relationship to :data:`MAX_MINUTES_CHARS` is load-bearing, so it is spelled
#: out.** ``json_body``'s default cap is 256 KiB, which is FEWER bytes than
#: ``MAX_MINUTES_CHARS`` characters as soon as the text is not ASCII — Japanese
#: minutes run three bytes per character, so a 200 000-character document is ~600 KB
#: and the default cap would answer 413 for a document the char limit accepts. That
#: is the worst shape of failure available here: the user can OPEN the document, edit
#: it, and only then be told it cannot be saved.
#:
#: 1 MiB covers 200 000 characters at three bytes each with headroom for JSON
#: escaping, and is far under the gateway's own 60 MiB ``client_max_size``, so this
#: is the only cap that governs. Raised for this ONE route rather than for all of
#: them: every other body here is a short field.
MAX_MINUTES_BODY_BYTES = 1024 * 1024

# ── importing an existing recording ─────────────────────────────────────────

#: Extensions the import route accepts. An allowlist, so an unrecognised suffix is
#: refused rather than handed to the transcriber.
#:
#: Extension-only, and it is worth being clear that this is NOT a content check: it
#: is a cheap "did the user mean to pick this file" filter in front of a decoder that
#: does its own format detection. The barrier that matters for an import is
#: :func:`kiro_crew.hooks.validate_file_path` (which enforces ``is_sensitive_path``),
#: and ``transcribe_audio`` re-checks the path itself — sniffing container magic here
#: would add a third opinion about audio formats without adding a guarantee.
IMPORT_AUDIO_EXTENSIONS = frozenset(
    {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".opus", ".aac", ".wma"}
)

#: Cap on the path an import request may carry. Generous — this is a host path the
#: user chose, and PATH_MAX is 4096 on Linux.
MAX_AUDIO_PATH_CHARS = 4096

#: Lines one import may feed into a meeting.
#:
#: An hour of speech is roughly 500–900 sentences, so this covers a long recording
#: with room to spare while keeping a pathological file (a transcript of silence
#: split into thousands of fragments) from filling every agent queue.
MAX_IMPORT_LINES = 2000
