# Meetings (builtin app)

Last Updated: 2026-08-04 (recording, system audio, faster-whisper, live translation, editable minutes, the user's note, audio import — see `src/kiro_crew/apps/builtins/meetings/ATTRIBUTION.md`)

An AI meeting assistant. Transcribes a live meeting through KiroCrew's own
streaming speech-to-text, fans each line out to a small crew of background agents
(structured notes, an HTML/Mermaid diagram, an action-item list), and gates the
meeting's close behind a review of the extracted action items.

Around that core it also **records** the meeting to a WAV file, captures the far
side of a call as well as the microphone, translates the transcript line by line
into a side panel, lets the user **edit** the minutes without taking the file away
from the agent that writes it, keeps a **note** the user owns (with pasted
screenshots), and can **import** a recording made elsewhere as if it had been
spoken.

`defaultEnabled: false` — it appears in the App Store and is opt-in.

**One capture, many consumers.** The single most important structural fact about
the audio path: a meeting prompts for the microphone once and for a display
surface once, however many things want the audio.
`hooks/useMeetingTranscription.ts` owns the one pipeline and TEES every PCM chunk
to the recording through an `onPcm` option, so transcription and recording are two
readers of one stream rather than two captures. `useMeetingSession` instantiates
the recording hook BEFORE transcription, because the tee points that way.

## Layout

| Path | What it is |
|---|---|
| `src/kiro_crew/apps/builtins/meetings/app.json` | manifest (`backend.routes`, `ui.pages`, agents, permissions) |
| `.../backend/constants.py` | every limit, state name, and provider id |
| `.../backend/store.py` | on-disk layout **and the single path-containment barrier** |
| `.../backend/domain/dictionary.py` | speech-correction dictionary (TOML) |
| `.../backend/domain/session.py` | batching dispatcher + meeting state machine |
| `.../backend/domain/translate.py` | live per-line translation queue + its prompt |
| `.../backend/domain/images.py` | image-signature sniffing for note pastes |
| `.../backend/domain/audio.py` | splitting an imported transcript into lines |
| `.../backend/recording_store.py` | the `MeetingStore` adapter core recording resolves paths through |
| `.../backend/providers/tasks.py` | **task-provider seam** + the local ledger |
| `.../backend/providers/calendar.py` | **calendar-provider seam** + the `.ics` reader |
| `.../backend/routes/` | `_common` (gate + validation + `live_session`), `meeting_lifecycle`, `agents`, `audio_import`, `tasks`, `calendar`, `settings` |
| `.../agents/*.json` | the three shipped agent specs |
| `src/kiro_crew/recording/` | **core**, not this app: WAV writer, session, `/api/ws/recording`, recovery |
| `src/kiro_crew/builtin_skills/meetings/SKILL.md` | the bundled skill (data layout, lifecycle, provider config) |
| `website/src/apps/meetings/` | `MeetingsPage` (list) → `MeetingView` → `TaskReviewView`, `SettingsView` |
| `.../meetings/audio/systemAudio.ts` | requests the display surface and drops its video track |
| `.../meetings/audio/captureTier.ts` | what this client can capture, resolved in the renderer |
| `.../meetings/hooks/useMeetingRecording.ts` | owns the `/api/ws/recording` socket |
| `.../meetings/components/` | `RecordingMeter`, `TranslationSidebar`, `NoteSidebar`, `AgentPanel`, … |
| `website/electron/display-media.js` | `describeAudioTier` — the one place the Electron capture tier is decided |
| `website/public/app-assets/meetings/` | icon + hero art |

## Routes

Registered on the gateway's OWN aiohttp Application by
`backend/routes/__init__.py:register_routes` (the manifest names the same entry
point for the generic App Kit loader). Base path `/api/apps/meetings` — the
same-origin convention issue-radar and code-review-sage use, **not** the
`/apps/{name}/api` reverse proxy, because this app has no child process.

```
GET    /config                      config + the three provider catalogs
PUT    /config                      replace config (narrow allow-list)
GET    /dictionary                  speech-correction terms
POST   /dictionary                  add a term          {correct, aliases[]}
POST   /dictionary/remove           remove a term       {correct}
POST   /dictionary/reload           re-read from disk

GET    /calendar                    cached events + provider + configured flag
POST   /calendar/sync[?days=N]      fetch from the provider, replace the cache
GET    /calendar/providers          registered calendar providers

GET    /agents                      configured meeting agents
GET    /status                       live dispatcher status (or an all-idle shape)
GET    /task-providers              registered task providers + the active one

GET    /meetings                    every meeting with metadata on disk
GET    /meetings/{id}               one meeting's metadata + live status
POST   /meetings/{id}/init          create folder/metadata/tasks/outputs (idempotent)
POST   /meetings/{id}/start         activate: seed outputs, spawn agent sessions
POST   /meetings/{id}/status        {status} — active | paused | reviewing | ended
POST   /meetings/{id}/stop          flush agents, send the finalize notice, mark ended
GET    /meetings/{id}/outputs       every agent's EFFECTIVE output + edit metadata + tasks
PUT    /meetings/{id}/outputs       {agent_id, content} — save the user's edit (sidecar)
DELETE /meetings/{id}/outputs       {agent_id} — revert to what the agent wrote
GET    /meetings/{id}/translations[?since=N]   translated lines, cursor-paged
GET    /meetings/{id}/note          the user's own note + its absolute path
PUT    /meetings/{id}/note          {content} — replace it
POST   /meetings/{id}/note/images   multipart — one pasted screenshot
POST   /meetings/{id}/attachments   {action: add|remove, attachments[]|index}
POST   /meetings/{id}/agents        {agent_id, enable} — toggle mid-meeting
POST   /meetings/{id}/mute          {agent_id, muted}
POST   /meetings/{id}/dispatch      {text, chat?} — one transcription/typed line
POST   /meetings/{id}/import        {audio_path} — transcribe a file into the meeting
POST   /meetings/{id}/message       {agent_id, text} — one agent, flushed at once
POST   /meetings/{id}/reset         reset tripped circuit breakers
GET    /meetings/{id}/tasks         extracted action items
POST   /meetings/{id}/tasks         add one by hand   {description, …}
PATCH  /meetings/{id}/tasks         edit one          {id, fields}
DELETE /meetings/{id}/tasks         remove one        {id}
POST   /meetings/{id}/tasks/file    file through the task provider  {id}
POST   /meetings/{id}/tasks/review  {id, review_status} — pending | archived
```

Audio itself does **not** travel over these routes. Recording uses core's own
`/api/ws/recording` socket (`src/kiro_crew/recording/ws.py`) — see *Recording*
below for why the app still owns the path it writes to.

Every handler is wrapped by `_common.route`, which applies the enable gate and
turns validation failures into 4xx. `_common.error_response` maps an exception's
status to a LITERAL `web.json_response(..., status=NNN)` per branch — repetitive on
purpose, because the error-code contract scanner reads `status=exc.status` as
`dynamic_status` and cannot prove the contract is met. **A status with no branch
falls through to 400**, which was a live bug before the import route needed 403:
`store.contain` raises `MeetingsPathError(status=403)` for a path escaping the data
root, and that was reported as "bad request".

The two transcript PRODUCERS — `…/dispatch` and `…/import` — share
`_common.live_session`, which resolves the live session or raises the right 409/410.
It is shared rather than copied because the expired branch has SIDE EFFECTS (drain
the queues, then mark the meeting ended on disk).

## Data

All under `app_data_dir("meetings")` (`~/.kiro/crew/apps/meetings/data/`):

```
config.json                      app config (agents, providers, presets)
dictionary.toml                  speech-correction terms
calendar-cache.json              last calendar sync
task-ledger.json                 tasks filed through the local task provider
meetings/<safe_id>/session.json  per-meeting metadata
meetings/<safe_id>/tasks.json    extracted action items
meetings/<safe_id>/<agent>.md    a markdown agent's output
meetings/<safe_id>/<agent>.html  an HTML agent's output
meetings/<safe_id>/translations.json  live translation, reset on language change
meetings/<safe_id>/audio.wav     the recording (written by core, path resolved here)
meetings/<safe_id>/_note.md      the user's own note
meetings/<safe_id>/images/*      screenshots pasted into the note
meetings/<safe_id>/edits/<agent>.md   the user's edit of that agent's output
```

**Two names in that layout are security properties rather than style, and both are
about who can be handed which path.** An agent's output path is always
`meeting_dir / (safe_agent_id(id) + WIDGET_EXT_MAP[type])` — a FLAT filename — and
`_SAFE_AGENT_ID_RE` (`^[a-z0-9][a-z0-9-]*$`) can produce neither a leading
underscore nor a path separator. So:

* `_note.md` is unreachable by any agent because of the underscore. `note.md` and
  `notes.md` are both legal agent ids and WOULD be reachable.
* `edits/` and `images/` are unreachable because they are DIRECTORIES. That is why
  the edit sidecar needs no underscore trick of its own.

Pinned by `test_note_filename_is_unreachable_by_any_agent` and
`test_the_edits_directory_is_unreachable_by_any_agent`, both asserted through the
validator so loosening the regex fails there rather than silently handing an agent
the user's writing.

`ensure_data_dirs()` creates the subtree and seeds `dictionary.toml` +
`config.json` at app startup (an `on_startup` hook, run on the executor). It
never overwrites, so user edits survive every restart.

## Lifecycle

```
idle ──start──> active ⇄ paused ──> reviewing ──> ended
                  │                    ▲             │
                  └────────────────────┘         restart
```

`reviewing` is a **gate, not a state to pass through**: `ended` is reachable only
from it, so no extracted action item is silently dropped. The UI's transition
table (`useMeetingSession.ALLOWED_TRANSITIONS`) has a test asserting no other
state can reach `ended`.

`MAX_CONCURRENT_MEETINGS == 1`: a second `start` for a different meeting answers
409 while the first is live and unexpired. A session past
`MAX_SESSION_DURATION` (4h) answers 410 on dispatch.

## Agent dispatch

`domain/session.py`. One `AgentQueue` per enabled agent plus the always-on task
extractor. A queue batches lines and flushes every `BATCH_INTERVAL_SECS` (30s),
so an agent gets a paragraph of context rather than one interruption per
utterance. Three consecutive dispatch failures trip a circuit breaker (backoff
60s → 120s → stop); `POST …/reset` resumes.

A flush takes **whole lines up to `MAX_BATCH_CHARS` (60k)** and deletes exactly
the lines it dispatched, so a queue that grew past the cap — a long pause, or a
backed-off agent resuming — carries its tail into the next flush. Truncating the
joined batch while clearing the whole queue silently DESTROYED transcript, whose
only symptom was notes that skip the end of what was said. A single line over the
cap is still truncated and consumed, because requeueing it would wedge the queue.
Pinned by `test_meetings_session.py::TestAgentQueue`.

Ending or pausing a meeting drains rather than interrupts. `flush_now` treats a
pending flush task by state: still SLEEPING on its interval, it is cancelled (that
is the point of flushing now); already inside `flush()` awaiting the agent, it is
AWAITED. Cancelling an in-flight dispatch killed the live turn, and because `busy`
was still set the follow-up flush then no-opped — so stopping a meeting mid-dispatch
lost that batch and the finalization notice, at the one moment a meeting's notes
matter most. `busy` is the discriminator. Pinned by
`::test_flush_now_waits_for_an_in_flight_dispatch` and
`::test_flush_now_still_cancels_a_sleeping_timer`.

**A drain is a loop, not one flush.** `flush()` deliberately sends exactly ONE
batch, so an over-cap queue needs several — and `flush()` cannot reschedule itself,
because it runs as the body of `_flush_task` and `_schedule_flush` takes its
"already running" early return from in there. Attempting the reschedule inline
scheduled nothing at all, which re-opened the very tail loss `_take_batch` closed.
The loop therefore lives in the two places that own the lifecycle: `_delayed_flush`
chains sleep→flush while `flush()` reports work remaining, and `flush_now` drains
before returning because teardown discards anything still queued. Both are bounded
by `_MAX_DRAIN_BATCHES`, and a flush that consumes nothing (a failing dispatch)
exits the loop instead of spinning — the circuit breaker still trips normally.
Pinned by `::test_flush_now_drains_every_queued_batch`,
`::test_the_batching_timer_chains_until_the_queue_is_empty`, and
`::test_a_failing_dispatch_does_not_spin_the_drain`.

**Teardown drains; only `set()` may cancel.** `ACTIVE.clear()` calls `cancel_all()`,
which drops the pending flush timers — so a session torn down with a half-batch
queued lost that transcript, and the final notes silently omitted whatever had not
been dispatched. Every teardown path now calls `await ACTIVE.drain_and_clear()`,
which flushes first: the expiry path (a long meeting whose next line arrives after
the session lapsed), gateway shutdown, `status=ENDED`, and `handle_stop_meeting` —
where it is load-bearing, because the finalize notice is itself enqueued and the old
cancel would have discarded the very notice just broadcast. A flush failure still
tears the session down, so a wedged agent cannot block shutdown. **Replacing a session is a teardown too.** `set()` cancels the outgoing session's
queues, so starting a second meeting while an earlier (typically expired) one still
held a half-batch discarded that transcript — the same loss by a different route.
`handle_start_meeting` therefore drains before it replaces. `set()` itself now LOGS
the undispatched count rather than dropping it silently, because a leftover queue at
replace time always means transcript is about to be lost. `clear()` survives only as
the second half of `drain_and_clear`.

Guarded by AST checks over the route modules: no `ACTIVE.clear()` outside the
draining helper, and no handler calling `ACTIVE.set()` without a drain — so a new
teardown OR replace path cannot quietly reintroduce the loss.
`test_meetings_routes.py::TestTeardownDrainsBeforeClearing`.

**Dispatch is in-process.** Upstream POSTed each batch back to its own gateway
over authenticated loopback HTTP. Here the routes live ON the gateway, so a batch
goes straight to the shared `SessionManager` via
`llm_helpers.stream_and_collect` under `ToolApprovalPolicy.HOOK_BASED` — the
agents' file writes still traverse the PreToolUse gate (deny patterns,
sensitive paths, governance) exactly like any other turn.

