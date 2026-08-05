# Attribution

The Meetings app was originally written by **adunuthu** as a standalone
KiroCrew-family app named **Meetings**, distributed as its own package with its
own backend server, UI bundle, and agent specs. This builtin is a port of that
work into the KiroCrew open-source tree.

`app.json`'s `author` field is unchanged — the original author remains the
author of record.

## What changed in the port

The original targeted an organization-internal environment. Four couplings had
no public equivalent and were replaced with generic seams; the rest of the app
(the meeting lifecycle, the batching agent dispatcher, the domain dictionary,
the task-review flow, the multi-agent panel UI) is a faithful port.

| Original | This port |
|---|---|
| Filed action items into one company-internal task system, named throughout the UI, the presets, and a dedicated agent prompt | A `TaskProvider` seam (`backend/providers/tasks.py`) with one shipped implementation: a local KiroCrew task ledger. An organization registers its own provider out of tree. |
| Read the calendar through a company-internal MCP server, with an internal-website scrape as a fallback | A `CalendarProvider` seam (`backend/providers/calendar.py`) with a stdlib iCalendar (`.ics`) reader — a local file or a published `https://` URL. |
| Two speech-to-text providers, one of which was a separate locally built daemon from an internal source repository | KiroCrew's own streaming speech-to-text (`/api/ws/stt`). The daemon and its setup script are removed. |
| A standalone `aiohttp` server on its own port, called back into by the gateway over authenticated loopback HTTP | In-gateway routes under `/api/apps/meetings/*`, and in-process agent dispatch through the shared session manager. |
| Two crons: a data-directory self-heal written as a shell blob in the cron message, and an update check that pulled from an internal git host | The self-heal is Python that runs at app startup. The update check is removed — a builtin app versions with the KiroCrew package, so there is nothing to check. |

## What this change adds

**Added in `0.2.0` (2026-08)**, as a single change on top of the port — every row in
the table below ships together, so there is no per-feature ordering to read into it.
`0.1.0` is the ported app described above, which is the version boundary to compare
against.

This section records what has been added since that port, so the line between the two
stays legible to anyone reading the app cold.

The designs come from **MeetNote**, Kai Mitsuzawa's native macOS meeting app — a
SwiftUI application that records a meeting, transcribes it on the machine, and
generates summaries and minutes from it. The designs port; the implementations mostly
do not, because MeetNote rests on macOS frameworks a browser has no equivalent for:
Core Audio process taps, ScreenCaptureKit, EventKit, and Apple's Speech and
Translation frameworks. So each row below is the web equivalent of an idea rather than
a transliteration of code.

| MeetNote (native macOS) | Here |
|---|---|
| Core Audio process taps and ScreenCaptureKit record both sides of a call | `getDisplayMedia` for the far side, summed with the microphone in an `AudioWorklet` — one capture, teed to both transcription and recording. Electron grants a loopback device where the platform supports one. The writer, session and socket live in core (`src/kiro_crew/recording/`), not in this app; the app only resolves the per-meeting directory the WAV lands in, through the `MeetingStore` seam. |
| SpeechAnalyzer / Apple Speech / a locally built `whisper.cpp` | KiroCrew's own speech-to-text, with **faster-whisper** added as a provider: full model enum plus a hallucination filter, so an entirely-boilerplate transcript returns nothing rather than text for an agent to write down. Installed on demand from Settings, not a declared extra. |
| Apple's `Translation` framework, run on demand over a finished summary | LLM translation, **per transcript line** as the meeting runs — the same idea with a different driver. A bounded sequential queue in `backend/domain/translate.py`, and this app's first non-agent LLM path. Off by default; it costs one model call per spoken line. |
| A WYSIWYG editor over notes and minutes, with edits kept so regenerating never overwrites them | The same bargain, applied to an agent's output file: a user edit is a sidecar (`edits/<agent>.md`) that wins on read, so the agent keeps sole ownership of its own file and reverting is a delete. Ported from `meetnote_lib/minutes.py` on the unlanded `feat/meetnote` branch in this repository; the layout is per-meeting here because this app already has a per-meeting directory. |
| Screenshots pasted into a note while recording, with the elapsed time as alt text | The same, as `_note.md` + `images/` — deliberately outside every path this app can hand an agent. |
| Importing an existing recording as a new meeting record | Imported into a LIVE meeting instead, transcribed and dispatched through the same entry point live speech uses, because this app keeps no transcript record for an import to become. |