---
name: meetings
description: Context for working with the Meetings app's data — where a meeting's notes, diagram, transcript-derived tasks, and calendar cache live, what the lifecycle states mean, and how the app's agents are driven. Load when the user asks about a meeting's notes or action items, or when writing/reading files under the meetings app data dir.
triggers: meeting, meeting notes, action items, meeting tasks, transcript, standup, retro, calendar, meetings app
---

# Meetings app

The Meetings app transcribes a live meeting and fans each line out to a small
crew of background agents. Each agent owns exactly ONE output file and rewrites
it in full after every batch.

## Where the data lives

All paths are under `~/.kiro/crew/apps/meetings/data/`:

| Path | What it is |
|---|---|
| `config.json` | app config: the agent roster, the task/calendar provider ids, presets |
| `dictionary.toml` | speech-to-text correction terms (`[[term]]` blocks) |
| `calendar-cache.json` | the last calendar sync's events |
| `task-ledger.json` | tasks filed through the local task provider |
| `meetings/<id>/session.json` | one meeting's metadata (title, status, attendees, attachments, outputs map) |
| `meetings/<id>/tasks.json` | that meeting's extracted action items |
| `meetings/<id>/<agent-id>.md` | a markdown agent's output (e.g. `note-taker.md`) |
| `meetings/<id>/<agent-id>.html` | an HTML agent's output (e.g. `sketch-artist.html`) |
| `meetings/<id>/translations.json` | live translation of the transcript, for the UI panel |
| `meetings/<id>/audio.wav` | the meeting recording |

`<id>` is the meeting id with `:` replaced by `_`. Only `[A-Za-z0-9._-]` is
legal in it — the backend rejects anything else, so do not construct a path from
a raw calendar UID.

### Paths that are the user's, not yours

Three things in a meeting folder belong to the person, and an agent must **never
read or write them** — not to tidy them, not to merge them, not to quote them:

| Path | What it is |
|---|---|
| `meetings/<id>/_note.md` | the user's own memo, with their own thoughts in it |
| `meetings/<id>/images/*` | screenshots they pasted into that memo |
| `meetings/<id>/edits/<agent-id>.md` | **their correction of your output** |

The last one matters most to you. When a user edits your minutes, their version
goes there and is what the app shows them from then on; your own file is left
exactly as you wrote it. So keep rewriting your own file normally — that is the
design, and it is why their correction cannot be lost. But do not go looking for
`edits/` to "reconcile" with, and never write into it: overwriting a correction is
the one failure this layout exists to make impossible.

They are listed here so you recognise them and leave them alone, not so you can
find them.

## Lifecycle

1. `idle` — the meeting folder exists, nothing is running.
2. `active` — transcription is flowing; agents receive batched lines every ~30s.
3. `paused` — transcription stopped, the session and its queues are intact.
4. `reviewing` — the meeting is over and the user is triaging extracted tasks.
5. `ended` — finished; outputs are final.

## The agents

| Agent | Writes | Notes |
|---|---|---|
| `meetings-note-taker` | `note-taker.md` | structured notes: topics, decisions, action items, open questions |
| `meetings-sketch-artist` | `sketch-artist.html` | one self-contained HTML/Mermaid diagram, revised in place |
| `meetings-task-extractor` | `tasks.json` | always runs; the app's core output |

Each agent's first message carries an `OUTPUT_FILE:` line. Write to that exact
path, character for character, and rewrite the FULL file after every batch —
never accumulate content in memory, because a context limit would lose it.

Lines prefixed `[chat]` are typed by the user, not transcribed: treat them as
corrections or added context and act on them immediately.

## Providers

Two things are pluggable, and which implementation is active comes from
`config.json`:

- **`task_provider`** — where a reviewed action item is filed. The shipped
  provider is `local`, which appends to `task-ledger.json`.
- **`calendar.provider`** — where upcoming meetings come from. `none` (default,
  meetings are created by hand) or `ics`, which reads the iCalendar document at
  `calendar.source` (a local `.ics` file path, or a published `https://` URL).

To sync the calendar, call `POST /api/apps/meetings/calendar/sync` — do not try
to fetch or parse the calendar yourself.

## Speech-to-text

Transcription uses KiroCrew's own streaming endpoint (`/api/ws/stt`), driven from
the browser. Cloud transcription is an optional extra
(`pip install kirocrew[voice]`); when it is not installed the app says so and the
user can still type into the broadcast bar to feed the agents.

## Correcting recurring mistranscriptions

When the user complains that a project noun keeps coming through wrong, add a
dictionary term rather than correcting it in the notes:

```
POST /api/apps/meetings/dictionary
{"correct": "DynamoDB", "aliases": ["dynamo db", "dynamo d.b."]}
```

Matching is case-insensitive with word boundaries and the longest alias wins, so
every later meeting gets it right at the point of transcription.