## Recording

The writer, the session, and the `/api/ws/recording` socket are **core**
(`src/kiro_crew/recording/`), not this app — recording a meeting is not
meetings-specific. But the file has to land in the right per-meeting directory, and
core must not import an app.

That is resolved through the `MeetingStore` seam `recording/recovery.py` already
had: `start` takes an optional `meeting_id`, and core asks the registered store to
`resolve_meeting_dir`. This app registers `backend/recording_store.py` from
`register_routes`. So `safe_meeting_id` + `contain` stay in the app, and an id core
cannot place is REFUSED rather than started. A registration failure costs recording
persistence, not gateway startup.

**Crash recovery is not wired up yet, and there is one blocker worth knowing**:
`recovery.py` looks for meetings whose status is `"recording"`, and this app has no
such status (`idle`/`active`/`paused`/`reviewing`/`ended`, gated by
`ALLOWED_TRANSITIONS`), so detection finds nothing. Reconciling the two
vocabularies is a change to the meeting lifecycle rather than to the adapter.

### Capturing the far side of a call

`audio/systemAudio.ts` requests the display surface, and the shape of that request
is load-bearing: **`getDisplayMedia({audio: true})` does not work and must not be
put back.** The spec requires a video track — omitting `video`, or passing
`video: false`, rejects with a `TypeError` in every browser. The code asks for a
1 fps video track and then stops and removes it. `systemAudioConstraints`' test is
the regression guard.

In Electron the tier is decided in exactly one place,
`website/electron/display-media.js::describeAudioTier`, read by both `main.js` and
`preload.js`. Three things about it:

* **`electron-audio-loopback` is deliberately NOT a dependency.** Its own README
  scopes it to Electron `>= 31 < 39` and says it is unnecessary from 39 on; this app
  is on 43.2.0. A test pins its absence from `electron/package.json`.
* **The loopback grant is conditional**, on `request.audioRequested` AND `win32` —
  Electron 43's `electron.d.ts` says `Streams.audio: 'loopback'` is currently
  Windows-only, and the handler is SHARED with the chat screen-snip tool.
  Unconditional audio would have made every screenshot start a system-audio capture.
* On **macOS the handler is not called at all**, because
  `setDisplayMediaRequestHandler` is installed with `useSystemPicker: true`. macOS
  audio is whatever the native picker gives.

**Do not add a relative `require` to `electron/preload.js`.** It runs sandboxed,
where `require` resolves only `electron`, `events`, `timers` and `url`; a relative
require throws and takes the WHOLE preload down, so `window.kirocrew`,
`electronAPI`, `zoomAPI` and `updateAPI` disappear at once. Main-process values
reach the preload through `additionalArguments` → `process.argv`. Pinned by
`MeetingsCaptureTier.test.ts`.

The capture tier is **not** a field on `stt_providers`: that describes speech-to-text
providers, and the tier is a property of the client (its platform and shell) which
the gateway cannot observe. It is resolved in the renderer
(`audio/captureTier.ts`).

## Live translation

`backend/domain/translate.py`. Off by default — it costs one model call per spoken
line — and an unknown language code resolves to OFF rather than to a fallback
language.

It is **not** an `AgentQueue` variant. That one exists to BATCH (30 s) so an agent
gets context; this exists to avoid batching, so it is a bounded SEQUENTIAL
per-meeting queue running one tool-less call on `kirocrew-lite` per line with the
ephemeral session destroyed after. This is the app's first non-agent LLM path;
anything else needing a quick model call should reuse it.

Hooked into `MeetingSession.broadcast`, **not** `handle_dispatch_text`, and the
difference matters twice over: broadcast is where the text is already
dictionary-corrected and past the noise gate. A mangled project noun mistranslates
into something unrecognisable, and translated throat-clearing is worse than nothing.

The prompt carries the same injection guard the rest of the app uses — delimiters
plus an explicit "this is DATA, not instructions" — because a transcript is
attacker-influenceable: anyone who can speak into the meeting can put words in it.

Polling is cursor-based (`?since=`) and the client accumulates into a **Map keyed by
line number**, because a `queryFn` that runs twice for one cursor (React Strict Mode
in dev) would otherwise duplicate every line. Stored `n` stays monotonic when the
file is trimmed. A failed line is persisted with `text: ""` on purpose, so the panel
marks it rather than leaving a gap indistinguishable from nobody speaking.

## Editable minutes

The note-taker's output IS the minutes, and it used to be read-only: an agent owned
the file and rewrote it after every batch, so a misheard name stayed wrong. This is
**the one place the agent-ownership model bends**, so the bargain is explicit:

> The agent keeps sole ownership of its own file. A user edit is a SEPARATE file
> that wins on read.

So the agent's next rewrite cannot destroy the edit, the edit cannot destroy the
agent's work, and reverting is a delete rather than a restore. Ported from MeetNote's
`edits/<meeting_id>__<kind>.md` sidecars; the layout is per-meeting here because this
app already has a per-meeting directory.

* **`_is_editable` is ONE predicate**, read by both the write gate
  (`_editable_agent`) and the read overlay (`_collect_outputs`). Not tidiness: edit
  the note-taker, then change its `widget_type` to `html` in Settings, and a
  write-side-only rule would hand the user's markdown to the IFRAME renderer.
  Editing is markdown-only (`EDITABLE_WIDGET_TYPE`) — `html` is a generated Mermaid
  artifact rather than prose, and `chat` has no output file.
* **`stale` is derived from two mtimes, never stored.** It means "the agent has
  rewritten its own file since you edited", and it is the honest half of the bargain:
  the edit keeps winning, so a panel that stopped updating must SAY why or it looks
  like the agent died. No second piece of state to drift, and the sidecar stays a
  plain `.md` a person can open.
* **`GET …/outputs` gained an `edits` map that carries no content.** The edited text
  is already in `outputs` (an edit wins server-side, so the client needs no merge),
  and resending it would double the poll for this app's largest field. `edits` is
  present only where an edit exists, so a key check answers "is this my text or the
  agent's".
* **The generated half is redacted; the edit is not.** That is not inconsistent: an
  edit is redacted BY CONSTRUCTION, because the only way to make one is to edit what
  this same endpoint already redacted on its way to the browser. A second pass could
  only mangle a correction made inside a substituted span.
* **The PUT raises its own body cap.** `json_body`'s shared 256 KiB is FEWER bytes
  than `MAX_MINUTES_CHARS` allows as soon as the text is not ASCII — Japanese minutes
  run three bytes per character — so the default would let a user open a document,
  edit it, and only then get a 413. `MAX_MINUTES_BODY_BYTES >= MAX_MINUTES_CHARS * 3`
  is pinned by a test.

Client-side the mutations **invalidate** the outputs poll rather than seeding the
cache — the opposite of the note, deliberately: after a write the interesting
question is what the OTHER writer has been doing, which the response cannot answer.
Safe to refetch because the editor's draft is local state seeded when edit mode
opens, so a 5-second poll cannot type over the user.

**Open design question**: while an edit exists the user stops seeing what the agent
writes. That is MeetNote's semantic, but MeetNote generated minutes once, after the
meeting. `stale` plus one-click revert is the answer implemented here; gating edits
to the `reviewing`/`ended` statuses is the alternative.

## The user's note

`_note.md` per meeting, with pasted screenshots under `images/`. Three decisions
worth carrying forward:

* **The note is not redacted**, unlike everything else this app accepts, and the PUT
  body is validated by hand rather than with `field_str`. That helper treats a
  non-string as *missing* — so a malformed request would answer 200 having ERASED
  the memo — and it `strip()`s, destroying trailing blank lines the user typed. A
  note is the one thing here the user cannot regenerate.
* **The note is never polled**, and the save response seeds the cache instead of
  invalidating: the textarea is the authoritative copy, and refetching under the user
  is how an autosaving editor loses a sentence.
* **Image paste never reads the client filename.** The extension is sniffed from the
  bytes (`domain/images.sniff_image_ext`) and the name is a fresh uuid4, so no client
  string reaches a path. An unrecognised signature is REFUSED, which is what keeps
  SVG out (no binary signature, and a document that can carry `<script>`).
  `store.safe_note_image_name` additionally requires exactly the generated shape,
  because `contain()` alone is not enough: it bounds a path to the DATA ROOT, so
  `../m2/note-taker.md` — another meeting's agent output — would pass.

No second file-serving route was added. `MarkdownRenderer`'s `ImgWithFallback`
rewrites a relative `<img src>` to `/api/file-raw?path=…` when a `BasePathCtx` is
present, and that route is already hardened (content type from MAGIC BYTES only,
`O_NOFOLLOW`, `nosniff`, SVG CSP). The note's `GET` returns its own absolute `path`
for exactly this.

## Importing a recording

`POST …/{id}/import` takes a host path, transcribes it with the gateway's batch
speech-to-text, and feeds the result into the meeting **as if it had been spoken** —
through `MeetingSession.broadcast`, the same entry point a microphone uses.

That choice is the design. This app keeps no transcript file, so an import that
wrote one would produce a file nothing reads; routing through `broadcast` instead
means domain-dictionary correction, the noise gate, per-agent batching, the muted
list, and live translation all apply with nothing re-implemented and nothing that can
drift. The consequence, the honest way round: **an import needs a LIVE meeting**, and
the session is resolved FIRST so an hour of audio is not decoded on the way to an
error that could be given immediately.

`domain/audio.split_transcript` turns the one returned blob into lines in three
tiers: the transcriber's own segments when it gave any (a whisper segment is the
closest thing to one utterance), sentence boundaries when it returned a single
paragraph (AWS Transcribe does), and a hard wrap at `MAX_TRANSCRIPT_CHARS` — wrapped
rather than truncated, because truncating drops the tail of a long sentence.

The path goes through `hooks.validate_file_path`, the shared dashboard file gate,
which canonicalizes (following symlinks) and enforces `is_sensitive_path`. The
predicate is never called directly, so this route's answer is identical to every
other file read in the product, and the extension check runs on the CANONICAL path —
a symlink named `.mp3` cannot smuggle in its target. `transcribe_audio` re-checks the
path itself, so a refusal is enforced twice by two owners. The extension allowlist is
a "did you mean this file" filter, not a content check.

`lines` and `dispatched` are reported separately: the gap is what the noise gate
dropped, and a recording that yields 400 lines of which 0 were dispatched (an empty
room, filler) is a real outcome the user must be able to see.

There is **no UI for this yet** — it is an API surface. A host-path picker in the
meeting view is a separate UI decision.

## The two provider seams

Both follow `kiro_crew.embeddings`' `EmbeddingBackend` /
`register_embedding_backend` shape: an ABC, a name-keyed factory registry, and a
resolver that **degrades instead of raising** on an unknown id. Each ships
exactly one real implementation; the seam exists so an out-of-repo edition can
register an organization's own provider without patching the app. Nothing in the
app branches on a provider name, and the settings UI is populated from the
registries, so a registered provider appears with no frontend change.

### Task provider (`backend/providers/tasks.py`)

`TaskProvider` (`provider_id`, `display_name`, `create(TaskDraft) -> TaskRef`).
Shipped: `local` — an app-scoped JSON ledger (`task-ledger.json`). `create` is
called on the subprocess executor, because an edition provider may talk to a
tracker over the network.

That executor makes `create` genuinely concurrent, so its read-append-write is
held under a **module-level** lock (`_LEDGER_LOCK`). The write is atomic; the
read-modify-write around it was not, so two parallel filings each read the same
list and the second write landed a snapshot missing the first — with both
requests reporting success. The lock is module level rather than per instance
because `get_task_provider` builds a fresh provider per request. Pinned by
`test_meetings_providers.py::TestLocalTaskProvider::test_concurrent_filings_do_not_overwrite_each_other`.

`TaskDraft.sanitized()` runs before anything leaves the process: an action item
is LLM output and a filed task is an external surface, so credential +
exfiltration-URL redaction and length caps are applied there.

Why not `task_models.Project`: the task runner's dataclasses model an autonomous
execution plan (ordered, dependency-linked, attempt counts, a state machine the
runner drives). A meeting action item is a durable human-owned to-do nobody
executes automatically; reusing `Project` would mean inventing a fake spec per
meeting and leaving the executor fields permanently unused.

### Calendar provider (`backend/providers/calendar.py`)

`CalendarProvider` (`provider_id`, `display_name`, `requires_source`,
`async fetch(days) -> [CalendarEvent]`). Shipped: `none` (the default — the app
is fully usable with ad-hoc meetings) and `ics`, a stdlib iCalendar reader fed by
a local `.ics` path or a published `https://` URL.

`parse_ics` reads only the `VEVENT` fields the app displays. Recurrence
(`RRULE`) is deliberately **not** expanded: a correct expansion needs a full RFC
5545 engine, and silently showing wrong occurrence times is worse than showing
only the series' first instance.

Fetch safety:

* an `https://` source is fetched with **aiohttp** (never `requests`/`urllib`,
  which would block the gateway's single event loop); the response is size-capped
  (4 MiB) while streaming, and a redirect off https is refused;
* only `https://` is accepted (`webcal://` is rewritten to it) — every other
  scheme, including `file://` and `http://`, is refused, so a config value cannot
  turn the sync into a local-file read or a plaintext hop;
* the resolved address is refused when it is loopback/private/link-local/
  reserved/multicast/unspecified — the gateway performs this fetch, so an
  internal-only address would make the endpoint a request-forgery hop. An IPv4
  address embedded in IPv6 (`::ffff:10.0.0.1` v4-mapped, `2002:…` 6to4) is judged
  by the address it embeds, since that is where the packet lands. Resolution is a
  blocking syscall, so the whole validation step runs on the executor;
* **the vetted address is the connected address.** `_normalize_url` returns a
  `VettedTarget` (url + host + port + approved addresses), and the fetch hands
  those addresses to a `_PinnedResolver` installed on the `TCPConnector`
  (`use_dns_cache=False`). Returning only the URL is what made the old gate a
  TOCTOU: aiohttp resolved the same name a second time for the connect, so a host
  whose DNS answer changed in between (short TTL, or a resolver alternating a
  public and a private record) passed validation and was then fetched at the
  private address — the `169.254.169.254` metadata shape. `calendar.source` is
  reachable from a dashboard `PUT /api/apps/meetings/config`, so this is
  request-supplied, not operator-only. Substituting the *resolution* step rather
  than rewriting the URL to an IP is deliberate: the request URL keeps its
  hostname, so the `Host` header, TLS SNI, and certificate verification stay
  correct. Verification is never disabled and `ssl=False` is never passed — a
  test asserts the connector keeps aiohttp's verified context
  (`CERT_REQUIRED`, `check_hostname`). An unpinned host is refused by the
  resolver rather than resolved, so the mechanism is fail-closed. Each redirect
  hop is re-validated **and** pinned before its own request, so no hop is
  vetted-then-re-resolved;
* a multi-record answer is **all-or-nothing**: every address must pass, and the
  whole set is pinned. A host answering with a mix of public and private
  addresses is refused outright rather than filtered down to the public ones —
  that mix is the rebinding signature, and keeping the public record would let an
  attacker retry until the connector picked the private one. Same rule for IPv4
  and IPv6;
* a local path is read on the executor, size-capped, and refused when
  `is_sensitive_path` matches.

## Speech-to-text

KiroCrew's own `/api/ws/stt` (`dashboard/stt_stream.py`).
`hooks/useMeetingTranscription.ts` conforms to that endpoint's existing wire
protocol — connect, wait for `{"type":"ready"}`, send 16 kHz Int16 PCM from
`/pcm-worklet.js`, receive `partial`/`final`/`error`, send `{"type":"stop"}` and
let the server close so trailing finals arrive. Every FINAL segment is POSTed to
`…/dispatch`, which is what feeds the agents; partials only drive the caption.

Cloud transcription is an optional extra (`pip install kirocrew[voice]`). When it
is absent the endpoint answers a friendly WS error, the hook surfaces it as a
toast, and the user can still type into the broadcast bar to feed the agents.

**faster-whisper** is available as a provider alongside whisper, mlx and AWS
Transcribe, with the full model enum and a hallucination filter (a transcript that
is entirely boilerplate comes back as `None`, not as text for an agent to write into
the notes). Two things about it:

* It is deliberately **not** a declared extra in `setup.cfg` — it installs on demand
  from Settings, and `test_pip_deps_consistency.py` pins the extras set, so adding
  one there is a separate decision.
* `_STT_MODEL_SIZES` in `dashboard/handlers/core.py` is the PUT **allowlist**.
  Expanding `_VALID_STT_MODELS` in the loader without expanding it means the API
  silently rejects every new model; a test now pins the two sets equal.

## Security posture

* **Path containment.** `store.safe_meeting_id` is the only way a client-supplied
  id becomes a path segment (`[A-Za-z0-9._-]` after the one documented `:` → `_`
  substitution, leading dots refused). `store.contain` is the barrier every
  derived path passes through: `resolve()` collapses `..` AND follows symlinks,
  then containment under the data root is asserted; callers must use the returned
  path. A violation is SEL-audited and raises. Tests cover traversal, a symlink
  planted inside the data dir, and non-string ids.
* **Deny-by-default authorization.** `_common.require_enabled` refuses every
  route while the app is disabled (routes are registered once at startup, so a
  default-disabled app would otherwise stay callable). `is_app_enabled` runs off
  the loop.
* **Redaction.** Transcripts, agent outputs, extracted tasks, and calendar fields
  are LLM/user content on the way to the dashboard or a task provider, so
  `security.redact` (exfiltration URLs + credentials) is applied at each
  boundary: the dispatch entry point, the outputs response, task normalization,
  `TaskDraft.sanitized`, and `parse_ics`.
* **Strict field readers.** `_common.field_bool` refuses a non-boolean rather
  than coercing (`bool("false")` is `True`, which would invert a mute decision);
  `field_str` treats a non-string as missing rather than stringifying it.
* **Narrow config writer.** `PUT /config` is an allow-list, not a merge: an
  unknown provider id collapses to the default, an agent id that is not a safe
  slug is dropped, and an agent-spec reference with `..` or a leading `/` becomes
  `""`.
* **Model-generated HTML.** The sketch artist writes HTML *from* the transcript,
  which anyone who speaks in the meeting can influence, so the frame takes three
  independent controls — each one added because the previous one turned out to be
  insufficient. All three are built by
  `website/src/apps/meetings/lib/sketchSrcdoc.ts`; the markup is never mounted
  into the dashboard's own DOM.

  1. **Null-origin sandbox.** `srcDoc` iframe with `sandbox="allow-scripts"` and
     **no** `allow-same-origin`, so nothing in the frame can read this page, its
     cookies, or the gateway. A test pins the absence of `allow-same-origin`.
  2. **Egress-denying CSP.** The sandbox says nothing about OUTBOUND requests, so
     a `<meta>` CSP is emitted as the first child of `<head>` (ahead of every
     model byte — a meta policy only binds from where it is parsed):
     `default-src 'none'`, `connect-src 'none'`, `img-src data:`, `font-src
     data:`, `form-action 'none'`, `base-uri 'none'`, and `script-src` pinned to
     the single vendored same-origin Mermaid FILE. The frame needs no network, so
     it is granted none.
  3. **Model script is stripped.** The CSP must grant `script-src
     'unsafe-inline'` for the Mermaid bootstrap, which also let the *model's*
     inline script run — and script can loop `document.createElement('link')`
     with `rel="dns-prefetch"` to stream the transcript out through DNS lookups
     that no CSP directive governs. An earlier revision recorded this as an
     accepted "hostname-only" residual; **that assessment was wrong** (it assumed
     the channel was limited to static markup, and treated ~200 bytes per
     unlimited repeatable lookup as a trickle). The document is therefore scrubbed
     before serialization: `script` (HTML and SVG), `iframe`/`frame`,
     `object`/`embed`/`applet`, `link` (every `rel`, not an allowlist),
     `meta`, `base`, `template` and `noscript` are removed as elements; `on*`
     handler attributes are removed; and `javascript:`/`vbscript:`/non-image
     `data:` URLs are removed from URL attributes (matched after stripping
     whitespace and control characters, which the HTML URL parser ignores).
     Remote-URL *fetches* are left to the CSP rather than pattern-matched.

  Mermaid still renders, and this is what makes control 3 affordable: it is driven
  by KiroCrew's own bootstrap from the declarative `div.mermaid` / fenced
  ```mermaid markup the agent is instructed to emit, so the agent has no
  documented need to ship JS. Both directions are tested in
  `website/src/test/sketchSrcdoc.test.ts` — nothing executable survives, **and** a
  Mermaid diagram plus an inline-styled HTML table still render (the guards
  against over-stripping the panel into a blank).
* **Two deliberate redaction EXEMPTIONS, both for the user's own writing.** The note
  and a minutes edit are returned verbatim. The note is text on its way back to only
  the person who typed it and is never fed to an agent; an edit is redacted by
  construction, since the only way to make one is to edit what the same endpoint
  already redacted. Both render through the dashboard's shared markdown sanitizer,
  which is what keeps the round trip safe without altering the text.
* **A client-supplied FILE path exists in exactly one place** — the audio import —
  and it goes through `hooks.validate_file_path` rather than a local check, so it
  gets the same canonicalization and `is_sensitive_path` verdict as every other file
  read in the product. The format check runs on the canonical path, so a symlink
  cannot use its own name to pass it.
* **An uploaded image's name is never the client's.** The extension comes from the
  bytes and the stem is a uuid4; `store.safe_note_image_name` then requires exactly
  that shape, because path containment alone permits `../<other-meeting>/<agent>.md`.
* **No blocking call on the loop.** The calendar fetch is aiohttp; DNS
  validation, the local `.ics` read, the data-dir seed, the enable check, the
  task-provider `create`, the recording-path resolution, the note and minutes
  reads/writes, image sniffing, and the import's path vetting and availability probe
  all run on an executor. This is enforced by an **AST check** over every route
  module rather than a per-handler grep, so a NEW handler that reads inline fails too
  (`TestNoStoreCallRunsOnTheEventLoop`). Every blocking `store` function is listed
  there; a handler may NAME one (handing it to `asyncio.to_thread`) but never call it.

## What the port changed

See `ATTRIBUTION.md` for the table. In short: the internal task system became
the task-provider seam, the internal calendar MCP became the calendar-provider
seam, the second (separately built, internally sourced) speech-to-text daemon was
deleted in favour of KiroCrew's own, the standalone server became in-gateway
routes, the shell-blob self-heal cron became Python at startup, and the
internal-git update-check cron was deleted (a builtin versions with the package).

## Tests

`test/test_meetings_store.py` (containment, layout, config),
`test_meetings_dictionary.py` (matching + hostile input),
`test_meetings_session.py` (dispatcher, breaker, lifecycle, prompts),
`test_meetings_providers.py` (both registries, the `.ics` parser,
scheme/address refusals), `test_meetings_routes.py` (the HTTP contract,
validation, redaction, the enable gate), `test_meetings_translation.py` (the
injection guard, the bounded queue, off-by-default), `test_meetings_minutes.py`
(the two ownership properties, the read overlay, the body-cap arithmetic), and
`test_meetings_audio_import.py` (the split's boundary rules, and the refusals in
ORDER), with the shared fixtures and the fake session manager in
`test/meetings_helpers.py`. Every dispatch goes through that fake session manager;
no test spawns a process, opens a socket, calls a model, or decodes audio.

These live in the repo-level `test/` tree, not an in-package `tests/`:
`setup.cfg` sets `testpaths = test transfer`, so a test under
`src/kiro_crew/apps/builtins/...` is never collected by CI.

Frontend: `website/src/test/MeetingsApiClient.test.ts` (fetch-boundary
translation), `MeetingsSessionLogic.test.ts` (dedup, preset resolution, the
transition table), `MeetingsAgentPillBar.test.tsx`, `MeetingsBroadcastBar.test.tsx`,
`MeetingsAgentPanel.test.tsx` (the iframe sandbox **and** the minutes editor),
`MeetingsRecording.test.ts`, `MeetingsSystemAudio.test.ts` (including the
`getDisplayMedia` constraint guard), `MeetingsCaptureTier.test.ts` (including the
preload-require pin), `MeetingsTranslation.test.tsx`, and `MeetingsNote.test.tsx`.

Electron: `website/electron` is a SECOND npm package with its own `node_modules`;
its suite needs `npm ci` there before it will run.

Two repo-wide gates catch this app's additions in places that are easy to miss:

* `src/i18n/catalogParity.test.ts` requires every new `en.json` key in all nine
  non-English catalogs **in the same commit**, and it is a vitest suite rather than
  one of the `i18n:check` gate steps — so that gate can report PASS while the
  frontend suite is red. The per-locale ratchets in `src/i18n/style/` additionally
  constrain register (informal `du`/`tu`/`ты`, no `usted`, Brazilian Portuguese, no
  formal `आप`, Western digits in Bengali, no `您`).
* `test/test_security_posture.py` walks the package for redactor CALL SITES and
  requires each module to be a registered sink or an explicitly reasoned non-egress
  entry — so adding a `redact()` anywhere forces that decision.
